#import <WebRTC/RTCAudioSession.h>

// Build-only API contract against the pinned real backend. Never called by the
// example; runtime activation remains the SDK's genuine LCK delegate path.
#pragma clang diagnostic error "-Wobjc-method-access"
__attribute__((unused)) static void checkStandardWebRTCContract(RTCAudioSession *session, AVAudioSession *audio) {
  [session lockForConfiguration];
  @try {
    session.useManualAudio = YES;
    session.isAudioEnabled = NO;
  } @finally {
    [session unlockForConfiguration];
  }
  [session audioSessionDidActivate:audio];
  [session audioSessionDidDeactivate:audio];
}
