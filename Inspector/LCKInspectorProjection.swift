import Foundation

struct InspectorDeadlineTiming {
  let remainingSeconds: Double
  let progress: Double
}
struct InspectorActionTiming {
  let reported: InspectorDeadlineTiming?
  let effective: InspectorDeadlineTiming?
}

enum InspectorLCKProjection {
  static let provenance = "Measured #85 · LCK · iPhone 13 · iOS 27.0 · Oct 24, 2026"
  static let sourcePath = "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/README.md"

  static func systemChoices(for call: RuntimeEvent) -> [String]? {
    guard call.apiKind == "livecommunicationkit", let choices = call.availableSystemChoices else { return nil }
    let recognized = Set(["answer", "decline", "hold-and-accept", "end-and-accept", "end-held-and-accept"])
    var seen = Set<String>()
    return choices.filter { recognized.contains($0) && seen.insert($0).inserted }
  }

  static func appMergeTargets(for call: RuntimeEvent, calls: [RuntimeEvent]) -> [RuntimeEvent] {
    guard let id = call.uuid, !id.isEmpty else { return [] }
    var latest: [String: RuntimeEvent] = [:]
    for target in calls {
      guard let targetID = target.uuid, !targetID.isEmpty, targetID != id else { continue }
      if latest[targetID] == nil || latest[targetID]!.sequence < target.sequence { latest[targetID] = target }
    }
    return latest.values.filter { $0.ended != true }.sorted { $0.sequence < $1.sequence }
  }

  static func actionTiming(for action: RuntimeEvent, nowMS: Double) -> InspectorActionTiming {
    .init(reported: timing(deadlineMS: action.deadlineMS, duration: action.deadlineSeconds, nowMS: nowMS),
          effective: timing(deadlineMS: action.expiryMS, duration: action.expirySeconds, nowMS: nowMS))
  }

  private static func timing(deadlineMS: Double?, duration: Double?, nowMS: Double) -> InspectorDeadlineTiming? {
    guard let deadlineMS, deadlineMS.isFinite, let duration, duration.isFinite, duration > 0, nowMS.isFinite else { return nil }
    let remaining = (deadlineMS - nowMS) / 1000
    guard remaining.isFinite else { return nil }
    return .init(remainingSeconds: max(0, remaining), progress: min(1, max(0, 1 - remaining / duration)))
  }
}
