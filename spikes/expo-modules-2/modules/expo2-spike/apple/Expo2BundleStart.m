#import "Expo2BundleStart.h"
#import <React/RCTBridgeProxy.h>
#import <React/RCTPerformanceLogger.h>
#import <React/RCTPLTag.h>
#import <objc/runtime.h>
double Expo2ScriptDownloadStart(id bridgeValue) {
  if (object_getClass(bridgeValue) != RCTBridgeProxy.class) return 0;
  RCTBridgeProxy *bridge = bridgeValue;
  NSArray<NSNumber *> *values = [bridge.performanceLogger valuesForTags];
  NSUInteger index = 2 * RCTPLScriptDownload;
  return values.count > index ? values[index].doubleValue : 0;
}
