#import "include/VSAudioSessionQueue.h"

static char audioQueueKey;

dispatch_queue_t VSAudioSessionGetQueue(void) {
  static dispatch_queue_t queue;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    queue = dispatch_queue_create("VoIPSDK.AudioSessionQueue", DISPATCH_QUEUE_SERIAL);
    dispatch_queue_set_specific(queue, &audioQueueKey, &audioQueueKey, NULL);
  });
  return queue;
}

BOOL VSAudioSessionIsOnQueue(void) {
  return dispatch_get_specific(&audioQueueKey) == &audioQueueKey;
}
