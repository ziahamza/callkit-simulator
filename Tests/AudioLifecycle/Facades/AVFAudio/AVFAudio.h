#import <Foundation/Foundation.h>

// Explicit host-only API facade. It exposes notification values and immutable
// contexts, but deliberately no AVAudioSession class or activation methods.
FOUNDATION_EXPORT NSNotificationName const AVAudioSessionInterruptionNotification;
FOUNDATION_EXPORT NSNotificationName const AVAudioSessionMediaServicesWereResetNotification;
FOUNDATION_EXPORT NSString *const AVAudioSessionInterruptionTypeKey;
FOUNDATION_EXPORT NSString *const AVAudioSessionInterruptionReasonKey;
FOUNDATION_EXPORT NSString *const AVAudioSessionInterruptionOptionKey;
enum { AVAudioSessionInterruptionTypeEnded = 0, AVAudioSessionInterruptionTypeBegan = 1,
       AVAudioSessionInterruptionOptionShouldResume = 1 };

#if VS_HOST_NEW_API
// Keep host fakes distinct from classes exported by the installed macOS runtime.
// Both the controls and the actual decoder's host translation import this facade.
#define AVAudioSessionInterruptionContext VSTestAudioSessionInterruptionContext
#define AVAudioSessionDeactivationContext VSTestAudioSessionDeactivationContext
#define AVAudioSessionResumptionContext VSTestAudioSessionResumptionContext
FOUNDATION_EXPORT NSNotificationName const AVAudioSessionDidBecomeActiveNotification;
FOUNDATION_EXPORT NSNotificationName const AVAudioSessionDidBecomeInactiveNotification;
FOUNDATION_EXPORT NSNotificationName const AVAudioSessionResumptionRecommendationNotification;
FOUNDATION_EXPORT NSString *const AVAudioSessionDeactivationContextKey;
FOUNDATION_EXPORT NSString *const AVAudioSessionResumptionContextKey;
enum { AVAudioSessionResumptionRecommendationShouldResume = 1 };
@interface AVAudioSessionInterruptionContext : NSObject
@property(nonatomic) NSUInteger reason;
@end
@interface AVAudioSessionDeactivationContext : NSObject
@property(nonatomic) NSInteger source;
@property(nonatomic, strong) AVAudioSessionInterruptionContext *interruptionContext;
@end
@interface AVAudioSessionResumptionContext : NSObject
@property(nonatomic) NSInteger recommendation;
@end
#endif
