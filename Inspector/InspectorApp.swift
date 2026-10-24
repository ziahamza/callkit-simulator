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
        Section("Conversations") {
          ForEach(model.providerConfigurations) { configuration in
            Text("Configured maximums: \(configuration.maximumCallGroups.map(String.init) ?? "?") groups · \(configuration.maximumCallsPerCallGroup.map(String.init) ?? "?") calls per group")
              .font(.caption).foregroundStyle(.secondary)
          }
          if model.conversations.isEmpty {
            Text("Waiting for the app to report a call.").foregroundStyle(.secondary)
          }
          ForEach(model.conversations.values.sorted { $0.sequence > $1.sequence }) { call in
            Button { expanded = call.uuid } label: {
              HStack {
                VStack(alignment: .leading, spacing: 4) {
                  Text(call.caller).foregroundStyle(.primary)
                  Text(call.uuid ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                  if let capabilities = call.capabilities {
                    Text("Capabilities (informational): " + (capabilities.isEmpty ? "none" : capabilities.joined(separator: ", "))).font(.caption).foregroundStyle(.secondary)
                  }
                }
                Spacer()
                Text(call.status).font(.subheadline).foregroundStyle(.secondary)
              }.frame(minHeight: 44).contentShape(Rectangle())
            }.buttonStyle(.plain)
          }
        }
        Section {
          Text("Pause / End: effective expiry ≈5.3 s. Join: ≈64 s. LCK still reports a timeoutDate of 30 s.")
          Text(InspectorLCKProjection.provenance).font(.caption).foregroundStyle(.secondary)
          Text("LCK exposes no group membership or transaction IDs. Simulator IDs are modeled and unverified on LCK. App Merge / Unmerge pass through without changing system state; lock-screen Merge delivers no app action.").font(.caption).foregroundStyle(.secondary)
        } header: { Text("Measured LCK multi-call · #85") }
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
          Text("One filter applies to subsequent reports. Do Not Disturb returns an error after about 2.07 s; repeated calls stay filtered. Codes 5–7 and their timings are unverified.")
        }.disabled(model.busy || !model.online)
        Section {
          Toggle("Inject incoming error 4", isOn: Binding(get: { model.incomingErrorCode == 4 }, set: {
            model.configureIncomingError($0 ? 4 : 0)
          })).accessibilityIdentifier("inject-incoming-error-4")
        } header: { Text("Fault injection") } footer: {
          Text("Not produced by iOS 27.0 for LCK")
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
                  let timing = InspectorLCKProjection.actionTiming(for: action, nowMS: context.date.timeIntervalSince1970 * 1000)
                  if let effective = timing.effective {
                    ProgressView(String(format: "Effective expiry in %.1f s", effective.remainingSeconds), value: effective.progress).tint(.orange)
                  }
                  if let reported = timing.reported {
                    ProgressView(String(format: "Reported timeoutDate in %.1f s", reported.remainingSeconds), value: reported.progress)
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
private struct CallButtons: View {
  @ObservedObject var model: InspectorModel
  let call: RuntimeEvent
  var expand: (() -> Void)? = nil
  var compact = false
  var body: some View {
    VStack(spacing: 12) {
      if call.ended != true {
        if call.apiKind == "livecommunicationkit" {
          LCKCallButtons(model: model, call: call)
        } else if call.ringing == true {
          if let active = model.active(excluding: call.uuid) {
            if compact {
              Button(action: { expand?() }) { Text("Call waiting options").frame(minHeight: 44) }.buttonStyle(.borderedProminent)
            } else {
              action("Hold & Accept", symbol: "pause.fill", operation: "hold-and-accept", tint: .green, active: active)
              action("End & Accept", symbol: "phone.down.fill", operation: "end-and-accept", tint: .orange, active: active)
            }
            action("Decline", symbol: "phone.down.fill", operation: "decline", tint: .red)
          } else {
            HStack(spacing: 20) {
              action("Decline", symbol: "phone.down.fill", operation: "decline", tint: .red)
              action("Answer", symbol: "phone.fill", operation: "answer", tint: .green)
            }
          }
        } else {
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
    }.buttonStyle(.borderedProminent).tint(tint).accessibilityIdentifier(operation)
  }
}
private struct LCKCallButtons: View {
  @ObservedObject var model: InspectorModel
  let call: RuntimeEvent
  private var active: [RuntimeEvent] { model.targets(excluding: call, held: false) }
  private var held: [RuntimeEvent] { model.targets(excluding: call, held: true) }
  private func title(_ choice: String) -> String {
    ["answer": "Answer", "decline": "Decline", "hold-and-accept": "Hold & Accept",
     "end-and-accept": "End & Accept", "end-held-and-accept": "End Held & Accept"][choice] ?? choice
  }
  var body: some View {
    VStack(spacing: 12) {
      if call.ringing == true {
        Text("System choices").font(.caption).foregroundStyle(.secondary)
        if let choices = InspectorLCKProjection.systemChoices(for: call) {
          ForEach(choices, id: \.self) { choice in
            if choice == "end-held-and-accept" {
              Menu(title(choice)) {
                ForEach(active) { activeCall in
                  Menu("Active: \(activeCall.caller) · \(activeCall.uuid ?? "")") {
                    ForEach(held) { heldCall in
                      Button("End held: \(heldCall.caller) · \(heldCall.uuid ?? "")") {
                        model.send(choice, call: call, active: activeCall, heldCall: heldCall)
                      }.disabled(model.isPending(activeCall) || model.isPending(heldCall))
                    }
                  }
                }
              }.disabled(active.isEmpty || held.isEmpty).accessibilityIdentifier(choice)
            } else if choice == "hold-and-accept" || choice == "end-and-accept" {
              Menu(title(choice)) {
                ForEach(active) { target in
                  Button("Active: \(target.caller) · \(target.uuid ?? "")") { model.send(choice, call: call, active: target) }
                    .disabled(model.isPending(target))
                }
              }.disabled(active.isEmpty).accessibilityIdentifier(choice)
            } else {
              Button(title(choice)) { model.send(choice, call: call) }.accessibilityIdentifier(choice)
            }
          }
        } else { Text("System choices unavailable").font(.caption).foregroundStyle(.secondary) }
      }
      Text("App requests · capabilities do not gate these requests").font(.caption).foregroundStyle(.secondary)
      Button(call.held == true ? "App Resume" : "App Pause") { model.send("hold", call: call, held: call.held != true, applicationRequest: true) }
        .accessibilityIdentifier(call.held == true ? "resume" : "pause")
      Menu("App Merge with…") {
        ForEach(InspectorLCKProjection.appMergeTargets(for: call, calls: Array(model.conversations.values))) { target in
          Button("\(target.caller) · \(target.uuid ?? "")") { model.send("merge", call: call, groupTarget: target, applicationRequest: true) }
            .disabled(model.isPending(target))
        }
      }.disabled(model.targets(excluding: call).isEmpty).accessibilityIdentifier("app-merge")
      Button("App Unmerge") { model.send("unmerge", call: call, applicationRequest: true) }.accessibilityIdentifier("app-unmerge")
      Button("End Call", role: .destructive) { model.send("end", call: call) }.accessibilityIdentifier("end")
    }.buttonStyle(.bordered).controlSize(.large)
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
