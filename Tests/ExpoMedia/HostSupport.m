#import "HostSupport.h"
#import <stdarg.h>
@implementation AVAudioSession
@end
static dispatch_semaphore_t diagnostic;
VSMediaConfiguration configuration(void) {
  return ^NSError * { CHECK(VSAudioSessionIsOnQueue() && !NSThread.isMainThread); return nil; };
}
void expectDiagnostic(dispatch_semaphore_t semaphore) { @synchronized (AVAudioSession.class) { diagnostic = semaphore; } }
void VSHostMediaLog(NSString *format, ...) {
  va_list args; va_start(args, format);
  NSString *message = [[NSString alloc] initWithFormat:format arguments:args];
  va_end(args);
  fprintf(stderr, "%s\n", message.UTF8String);
  @synchronized (AVAudioSession.class) {
    if ([message containsString:@"selected LiveKit React Native audio module is unavailable"] && diagnostic) {
      dispatch_semaphore_signal(diagnostic);
      diagnostic = nil;
    }
  }
}
void waitFor(dispatch_semaphore_t semaphore) {
  CHECK(dispatch_semaphore_wait(semaphore, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)) == 0);
}
void flushAudioQueue(void) {
  dispatch_semaphore_t done = dispatch_semaphore_create(0);
  dispatch_async(VSAudioSessionGetQueue(), ^{ dispatch_semaphore_signal(done); });
  waitFor(done);
}
void completeOperation(void (^operation)(VSMediaCompletion), NSInteger expectedError) {
  completeOperationWithDomain(operation, expectedError, @"VoIPSDK.MediaAdapter");
}
void completeOperationWithDomain(void (^operation)(VSMediaCompletion), NSInteger expectedError, NSString *domain) {
  dispatch_semaphore_t done = dispatch_semaphore_create(0);
  operation(^(NSError *error) {
    CHECK(!NSThread.isMainThread && VSAudioSessionIsOnQueue());
    CHECK((error ? error.code : 0) == expectedError);
    if (error) CHECK([error.domain isEqual:domain]);
    dispatch_semaphore_signal(done);
  });
  waitFor(done);
}
