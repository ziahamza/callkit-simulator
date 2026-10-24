#import "HostSupport.h"
#import <React/RCTBridgeModule.h>
@import livekit_react_native_webrtc;

static NSUInteger deviceConstructions;
@interface LKRTCAudioDeviceModule (Host)
- (NSArray<NSNumber *> *)events;
- (void)observeAvailability:(BOOL)active semaphore:(dispatch_semaphore_t)semaphore;
- (void)failNextAvailability:(NSUInteger)mode;
- (void)clearEvents;
- (void)bindObserver:(AudioDeviceModuleObserver *)observer;
@end
@implementation LKRTCAudioDeviceModule {
  LKRTCAudioEngineAvailability _availability;
  NSMutableArray<NSNumber *> *_events;
  dispatch_semaphore_t _observed;
  BOOL _expected;
  NSUInteger _failure;
  AudioDeviceModuleObserver *_policyObserver;
}
- (instancetype)init { if ((self = [super init])) { deviceConstructions++; _events = [NSMutableArray new]; _availability.isInputAvailable = YES; _availability.isOutputAvailable = YES; } return self; }
- (NSInteger)setEngineAvailability:(LKRTCAudioEngineAvailability)value {
  CHECK(!NSThread.isMainThread && VSAudioSessionIsOnQueue() && value.isInputAvailable == value.isOutputAvailable);
  CHECK(_policyObserver && !_policyObserver.automaticAudioSessionConfig &&
    !_policyObserver.isWillEnableEngineActive && !_policyObserver.isDidDisableEngineActive);
  @synchronized(self) {
    NSUInteger failure = _failure; _failure = 0;
    if (failure == 1) return -1;
    if (failure == 2) return 0; // A successful return without applying the requested state.
    if (failure == 3) [NSException raise:@"HostAvailabilityFailure" format:@"Inert availability failure"];
    _availability = value; [_events addObject:@(value.isInputAvailable)];
    if (_observed && value.isInputAvailable == _expected) { dispatch_semaphore_signal(_observed); _observed = nil; }
  }
  return 0;
}
- (LKRTCAudioEngineAvailability)engineAvailability { @synchronized(self) { return _availability; } }
- (NSArray<NSNumber *> *)events { @synchronized(self) { return [_events copy]; } }
- (void)observeAvailability:(BOOL)active semaphore:(dispatch_semaphore_t)semaphore { @synchronized(self) { _expected = active; _observed = semaphore; } }
- (void)failNextAvailability:(NSUInteger)mode { @synchronized(self) { _failure = mode; } }
- (void)clearEvents { @synchronized(self) { [_events removeAllObjects]; } }
- (void)bindObserver:(AudioDeviceModuleObserver *)observer { _policyObserver = observer; }
@end
static NSUInteger moduleConstructions;
@interface AudioDeviceModuleObserver (Host)
- (NSUInteger)policyWrites;
- (void)ignoreNextAutomaticPolicyWrite;
@end
@implementation AudioDeviceModuleObserver {
  NSDictionary<NSString *, id> *_automatic;
  NSUInteger _writes;
  BOOL _willEnable, _didDisable;
  BOOL _ignoreAutomaticWrite;
}
- (instancetype)init { if ((self = [super init])) { _automatic = @{@"synthetic": @YES}; _willEnable = YES; _didDisable = YES; } return self; }
- (NSDictionary<NSString *, id> *)automaticAudioSessionConfig { @synchronized(self) { return _automatic; } }
- (void)setAutomaticAudioSessionConfig:(NSDictionary<NSString *, id> *)value {
  CHECK(!NSThread.isMainThread && VSAudioSessionIsOnQueue() && value == nil);
  @synchronized(self) { _writes++; if (_ignoreAutomaticWrite) { _ignoreAutomaticWrite = NO; return; } _automatic = [value copy]; }
}
- (void)ignoreNextAutomaticPolicyWrite { @synchronized(self) { _ignoreAutomaticWrite = YES; } }
- (NSUInteger)policyWrites { @synchronized(self) { return _writes; } }
- (BOOL)isWillEnableEngineActive { @synchronized(self) { return _willEnable; } }
- (void)setIsWillEnableEngineActive:(BOOL)value { CHECK(VSAudioSessionIsOnQueue() && !NSThread.isMainThread && !value); @synchronized(self) { _willEnable = value; } }
- (BOOL)isDidDisableEngineActive { @synchronized(self) { return _didDisable; } }
- (void)setIsDidDisableEngineActive:(BOOL)value { CHECK(VSAudioSessionIsOnQueue() && !NSThread.isMainThread && !value); @synchronized(self) { _didDisable = value; } }
@end
@interface WebRTCModule (Host)
- (instancetype)initWithDevice:(LKRTCAudioDeviceModule *)device;
- (void)observeAttachment:(dispatch_semaphore_t)semaphore;
- (AudioDeviceModuleObserver *)testObserver;
@end
@implementation WebRTCModule {
  LKRTCAudioDeviceModule *_testDevice;
  AudioDeviceModuleObserver *_testObserver;
  dispatch_semaphore_t _attached;
}
- (instancetype)initWithDevice:(LKRTCAudioDeviceModule *)device { if ((self = [super init])) { moduleConstructions++; _testDevice = device; _testObserver = [AudioDeviceModuleObserver new]; [device bindObserver:_testObserver]; } return self; }
- (LKRTCAudioDeviceModule *)audioDeviceModule {
  CHECK(!NSThread.isMainThread && VSAudioSessionIsOnQueue());
  @synchronized(self) { if (_attached) { dispatch_semaphore_signal(_attached); _attached = nil; } }
  return _testDevice;
}
- (AudioDeviceModuleObserver *)audioDeviceModuleObserver { CHECK(VSAudioSessionIsOnQueue()); return _testObserver; }
- (AudioDeviceModuleObserver *)testObserver { return _testObserver; }
- (void)observeAttachment:(dispatch_semaphore_t)semaphore { @synchronized(self) { _attached = semaphore; } }
@end
@implementation RCTModuleRegistry
- (id)moduleForClass:(Class)moduleClass { return nil; }
@end
@interface ExistingRegistry : RCTModuleRegistry
- (instancetype)initWithModule:(WebRTCModule *)module;
@property(nonatomic, readonly) NSUInteger lookupCount;
@end
@implementation ExistingRegistry {
  WebRTCModule *_module;
  NSUInteger _lookups;
}
- (instancetype)initWithModule:(WebRTCModule *)module { if ((self = [super init])) _module = module; return self; }
- (id)moduleForClass:(Class)moduleClass { CHECK(moduleClass == WebRTCModule.class && !NSThread.isMainThread && !VSAudioSessionIsOnQueue()); @synchronized(self) { _lookups++; return _module; } }
- (NSUInteger)lookupCount { @synchronized(self) { return _lookups; } }
@end
// Private production attachment methods are declared only in this host control.
@interface VSSelectedMediaBinding (HostRegistryContract)
- (uint64_t)beginOwner;
- (void)attach:(WebRTCModule *)module owner:(uint64_t)owner;
- (void)invalidateOwner:(uint64_t)owner;
@end
@protocol HostRegistryLifecycle <NSObject>
@property(nonatomic, weak, readwrite) RCTModuleRegistry *moduleRegistry;
- (void)initialize;
- (void)invalidate;
@end
static id<HostRegistryLifecycle> helper(RCTModuleRegistry *registry) {
  Class cls = NSClassFromString(@"VoIPSDKMediaRegistry"); CHECK(cls);
  id<HostRegistryLifecycle> value = [cls new]; value.moduleRegistry = registry; return value;
}
static void flushActivation(VSSelectedMediaBinding *binding, AVAudioSession *audio) {
  completeOperation(^(VSMediaCompletion completion) { [binding activate:audio completion:completion]; }, 0);
}
static void expectUnprepared(VSSelectedMediaBinding *binding, AVAudioSession *audio, LKRTCAudioDeviceModule *device) {
  NSUInteger count = device.events.count;
  [binding setSystemAudioAuthorization:YES];
  completeOperation(^(VSMediaCompletion completion) { [binding activate:audio completion:completion]; }, 2);
  [binding setSystemAudioAuthorization:NO]; flushAudioQueue();
  CHECK(device.events.count == count);
}
int main(void) { @autoreleasepool {
  VSSelectedMediaBinding *binding = [VSSelectedMediaBinding sharedBinding];
  AVAudioSession *audio = [AVAudioSession new];
  __block NSUInteger configurations = 0;
  completeOperation(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; } configuration:^NSError * { configurations++; return nil; } completion:completion]; }, 1);
  CHECK(configurations == 0);
  completeOperation(^(VSMediaCompletion completion) { [binding activate:audio completion:completion]; }, 2);
  LKRTCAudioDeviceModule *first = [LKRTCAudioDeviceModule new];
  WebRTCModule *firstModule = [[WebRTCModule alloc] initWithDevice:first];
  ExistingRegistry *firstRegistry = [[ExistingRegistry alloc] initWithModule:firstModule];
  id<HostRegistryLifecycle> old = helper(firstRegistry);
  dispatch_semaphore_t firstAttached = dispatch_semaphore_create(0); [firstModule observeAttachment:firstAttached];
  [old initialize]; waitFor(firstAttached); flushAudioQueue();
  CHECK(firstRegistry.lookupCount == 1 && moduleConstructions == 1 && deviceConstructions == 1);
  CHECK(first.events.count == 0 && first.engineAvailability.isInputAvailable && firstModule.testObserver.policyWrites == 0);
  CHECK(firstModule.testObserver.automaticAudioSessionConfig != nil);
  CHECK(firstModule.testObserver.isWillEnableEngineActive && firstModule.testObserver.isDidDisableEngineActive);
  [binding setSystemAudioAuthorization:NO]; flushAudioQueue();
  CHECK(first.events.count == 0 && firstModule.testObserver.policyWrites == 0);
  completeOperationWithDomain(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * {
    CHECK(VSAudioSessionIsOnQueue());
    return [NSError errorWithDomain:NSCocoaErrorDomain code:NSUserCancelledError userInfo:nil];
  } configuration:^NSError * { CHECK(NO); return nil; } completion:completion]; }, NSUserCancelledError, NSCocoaErrorDomain);
  CHECK(first.events.count == 0 && firstModule.testObserver.policyWrites == 0);
  CHECK(firstModule.testObserver.automaticAudioSessionConfig != nil && firstModule.testObserver.isWillEnableEngineActive && firstModule.testObserver.isDidDisableEngineActive);
  [firstModule.testObserver ignoreNextAutomaticPolicyWrite];
  completeOperation(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; }
    configuration:^NSError * { CHECK(NO); return nil; } completion:completion]; }, 2);
  expectUnprepared(binding, audio, first);
  for (NSNumber *failure in @[@2, @1, @3]) {
    [first failNextAvailability:failure.unsignedIntegerValue];
    completeOperation(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; }
      configuration:configuration() completion:completion]; }, 2);
    CHECK(first.events.count == 0);
    expectUnprepared(binding, audio, first);
  }
  completeOperationWithDomain(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; }
    configuration:^NSError * { return [NSError errorWithDomain:NSCocoaErrorDomain code:17 userInfo:nil]; }
    completion:completion]; }, 17, NSCocoaErrorDomain);
  expectUnprepared(binding, audio, first);
  __block NSUInteger cancellationChecks = 0;
  completeOperationWithDomain(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * {
    CHECK(VSAudioSessionIsOnQueue());
    return ++cancellationChecks == 1 ? nil : [NSError errorWithDomain:NSCocoaErrorDomain code:NSUserCancelledError userInfo:nil];
  } configuration:configuration() completion:completion]; }, NSUserCancelledError, NSCocoaErrorDomain);
  CHECK(cancellationChecks == 2 && [first.events isEqual:@[@NO]]);
  expectUnprepared(binding, audio, first);
  [first clearEvents];
  // Superseding the RN owner during configuration must refuse old readiness.
  dispatch_semaphore_t configurationEntered = dispatch_semaphore_create(0);
  dispatch_semaphore_t configurationRelease = dispatch_semaphore_create(0);
  dispatch_semaphore_t stalePreparationDone = dispatch_semaphore_create(0);
  [binding prepareWithCheck:^NSError * { return nil; } configuration:^NSError * {
    CHECK(VSAudioSessionIsOnQueue() && !NSThread.isMainThread);
    dispatch_semaphore_signal(configurationEntered); waitFor(configurationRelease);
    return nil;
  } completion:^(NSError *error) {
    CHECK(VSAudioSessionIsOnQueue() && !NSThread.isMainThread && error.code == 1 &&
      [error.domain isEqual:@"VoIPSDK.MediaAdapter"]);
    dispatch_semaphore_signal(stalePreparationDone);
  }];
  waitFor(configurationEntered);
  uint64_t replacementOwner = [binding beginOwner];
  dispatch_semaphore_signal(configurationRelease); waitFor(stalePreparationDone);
  flushAudioQueue();
  [binding attach:firstModule owner:replacementOwner]; flushAudioQueue();
  expectUnprepared(binding, audio, first);
  CHECK(![first.events containsObject:@YES]);
  [first clearEvents];
  NSUInteger earlierPolicyWrites = firstModule.testObserver.policyWrites;
  VSMediaConfiguration configure = ^NSError * {
    CHECK(!NSThread.isMainThread && VSAudioSessionIsOnQueue() && firstModule.testObserver.automaticAudioSessionConfig == nil);
    CHECK(!firstModule.testObserver.isWillEnableEngineActive && !firstModule.testObserver.isDidDisableEngineActive);
    if (configurations == 0) CHECK(first.events.count == 0);
    configurations++; return nil;
  };
  completeOperation(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; } configuration:configure completion:completion]; }, 0);
  CHECK(configurations == 1 && [first.events isEqual:@[@NO]] && firstModule.testObserver.policyWrites == earlierPolicyWrites + 1);
  [binding setSystemAudioAuthorization:YES]; flushActivation(binding, audio);
  CHECK([first.events isEqual:@[@NO, @YES]]);
  completeOperation(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; } configuration:configure completion:completion]; }, 0);
  CHECK(configurations == 2 && firstModule.testObserver.policyWrites == earlierPolicyWrites + 2 && [first.events isEqual:@[@NO, @YES]]);
  // A failed new preparation must not erase an earlier valid held call's readiness.
  [binding setSystemAudioAuthorization:NO]; flushAudioQueue();
  completeOperation(^(VSMediaCompletion completion) { [binding deactivate:audio completion:completion]; }, 0);
  completeOperationWithDomain(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; }
    configuration:^NSError * { return [NSError errorWithDomain:NSCocoaErrorDomain code:17 userInfo:nil]; }
    completion:completion]; }, 17, NSCocoaErrorDomain);
  [binding setSystemAudioAuthorization:YES]; flushActivation(binding, audio);
  CHECK([first.events.lastObject isEqual:@YES]);
  LKRTCAudioDeviceModule *second = [LKRTCAudioDeviceModule new];
  WebRTCModule *secondModule = [[WebRTCModule alloc] initWithDevice:second];
  ExistingRegistry *secondRegistry = [[ExistingRegistry alloc] initWithModule:secondModule];
  id<HostRegistryLifecycle> current = helper(secondRegistry);
  dispatch_semaphore_t secondEnabled = dispatch_semaphore_create(0); [second observeAvailability:YES semaphore:secondEnabled];
  [current initialize]; waitFor(secondEnabled);
  CHECK(moduleConstructions == 2 && deviceConstructions == 2 && secondRegistry.lookupCount == 1);
  CHECK(secondModule.testObserver.policyWrites == 1 && secondModule.testObserver.automaticAudioSessionConfig == nil);
  CHECK(!secondModule.testObserver.isWillEnableEngineActive && !secondModule.testObserver.isDidDisableEngineActive);
  CHECK([first.events.lastObject isEqual:@NO]);
  [old invalidate]; flushActivation(binding, audio);
  CHECK([second.events isEqual:@[@NO, @YES, @YES]]);
  // A delayed registry acquisition carries this owner token into attach. Drive
  // the exact production attachment path after a newer token already won.
  uint64_t staleOwner = [binding beginOwner];
  uint64_t latestOwner = [binding beginOwner];
  LKRTCAudioDeviceModule *stale = [LKRTCAudioDeviceModule new], *latest = [LKRTCAudioDeviceModule new];
  WebRTCModule *staleModule = [[WebRTCModule alloc] initWithDevice:stale], *latestModule = [[WebRTCModule alloc] initWithDevice:latest];
  [binding attach:latestModule owner:latestOwner]; flushActivation(binding, audio);
  [binding attach:staleModule owner:staleOwner]; flushActivation(binding, audio);
  CHECK([stale.events isEqual:@[@NO]] && !stale.engineAvailability.isInputAvailable);
  CHECK([latest.events isEqual:@[@NO, @YES, @YES, @YES]]);
  [binding attach:latestModule owner:staleOwner]; flushActivation(binding, audio);
  CHECK([latest.events isEqual:@[@NO, @YES, @YES, @YES, @YES]]);
  [binding invalidateOwner:staleOwner]; flushActivation(binding, audio);
  CHECK([latest.events.lastObject isEqual:@YES]);
  dispatch_semaphore_t reset = dispatch_semaphore_create(0); [latest observeAvailability:NO semaphore:reset];
  [binding setSystemAudioAuthorization:NO]; waitFor(reset);
  CHECK([latest.events.lastObject isEqual:@NO]);
  uint64_t invalidatedOwner = [binding beginOwner];
  [binding invalidateOwner:invalidatedOwner];
  LKRTCAudioDeviceModule *orphan = [LKRTCAudioDeviceModule new];
  CHECK(orphan.engineAvailability.isInputAvailable);
  WebRTCModule *orphanModule = [[WebRTCModule alloc] initWithDevice:orphan];
  [binding attach:orphanModule owner:invalidatedOwner];
  completeOperation(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; } configuration:configuration() completion:completion]; }, 1);
  CHECK([orphan.events isEqual:@[@NO]] && !orphan.engineAvailability.isInputAvailable);
  dispatch_semaphore_t diagnostic = dispatch_semaphore_create(0); expectDiagnostic(diagnostic);
  id<HostRegistryLifecycle> missing = helper(nil); [missing initialize]; waitFor(diagnostic);
  [missing invalidate]; [current invalidate];
  puts("passed: idle attachment without backend writes; policy readback and availability return/readback/exception refusal; configuration/cancellation/stale-owner work cannot publish initial readiness; active/held-call readiness preserved; reload, stale/same-ADM and invalidated-owner retirement; reset revocation; missing registry diagnostic");
} return 0; }
