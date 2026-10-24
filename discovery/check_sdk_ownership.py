#!/usr/bin/env python3
"""Two actual linked SDK modules; platform factories are explicit host facades."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def run(argv, **kwargs):
    result = subprocess.run(argv, capture_output=True, timeout=90, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
    return result


def check_packaging(out):
    manifest = json.loads(run(['swift', 'package', 'dump-package'], cwd=ROOT).stdout)
    assert manifest['toolsVersion']['_version'] == '6.1.0'
    assert [trait['name'] for trait in manifest['traits']] == ['ProviderOwnershipDiagnostics']
    targets = {target['name']: target for target in manifest['targets']}
    for name, tools in [('VoIPPlatform', ['c']), ('CKSimRuntimeC', ['c']),
                        ('CKSimRuntime', ['swift']), ('VoIPSDK', ['c', 'swift'])]:
        for tool in tools:
            settings = [setting for setting in targets[name]['settings'] if setting['tool'] == tool and
                        setting['kind'].get('define', {}).get('_0', '').startswith('VSSDK_ENABLE_PROVIDER_DIAGNOSTICS')]
            assert len(settings) == 1 and settings[0]['condition'].get('traits') == ['ProviderOwnershipDiagnostics'], (name, tool)
    # CocoaPods IPC evaluates a spec in its own directory. Copy exact source
    # bytes so the isolated Podfile.properties fixture models that install root.
    for name in ['IosVoipSDK.podspec', 'package.json', 'VERSION']:
        shutil.copyfile(ROOT / name, out / name)
    for enabled in [None, False, True]:
        properties = out / 'Podfile.properties.json'
        properties.write_text(json.dumps({} if enabled is None else {'ios-voip-sdk.providerOwnershipMonitoring': str(enabled).lower()}))
        spec = json.loads(run(['pod', 'ipc', 'spec', str(out / 'IosVoipSDK.podspec')], cwd=out).stdout)
        flags = spec['pod_target_xcconfig']
        assert ('VSSDK_ENABLE_PROVIDER_DIAGNOSTICS' in flags['SWIFT_ACTIVE_COMPILATION_CONDITIONS']) == (enabled is True)
        assert ('VSSDK_ENABLE_PROVIDER_DIAGNOSTICS=1' in flags['GCC_PREPROCESSOR_DEFINITIONS']) == (enabled is True)
        assert 'CallKit' not in spec['frameworks']
    properties.write_text(json.dumps({'ios-voip-sdk.providerOwnershipMonitoring': 'yes'}))
    invalid = subprocess.run(['pod', 'ipc', 'spec', str(out / 'IosVoipSDK.podspec')], cwd=out, capture_output=True, timeout=30)
    assert invalid.returncode != 0 and b'must be true or false' in invalid.stdout + invalid.stderr

def main():
    inputs = [ROOT / name for name in (
        'Sources/VoIPPlatform/VSProcessSDKOwnership.m',
        'Sources/VoIPPlatform/include/VSProcessSDKOwnership.h',
        'Sources/VoIPSDK/VoIPApplication.swift', 'Sources/VoIPSDK/ConversationClient.swift',
        'Sources/VoIPSDK/ConversationModels.swift', 'Sources/VoIPSDK/ProcessSDKOwnership.swift',
        'Package.swift', 'IosVoipSDK.podspec', 'package.json', 'VERSION',
        'Sources/VoIPSDK/AudioActivation.swift',
        'discovery/check_sdk_ownership.py')]
    before = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    with tempfile.TemporaryDirectory(prefix='voip-process-ownership-') as temporary:
        out = Path(temporary)
        check_packaging(out)
        application = (ROOT / 'Sources/VoIPSDK/VoIPApplication.swift').read_text()
        boundary = '#if canImport(LiveCommunicationKit) && (os(iOS) || os(watchOS))'
        assert application.count(boundary) == 1
        (out / 'VoIPApplication.swift').write_text(application.replace(boundary, '#if true', 1))
        client = (ROOT / 'Sources/VoIPSDK/ConversationClient.swift').read_text()
        assert not re.search(r'public\s+(?:static\s+)?(?:var shared|func configure\()', client), 'only Application may publish/create the client'
        configuration = client[client.index('  public struct Configuration:'):client.index('  private static var instance')]
        selection = client[client.index('  private static var instance:'):client.index('  private typealias Record')]
        finish = client[client.index('  func finishLaunch() {'):client.index('  private func configureAudio()')]
        events = client[client.index('  @discardableResult\n  public func observeEvents'):client.index('  public func setConversationHandler')]
        emit_start = client.index('  private func emit(_ event: ConversationEvent,')
        emit = client[emit_start:client.index('\n  }', emit_start) + 4]
        activation = (ROOT / 'Sources/VoIPSDK/AudioActivation.swift').read_text()
        policy = activation[activation.index('public struct AudioActivationPolicy:'):activation.index('enum AudioActivationEvent:')]
        (out / 'Facades.swift').write_text('''import Foundation
''' + policy + '''
// Explicit platform facade: a coverage diagnostic is not a provider conflict.
@MainActor enum ProviderCreationMonitor {
  static var conflict: (@MainActor () -> Void)?
  static var installs = 0
  static var coverageDiagnostics = 0
  static func setOwnershipConflictHandler(_ handler: @escaping @MainActor () -> Void) { conflict = handler }
  static func install(_ diagnostic: (String) -> Void) {
    installs += 1; coverageDiagnostics += 1; diagnostic("host facade: coverage unavailable")
  }
}
@MainActor public protocol MediaAdapter: AnyObject {
  func setSystemAudioAuthorization(_ active: Bool)
}
@MainActor public final class NoMediaAdapter: MediaAdapter {
  public init() {}
  public func setSystemAudioAuthorization(_ active: Bool) {}
}
@MainActor final class RecordingMediaAdapter: MediaAdapter {
  var authorizations: [Bool] = []
  var onAuthorization: (@MainActor (Bool) -> Void)?
  func setSystemAudioAuthorization(_ active: Bool) {
    authorizations.append(active); onAuthorization?(active)
  }
}
@MainActor public final class ConversationClient {
''' + configuration + selection + '''
  static var creations = 0
  static var tracks = 0
  static var onTrack: (@MainActor () -> Void)?
  private let configuration: Configuration
  private let mediaAdapter: any MediaAdapter
  private let audioPolicy: AudioActivationPolicy
  private var launchFinished = false
  private var eventObservers: [UUID: @MainActor (ConversationEventContext) -> Void] = [:]
  private init(configuration: Configuration, mediaAdapter: any MediaAdapter, audioPolicy: AudioActivationPolicy) {
    self.configuration = configuration; self.mediaAdapter = mediaAdapter; self.audioPolicy = audioPolicy
    Self.creations += 1 // Explicit facade for constructing the LCK manager.
  }
  var selectedAdapterForHost: any MediaAdapter { mediaAdapter }
  private func trackManager() { // Explicit facade for observing LCK state.
    Self.tracks += 1; Self.onTrack?()
  }
''' + finish + events + emit + '''
}
@MainActor final class SimulatorSupport {
  static func configure(enabled: Bool) throws {}
}
@MainActor public final class PushService {
  init(client: ConversationClient) {}
  func start() throws {}
}
''')
        (out / 'Probe.swift').write_text('''import Foundation
#if DEBUG
#error("Ownership callback probe must exercise Release Swift, without DEBUG")
#endif
#if !VSSDK_ENABLE_PROVIDER_DIAGNOSTICS
#error("Ownership callback probe requires the explicit Release build option")
#endif
@MainActor public func startProbe() -> String {
  do { _ = try VoIPApplication.start(); return "started" }
  catch { return String(describing: error) }
}
@MainActor public func managerCreations() -> Int { ConversationClient.creations }
@MainActor public func monitorInstallations() -> Int { ProviderCreationMonitor.installs }
@MainActor public func configurationAndPublicationProbe() -> Bool {
  do {
    // Invalid first configurations must fail before the manager factory. The
    // process claim remains with this same linked SDK, so valid start can retry.
    for (groups, members) in [(0, 2), (2, 0)] {
      do {
        _ = try VoIPApplication.start(configuration: .init(client: .init(
          maximumConversationGroups: groups, maximumConversationsPerGroup: members)))
        return false
      } catch {
        guard (error as? ConversationClientError) == .invalidConfiguration,
              ConversationClient.creations == 0, VoIPApplication.current == nil else { return false }
      }
    }
    guard !ConversationClient.Configuration.defaultProviderOwnershipMonitoring,
          ProviderCreationMonitor.installs == 0 else { return false }
    let configuration = VoIPApplication.Configuration(client: .init(
      supportsVideo: false, providerOwnershipMonitoring: true))
    let adapter = RecordingMediaAdapter()
    var reentered = false
    var authorizationReentered = false
    ConversationClient.onTrack = {
      guard let published = VoIPApplication.current else { return }
      do {
        let repeated = try VoIPApplication.start(configuration: configuration, mediaAdapter: adapter)
        let publishedClient = try ConversationClient.forApplication(configuration: configuration.client, mediaAdapter: adapter)
        reentered = repeated === published && repeated.client === publishedClient &&
          repeated.client === published.client && ConversationClient.creations == 1
      } catch {}
    }
    adapter.onAuthorization = { active in
      guard !active, let published = VoIPApplication.current else { return }
      do {
        let repeated = try VoIPApplication.start(configuration: configuration, mediaAdapter: adapter)
        authorizationReentered = repeated === published && repeated.client === published.client
      } catch {}
    }
    let application = try VoIPApplication.start(configuration: configuration, mediaAdapter: adapter)
    guard reentered, authorizationReentered, application === VoIPApplication.current,
          application.client.selectedAdapterForHost === adapter, adapter.authorizations == [false],
          ConversationClient.creations == 1, ConversationClient.tracks == 1,
          try VoIPApplication.start(configuration: configuration) === application,
          try VoIPApplication.start(configuration: configuration, mediaAdapter: adapter) === application,
          adapter.authorizations == [false] else { return false }
    let conflictingAdapter = RecordingMediaAdapter()
    do {
      _ = try VoIPApplication.start(configuration: configuration, mediaAdapter: conflictingAdapter)
      return false
    } catch {
      guard (error as? ConversationClientError) == .alreadyConfigured else { return false }
    }
    do {
      _ = try ConversationClient.forApplication(configuration: configuration.client, mediaAdapter: conflictingAdapter)
      return false
    } catch {
      guard (error as? ConversationClientError) == .alreadyConfigured else { return false }
    }
    do {
      _ = try VoIPApplication.start(configuration: configuration, mediaAdapter: adapter,
        audioPolicy: AudioActivationPolicy(activationTimeout: 12, recoveryTimeout: 6))
      return false
    } catch {
      guard (error as? ConversationClientError) == .alreadyConfigured else { return false }
    }
    var changedClient = configuration; changedClient.client.supportsVideo.toggle()
    var changedApplication = configuration; changedApplication.simulatorSupport.toggle()
    // Reach the production client guard directly: Application rejects its own
    // mismatched configuration before delegating this particular check.
    do {
      _ = try ConversationClient.forApplication(configuration: changedClient.client)
      return false
    } catch {
      guard (error as? ConversationClientError) == .alreadyConfigured else { return false }
    }
    for conflict in [changedClient, changedApplication] {
      do { _ = try VoIPApplication.start(configuration: conflict); return false }
      catch {
        guard (error as? ConversationClientError) == .alreadyConfigured else { return false }
      }
    }
    guard ConversationClient.creations == 1 && ConversationClient.tracks == 1,
          ProviderCreationMonitor.installs == 1, conflictingAdapter.authorizations.isEmpty,
          adapter.authorizations == [false] else { return false }
    var events: [ConversationEventContext] = []
    let observer = application.client.observeEvents { events.append($0) }
    // These are the actual finishLaunch closure and actual observe/emit methods.
    // Interception/coverage itself remains the explicit platform facade above.
    ProviderCreationMonitor.install { _ in }
    guard events.isEmpty, ProviderCreationMonitor.coverageDiagnostics == 2,
          let conflict = ProviderCreationMonitor.conflict else { return false }
    conflict()
    guard events == [.init(type: .providerOwnershipLost)] else { return false }
    application.client.removeEventObserver(observer)
    conflict()
    return events == [.init(type: .providerOwnershipLost)] && ConversationClient.creations == 1 &&
      ConversationClient.tracks == 1
  } catch { return false }
}
public func claimOnly() -> Bool {
  do { try ProcessSDKOwnership.claim(); return true } catch { return false }
}
''')
        gate = ROOT / 'Sources/VoIPPlatform/VSProcessSDKOwnership.m'
        assert gate.is_file(), 'the actual Objective-C process claim must exist'
        header = out / 'Ownership.h'
        header.write_text('#import "' + str(ROOT / 'Sources/VoIPPlatform/include/VSProcessSDKOwnership.h') + '"\n')
        obj = out / 'gate.o'
        run(['clang', '-fobjc-arc', '-fmodules', '-c', str(gate), '-o', str(obj)])
        objects = [str(obj)]
        models = (ROOT / 'Sources/VoIPSDK/ConversationModels.swift').read_text()
        selected_models = '\n'.join(re.findall(r'public enum (?:UnhandledActionPolicy|ConversationClientError|ConversationEvent)[^{]+\{.*?\}', models, re.S))
        assert selected_models.count('public enum ') == 3
        selected_models += '\n' + models[models.index('public struct ConversationEventContext:'):]
        claim = ROOT / 'Sources/VoIPSDK/ProcessSDKOwnership.swift'
        combined = out / 'SDK.swift'
        combined.write_text('import Foundation\n' + selected_models + '\n' +
                            '\n'.join((out / name).read_text() for name in ('Facades.swift', 'VoIPApplication.swift', 'Probe.swift')) +
                            '\n' + claim.read_text())
        for module in ('SwiftPackageSDK', 'ExpoPodSDK'):
            print('Compiling ' + module, flush=True)
            run(['swiftc', '-O', '-D', 'VSSDK_ENABLE_PROVIDER_DIAGNOSTICS', '-swift-version', '6', '-warnings-as-errors', '-parse-as-library',
                 '-emit-library', '-emit-module', '-module-name', module, '-import-objc-header', str(header),
                 str(combined), *objects, '-emit-module-path', str(out / (module + '.swiftmodule')),
                 '-o', str(out / ('lib' + module + '.dylib'))])
            symbols = run(['nm', '-gU', str(out / ('lib' + module + '.dylib'))]).stdout
            assert b'_OBJC_CLASS_$_VSProcessSDKOwnership' in symbols, 'each image must contain the actual same-named helper'
        driver = out / 'Main.swift'
        driver.write_text('''import Foundation
import SwiftPackageSDK
import ExpoPodSDK
@main struct Driver {
  @MainActor static func main() {
    if CommandLine.arguments.last == "package-contract" || CommandLine.arguments.last == "pod-contract" {
      let podFirst = CommandLine.arguments.last == "pod-contract"
      precondition(podFirst ? ExpoPodSDK.configurationAndPublicationProbe() : SwiftPackageSDK.configurationAndPublicationProbe(),
        "Actual selection and finishLaunch must validate config and publish before reentrant observation")
      precondition((podFirst ? SwiftPackageSDK.startProbe() : ExpoPodSDK.startProbe()) == "duplicateSDKLinkage")
      precondition(SwiftPackageSDK.managerCreations() + ExpoPodSDK.managerCreations() == 1)
      print("passed"); return
    }
    let podFirst = CommandLine.arguments.last == "pod-first"
    let start = podFirst ? ExpoPodSDK.startProbe : SwiftPackageSDK.startProbe
    let duplicate = podFirst ? SwiftPackageSDK.startProbe : ExpoPodSDK.startProbe
    precondition(start() == "started" && start() == "started", "Same linked SDK owner must be idempotent")
    precondition(SwiftPackageSDK.monitorInstallations() + ExpoPodSDK.monitorInstallations() == 0,
      "Release must default to no provider monitor installation even when support is compiled")
    precondition(duplicate() == "duplicateSDKLinkage", "A second linked SDK must fail before constructing its manager")
    precondition(SwiftPackageSDK.managerCreations() + ExpoPodSDK.managerCreations() == 1,
      "The process must construct exactly one manager across both SDK modules")
    DispatchQueue.concurrentPerform(iterations: 200) { _ in
      precondition(podFirst ? ExpoPodSDK.claimOnly() : SwiftPackageSDK.claimOnly(), "The owner must survive concurrent claims")
      precondition(podFirst ? !SwiftPackageSDK.claimOnly() : !ExpoPodSDK.claimOnly(), "Concurrent duplicate claims must refuse")
    }
    print("passed")
  }
}
''')
        binary = out / 'Driver'
        run(['swiftc', '-swift-version', '6', '-warnings-as-errors', '-parse-as-library', str(driver),
             '-I', str(out), '-L', str(out), '-lSwiftPackageSDK', '-lExpoPodSDK',
             '-Xlinker', '-rpath', '-Xlinker', str(out), '-o', str(binary)])
        for order in ('package-first', 'pod-first', 'package-contract', 'pod-contract'):
            assert run([str(binary), order]).stdout.strip() == b'passed'
        # Static-link duplicates can fail before startup; this is already loud
        # and is separate from the dynamic two-SDK error path above.
        source = out / 'static.m'
        source.write_text('int main(void) { return 0; }\n')
        copied = out / 'gate-copy.o'; shutil.copyfile(out / 'gate.o', copied)
        static = subprocess.run(['clang', str(source), str(out / 'gate.o'), str(copied),
                                 '-framework', 'Foundation', '-o', str(out / 'Static')],
                                capture_output=True, timeout=30)
        assert static.returncode != 0 and b'duplicate symbol' in static.stderr, 'static duplicate failure must be an actual linker collision'
    assert before == {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}, 'sources changed during host qualification'
    print(json.dumps(dict(success=True, sourceSHA256=before, classification='two real optimized Release-opt-in host modules/dylibs; actual Application, client selection, finishLaunch, event observe/emit and process claim; provider interception, LCK observation/construction, simulator configuration and PushKit factories are explicit facades',
                          checks=['package-first-duplicate-refused-before-second-manager', 'pod-first-duplicate-refused-before-second-manager',
                                  'same-objc-helper-present-in-both-images', 'concurrent-claims-preserve-owner', 'static-linker-duplicate-is-already-loud',
                                  'actual-forApplication-repeated-same-configuration-is-idempotent',
                                  'actual-forApplication-rejects-invalid-first-configurations-before-manager',
                                  'actual-forApplication-rejects-changed-configuration-before-second-manager',
                                  'actual-Application-rejects-client-and-simulator-configuration-conflicts',
                                  'omitted-repeat-adapter-preserves-selection-and-conflicting-adapter-refuses',
                                  'actual-finishLaunch-reentrant-authorization-sees-published-owner-exactly-once',
                                  'actual-finishLaunch-reentrant-observation-sees-published-application-and-client',
                                  'actual-finishLaunch-observes-exactly-once-for-both-linked-module-orders',
                                  'release-monitor-default-does-not-install',
                                  'release-explicit-opt-in-installs-once-and-emits-actual-public-ownership-event',
                                  'release-coverage-diagnostics-do-not-emit-ownership-loss',
                                  'release-event-observer-removal-stops-delivery',
                                  'swiftpm-trait-and-pod-property-compile-time-opt-in',
                                  'pod-default-excludes-monitor-and-invalid-opt-in-refuses' ])))


if __name__ == '__main__':
    main()
