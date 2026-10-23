#import "QueuedCallbackBoundary.h"

static NSUInteger preparations;
NSUInteger VSHostMediaPreparationCount(void) {
  @synchronized(VSSelectedMediaBinding.class) { return preparations; }
}

@implementation VSSelectedMediaBinding
+ (instancetype)sharedBinding {
  static VSSelectedMediaBinding *binding;
  static dispatch_once_t once;
  dispatch_once(&once, ^{ binding = [VSSelectedMediaBinding new]; });
  return binding;
}
- (void)setSystemAudioAuthorization:(BOOL)active {}
- (void)prepareWithCheck:(VSMediaConfiguration)check configuration:(VSMediaConfiguration)configuration
              completion:(VSMediaCompletion)completion {
  @synchronized(VSSelectedMediaBinding.class) { preparations++; }
  dispatch_async(VSAudioSessionGetQueue(), ^{
    NSCAssert(VSAudioSessionIsOnQueue() && !NSThread.isMainThread, @"Expected the SDK audio queue");
    NSError *error = check();
    completion(error ?: configuration());
  });
}
- (void)activate:(AVAudioSession *)session completion:(VSMediaCompletion)completion {
  dispatch_async(VSAudioSessionGetQueue(), ^{ completion(nil); });
}
- (void)deactivate:(AVAudioSession *)session completion:(VSMediaCompletion)completion {
  dispatch_async(VSAudioSessionGetQueue(), ^{ completion(nil); });
}
@end
