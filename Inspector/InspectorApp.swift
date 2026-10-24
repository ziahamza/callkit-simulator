#if targetEnvironment(simulator)
import SwiftUI

@main
struct InspectorApp: App {
  @StateObject private var model = InspectorModel()
  var body: some Scene {
    WindowGroup { InspectorView(model: model).task { await model.follow() } }
  }
}
struct InspectorView: View {
  @ObservedObject var model: InspectorModel
  @State private var expanded: String?
  var body: some View {
    NavigationStack {
      List {
        Section {
          Label(model.online ? "Live runtime" : "Disconnected", systemImage: model.online ? "waveform.path" : "wifi.slash")
            .foregroundStyle(model.online ? Color.primary : .secondary)
          if let session = model.session {
            Text(session.bundleID).font(.subheadline).textSelection(.enabled)
            Text("\(session.profile) · run \(session.runID)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
          }
          if let error = model.error {
            Text(error).foregroundStyle(.red)
            Text("Keep cksim inspect running. If the target app restarted, reconnect from the terminal.")
              .font(.footnote).foregroundStyle(.secondary)
          }
          if let error = model.commandError {
            Text(error).foregroundStyle(.red)
            Button("Dismiss error") { model.commandError = nil }
          }
        }
        if let call = model.incoming {
          Section("Emulated incoming call") {
              Button { expanded = call.uuid } label: {
                HStack(spacing: 12) {
                  Image(systemName: "phone.fill").foregroundStyle(.green)
                  VStack(alignment: .leading, spacing: 4) {
                    Text(call.caller).font(.headline).foregroundStyle(.primary)
                    Text(model.active(excluding: call.uuid) == nil ? "Incoming call" : "Call waiting")
                      .font(.subheadline).foregroundStyle(.secondary)
                  }
                  Spacer()
                  Image(systemName: "arrow.up.left.and.arrow.down.right").foregroundStyle(.secondary)
                }.frame(minHeight: 44).contentShape(Rectangle())
              }.buttonStyle(.plain).accessibilityIdentifier("expand-call")
              CallButtons(model: model, call: call, expand: { expanded = call.uuid }, compact: true)
          }
        }
        ConversationGroups(model: model, expanded: $expanded)
        SystemTransactions(model: model)
        Section("Audio") {
          Label(model.audioActive ? "Audio active" : "Audio inactive", systemImage: model.audioActive ? "waveform" : "speaker.slash")
            .accessibilityIdentifier("audio-state")
        }
        Section {
          ForEach(InspectorIncomingFilter.allCases.filter { $0 != .none }) { reason in
            Toggle(isOn: Binding(get: { model.incomingFilter == reason }, set: {
              model.configureIncomingFilter($0 ? reason : .none)
            })) {
              VStack(alignment: .leading, spacing: 3) {
                Text(reason.title)
                Text("Code \(reason.code) · \(reason == .doNotDisturb ? "Measured on device" : "Unverified on device")")
                  .font(.caption).foregroundStyle(.secondary)
              }
            }.accessibilityIdentifier("incoming-filter-" + reason.rawValue)
          }
        } header: { Text("Incoming report filter") } footer: {
          Text("One filter applies to subsequent reports. Do Not Disturb returns an error after about 2.07 s; repeated calls stay filtered. Codes 4–7 and their timings are unverified.")
        }.disabled(model.busy || !model.online)
        Section {
          ForEach(model.timingReferences) { readout in
            VStack(alignment: .leading, spacing: 8) {
              Text(readout.title).font(.headline)
              HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                  Text("Simulator actual").font(.caption).foregroundStyle(.secondary)
                  Text(readout.actualValue).font(.subheadline.weight(.semibold)).monospacedDigit()
                }.frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: .leading, spacing: 4) {
                  Text("Device reference").font(.caption).foregroundStyle(.secondary)
                  Text(readout.deviceValue).font(.subheadline.weight(.semibold)).monospacedDigit()
                }.frame(maxWidth: .infinity, alignment: .leading)
              }
              Text(readout.actualLabel).font(.caption).foregroundStyle(.secondary)
              Text(readout.deviceProvenance).font(.caption).foregroundStyle(.secondary)
            }.padding(.vertical, 4).accessibilityElement(children: .combine)
              .accessibilityIdentifier("timing-\(readout.id)")
          }
        } header: {
          Text("Timing · actual and device reference")
        } footer: {
          Text("Actuals use matching events in the latest 300 journal rows. Unavailable means no complete, unambiguous pair. Device source: livecommunicationkit/push/README.md. These observations are references, not pass/fail limits.")
        }
        if !model.pending.isEmpty {
          Section("Pending actions") {
            ForEach(model.pending.values.sorted { $0.sequence < $1.sequence }) { action in
              TimelineView(.periodic(from: .now, by: 0.25)) { context in
                VStack(alignment: .leading, spacing: 4) {
                  Text(action.title).font(.headline)
                  if let deadline = action.deadlineMS {
                    Text(String(format: "Deadline in %.1f s", max(0, deadline / 1000 - context.date.timeIntervalSince1970)))
                      .monospacedDigit().foregroundStyle(.orange)
                  }
                  Text(action.detail).font(.caption).foregroundStyle(.secondary)
                }
              }
            }
          }
        }
        Section("Live timeline · latest 300 events") {
          ForEach(model.timeline.reversed()) { row in
            VStack(alignment: .leading, spacing: 5) {
              HStack(alignment: .firstTextBaseline) {
                Text(String(format: "+%.3f s", row.t_ms / 1000)).monospacedDigit().foregroundStyle(.secondary)
                Text(row.title).foregroundStyle(row.fulfilled == false ? Color.red : .primary)
              }.font(.subheadline)
              if !row.detail.isEmpty { Text(row.detail).font(.caption).foregroundStyle(.secondary) }
            }.padding(.vertical, 3).accessibilityElement(children: .combine)
          }
        }
      }
      .navigationTitle("Call Inspector")
      .fullScreenCover(isPresented: Binding(get: { expanded != nil }, set: { if !$0 { expanded = nil } })) {
        if let id = expanded, let call = model.conversations[id] {
          CallScreen(model: model, call: call) { expanded = nil }
        }
      }
    }
  }
}
private struct ConversationGroups: View {
  @ObservedObject var model: InspectorModel
  @Binding var expanded: String?
  var body: some View {
    Section {
      if model.providerConfigurations.isEmpty { Text("Provider configuration unavailable").font(.caption).foregroundStyle(.secondary) }
      ForEach(model.providerConfigurations) { configuration in
        VStack(alignment: .leading, spacing: 4) {
          Text("Configured groups: \(configuration.maximumCallGroups.map(String.init) ?? "Unavailable") · Calls per group: \(configuration.maximumCallsPerCallGroup.map(String.init) ?? "Unavailable")")
            .font(.subheadline)
          Text("\(configuration.apiKind ?? "API unavailable") · \(configuration.provider)").font(.caption).foregroundStyle(.secondary)
        }.accessibilityIdentifier("provider-configuration")
      }
      if model.groupedConversations.isEmpty { Text("Waiting for an active conversation.").foregroundStyle(.secondary) }
      ForEach(model.groupedConversations) { group in
        VStack(alignment: .leading, spacing: 8) {
          Text(group.simulatorGroupID.map { "Simulator group · " + $0 } ?? "Group unavailable")
            .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
          ForEach(group.calls) { call in
            Button { expanded = call.uuid } label: {
              VStack(alignment: .leading, spacing: 4) {
                HStack { Text(call.caller).foregroundStyle(.primary); Spacer(); Text(call.status).foregroundStyle(.secondary) }
                Text(call.uuid ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Text(call.capabilities.map { $0.isEmpty ? "Capabilities: none" : "Capabilities: " + $0.joined(separator: " · ") } ?? "Capabilities unavailable")
                  .font(.caption).foregroundStyle(.secondary)
              }.frame(minHeight: 44).contentShape(Rectangle())
            }.buttonStyle(.plain).accessibilityIdentifier("conversation-" + (call.uuid ?? ""))
          }
        }.padding(.vertical, 4)
      }
    } header: { Text("Simulator conversation groups") } footer: {
      Text("Group IDs belong to the simulator; LiveCommunicationKit has no public group IDs. Pause, merge and unmerge are capabilities. Multi-call timing and ordering are unverified on device.")
    }
  }
}
private struct SystemTransactions: View {
  @ObservedObject var model: InspectorModel
  var body: some View {
    if !model.recentSystemTransactions.isEmpty {
      Section("Recent simulator system transactions") {
        ForEach(model.recentSystemTransactions.prefix(10)) { transaction in
          VStack(alignment: .leading, spacing: 6) {
            Text("Simulator transaction · " + transaction.id).font(.caption).textSelection(.enabled)
            ForEach(transaction.constituents) { action in
              Text("\(action.index + 1). \(action.kind) · \(action.callUUID)").font(.caption)
              Text("Action · " + action.actionID).font(.caption2).foregroundStyle(.secondary)
              if let held = action.parameters.held { Text(held ? "Pause" : "Resume").font(.caption).foregroundStyle(.secondary) }
              if let target = action.parameters.groupTargetUUID {
                Text(target.isEmpty ? "Unmerge · no group target" : "Group target · " + target).font(.caption).foregroundStyle(.secondary)
              }
            }
            Text(transaction.verification).font(.caption).foregroundStyle(.secondary)
          }.padding(.vertical, 4).accessibilityElement(children: .combine)
            .accessibilityIdentifier("system-transaction-" + transaction.id)
        }
      }
    }
  }
}
private struct CallButtons: View {
  @ObservedObject var model: InspectorModel
  let call: RuntimeEvent
  var expand: (() -> Void)? = nil
  var compact = false
  var body: some View {
    VStack(spacing: 12) {
      if call.ended != true {
        if call.ringing == true {
          if !model.activeCandidates(excluding: call.uuid).isEmpty {
            if compact {
              Button(action: { expand?() }) { Text("Call waiting options").frame(minHeight: 44) }.buttonStyle(.borderedProminent)
            } else {
              ForEach(model.activeCandidates(excluding: call.uuid)) { active in
                if model.activeCandidates(excluding: call.uuid).count > 1 {
                  Text("Active call: \(active.caller) · \(String((active.uuid ?? "").prefix(8)))")
                    .font(.caption).foregroundStyle(.secondary)
                }
                if active.capabilities?.contains("pausing") == true {
                  action("Hold & Accept", symbol: "pause.fill", operation: "hold-and-accept", tint: .green, active: active)
                }
                action("End & Accept", symbol: "phone.down.fill", operation: "end-and-accept", tint: .orange, active: active)
              }
            }
            action("Decline", symbol: "phone.down.fill", operation: "decline", tint: .red)
          } else {
            HStack(spacing: 20) {
              action("Decline", symbol: "phone.down.fill", operation: "decline", tint: .red)
              action("Answer", symbol: "phone.fill", operation: "answer", tint: .green)
            }
          }
        } else {
          if call.connected == true {
            if call.capabilities?.contains("pausing") == true {
              Button { model.send("hold", call: call, held: call.held != true) } label: {
                Label(call.held == true ? "Resume" : "Pause", systemImage: call.held == true ? "play.fill" : "pause.fill").frame(minHeight: 44)
              }.buttonStyle(.bordered).accessibilityIdentifier(call.held == true ? "resume" : "pause")
            }
            if call.capabilities?.contains("merging") == true {
              Menu {
                ForEach(model.mergeTargets(for: call)) { target in
                  Button("\(target.caller) · \(target.uuid ?? "")") { model.send("merge", call: call, groupTarget: target) }
                    .disabled(model.isPending(target))
                }
              } label: { Label("Merge with…", systemImage: "person.2.fill").frame(minHeight: 44) }
                .disabled(model.mergeTargets(for: call).isEmpty).accessibilityIdentifier("merge")
            }
            if call.capabilities?.contains("unmerging") == true,
               model.groupedConversations.contains(where: { $0.simulatorGroupID == call.group && $0.calls.count > 1 }) {
              Button("Unmerge") { model.send("unmerge", call: call) }.frame(minHeight: 44).buttonStyle(.bordered).accessibilityIdentifier("unmerge")
            }
          }
          action("End Call", symbol: "phone.down.fill", operation: "end", tint: .red)
        }
      }
    }.disabled(model.busy || !model.online || model.isPending(call))
  }
  private func action(_ title: String, symbol: String, operation: String, tint: Color, active: RuntimeEvent? = nil) -> some View {
    Button { model.send(operation, call: call, active: active) } label: {
      Label(title, systemImage: symbol).font(.headline)
        .foregroundStyle(["answer", "hold-and-accept", "end-and-accept"].contains(operation) ? Color.black : .white)
        .frame(maxWidth: .infinity, minHeight: 44)
    }.buttonStyle(.borderedProminent).tint(tint).disabled(active.map { model.isPending($0) } ?? false).accessibilityIdentifier(operation)
  }
}
private struct CallScreen: View {
  @ObservedObject var model: InspectorModel
  let call: RuntimeEvent
  let dismiss: () -> Void
  @ScaledMetric(relativeTo: .largeTitle) private var avatarSize = 88
  var body: some View {
    NavigationStack {
      ScrollView {
        VStack(spacing: 24) {
          Spacer(minLength: 44)
          Image(systemName: "person.crop.circle.fill").resizable().scaledToFit()
            .frame(width: avatarSize, height: avatarSize).foregroundStyle(.secondary).accessibilityHidden(true)
          VStack(spacing: 10) {
            Text(call.caller).font(.largeTitle.bold()).multilineTextAlignment(.center)
            if let handle = call.handle, !handle.isEmpty { Text(handle).foregroundStyle(.secondary) }
            Text(call.status).font(.title3).accessibilityIdentifier("call-status")
            Text("Emulated call · \(model.session?.profile ?? "runtime")").font(.caption).foregroundStyle(.secondary)
          }
          if call.connected == true {
            Label(model.audioActive ? "Audio active" : "Waiting for audio activation", systemImage: "waveform")
              .font(.subheadline).foregroundStyle(.secondary)
          }
          Spacer(minLength: 44)
          if !model.online {
            Label("Runtime disconnected", systemImage: "wifi.slash").foregroundStyle(.red)
            if let error = model.error { Text(error).font(.footnote).foregroundStyle(.secondary) }
          }
          CallButtons(model: model, call: call)
          if let error = model.commandError {
            Text(error).foregroundStyle(.red)
            Button("Dismiss error") { model.commandError = nil }
          }
          Button(call.ended == true ? "Done" : "Show timeline", action: dismiss).frame(minHeight: 44)
        }.padding(24).frame(maxWidth: 520)
          .frame(maxWidth: .infinity)
      }
      .background(Color(uiColor: .systemGroupedBackground))
      .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Minimize", systemImage: "chevron.down", action: dismiss) } }
    }
  }
}
#endif
