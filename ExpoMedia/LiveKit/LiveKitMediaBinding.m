#import "VSMediaBinding.h"
#import <React/RCTBridgeModule.h>
#import <React/RCTInitializing.h>
#import <React/RCTInvalidating.h>
@import livekit_react_native_webrtc;

static NSError *mediaError(NSInteger code) {
  return [NSError errorWithDomain:@"VoIPSDK.MediaAdapter" code:code userInfo:@{
    NSLocalizedDescriptionKey: code == 1 ? @"The selected LiveKit React Native audio module is not attached." :
      @"The existing LiveKit audio engine could not apply media availability."
  }];
}

@interface VSSelectedMediaBinding (Registry)
- (uint64_t)beginOwner;
- (void)attach:(WebRTCModule *)module owner:(uint64_t)owner;
- (void)invalidateOwner:(uint64_t)owner;
@end

@implementation VSSelectedMediaBinding {
  dispatch_queue_t _worker;
  NSLock *_stateLock;
  BOOL _authorized;
  BOOL _prepared;
  uint64_t _nextOwner;
  uint64_t _currentOwner;
  // Framework objects are confined to the serial media worker.
  LKRTCAudioDeviceModule *_device;
  WebRTCModule *_module;
  uint64_t _deviceOwner;
}
+ (instancetype)sharedBinding {
  static VSSelectedMediaBinding *binding;
  static dispatch_once_t once;
  dispatch_once(&once, ^{ binding = [VSSelectedMediaBinding new]; });
  return binding;
}
- (instancetype)init {
  self = [super init];
  if (self) {
    _worker = VSAudioSessionGetQueue();
    _stateLock = [NSLock new];
  }
  return self;
}
- (uint64_t)currentOwner {
  [_stateLock lock];
  uint64_t value = _currentOwner;
  [_stateLock unlock];
  return value;
}
- (BOOL)isAuthorized {
  [_stateLock lock];
  BOOL value = _authorized;
  [_stateLock unlock];
  return value;
}
- (NSError *)applyAvailability:(BOOL)active device:(LKRTCAudioDeviceModule *)device {
  if (!device) return mediaError(1);
  LKRTCAudioEngineAvailability availability;
  availability.isInputAvailable = active;
  availability.isOutputAvailable = active;
  NSInteger result = [device setEngineAvailability:availability];
  LKRTCAudioEngineAvailability applied = device.engineAvailability;
  if (result != 0 || applied.isInputAvailable != active || applied.isOutputAvailable != active) return mediaError(2);
  return nil;
}
- (NSError *)takeExternalOwnership:(WebRTCModule *)module {
  AudioDeviceModuleObserver *observer = module.audioDeviceModuleObserver;
  if (!observer) return mediaError(1);
  observer.automaticAudioSessionConfig = nil;
  observer.isWillEnableEngineActive = NO;
  observer.isDidDisableEngineActive = NO;
  return observer.automaticAudioSessionConfig || observer.isWillEnableEngineActive ||
    observer.isDidDisableEngineActive ? mediaError(2) : nil;
}
- (void)retireObsoleteDevice {
  if (_device && _deviceOwner != [self currentOwner]) {
    NSError *error = _prepared ? [self takeExternalOwnership:_module] : nil;
    if (_prepared && !error) error = [self applyAvailability:NO device:_device];
    if (error) NSLog(@"[VoIPSDK] The retired LiveKit engine could not revoke media availability.");
    _device = nil;
    _module = nil;
    _deviceOwner = 0;
  }
}
- (uint64_t)beginOwner {
  [_stateLock lock];
  uint64_t owner = ++_nextOwner;
  _currentOwner = owner;
  [_stateLock unlock];
  dispatch_async(_worker, ^{
    @try { [self retireObsoleteDevice]; }
    @catch (NSException *exception) { NSLog(@"[VoIPSDK] The retired LiveKit engine could not revoke media availability."); }
  });
  return owner;
}
- (void)attach:(WebRTCModule *)module owner:(uint64_t)owner {
  dispatch_async(_worker, ^{
    @try {
      LKRTCAudioDeviceModule *device = module.audioDeviceModule;
      if (!device) { NSLog(@"[VoIPSDK] The selected LiveKit React Native audio module is unavailable."); return; }
      [self retireObsoleteDevice];
      if (!owner || owner != [self currentOwner]) {
        // A stale lookup may have initialized an otherwise available old ADM.
        // Gate that object, but never gate the identical newer bound engine.
        if (_prepared && device != _device) {
          NSError *error = [self takeExternalOwnership:module];
          if (!error) error = [self applyAvailability:NO device:device];
          if (error) NSLog(@"[VoIPSDK] The obsolete LiveKit audio engine could not revoke media availability.");
        }
        return;
      }
      _device = device;
      _module = module;
      _deviceOwner = owner;
      if (!_prepared) return;
      NSError *error = [self takeExternalOwnership:module];
      if (!error) error = [self applyAvailability:NO device:device];
      // Binding only replays genuine system authorization, never an activation
      // event. A newer RN instance or invalidation makes this result obsolete.
      if (!error && owner == [self currentOwner] && [self isAuthorized]) {
        error = [self applyAvailability:YES device:device];
      }
      if (error) NSLog(@"[VoIPSDK] The existing LiveKit audio engine could not bind media availability.");
    } @catch (NSException *exception) { NSLog(@"[VoIPSDK] The existing LiveKit audio engine could not bind media availability."); }
  });
}
- (void)invalidateOwner:(uint64_t)owner {
  [_stateLock lock];
  if (owner && _currentOwner == owner) _currentOwner = 0;
  [_stateLock unlock];
  dispatch_async(_worker, ^{
    @try { [self retireObsoleteDevice]; }
    @catch (NSException *exception) { NSLog(@"[VoIPSDK] The retired LiveKit engine could not revoke media availability."); }
  });
}
- (void)setSystemAudioAuthorization:(BOOL)active {
  [_stateLock lock];
  _authorized = active;
  [_stateLock unlock];
  if (!active) dispatch_async(_worker, ^{
    if (!_prepared) return;
    if ([self isAuthorized]) return;
    @try {
      if (_device && [self applyAvailability:NO device:_device]) NSLog(@"[VoIPSDK] LiveKit could not revoke media availability.");
    } @catch (NSException *exception) { NSLog(@"[VoIPSDK] LiveKit could not revoke media availability."); }
  });
}
- (void)performAvailability:(BOOL)active completion:(VSMediaCompletion)completion {
  dispatch_async(_worker, ^{
    if (!_prepared) { completion(active ? mediaError(2) : nil); return; }
    NSError *error = nil;
    @try {
      [self retireObsoleteDevice];
      if (_device) {
        BOOL enabled = active && _deviceOwner == [self currentOwner] && [self isAuthorized];
        error = [self applyAvailability:enabled device:_device];
      } else if (active) error = mediaError(1);
      // Before RN attachment prepare is deliberately pending ownership only;
      // the registry binds and gates the existing engine before replaying state.
    } @catch (NSException *exception) { error = mediaError(2); }
    completion(error);
  });
}
- (void)prepareWithCheck:(VSMediaConfiguration)check configuration:(VSMediaConfiguration)configuration completion:(VSMediaCompletion)completion {
  dispatch_async(_worker, ^{
    NSError *error = nil;
    @try {
      // A cancelled queued action must not resolve or mutate a backend.
      error = check();
      if (error) { completion(error); return; }
      [self retireObsoleteDevice];
      if (!_device || !_module) { completion(mediaError(1)); return; }
      error = [self takeExternalOwnership:_module];
      if (!error) error = configuration();
      if (!error) {
        // A second call must preserve an engine already authorized by LCK.
        if (![self isAuthorized]) error = [self applyAvailability:NO device:_device];
        if (!error) error = check();
        // Blocking work may outlive its RN owner. Commit readiness under the
        // owner lock, never holding that lock across configuration/native calls.
        // An earlier valid held call keeps its already-established readiness.
        if (!error) {
          [_stateLock lock];
          BOOL current = _deviceOwner && _deviceOwner == _currentOwner;
          if (current) _prepared = YES;
          [_stateLock unlock];
          if (!current) error = mediaError(1);
        }
      }
    } @catch (NSException *exception) { error = mediaError(2); }
    completion(error);
  });
}
- (void)activate:(AVAudioSession *)session completion:(VSMediaCompletion)completion {
  [self performAvailability:YES completion:completion];
}
- (void)deactivate:(AVAudioSession *)session completion:(VSMediaCompletion)completion {
  [self performAvailability:NO completion:completion];
}
@end

/// This private RN module exports no audio controls. The JS lookup only asks RN
/// to instantiate it; all activation authority remains in genuine LCK events.
@interface VoIPSDKMediaRegistry : NSObject <RCTBridgeModule, RCTInitializing, RCTInvalidating>
@property(nonatomic, weak, readwrite) RCTModuleRegistry *moduleRegistry;
@end
@implementation VoIPSDKMediaRegistry {
  BOOL _invalidated;
  uint64_t _owner;
}
@synthesize moduleRegistry = _moduleRegistry;
RCT_EXPORT_MODULE(VoIPSDKMediaRegistry)
+ (BOOL)requiresMainQueueSetup { return NO; }
- (void)initialize {
  RCTModuleRegistry *registry = self.moduleRegistry;
  uint64_t owner;
  @synchronized (self) {
    if (_invalidated) return;
    owner = _owner = [[VSSelectedMediaBinding sharedBinding] beginOwner];
  }
  // The RN holder is still being created during initialize. Return before
  // lookup, and never let a possibly waiting registry block the media worker.
  dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
    @try {
      WebRTCModule *module = [registry moduleForClass:WebRTCModule.class];
      if (module) [[VSSelectedMediaBinding sharedBinding] attach:module owner:owner];
      else NSLog(@"[VoIPSDK] The selected LiveKit React Native audio module is unavailable.");
    } @catch (NSException *exception) { NSLog(@"[VoIPSDK] The selected LiveKit React Native audio module is unavailable."); }
  });
}
- (void)invalidate {
  uint64_t owner;
  @synchronized (self) {
    _invalidated = YES;
    owner = _owner;
    _owner = 0;
  }
  [[VSSelectedMediaBinding sharedBinding] invalidateOwner:owner];
}
- (void)dealloc {
  [[VSSelectedMediaBinding sharedBinding] invalidateOwner:_owner];
}
@end
