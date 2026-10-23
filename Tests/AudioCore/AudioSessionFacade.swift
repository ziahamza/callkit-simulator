import Foundation
import os
import VoIPPlatform

/// Test-owned inert boundary. No activation API or system AV session exists here.
public final class AVAudioSession: Sendable {
  public enum Category: Sendable { case playAndRecord }
  public enum Mode: Sendable { case voiceChat }
  public struct CategoryOptions: OptionSet, Sendable {
    public let rawValue: UInt
    public init(rawValue: UInt) { self.rawValue = rawValue }
    public static let allowBluetoothHFP = CategoryOptions(rawValue: 1)
  }
  public enum FixtureError: Error { case configurationRejected }
  private struct State { var configurations = 0; var rejectNext = false }
  private static let instance = AVAudioSession()
  private let state = OSAllocatedUnfairLock(initialState: State())
  public init() {}
  public static var fixture: AVAudioSession { instance }
  public static func sharedInstance() -> AVAudioSession {
    precondition(VSAudioSessionIsOnQueue() && !Thread.isMainThread)
    return instance
  }
  public func setCategory(_ category: Category, mode: Mode, options: CategoryOptions = []) throws {
    precondition(VSAudioSessionIsOnQueue() && !Thread.isMainThread)
    let rejected = state.withLock { value in
      value.configurations += 1
      let rejected = value.rejectNext; value.rejectNext = false
      return rejected
    }
    if rejected { throw FixtureError.configurationRejected }
  }
  public var configurationCalls: Int { state.withLock { $0.configurations } }
  public func rejectNextConfiguration() { state.withLock { $0.rejectNext = true } }
}
