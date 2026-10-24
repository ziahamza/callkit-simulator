const mediaAdapter = process.env.SDK_EXAMPLE_MEDIA_ADAPTER ?? 'none';
if (!['none', 'webrtc', 'livekit'].includes(mediaAdapter)) {
  throw new Error(
    'SDK_EXAMPLE_MEDIA_ADAPTER must be none, webrtc, or livekit.',
  );
}
// Both packages export WebRTCModule. Link only the selected native provider.
module.exports = {
  dependencies: {
    ...(mediaAdapter === 'webrtc'
      ? {}
      : { 'react-native-webrtc': { platforms: { ios: null } } }),
    ...(mediaAdapter === 'livekit'
      ? {}
      : {
          '@livekit/react-native': { platforms: { ios: null } },
          '@livekit/react-native-webrtc': { platforms: { ios: null } },
        }),
  },
};
