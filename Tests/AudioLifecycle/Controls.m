#import "VSAudioSessionLifecycleObserver.h"
#import <AVFAudio/AVFAudio.h>

NSNotificationName const AVAudioSessionInterruptionNotification = @"legacy";
NSNotificationName const AVAudioSessionMediaServicesWereResetNotification = @"reset";
NSString *const AVAudioSessionInterruptionTypeKey = @"type";
NSString *const AVAudioSessionInterruptionReasonKey = @"reason";
NSString *const AVAudioSessionInterruptionOptionKey = @"options";
#if VS_HOST_NEW_API
NSNotificationName const AVAudioSessionDidBecomeActiveNotification = @"active";
NSNotificationName const AVAudioSessionDidBecomeInactiveNotification = @"inactive";
NSNotificationName const AVAudioSessionResumptionRecommendationNotification = @"resume";
NSString *const AVAudioSessionDeactivationContextKey = @"deactivation-context";
NSString *const AVAudioSessionResumptionContextKey = @"resumption-context";
@implementation AVAudioSessionInterruptionContext @end
@implementation AVAudioSessionDeactivationContext @end
@implementation AVAudioSessionResumptionContext @end
#endif

static void post(NSString *name, NSDictionary *info) {
  [NSNotificationCenter.defaultCenter postNotificationName:name object:nil userInfo:info];
}
#define CHECK(value) do { if (!(value)) { fprintf(stderr, "failed at line %d\n", __LINE__); return 1; } } while (0)

int main(void) { @autoreleasepool {
  __block NSMutableArray<NSValue *> *values = [NSMutableArray new];
  __block BOOL onlyMain = YES;
  VSAudioSessionLifecycleObserver *observer = [[VSAudioSessionLifecycleObserver alloc]
    initWithHandler:^(VSAudioSessionLifecycleValue value) {
      onlyMain = onlyMain && NSThread.isMainThread;
      [values addObject:[NSValue valueWithBytes:&value objCType:@encode(VSAudioSessionLifecycleValue)]];
    }];
  VSAudioSessionLifecycleValue value = {0};
  __block NSUInteger observed = 0;
  __block VSAudioSessionLifecycleValue observedValue = {0};
  VSAudioSessionSetLifecycleDiagnosticHandler(^(NSString *name, NSString *context, bool main,
                                               VSAudioSessionLifecycleValue raw) {
    onlyMain = onlyMain && name.length > 0 && main;
    (void)context;
    observed++; observedValue = raw;
  });
  NSUInteger cases = 0;
#if VS_HOST_NEW_API
  post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @1 });
  CHECK(values.count == 0); cases++; // No duplicate legacy observation on the new path.
  post(AVAudioSessionDidBecomeActiveNotification, nil);
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(values.count == 1 && value.kind == VSAudioSessionLifecycleKindActive &&
        !value.hasSource && !value.hasInterruptionReason && !value.shouldResume); cases++;
  AVAudioSessionInterruptionContext *interruption = [AVAudioSessionInterruptionContext new];
  interruption.reason = 4;
  AVAudioSessionDeactivationContext *context = [AVAudioSessionDeactivationContext new];
  context.source = 2; context.interruptionContext = interruption;
  post(AVAudioSessionDidBecomeInactiveNotification, @{ AVAudioSessionDeactivationContextKey: context });
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(values.count == 2 && value.kind == VSAudioSessionLifecycleKindInactive &&
        value.hasSource && value.source == 2 && value.hasInterruptionReason &&
        value.interruptionReason == 4 && !value.shouldResume); cases++;
  context.interruptionContext = nil;
  post(AVAudioSessionDidBecomeInactiveNotification, @{ AVAudioSessionDeactivationContextKey: context });
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(values.count == 3 && value.hasSource && !value.hasInterruptionReason); cases++;
  for (id invalid in @[[NSNull null], @"invalid"]) {
    post(AVAudioSessionDidBecomeInactiveNotification, @{ AVAudioSessionDeactivationContextKey: invalid });
    [values.lastObject getValue:&value size:sizeof value];
    CHECK(value.kind == VSAudioSessionLifecycleKindInactive && !value.hasSource &&
          !value.hasInterruptionReason && !value.shouldResume); cases++;
  }
  post(AVAudioSessionDidBecomeInactiveNotification, nil);
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(value.kind == VSAudioSessionLifecycleKindInactive && !value.hasSource && !value.hasInterruptionReason); cases++;
  AVAudioSessionResumptionContext *resume = [AVAudioSessionResumptionContext new];
  for (NSNumber *raw in @[@1, @0, @42]) {
    resume.recommendation = raw.integerValue;
    NSUInteger before = values.count;
    post(AVAudioSessionResumptionRecommendationNotification, @{ AVAudioSessionResumptionContextKey: resume });
    [values.lastObject getValue:&value size:sizeof value];
    CHECK(values.count == before + 1 && value.kind == VSAudioSessionLifecycleKindResumptionRecommended &&
          value.shouldResume == (raw.integerValue == 1) && !value.hasSource && !value.hasInterruptionReason); cases++;
  }
  NSUInteger beforeInvalid = values.count;
  post(AVAudioSessionResumptionRecommendationNotification, nil);
  post(AVAudioSessionResumptionRecommendationNotification, @{ AVAudioSessionResumptionContextKey: @"invalid" });
  CHECK(values.count == beforeInvalid); cases++;
#else
  post(@"active", nil); post(@"inactive", nil); post(@"resume", nil);
  CHECK(values.count == 0); cases++;
  post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @1,
    AVAudioSessionInterruptionReasonKey: @4 });
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(values.count == 1 && value.kind == VSAudioSessionLifecycleKindInactive &&
        !value.hasSource && value.hasInterruptionReason && value.interruptionReason == 4 && !value.shouldResume); cases++;
  post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @1,
    AVAudioSessionInterruptionReasonKey: @"invalid" });
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(values.count == 2 && !value.hasInterruptionReason); cases++;
  for (id raw in @[@1, @0, @2, @3, @"invalid", [NSNull null]]) {
    post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @0,
      AVAudioSessionInterruptionOptionKey: raw });
    [values.lastObject getValue:&value size:sizeof value];
    BOOL shouldResume = [raw isKindOfClass:NSNumber.class] && ([raw unsignedIntegerValue] & 1) != 0;
    CHECK(value.kind == VSAudioSessionLifecycleKindResumptionRecommended && value.shouldResume == shouldResume &&
          !value.hasSource && !value.hasInterruptionReason); cases++;
  }
  post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @0 });
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(!value.shouldResume); cases++;
  NSUInteger beforeInvalid = values.count;
  post(AVAudioSessionInterruptionNotification, nil);
  post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @"invalid" });
  post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @22 });
  CHECK(values.count == beforeInvalid); cases++;
#endif
  post(AVAudioSessionMediaServicesWereResetNotification, nil);
  [values.lastObject getValue:&value size:sizeof value];
  CHECK(value.kind == VSAudioSessionLifecycleKindMediaServicesReset &&
        !value.hasSource && !value.hasInterruptionReason && !value.shouldResume); cases++;
  NSUInteger beforeBackgroundPost = values.count;
  dispatch_async(dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^{
    post(AVAudioSessionMediaServicesWereResetNotification, nil);
  });
  NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:2];
  while (values.count == beforeBackgroundPost && deadline.timeIntervalSinceNow > 0) {
    [NSRunLoop.currentRunLoop runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
  }
  CHECK(values.count == beforeBackgroundPost + 1 && onlyMain); cases++;
  CHECK(observed == values.count && observedValue.kind == VSAudioSessionLifecycleKindMediaServicesReset); cases++;
  VSAudioSessionSetLifecycleDiagnosticHandler(nil);
  NSUInteger observedBeforeDisabled = observed;
  post(AVAudioSessionMediaServicesWereResetNotification, nil);
  CHECK(observed == observedBeforeDisabled && values.count == observed + 1); cases++;
  NSUInteger beforeInvalidation = values.count;
  [observer invalidate]; [observer invalidate];
  post(AVAudioSessionMediaServicesWereResetNotification, nil);
  post(AVAudioSessionInterruptionNotification, @{ AVAudioSessionInterruptionTypeKey: @1 });
  post(@"active", nil); post(@"inactive", nil); post(@"resume", nil);
  CHECK(values.count == beforeInvalidation && onlyMain); cases++;
  observer = nil;
  @autoreleasepool {
    VSAudioSessionLifecycleObserver *temporary = [[VSAudioSessionLifecycleObserver alloc]
      initWithHandler:^(VSAudioSessionLifecycleValue ignored) {
        [values addObject:[NSValue valueWithBytes:&ignored objCType:@encode(VSAudioSessionLifecycleValue)]];
      }];
    __weak VSAudioSessionLifecycleObserver *weakTemporary = temporary;
    CHECK(weakTemporary != nil);
    temporary = nil;
    CHECK(weakTemporary == nil);
  }
  post(AVAudioSessionMediaServicesWereResetNotification, nil);
  CHECK(values.count == beforeInvalidation); cases++;
  printf("passed: %lu lifecycle decoding and invalidation groups; compiler-selected %s branch; host facades only\n",
    (unsigned long)cases, VS_HOST_NEW_API ? "27" : "legacy");
  return 0;
} }
