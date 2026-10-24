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
          if model.conversations.isEmpty {
            Text("Waiting for the app to report a call.").foregroundStyle(.secondary)
          }
          ForEach(model.conversations.values.sorted { $0.sequence > $1.sequence }) { call in
            Button { expanded = call.uuid } label: {
              HStack {
                VStack(alignment: .leading, spacing: 4) {
                  Text(call.caller).foregroundStyle(.primary)
                  Text(call.uuid ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                Text(call.status).font(.subheadline).foregroundStyle(.secondary)
              }.frame(minHeight: 44).contentShape(Rectangle())
            }.buttonStyle(.plain)
          }
        }
        Section("Audio") {
          Label(model.audioActive ? "Audio active" : "Audio inactive", systemImage: model.audioActive ? "waveform" : "speaker.slash")
            .accessibilityIdentifier("audio-state")
        }
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
private struct CallButtons: View {
  @ObservedObject var model: InspectorModel
  let call: RuntimeEvent
  var expand: (() -> Void)? = nil
  var compact = false
  var body: some View {
    VStack(spacing: 12) {
      if call.ended != true {
        if call.ringing == true {
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
