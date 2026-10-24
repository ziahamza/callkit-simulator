import UIKit
import CallKit
import PushKit
import ObjectiveC

@MainActor
@objc(SDKBootstrapProbe)
final class SDKBootstrapProbe: NSObject, UIApplicationDelegate {
  private var failures = 0

  fileprivate static func trace(_ event: String, _ fields: [String: Any] = [:]) {
    var row = fields
    row["event"] = event
    row["t_ms"] = Int(ProcessInfo.processInfo.systemUptime * 1000)
    row["pid"] = Int(getpid())
    row["runID"] = ProcessInfo.processInfo.environment["CKSIM_RUN_ID"] ?? ""
    row["simulatorID"] = ProcessInfo.processInfo.environment["SIMULATOR_UDID"] ?? ""
    row["bundleID"] = Bundle.main.bundleIdentifier ?? ""
    row["main"] = Thread.isMainThread
    if let bytes = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) {
      FileHandle.standardError.write(Data("PROBE ".utf8) + bytes + Data([10]))
    }
  }

  private func check(_ condition: Bool, _ label: String) {
    if !condition { failures += 1 }
    Self.trace("bootstrap.check", ["label": label, "passed": condition])
  }

  func application(_ application: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
    Self.trace("bootstrap.didFinishLaunching")
    return true
  }

  fileprivate func run() {
    let mode = ProcessInfo.processInfo.environment["CKSIM_BOOTSTRAP_CASE"] ?? "healthy"
    let inserted = (ProcessInfo.processInfo.environment["DYLD_INSERT_LIBRARIES"] ?? "").split(separator: ":")
    let runtimeInjected = inserted.contains { entry in
      ["CKSimRuntime.dylib", "CKSimLoader.dylib"].contains(URL(fileURLWithPath: String(entry)).lastPathComponent)
    }
    Self.trace("launch", ["mode": mode, "injected": runtimeInjected,
      "bundlePath": Bundle.main.bundleURL.resolvingSymlinksInPath().path])
    check(!CKSimRuntimeIsActive(), "explicit-activation-required")
    let provider = CXProvider(configuration: CXProviderConfiguration())
    let controller = CXCallController(queue: .main)
    let registry = PKPushRegistry(queue: .main)
    provider.invalidate()
    let providerSelectors = ["initWithConfiguration:", "setDelegate:queue:", "reportNewIncomingCallWithUUID:update:completion:",
      "reportCallWithUUID:updated:", "reportCallWithUUID:endedAtDate:reason:",
      "reportOutgoingCallWithUUID:startedConnectingAtDate:", "reportOutgoingCallWithUUID:connectedAtDate:",
      "pendingTransactions", "pendingCallActionsOfClass:withCallUUID:", "invalidate"]
    let controllerSelectors = ["initWithQueue:", "requestTransaction:completion:",
      "requestTransactionWithAction:completion:", "requestTransactionWithActions:completion:"]
    let observerSelectors = ["calls", "setDelegate:queue:"]
    var entries: [(AnyClass, String)] = []
    for cls in [CXProvider.self, object_getClass(provider)!] { entries += providerSelectors.map { (cls, $0) } }
    for cls in [CXCallController.self, object_getClass(controller)!] { entries += controllerSelectors.map { (cls, $0) } }
    for cls in [CXCallObserver.self, object_getClass(controller.callObserver)!] { entries += observerSelectors.map { (cls, $0) } }
    for cls in [PKPushRegistry.self, object_getClass(registry)!] { entries += ["initWithQueue:", "setDelegate:", "setDesiredPushTypes:"].map { (cls, $0) } }
    for cls in [CXAction.self, CXStartCallAction.self, CXAnswerCallAction.self, CXEndCallAction.self,
                CXSetHeldCallAction.self, CXSetMutedCallAction.self, CXSetGroupCallAction.self, CXPlayDTMFCallAction.self] {
      entries += ["fulfill", "fail", "timeoutDate"].map { (cls, $0) }
    }
    entries += [(CXStartCallAction.self, "fulfillWithDateStarted:"), (CXAnswerCallAction.self, "fulfillWithDateConnected:"),
                (CXEndCallAction.self, "fulfillWithDateEnded:")]
    func snapshot() -> [UInt] {
      entries.map { cls, name in
        class_getMethodImplementation(cls, NSSelectorFromString(name)).map { unsafeBitCast($0, to: UInt.self) } ?? 0
      }
    }
    let before = snapshot()
    if mode == "healthy" {
      check(SimulatorSupport.activateIfNeeded(), "activation-succeeds")
      let active = snapshot()
      check(active != before && CKSimRuntimeIsActive(), "successful-runtime-active")
      check(SimulatorSupport.activateIfNeeded() && active == snapshot(), "activation-idempotent")
    } else {
      if mode == "profile" { setenv("CKSIM_PROFILE", "unsupported-bootstrap-test", 1) }
      else { setenv("CKSIM_TEST_FAILURE", "push-after-install", 1) }
      check(!SimulatorSupport.activateIfNeeded(), "activation-refuses")
      check(!CKSimRuntimeIsActive(), "refused-runtime-inactive")
      let after = snapshot()
      check(before == after, "all-root-concrete-action-push-imps-restored")
      for (index, entry) in entries.enumerated() where before[index] != after[index] {
        Self.trace("bootstrap.changedIMP", ["class": NSStringFromClass(entry.0), "selector": entry.1])
      }
      check(!SimulatorSupport.activateIfNeeded() && before == snapshot(), "refusal-idempotent")
    }
    Self.trace("done", ["success": failures == 0, "checks": mode == "healthy" ? 4 : 5, "snapshots": entries.count])
  }
}

@MainActor
@objc(SDKBootstrapScene)
final class SDKBootstrapScene: UIResponder, UIWindowSceneDelegate {
  var window: UIWindow?
  func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
    SDKBootstrapProbe.trace("bootstrap.scene-entry")
    guard let scene = scene as? UIWindowScene else { return }
    window = UIWindow(windowScene: scene)
    window?.rootViewController = UIViewController()
    window?.makeKeyAndVisible()
    DispatchQueue.main.async { (UIApplication.shared.delegate as? SDKBootstrapProbe)?.run() }
  }
}

@main
struct SDKBootstrapMain {
  @MainActor static func main() {
    SDKBootstrapProbe.trace("app.entry")
    UIApplicationMain(CommandLine.argc, CommandLine.unsafeArgv, nil, NSStringFromClass(SDKBootstrapProbe.self))
  }
}
