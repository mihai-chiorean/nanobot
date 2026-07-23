import SwiftUI

struct WorkListView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        NavigationStack {
            Group {
                if appModel.workTasks.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "bolt.horizontal").foregroundStyle(ZiggyPalette.mutedForeground)
                        Text("No background work").font(.subheadline.weight(.medium))
                        Text("Tasks sent from a conversation appear here.")
                            .font(.caption).foregroundStyle(ZiggyPalette.mutedForeground)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ScrollView {
                        LazyVStack(spacing: 0) {
                            ForEach(appModel.workTasks, id: \.id) { task in
                                NavigationLink {
                                    WorkDetailView(taskID: task.id)
                                } label: {
                                    ZiggyWorkTaskRow(task: task.presentation)
                                }
                                Rectangle().fill(ZiggyPalette.border.opacity(0.6)).frame(height: 1)
                            }
                        }
                        .padding(.horizontal, 14)
                    }
                    .refreshable { await appModel.loadWork() }
                }
            }
            .background(ZiggyPalette.background)
            .navigationTitle("Work")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await appModel.loadWork() } } label: { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(PWAIconButton(size: 32))
                }
            }
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
                                .font(.headline)
                                .foregroundStyle(ZiggyPalette.foreground)
                            if let description = task.description, !description.isEmpty {
                                Text(description)
                                    .font(.subheadline)
                                    .foregroundStyle(ZiggyPalette.mutedForeground)
                            }
                        }
                        Spacer(minLength: 12)
                        ZiggyWorkStatusBadge(status: task.status.presentation)
                    }

                    Divider()

                    if events.isEmpty {
                        ProgressView("Waiting for activity")
                            .foregroundStyle(ZiggyPalette.mutedForeground)
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
        .background(ZiggyPalette.background)
        .navigationTitle("Task")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            if let task {
                HStack(spacing: 10) {
                    TextField("Follow up", text: $followUp, axis: .vertical)
                        .lineLimit(1...4)
                        .textFieldStyle(.plain)
                        .padding(.horizontal, 12)
                        .frame(minHeight: 42)
                        .background(ZiggyPalette.card)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(ZiggyPalette.border))
                    Button {
                        let value = followUp
                        followUp = ""
                        Task { await appModel.sendFollowUp(value, to: task) }
                    } label: {
                        Image(systemName: "arrow.up")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(ZiggyPalette.primaryForeground)
                            .frame(width: 34, height: 34)
                            .background(ZiggyPalette.primary, in: Circle())
                    }
                    .disabled(followUp.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("Send follow-up")
                    if [.queued, .running, .waiting].contains(task.status) {
                        Button(role: .destructive) {
                            Task { await appModel.cancel(task: task) }
                        } label: {
                            Image(systemName: "stop.circle")
                                .font(.title3)
                        }
                        .accessibilityLabel("Cancel task")
                    }
                }
                .padding(10)
                .background(ZiggyPalette.background)
            }
        }
        .task(id: taskID) {
            if let task { await appModel.subscribe(to: task) }
        }
        .onDisappear {
            appModel.stopWorkStream(taskID: taskID)
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

extension ZiggyStatus {
    var presentation: ZiggyWorkStatus {
        switch self {
        case .scheduled: .scheduled
        case .queued: .queued
        case .running: .running
        case .waiting: .waiting
        case .succeeded: .succeeded
        case .failed: .failed
        case .cancelled: .cancelled
        case .interrupted: .interrupted
        case .unknown: .unknown
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
