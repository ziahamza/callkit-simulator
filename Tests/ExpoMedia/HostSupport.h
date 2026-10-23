#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <dispatch/dispatch.h>
#import "VSMediaBinding.h"
#define CHECK(...) do { if (!(__VA_ARGS__)) { fprintf(stderr, "failed %s:%d: %s\n", __FILE__, __LINE__, #__VA_ARGS__); abort(); } } while (0)
void waitFor(dispatch_semaphore_t semaphore);
void flushAudioQueue(void);
void completeOperation(void (^operation)(VSMediaCompletion), NSInteger expectedError);
void completeOperationWithDomain(void (^operation)(VSMediaCompletion), NSInteger expectedError, NSString *domain);
void VSHostMediaLog(NSString *format, ...);
void expectDiagnostic(dispatch_semaphore_t semaphore);
VSMediaConfiguration configuration(void);
