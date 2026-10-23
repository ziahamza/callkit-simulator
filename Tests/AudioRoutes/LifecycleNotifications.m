#import <AVFAudio/AVFAudio.h>

// Definitions for the existing explicit host facade. Link the actual observer,
// not an alternate lifecycle implementation or the system AVFAudio framework.
NSNotificationName const AVAudioSessionInterruptionNotification = @"legacy";
NSNotificationName const AVAudioSessionMediaServicesWereResetNotification = @"reset";
NSString *const AVAudioSessionInterruptionTypeKey = @"type";
NSString *const AVAudioSessionInterruptionReasonKey = @"reason";
NSString *const AVAudioSessionInterruptionOptionKey = @"options";
NSNotificationName const AVAudioSessionDidBecomeActiveNotification = @"active";
NSNotificationName const AVAudioSessionDidBecomeInactiveNotification = @"inactive";
NSNotificationName const AVAudioSessionResumptionRecommendationNotification = @"resume";
NSString *const AVAudioSessionDeactivationContextKey = @"deactivation-context";
NSString *const AVAudioSessionResumptionContextKey = @"resumption-context";
@implementation AVAudioSessionInterruptionContext @end
@implementation AVAudioSessionDeactivationContext @end
@implementation AVAudioSessionResumptionContext @end
