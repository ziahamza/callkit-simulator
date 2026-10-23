import CoreFoundation
import Foundation

/// Example-only sandbox control; no arbitrary URL, intent or destination input.
enum M3DirectControl {
  static func matches(_ bytes: Data, runID: String, simulatorID: String, pid: Int32) -> Bool {
    guard let value = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
          Set(value.keys) == ["schemaVersion", "runID", "simulatorID", "appPID", "delivery"],
          let version = value["schemaVersion"] as? NSNumber, integer(version), version.intValue == 1,
          value["runID"] as? String == runID, value["simulatorID"] as? String == simulatorID,
          let process = value["appPID"] as? NSNumber, integer(process), process.int64Value == Int64(pid),
          value["delivery"] as? String == "direct-url-and-intent" else { return false }
    return true
  }

  private static func integer(_ value: NSNumber) -> Bool {
    CFGetTypeID(value) != CFBooleanGetTypeID() && !["f", "d"].contains(String(cString: value.objCType))
  }
}
