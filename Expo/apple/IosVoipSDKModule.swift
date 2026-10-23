import ExpoModulesCore
import Foundation

/// The Expo surface carries JSON and promises. Framework ownership stays native.
public final class IosVoipSDKModule: Module {
  private let bridgeID = UUID()

  public func definition() -> ModuleDefinition {
    Name("IosVoipSDK")
    Constants(["mediaAdapter": ExpoMediaSelection.name])
    Events("conversationsChanged", "pushTokenUpdated", "audioRoutesChanged", "handlerAction", "actionCancelled", "conversationEvent")

    OnCreate {
      let id = self.bridgeID
      let sink = ModuleEventSink(module: self)
      DispatchQueue.main.async {
        do { try ConversationBridges.install(id) { name, body in sink.send(name, body) } }
        catch { NSLog("[VoIPSDK] Native launch registration failed; conversation commands will report the error.") }
      }
    }
    OnDestroy {
      let id = self.bridgeID
      DispatchQueue.main.async { ConversationBridges.remove(id) }
    }
    OnAppContextDestroys {
      let id = self.bridgeID
      DispatchQueue.main.async { ConversationBridges.remove(id) }
    }

    Function("setHandlerAttached") { (attached: Bool, generation: Int) in
      let id = self.bridgeID
      // Synchronous JS calls are enqueued in order on the native owner's queue.
      DispatchQueue.main.async { ConversationBridges.attach(id, attached: attached, generation: generation) }
    }
    Function("setDialRequestListenerAttached") { (attached: Bool, generation: Int) in
      let id = self.bridgeID
      DispatchQueue.main.async { ConversationBridges.listenForDialRequests(id, attached: attached, generation: generation) }
    }
    Function("settleAction") { (actionID: String, generation: Int, success: Bool, reason: [String: String]?) in
      guard let action = UUID(uuidString: actionID) else { return }
      let id = self.bridgeID
      let rejection = HandlerRejection(reason)
      DispatchQueue.main.async {
        ConversationBridges.settle(id, id: action, generation: generation, success: success, rejection: rejection)
      }
    }

    AsyncFunction("getConversations") { (promise: Promise) in
      Self.respond(promise) {
        try BridgeJSON.dictionary(ExpoMediaSupport.startFromBundle().client.state)
      }
    }
    AsyncFunction("getPushToken") { (promise: Promise) in
      Self.respond(promise) {
        var body = try BridgeJSON.dictionary(ExpoMediaSupport.startFromBundle().pushService.tokenState)
        if body["token"] == nil { body["token"] = NSNull() }
        return body
      }
    }
    AsyncFunction("getAudioRoutes") { (promise: Promise) in
      Self.respond(promise, failureCode: "audio_routes", failureMessage: "The system audio routes are unavailable.") {
        try BridgeJSON.dictionary(ExpoMediaSupport.startFromBundle().client.audioRoutes)
      }
    }
    AsyncFunction("selectAudioRoute") { (id: String, promise: Promise) in
      Self.respond(promise, failureCode: "audio_route", failureMessage: "The system could not select this audio route.") {
        try await ExpoMediaSupport.startFromBundle().client.selectAudioRoute(id)
        return nil
      }
    }

    AsyncFunction("startConversation") { (options: [String: Any], promise: Promise) in
      guard let data = try? BridgeJSON.data(options) else { Self.rejectArguments(promise); return }
      Self.respond(promise) {
        let options = try BridgeJSON.decode(StartOptions.self, from: data)
        let client = try ExpoMediaSupport.startFromBundle().client
        return try await client.startConversation(to: options.to, video: options.video, metadata: options.metadata).uuidString
      }
    }
    AsyncFunction("reportIncomingConversation") { (payload: [String: Any], promise: Promise) in
      guard let data = try? BridgeJSON.data(payload) else { Self.rejectArguments(promise); return }
      Self.respond(promise) {
        let payload = try BridgeJSON.decode(IncomingCallPayload.self, from: data)
        return try await ExpoMediaSupport.startFromBundle().client.reportIncomingConversation(payload).uuidString
      }
    }
    AsyncFunction("joinConversation") { (id: String, promise: Promise) in
      Self.respond(promise) { try await ExpoMediaSupport.startFromBundle().client.joinConversation(Self.uuid(id)); return nil }
    }
    AsyncFunction("endConversation") { (id: String, promise: Promise) in
      Self.respond(promise) { try await ExpoMediaSupport.startFromBundle().client.endConversation(Self.uuid(id)); return nil }
    }
    AsyncFunction("setMuted") { (id: String, muted: Bool, promise: Promise) in
      Self.respond(promise) { try await ExpoMediaSupport.startFromBundle().client.setMuted(Self.uuid(id), muted); return nil }
    }
    AsyncFunction("setHeld") { (id: String, held: Bool, promise: Promise) in
      Self.respond(promise) { try await ExpoMediaSupport.startFromBundle().client.setHeld(Self.uuid(id), held); return nil }
    }
    AsyncFunction("playTones") { (id: String, digits: String, promise: Promise) in
      Self.respond(promise) { try await ExpoMediaSupport.startFromBundle().client.playTones(Self.uuid(id), digits); return nil }
    }
    AsyncFunction("mergeConversations") { (first: String, second: String, promise: Promise) in
      Self.respond(promise) {
        try await ExpoMediaSupport.startFromBundle().client.mergeConversations(Self.uuid(first), Self.uuid(second))
        return nil
      }
    }
    AsyncFunction("reportConversation") { (id: String, report: [String: Any], promise: Promise) in
      guard let data = try? BridgeJSON.data(report) else { Self.rejectArguments(promise); return }
      Self.respond(promise) {
        let report = try BridgeJSON.decode(ReportOptions.self, from: data)
        try await ExpoMediaSupport.startFromBundle().client.reportConversation(Self.uuid(id), report: report.report())
        return nil
      }
    }

    AsyncFunction("bootstrap") { (promise: Promise) -> Void in
      Task { @MainActor in
        let active = SimulatorSupport.activateIfNeeded()
        #if targetEnvironment(simulator)
        let simulator = true
        #else
        let simulator = false
        #endif
        promise.resolve([
          "platform": "ios",
          "simulator": simulator,
          "runtimeActive": active
        ])
      }
    }
  }

  private static func uuid(_ value: String) throws -> UUID {
    guard let id = UUID(uuidString: value) else { throw BridgeError.invalidArguments }
    return id
  }

  private static func rejectArguments(_ promise: Promise) {
    promise.reject("invalid_arguments", "The conversation command has invalid arguments.")
  }

  private static func respond(_ promise: Promise, failureCode: String = "conversation",
                              failureMessage: String = "The system could not complete the conversation operation.",
                              _ operation: @escaping @MainActor @Sendable () async throws -> Any?) {
    Task { @MainActor in
      do { promise.resolve(try await operation()) }
      catch BridgeError.invalidArguments { rejectArguments(promise) }
      catch ConversationClientError.unknownConversation { promise.reject("unknown_conversation", "The conversation is no longer active.") }
      catch ConversationClientError.unsupported { promise.reject("unsupported", "This conversation operation is unsupported.") }
      catch ConversationClientError.invalidPayload { rejectArguments(promise) }
      catch ConversationClientError.invalidDigits { rejectArguments(promise) }
      catch ConversationClientError.duplicateSDKLinkage {
        promise.reject("duplicate_sdk_linkage", "Link one VoIP SDK product in this process; both SwiftPM and the Expo pod were loaded.")
      }
      catch { promise.reject(failureCode, failureMessage) }
    }
  }
}

private struct StartOptions: Decodable {
  let to: Participant
  let video: Bool
  let metadata: [String: JSONValue]?
}

private struct ReportOptions: Decodable {
  let type: String
  let members: [Participant]?
  let video: Bool?
  let reason: ConversationEndedReason?

  func report() throws -> ConversationReport {
    switch type {
    case "connecting": return .connecting
    case "connected": return .connected
    case "updated": return .updated(members: members, video: video)
    case "ended":
      guard let reason else { throw BridgeError.invalidArguments }
      return .ended(reason)
    default: throw BridgeError.invalidArguments
    }
  }
}

/// The imported Expo emitter schedules JS delivery itself. Only its identity is
/// carried across queues; this wrapper never reads or mutates module-owned state.
/// Keeping this weak reference narrowly scoped avoids declaring the module Sendable.
private struct ModuleEventSink: @unchecked Sendable {
  weak var module: IosVoipSDKModule?
  @MainActor func send(_ name: String, _ body: [String: Any]) { module?.sendEvent(name, body) }
}
