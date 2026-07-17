import SwiftUI

enum ZiggyWorkStatus: String, CaseIterable, Sendable {
    case queued, running, waiting, completed, failed, cancelled

    var title: String { rawValue.capitalized }
    var color: Color {
        switch self { case .queued: ZiggyPalette.mutedInk; case .running: ZiggyPalette.teal; case .waiting: ZiggyPalette.amber; case .completed: ZiggyPalette.moss; case .failed: ZiggyPalette.coral; case .cancelled: ZiggyPalette.mutedInk }
    }
    var symbol: String {
        switch self { case .queued: "clock"; case .running: "bolt.horizontal.circle"; case .waiting: "pause.circle"; case .completed: "checkmark.circle"; case .failed: "exclamationmark.circle"; case .cancelled: "xmark.circle" }
    }
}

struct ZiggyWorkStatusBadge: View {
    let status: ZiggyWorkStatus

    var body: some View {
        Label(status.title, systemImage: status.symbol)
            .font(.caption.weight(.semibold)).foregroundStyle(status.color)
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(status.color.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
            .accessibilityLabel("Work status: \(status.title)")
    }
}

struct ZiggyWorkTask: Identifiable, Sendable {
    let id: String
    let title: String
    let summary: String
    let status: ZiggyWorkStatus
    let updatedAt: String
    let progress: Double?
}

struct ZiggyWorkTaskRow: View {
    let task: ZiggyWorkTask
    var onSelect: () -> Void = {}

    var body: some View {
        Button(action: onSelect) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: task.status.symbol).font(.title3).foregroundStyle(task.status.color).frame(width: 24)
                VStack(alignment: .leading, spacing: 5) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(task.title).font(.headline).foregroundStyle(ZiggyPalette.ink).lineLimit(2)
                        Spacer(minLength: 8)
                        ZiggyWorkStatusBadge(status: task.status)
                    }
                    Text(task.summary).font(.subheadline).foregroundStyle(ZiggyPalette.mutedInk).lineLimit(2).multilineTextAlignment(.leading)
                    HStack {
                        Text(task.updatedAt).font(.caption).foregroundStyle(ZiggyPalette.mutedInk)
                        if let progress = task.progress {
                            ProgressView(value: progress).tint(task.status.color).frame(maxWidth: 100)
                        }
                    }
                }
                Image(systemName: "chevron.right").font(.caption.weight(.bold)).foregroundStyle(ZiggyPalette.mutedInk).padding(.top, 5)
            }
            .padding(.vertical, 12)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain).accessibilityLabel("Work task: \(task.title), \(task.status.title)")
    }
}

struct ZiggyWorkTimelineEvent: Identifiable, Sendable {
    let id: String
    let order: Int
    let title: String
    let detail: String
    let timestamp: String
    let isFailure: Bool

    init(id: String, order: Int, title: String, detail: String, timestamp: String, isFailure: Bool = false) {
        self.id = id; self.order = order; self.title = title; self.detail = detail; self.timestamp = timestamp; self.isFailure = isFailure
    }
}

struct ZiggyWorkTimelineRow: View {
    let event: ZiggyWorkTimelineEvent
    var isLast = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 0) {
                Text("\(event.order)").font(.caption.weight(.bold)).foregroundStyle(.white).frame(width: 24, height: 24).background(event.isFailure ? ZiggyPalette.coral : ZiggyPalette.teal, in: Circle())
                if !isLast { Rectangle().fill(ZiggyPalette.line).frame(width: 1).frame(minHeight: 34) }
            }
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(event.title).font(.subheadline.weight(.semibold)).foregroundStyle(event.isFailure ? ZiggyPalette.coral : ZiggyPalette.ink)
                    Spacer(minLength: 8)
                    Text(event.timestamp).font(.caption).foregroundStyle(ZiggyPalette.mutedInk)
                }
                Text(event.detail).font(.subheadline).foregroundStyle(ZiggyPalette.mutedInk).textSelection(.enabled)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Step \(event.order): \(event.title). \(event.detail). \(event.timestamp)")
    }
}

#Preview("Work states") {
    ScrollView {
        VStack(alignment: .leading, spacing: 16) {
            ZiggyWorkTaskRow(task: ZiggyWorkTask(id: "1", title: "Prepare weekly operations report", summary: "Collecting deployment health, task latency, and the latest failures across all services.", status: .running, updatedAt: "Updated just now", progress: 0.62))
            ZiggyWorkTaskRow(task: ZiggyWorkTask(id: "2", title: "Waiting for approval", summary: "The report is ready for review before it is sent to the team.", status: .waiting, updatedAt: "Updated 4 min ago", progress: nil))
            ZiggyWorkTaskRow(task: ZiggyWorkTask(id: "3", title: "Failed sync", summary: "The remote service did not respond before the deadline.", status: .failed, updatedAt: "Updated 12 min ago", progress: nil))
            Divider()
            ZiggyWorkTimelineRow(event: ZiggyWorkTimelineEvent(id: "a", order: 1, title: "Collected service health", detail: "All three services returned a healthy status.", timestamp: "09:41"))
            ZiggyWorkTimelineRow(event: ZiggyWorkTimelineEvent(id: "b", order: 2, title: "Waiting for approval", detail: "A human review is required before the final report can be delivered.", timestamp: "09:42"), isLast: true)
        }.padding().background(ZiggyPalette.canvas)
    }
}
