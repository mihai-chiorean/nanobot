import SwiftUI

struct WorkListView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        NavigationStack {
            Group {
                if appModel.workTasks.isEmpty {
                    ContentUnavailableView(
                        "No background work",
                        systemImage: "bolt.horizontal.circle",
                        description: Text("Tasks sent from a conversation appear here.")
                    )
                } else {
                    List(appModel.workTasks, id: \.id) { task in
                        NavigationLink {
                            WorkDetailView(taskID: task.id)
                        } label: {
                            ZiggyWorkTaskRow(task: task.presentation)
                        }
                    }
                    .listStyle(.plain)
                    .refreshable { await appModel.loadWork() }
                }
            }
            .background(ZiggyPalette.canvas)
            .navigationTitle("Work")
        }
    }
}

private struct WorkDetailView: View {
    @Environment(AppModel.self) private var appModel
    let taskID: String
    @State private var followUp = ""

    private var task: WorkTask? {
        appModel.workTasks.first { $0.id == taskID }
    }

    private var events: [WorkEvent] {
        appModel.workEventsByTaskID[taskID] ?? []
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                if let task {
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 7) {
                            Text(task.title ?? "Background task")
                                .font(.title2.bold())
                                .foregroundStyle(ZiggyPalette.ink)
                            if let description = task.description, !description.isEmpty {
                                Text(description)
                                    .foregroundStyle(ZiggyPalette.mutedInk)
                            }
                        }
                        Spacer(minLength: 12)
                        ZiggyWorkStatusBadge(status: task.status.presentation)
                    }

                    Divider()

                    if events.isEmpty {
                        ProgressView("Waiting for activity")
                            .foregroundStyle(ZiggyPalette.mutedInk)
                            .frame(maxWidth: .infinity, alignment: .center)
                            .padding(.vertical, 36)
                    } else {
                        VStack(spacing: 0) {
                            ForEach(Array(events.enumerated()), id: \.offset) { index, event in
                                ZiggyWorkTimelineRow(
                                    event: event.presentation(order: index + 1),
                                    isLast: index == events.indices.last
                                )
                            }
                        }
                    }
                } else {
                    ContentUnavailableView("Task unavailable", systemImage: "exclamationmark.circle")
                }
            }
            .padding(16)
        }
        .background(ZiggyPalette.canvas)
        .navigationTitle("Task")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            if let task {
                HStack(spacing: 10) {
                    TextField("Follow up", text: $followUp, axis: .vertical)
                        .lineLimit(1...4)
                        .textFieldStyle(.roundedBorder)
                    Button {
                        let value = followUp
                        followUp = ""
                        Task { await appModel.sendFollowUp(value, to: task) }
                    } label: {
                        Image(systemName: "arrow.up.circle.fill")
                            .font(.title2)
                    }
                    .disabled(followUp.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("Send follow-up")
                    if [.queued, .running, .waiting].contains(task.status) {
                        Button(role: .destructive) {
                            Task { await appModel.cancel(task: task) }
                        } label: {
                            Image(systemName: "stop.circle")
                                .font(.title2)
                        }
                        .accessibilityLabel("Cancel task")
                    }
                }
                .padding(10)
                .background(.bar)
            }
        }
        .task {
            if let task { await appModel.subscribe(to: task) }
        }
    }
}

private extension WorkTask {
    var presentation: ZiggyWorkTask {
        let date = updatedAt?.date ?? createdAt?.date
        return ZiggyWorkTask(
            id: id,
            title: title ?? "Background task",
            summary: description ?? "",
            status: status.presentation,
            updatedAt: date.map { $0.formatted(.relative(presentation: .named)) } ?? "",
            progress: progress.map { $0 > 1 ? $0 / 100 : $0 }
        )
    }
}

private extension ZiggyStatus {
    var presentation: ZiggyWorkStatus {
        switch self {
        case .queued: .queued
        case .running: .running
        case .waiting: .waiting
        case .completed: .completed
        case .failed: .failed
        case .cancelled: .cancelled
        case .unknown: .waiting
        }
    }
}

private extension WorkEvent {
    func presentation(order: Int) -> ZiggyWorkTimelineEvent {
        ZiggyWorkTimelineEvent(
            id: id ?? "\(taskID ?? "task")-\(sequence ?? order)",
            order: order,
            title: type.replacingOccurrences(of: "_", with: " ").capitalized,
            detail: message ?? actor.map { "Actor: \($0)" } ?? "Update received",
            timestamp: createdAt?.date.formatted(date: .omitted, time: .shortened) ?? "",
            isFailure: type.lowercased().contains("fail")
        )
    }
}
