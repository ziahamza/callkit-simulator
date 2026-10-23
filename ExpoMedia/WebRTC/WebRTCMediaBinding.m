#import "VSMediaBinding.h"
#import "WebRTCAudioContract.h"

static NSError *mediaError(NSInteger code) {
  return [NSError errorWithDomain:@"VoIPSDK.MediaAdapter" code:code userInfo:@{
    NSLocalizedDescriptionKey: code == 1 ? @"The selected WebRTC audio implementation is unavailable." :
      @"The selected WebRTC implementation could not apply external audio ownership."
  }];
}

@implementation VSSelectedMediaBinding {
  dispatch_queue_t _worker;
  NSLock *_authorizationLock;
  BOOL _authorized;
  BOOL _prepared;
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
    _authorizationLock = [NSLock new];
  }
  return self;
}
- (BOOL)isAuthorized {
  [_authorizationLock lock];
  BOOL value = _authorized;
  [_authorizationLock unlock];
  return value;
}
- (void)setSystemAudioAuthorization:(BOOL)active {
  [_authorizationLock lock];
  _authorized = active;
  [_authorizationLock unlock];
  // The setter never waits on media. A reset must gate media even when no
  // genuine deactivation callback follows it.
  if (!active) dispatch_async(_worker, ^{
    if (!_prepared) return;
    if ([self isAuthorized]) return;
    @try {
      NSError *error = nil;
      id<VSMediaRTCAudioSession> session = [self audioSession:&error];
      if (session) session.isAudioEnabled = NO;
    } @catch (NSException *exception) {
      NSLog(@"[VoIPSDK] WebRTC could not revoke media availability.");
    }
  });
}
- (id<VSMediaRTCAudioSession>)audioSession:(NSError **)error {
  // Public ObjC lookup keeps a missing optional framework linkable. All calls
  // below remain statically typed public selectors, with explicit availability.
  Class<VSMediaRTCAudioSessionFactory> factory = (Class<VSMediaRTCAudioSessionFactory>)NSClassFromString(@"RTCAudioSession");
  if (!factory || ![factory respondsToSelector:@selector(sharedInstance)]) {
    *error = mediaError(1); return nil;
  }
  id<VSMediaRTCAudioSession> session = [factory sharedInstance];
  SEL required[] = {@selector(useManualAudio), @selector(setUseManualAudio:),
    @selector(isAudioEnabled), @selector(setIsAudioEnabled:),
    @selector(lockForConfiguration), @selector(unlockForConfiguration),
    @selector(audioSessionDidActivate:), @selector(audioSessionDidDeactivate:)};
  for (NSUInteger index = 0; index < sizeof(required) / sizeof(required[0]); index++) {
    if (![session respondsToSelector:required[index]]) { *error = mediaError(1); return nil; }
  }
  return session;
}
- (void)prepareWithCheck:(VSMediaConfiguration)check configuration:(VSMediaConfiguration)configuration completion:(VSMediaCompletion)completion {
  dispatch_async(_worker, ^{
    NSError *error = nil;
    @try {
      // A cancelled queued action must not resolve or mutate a backend.
      error = check();
      if (error) { completion(error); return; }
      id<VSMediaRTCAudioSession> session = [self audioSession:&error];
      if (session) {
        [session lockForConfiguration];
        @try {
          error = configuration();
          if (!error) {
            session.useManualAudio = YES;
            if (![self isAuthorized]) session.isAudioEnabled = NO;
            if (!session.useManualAudio || (![self isAuthorized] && session.isAudioEnabled)) error = mediaError(2);
            if (!error) _prepared = YES;
          }
        } @finally { [session unlockForConfiguration]; }
      }
    } @catch (NSException *exception) { error = mediaError(2); }
    completion(error);
  });
}
- (void)activate:(AVAudioSession *)audioSession completion:(VSMediaCompletion)completion {
  dispatch_async(_worker, ^{
    if (!_prepared) { completion(mediaError(2)); return; }
    NSError *error = nil;
    @try {
      id<VSMediaRTCAudioSession> session = [self audioSession:&error];
      if (session) {
        [session audioSessionDidActivate:audioSession];
        BOOL enabled = [self isAuthorized];
        session.isAudioEnabled = enabled;
        if (session.isAudioEnabled != enabled) error = mediaError(2);
      }
    } @catch (NSException *exception) { error = mediaError(2); }
    completion(error);
  });
}
- (void)deactivate:(AVAudioSession *)audioSession completion:(VSMediaCompletion)completion {
  dispatch_async(_worker, ^{
    if (!_prepared) { completion(nil); return; }
    NSError *error = nil;
    @try {
      id<VSMediaRTCAudioSession> session = [self audioSession:&error];
      if (session) {
        session.isAudioEnabled = NO;
        [session audioSessionDidDeactivate:audioSession];
        if (session.isAudioEnabled) error = mediaError(2);
      }
    } @catch (NSException *exception) { error = mediaError(2); }
    completion(error);
  });
}
@end
