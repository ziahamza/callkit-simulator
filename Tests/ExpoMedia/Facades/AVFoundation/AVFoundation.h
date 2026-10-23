// Host-only facade: these controls never use a system audio session.
#import <Foundation/Foundation.h>
__attribute__((objc_runtime_name("VSHostAudioSession")))
@interface AVAudioSession : NSObject
@end
