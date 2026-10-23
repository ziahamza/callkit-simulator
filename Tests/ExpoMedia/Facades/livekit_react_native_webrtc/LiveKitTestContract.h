// Host-only subset of the selected LiveKit public ObjC contract.
#import <Foundation/Foundation.h>
NS_ASSUME_NONNULL_BEGIN
typedef struct {
  BOOL isInputAvailable;
  BOOL isOutputAvailable;
} LKRTCAudioEngineAvailability;
@interface LKRTCAudioDeviceModule : NSObject
@property(nonatomic, readonly) LKRTCAudioEngineAvailability engineAvailability;
- (NSInteger)setEngineAvailability:(LKRTCAudioEngineAvailability)availability;
@end
@interface AudioDeviceModuleObserver : NSObject
@property(nonatomic, copy, nullable) NSDictionary<NSString *, id> *automaticAudioSessionConfig;
@property(nonatomic) BOOL isWillEnableEngineActive;
@property(nonatomic) BOOL isDidDisableEngineActive;
@end
@interface WebRTCModule : NSObject
@property(nonatomic, readonly) LKRTCAudioDeviceModule *audioDeviceModule;
@property(nonatomic, readonly) AudioDeviceModuleObserver *audioDeviceModuleObserver;
@end
NS_ASSUME_NONNULL_END
