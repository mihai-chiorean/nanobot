import PhotosUI
import SwiftUI

enum ZiggyConnectionState: Equatable, Sendable {
    case connected
    case connecting
    case waiting(String)
    case failed(String)
    case offline

    var label: String {
        switch self {
        case .connected: "Connected"
        case .connecting: "Connecting"
        case .waiting(let message): message
        case .failed(let message): message
        case .offline: "Offline"
        }
    }

    var color: Color {
        switch self {
        case .connected: ZiggyPalette.emerald
        case .connecting, .waiting: ZiggyPalette.amber
        case .failed, .offline: ZiggyPalette.destructive
        }
    }
}

struct ZiggyConnectionStatus: View {
    let state: ZiggyConnectionState
    var showsLabel = true

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(state.color)
                .frame(width: 6, height: 6)
            if showsLabel { Text(state.label) }
        }
        .font(.caption2.weight(.medium))
        .foregroundStyle(state.color)
        .padding(.horizontal, showsLabel ? 8 : 0)
        .padding(.vertical, showsLabel ? 5 : 0)
        .background(showsLabel ? state.color.opacity(0.08) : .clear)
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .overlay {
            if showsLabel {
                RoundedRectangle(cornerRadius: 6).stroke(ZiggyPalette.border.opacity(0.65))
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Connection status: \(state.label)")
    }
}

enum ZiggyMessageKind: String, Sendable {
    case assistant
    case user
    case progress
}

struct ZiggyMessageBubble: View {
    let kind: ZiggyMessageKind
    let text: String
    var blocks: [RichBlock]? = nil
    var author: String? = nil
    var isStreaming = false

    var body: some View {
        Group {
            switch kind {
            case .user:
                HStack {
                    Spacer(minLength: 52)
                    Text(text)
                        .font(.system(size: 17))
                        .foregroundStyle(ZiggyPalette.foreground)
                        .lineSpacing(4)
                        .textSelection(.enabled)
                        .padding(.horizontal, 15)
                        .padding(.vertical, 9)
                        .background(ZiggyPalette.secondary.opacity(0.9))
                        .clipShape(RoundedRectangle(cornerRadius: 18))
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("User message")
                .accessibilityIdentifier("user-message")
                .accessibilityValue(text)
            case .assistant:
                HStack(alignment: .lastTextBaseline, spacing: 5) {
                    RichContentView(blocks: blocks ?? [.markdown(MarkdownBlock(text: text))])
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if isStreaming { StreamCursor() }
                }
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Assistant message")
                .accessibilityIdentifier("assistant-message")
            case .progress:
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "wrench.and.screwdriver")
                        .font(.caption2)
                        .padding(.top, 3)
                    Text(text)
                        .font(.caption.monospaced())
                        .lineSpacing(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .foregroundStyle(ZiggyPalette.mutedForeground)
                .padding(.vertical, 3)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Progress message")
                .accessibilityIdentifier("progress-message")
                .accessibilityValue(text)
            }
        }
    }
}

private struct StreamCursor: View {
    @State private var visible = true

    var body: some View {
        RoundedRectangle(cornerRadius: 1)
            .fill(ZiggyPalette.foreground.opacity(0.7))
            .frame(width: 3, height: 17)
            .opacity(visible ? 1 : 0.25)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.65).repeatForever(autoreverses: true)) {
                    visible = false
                }
            }
            .accessibilityHidden(true)
    }
}

struct ZiggyTraceGroup: Identifiable, Sendable {
    let id: UUID
    let title: String
    let detail: String
    var isFailure = false

    init(id: UUID = UUID(), title: String, detail: String, isFailure: Bool = false) {
        self.id = id
        self.title = title
        self.detail = detail
        self.isFailure = isFailure
    }
}

struct ZiggyTraceDisclosure: View {
    let title: String
    let traces: [ZiggyTraceGroup]
    @State private var isExpanded: Bool

    init(title: String = "Tools", traces: [ZiggyTraceGroup], initiallyExpanded: Bool = true) {
        self.title = title
        self.traces = traces
        _isExpanded = State(initialValue: initiallyExpanded)
    }

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(traces) { trace in
                    Text(trace.detail)
                        .font(.caption.monospaced())
                        .foregroundStyle(trace.isFailure ? ZiggyPalette.destructive : ZiggyPalette.mutedForeground)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(.leading, 12)
            .padding(.top, 6)
            .overlay(alignment: .leading) {
                Rectangle().fill(ZiggyPalette.border).frame(width: 1)
            }
        } label: {
            Label(title, systemImage: "wrench.and.screwdriver")
                .font(.caption.weight(.medium))
                .foregroundStyle(ZiggyPalette.mutedForeground)
        }
        .tint(ZiggyPalette.mutedForeground)
        .padding(.vertical, 4)
    }
}

struct ZiggyImageAttachment: Identifiable {
    let id: UUID
    let image: Image
    let title: String
    let data: Data?
    let mimeType: String

    init(id: UUID = UUID(), image: Image, title: String = "Attached image",
         data: Data? = nil, mimeType: String = "image/jpeg") {
        self.id = id
        self.image = image
        self.title = title
        self.data = data
        self.mimeType = mimeType
    }

    var outboundMedia: OutboundMedia? {
        guard let data else { return nil }
        return OutboundMedia(
            dataURL: "data:\(mimeType);base64,\(data.base64EncodedString())",
            name: title
        )
    }
}

struct ZiggyImageAttachmentStrip: View {
    @Binding var attachments: [ZiggyImageAttachment]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(attachments) { attachment in
                    attachment.image
                        .resizable()
                        .scaledToFill()
                        .frame(width: 64, height: 64)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(ZiggyPalette.border))
                        .overlay(alignment: .topTrailing) {
                            Button {
                                attachments.removeAll { $0.id == attachment.id }
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .symbolRenderingMode(.palette)
                                    .foregroundStyle(.white, .black.opacity(0.68))
                            }
                            .buttonStyle(.plain)
                            .padding(3)
                            .accessibilityLabel("Remove \(attachment.title)")
                        }
                        .accessibilityLabel(attachment.title)
                }
            }
        }
        .frame(height: 68)
    }
}

struct ZiggyComposer: View {
    @Binding var text: String
    @Binding var attachments: [ZiggyImageAttachment]
    @Binding var photoItems: [PhotosPickerItem]
    var isSending = false
    var isBackgroundWork = false
    var modelLabel: String? = nil
    var onSend: () -> Void = {}
    var onToggleBackgroundWork: () -> Void = {}
    var onMic: () -> Void = {}

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attachments.isEmpty
    }

    var body: some View {
        VStack(spacing: 0) {
            if !attachments.isEmpty {
                ZiggyImageAttachmentStrip(attachments: $attachments)
                    .padding(.horizontal, 12)
                    .padding(.top, 10)
            }

            TextField("Type your message...", text: $text, axis: .vertical)
                .lineLimit(1...7)
                .textFieldStyle(.plain)
                .font(.body)
                .padding(.horizontal, 14)
                .padding(.top, attachments.isEmpty ? 13 : 9)
                .padding(.bottom, 8)
                .accessibilityLabel("Message")

            HStack(spacing: 4) {
                PhotosPicker(selection: $photoItems, maxSelectionCount: 6, matching: .images) {
                    Image(systemName: "paperclip")
                }
                .buttonStyle(PWAIconButton(size: 32))
                .accessibilityLabel("Attach photos")

                Button(action: onMic) { Image(systemName: "mic") }
                    .buttonStyle(PWAIconButton(size: 32))
                    .accessibilityLabel("Dictate message")

                if let modelLabel, !modelLabel.isEmpty {
                    HStack(spacing: 6) {
                        Circle().fill(ZiggyPalette.emerald).frame(width: 6, height: 6)
                        Text(modelLabel.split(separator: "/").last.map(String.init) ?? modelLabel)
                            .lineLimit(1)
                    }
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(ZiggyPalette.foreground.opacity(0.8))
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(ZiggyPalette.foreground.opacity(0.035))
                    .clipShape(Capsule())
                    .overlay(Capsule().stroke(ZiggyPalette.foreground.opacity(0.1)))
                }

                Spacer(minLength: 4)

                Button(action: onToggleBackgroundWork) {
                    Image(systemName: isBackgroundWork ? "bolt.fill" : "bolt")
                }
                .buttonStyle(PWAIconButton(
                    size: 32,
                    foreground: isBackgroundWork ? ZiggyPalette.amber : ZiggyPalette.mutedForeground
                ))
                .accessibilityLabel(isBackgroundWork ? "Send as background work" : "Send as chat message")

                Button(action: onSend) {
                    Group {
                        if isSending {
                            ProgressView().controlSize(.small)
                        } else {
                            Image(systemName: "arrow.up")
                                .font(.system(size: 14, weight: .semibold))
                        }
                    }
                    .frame(width: 32, height: 32)
                    .foregroundStyle(canSend ? ZiggyPalette.primaryForeground : ZiggyPalette.mutedForeground)
                    .background(canSend ? ZiggyPalette.primary : ZiggyPalette.secondary)
                    .clipShape(Circle())
                    .overlay(Circle().stroke(ZiggyPalette.border.opacity(0.75)))
                }
                .buttonStyle(.plain)
                .disabled(!canSend || isSending)
                .accessibilityLabel(isSending ? "Sending message" : "Send message")
            }
            .padding(.horizontal, 9)
            .padding(.bottom, 9)
        }
        .background(ZiggyPalette.card)
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(ZiggyPalette.border.opacity(0.75)))
    }
}

#Preview("PWA chat") {
    @Previewable @State var attachments: [ZiggyImageAttachment] = []
    @Previewable @State var text = ""
    @Previewable @State var photos: [PhotosPickerItem] = []
    ScrollView {
        VStack(spacing: 20) {
            ZiggyMessageBubble(kind: .user, text: "Please keep the summary short.")
            ZiggyMessageBubble(kind: .assistant, text: "**Ready.** I found the deployment notes and can summarize them.")
            ZiggyMessageBubble(kind: .progress, text: "Checking deployment status")
            ZiggyComposer(text: $text, attachments: $attachments, photoItems: $photos, modelLabel: "qwen3.6-35b")
        }
        .padding()
    }
    .background(ZiggyPalette.background)
    .preferredColorScheme(.dark)
}
