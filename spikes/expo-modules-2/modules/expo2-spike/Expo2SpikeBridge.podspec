Pod::Spec.new do |s|
  s.name = 'Expo2SpikeBridge'
  s.version = '0.0.0'
  s.summary = 'Isolated Expo Modules 2.0 bridge spike.'
  s.homepage = 'https://github.com/ziahamza/ios-voip-sdk'
  s.author = 'Hamza Zia'
  s.license = { :type => 'UNLICENSED' }
  s.source = { :git => 'https://github.com/ziahamza/ios-voip-sdk.git' }
  s.platform = :ios, '17.4'
  s.swift_version = '6.0'
  s.static_framework = true
  s.dependency 'ExpoModulesCore'
  s.pod_target_xcconfig = { 'DEFINES_MODULE' => 'YES' }
  s.source_files = 'apple/*.{swift,h,m}'
  s.public_header_files = 'apple/*.h'
  s.frameworks = 'PushKit', 'CallKit'
end
