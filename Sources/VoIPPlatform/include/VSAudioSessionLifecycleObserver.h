#import <Foundation/Foundation.h>
#import <TargetConditionals.h>
#include <stdbool.h>

#if ((TARGET_OS_IOS && __IPHONE_OS_VERSION_MAX_ALLOWED < 270000) || \
     (TARGET_OS_WATCH && __WATCH_OS_VERSION_MAX_ALLOWED < 270000)) && \
    !defined(VSSDK_TEST_AUDIO_LIFECYCLE_LEGACY_SDK)
#error "VoIPSDK requires the iOS/watchOS 27 SDK or newer to handle both audio interruption paths. The deployment minimum remains iOS 17.4."
#endif

NS_ASSUME_NONNULL_BEGIN
typedef NS_ENUM(NSInteger, VSAudioSessionLifecycleKind) {
  VSAudioSessionLifecycleKindActive,
  VSAudioSessionLifecycleKindInactive,
  VSAudioSessionLifecycleKindResumptionRecommended,
  VSAudioSessionLifecycleKindMediaServicesReset
};

/// Only immutable scalar observations cross the platform boundary. No session or
/// notification object is delivered to the SDK actor.
typedef struct {
  VSAudioSessionLifecycleKind kind;
  NSInteger source;
  NSUInteger interruptionReason;
  bool hasSource;
  bool hasInterruptionReason;
  bool shouldResume;
} VSAudioSessionLifecycleValue;
typedef void (^VSAudioSessionLifecycleHandler)(VSAudioSessionLifecycleValue value);

#if DEBUG && TARGET_OS_SIMULATOR
/// Internal source-compiled acceptance sink. Receives scalars on the observer's
/// main queue; no notification or session object escapes and no I/O is performed.
typedef void (^VSAudioSessionLifecycleDiagnosticHandler)(NSString *notificationName,
  NSString *contextClass, bool mainThread, VSAudioSessionLifecycleValue value);
FOUNDATION_EXPORT void VSAudioSessionSetLifecycleDiagnosticHandler(
  VSAudioSessionLifecycleDiagnosticHandler _Nullable handler);
#endif

/// Build with the iOS/watchOS 27 SDK or newer. Runtime availability selects the
/// legacy notification on earlier systems and the typed modern pair on 27+.
/// The handler is delivered on NSOperationQueue.mainQueue.
@interface VSAudioSessionLifecycleObserver : NSObject
- (instancetype)initWithHandler:(VSAudioSessionLifecycleHandler)handler;
- (void)invalidate;
@end
NS_ASSUME_NONNULL_END
