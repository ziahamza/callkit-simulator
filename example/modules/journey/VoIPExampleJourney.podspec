require 'json'
properties_path = File.join(Pod::Config.instance.installation_root, 'Podfile.properties.json')
properties = File.exist?(properties_path) ? JSON.parse(File.read(properties_path)) : {}

Pod::Spec.new do |s|
  s.name = 'VoIPExampleJourney'
  s.version = '0.0.0'
  s.summary = 'The example app calling journey; excluded from the SDK package.'
  s.homepage = 'https://github.com/ziahamza/ios-voip-sdk'
  s.author = 'Hamza Zia'
  s.license = { :type => 'UNLICENSED' }
  s.source = { :git => 'https://github.com/ziahamza/ios-voip-sdk.git' }
  s.platform = :ios, '17.4'
  s.swift_version = '6.0'
  s.static_framework = true
  s.dependency 'ExpoModulesCore'
  s.dependency 'IosVoipSDK'
  # Pin the real standard backend only in the example's selected build variant.
  if properties['ios-voip-sdk.mediaAdapter'] == 'webrtc'
    s.dependency 'react-native-webrtc', '= 155.0.1'
    s.dependency 'JitsiWebRTC', '= 155.0.1'
  elsif properties['ios-voip-sdk.mediaAdapter'] == 'livekit'
    s.dependency 'livekit-react-native-webrtc', '>= 150.1.0', '< 151.0.0'
  end
  s.source_files = 'apple/*.swift'
  if properties['ios-voip-sdk.mediaAdapter'] == 'webrtc'
    s.source_files = ['apple/*.swift', 'apple/StandardWebRTCContract.m']
  end
  s.frameworks = 'LiveCommunicationKit', 'AVFoundation'
end
