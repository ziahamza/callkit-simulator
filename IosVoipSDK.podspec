require 'json'

package = JSON.parse(File.read(File.join(__dir__, 'package.json')))
runtime_version = File.read(File.join(__dir__, 'VERSION')).strip
properties_path = File.join(Pod::Config.instance.installation_root, 'Podfile.properties.json')
properties = File.exist?(properties_path) ? JSON.parse(File.read(properties_path)) : {}
provider_diagnostics = properties.fetch('ios-voip-sdk.providerOwnershipMonitoring', 'false')
unless ['true', 'false'].include?(provider_diagnostics)
  raise 'ios-voip-sdk.providerOwnershipMonitoring must be true or false.'
end
diagnostics_swift = provider_diagnostics == 'true' ? ' VSSDK_ENABLE_PROVIDER_DIAGNOSTICS' : ''
diagnostics_c = provider_diagnostics == 'true' ? ' VSSDK_ENABLE_PROVIDER_DIAGNOSTICS=1' : ''
media_adapter = properties.fetch('ios-voip-sdk.mediaAdapter', 'webrtc')
unless %w[webrtc livekit none].include?(media_adapter)
  raise 'IosVoipSDK: ios-voip-sdk.mediaAdapter must be webrtc, livekit, or none; regenerate the Expo iOS project.'
end
media_directory = { 'webrtc' => 'WebRTC', 'livekit' => 'LiveKit', 'none' => 'None' }.fetch(media_adapter)

Pod::Spec.new do |s|
  s.name = 'IosVoipSDK'
  s.version = package['version']
  s.summary = package['description']
  s.homepage = 'https://github.com/ziahamza/ios-voip-sdk'
  s.author = { 'Hamza Zia' => 'https://github.com/ziahamza' }
  s.license = { :type => 'UNLICENSED' }
  s.source = { :git => package['repository']['url'], :tag => "v#{s.version}" }
  s.platform = :ios, '17.4'
  s.swift_version = '6.0'
  s.static_framework = true
  s.header_dir = 'IosVoipSDK'
  s.dependency 'ExpoModulesCore'
  s.frameworks = 'LiveCommunicationKit', 'AVFoundation', 'PushKit'
  media_sources = ['ExpoMedia/Shared/ExpoMediaSupport.swift', "ExpoMedia/#{media_directory}/**/*.{swift,h,m}"]
  if media_adapter != 'none'
    media_sources += ['ExpoMedia/Shared/VSMediaBinding.h', 'ExpoMedia/Shared/QueuedMediaAdapter.swift']
  end
  s.source_files = ['Sources/**/*.{swift,h,m}', 'Expo/apple/**/*.{swift,h}', *media_sources]
  # The external DYLD loader is not part of the embedded SDK.
  s.exclude_files = 'Sources/Interposition/CKLoader.m'
  public_headers = ['Sources/Interposition/include/CKRuntimeBridge.h', 'Sources/VoIPPlatform/include/*.h', 'Expo/apple/IosVoipSDK.h']
  public_headers << 'ExpoMedia/Shared/VSMediaBinding.h' if media_adapter != 'none'
  s.public_header_files = public_headers
  s.private_header_files = 'Sources/Interposition/CKRuntimeSeam.h'
  if media_adapter == 'livekit'
    s.dependency 'React-Core'
    s.dependency 'livekit-react-native-webrtc', '= 150.1.0'
  end
  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    'SWIFT_ACTIVE_COMPILATION_CONDITIONS' => "$(inherited) CKSIM_SDK_SOURCE#{diagnostics_swift}",
    'GCC_PREPROCESSOR_DEFINITIONS' => "$(inherited) CKSIM_SDK_SOURCE=1 CKSIM_RUNTIME_VERSION=\\\"#{runtime_version}\\\"#{diagnostics_c}"
  }
end
