#import "HostSupport.h"
int main(void) { @autoreleasepool {
  CHECK(NSClassFromString(@"RTCAudioSession") == Nil);
  VSSelectedMediaBinding *binding = [VSSelectedMediaBinding new];
  AVAudioSession *audio = [AVAudioSession new];
  __block BOOL configured = NO;
  completeOperation(^(VSMediaCompletion completion) { [binding prepareWithCheck:^NSError * { return nil; } configuration:^NSError * { configured = YES; return nil; } completion:completion]; }, 1);
  CHECK(!configured);
  [binding setSystemAudioAuthorization:YES];
  completeOperation(^(VSMediaCompletion completion) { [binding activate:audio completion:completion]; }, 2);
  completeOperation(^(VSMediaCompletion completion) { [binding deactivate:audio completion:completion]; }, 0);
  puts("passed: absent optional RTCAudioSession links and returns explicit adapter-unavailable errors");
} return 0; }
