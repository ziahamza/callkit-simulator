#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>

NS_ASSUME_NONNULL_BEGIN

/// Shared executor for potentially blocking SDK audio work. Never synchronously
/// wait for this queue from the main thread or hold a lock while enqueueing work.
FOUNDATION_EXPORT dispatch_queue_t VSAudioSessionGetQueue(void);
FOUNDATION_EXPORT BOOL VSAudioSessionIsOnQueue(void);

NS_ASSUME_NONNULL_END
