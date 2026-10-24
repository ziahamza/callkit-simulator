// Exact public registry/property subset; no React Native runtime is supplied.
#import <Foundation/Foundation.h>
@class RCTModuleRegistry;
@protocol RCTBridgeModule <NSObject>
@property(nonatomic, weak, readwrite) RCTModuleRegistry *moduleRegistry;
@end
@interface RCTModuleRegistry : NSObject
- (id)moduleForClass:(Class)moduleClass;
@end
#define RCT_EXPORT_MODULE(name) + (NSString *)moduleName { return @#name; }
