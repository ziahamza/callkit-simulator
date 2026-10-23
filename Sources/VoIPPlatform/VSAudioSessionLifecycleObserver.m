#import "include/VSAudioSessionLifecycleObserver.h"
#import <TargetConditionals.h>
#if TARGET_OS_IOS || TARGET_OS_WATCH
#import <AVFAudio/AVFAudio.h>
#endif

#if DEBUG && TARGET_OS_SIMULATOR
static VSAudioSessionLifecycleDiagnosticHandler lifecycleDiagnostic;
void VSAudioSessionSetLifecycleDiagnosticHandler(VSAudioSessionLifecycleDiagnosticHandler handler) {
  // Refuse an off-main test installation; missing capture stays a failed gate.
  if (!NSThread.isMainThread) return;
  lifecycleDiagnostic = [handler copy];
}
#define VS_AUDIO_OBSERVED(note, context, value) do { \
  if (lifecycleDiagnostic) lifecycleDiagnostic((note).name, \
    (context) ? NSStringFromClass([(id)(context) class]) : @"", NSThread.isMainThread, (value)); \
} while (0)
#else
#define VS_AUDIO_OBSERVED(note, context, value) do {} while (0)
#endif

@implementation VSAudioSessionLifecycleObserver {
  NSMutableArray<id> *_tokens;
}
- (instancetype)initWithHandler:(VSAudioSessionLifecycleHandler)handler {
  self = [super init];
  if (!self) return nil;
  _tokens = [NSMutableArray new];
  #if TARGET_OS_IOS || TARGET_OS_WATCH
  #if __IPHONE_OS_VERSION_MAX_ALLOWED >= 270000 || __WATCH_OS_VERSION_MAX_ALLOWED >= 270000
  if (@available(iOS 27.0, watchOS 27.0, *)) {
    [self observe:AVAudioSessionDidBecomeActiveNotification receive:^(NSNotification *note) {
      VSAudioSessionLifecycleValue value = { .kind = VSAudioSessionLifecycleKindActive };
      VS_AUDIO_OBSERVED(note, nil, value);
      handler(value);
    }];
    [self observe:AVAudioSessionDidBecomeInactiveNotification receive:^(NSNotification *note) {
      AVAudioSessionDeactivationContext *context = note.userInfo[AVAudioSessionDeactivationContextKey];
      VSAudioSessionLifecycleValue value = { .kind = VSAudioSessionLifecycleKindInactive };
      if ([context isKindOfClass:AVAudioSessionDeactivationContext.class]) {
        value.hasSource = YES;
        value.source = context.source;
        if (context.interruptionContext) {
          value.hasInterruptionReason = YES;
          value.interruptionReason = context.interruptionContext.reason;
        }
      }
      VS_AUDIO_OBSERVED(note, context, value);
      handler(value);
    }];
    [self observe:AVAudioSessionResumptionRecommendationNotification receive:^(NSNotification *note) {
      AVAudioSessionResumptionContext *context = note.userInfo[AVAudioSessionResumptionContextKey];
      if (![context isKindOfClass:AVAudioSessionResumptionContext.class]) return;
      VSAudioSessionLifecycleValue value = {
        .kind = VSAudioSessionLifecycleKindResumptionRecommended,
        .shouldResume = context.recommendation == AVAudioSessionResumptionRecommendationShouldResume
      };
      VS_AUDIO_OBSERVED(note, context, value);
      handler(value);
    }];
  } else
  #endif
  {
    [self observe:AVAudioSessionInterruptionNotification receive:^(NSNotification *note) {
      NSNumber *raw = note.userInfo[AVAudioSessionInterruptionTypeKey];
      if (![raw isKindOfClass:NSNumber.class]) return;
      VSAudioSessionLifecycleValue value = { 0 };
      if (raw.unsignedIntegerValue == AVAudioSessionInterruptionTypeBegan) {
        value.kind = VSAudioSessionLifecycleKindInactive;
        NSNumber *reason = note.userInfo[AVAudioSessionInterruptionReasonKey];
        if ([reason isKindOfClass:NSNumber.class]) {
          value.hasInterruptionReason = YES;
          value.interruptionReason = reason.unsignedIntegerValue;
        }
      } else if (raw.unsignedIntegerValue == AVAudioSessionInterruptionTypeEnded) {
        value.kind = VSAudioSessionLifecycleKindResumptionRecommended;
        NSNumber *options = note.userInfo[AVAudioSessionInterruptionOptionKey];
        value.shouldResume = [options isKindOfClass:NSNumber.class] &&
          (options.unsignedIntegerValue & AVAudioSessionInterruptionOptionShouldResume) != 0;
      } else return;
      VS_AUDIO_OBSERVED(note, nil, value);
      handler(value);
    }];
  }
  [self observe:AVAudioSessionMediaServicesWereResetNotification receive:^(NSNotification *note) {
    VSAudioSessionLifecycleValue value = { .kind = VSAudioSessionLifecycleKindMediaServicesReset };
    VS_AUDIO_OBSERVED(note, nil, value);
    handler(value);
  }];
  #endif
  return self;
}
- (void)observe:(NSNotificationName)name receive:(void (^)(NSNotification *))receive {
  [_tokens addObject:[NSNotificationCenter.defaultCenter addObserverForName:name object:nil
    queue:NSOperationQueue.mainQueue usingBlock:receive]];
}
- (void)invalidate {
  for (id token in _tokens) [NSNotificationCenter.defaultCenter removeObserver:token];
  [_tokens removeAllObjects];
}
- (void)dealloc { [self invalidate]; }
@end
