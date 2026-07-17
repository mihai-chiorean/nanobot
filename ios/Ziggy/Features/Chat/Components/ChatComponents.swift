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
        case .connected: ZiggyPalette.moss
        case .connecting, .waiting: ZiggyPalette.amber
        case .failed, .offline: ZiggyPalette.coral
        }
    }
}

struct ZiggyConnectionStatus: View {
    let state: ZiggyConnectionState
    var showsLabel = true

    var body: some View {
        Label {
            if showsLabel { Text(state.label) }
        } icon: {
            Circle().fill(state.color).frame(width: 8, height: 8)
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(ZiggyPalette.mutedInk)
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
    var author: String? = nil
    var isStreaming = false

    private var isUser: Bool { kind == .user }

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if isUser { Spacer(minLength: 44) }
            VStack(alignment: isUser ? .trailing : .leading, spacing: 6) {
                if let author {
                    Text(author)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(isUser ? ZiggyPalette.teal : ZiggyPalette.mutedInk)
                }
                HStack(alignment: .bottom, spacing: 8) {
                    ZiggyMarkdownText(markdown: text)
                    if isStreaming { ZiggyStreamingIndicator() }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .background(isUser ? ZiggyPalette.teal.opacity(0.13) : ZiggyPalette.panel, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(isUser ? ZiggyPalette.teal.opacity(0.18) : ZiggyPalette.line, lineWidth: 1))
            if !isUser { Spacer(minLength: 20) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(kind.rawValue.capitalized) message")
        .accessibilityIdentifier("\(kind.rawValue)-message")
        .accessibilityValue(text)
    }
}

struct ZiggyTraceGroup: Identifiable, Sendable {
    let id: UUID
    let title: String
    let detail: String
    var isFailure = false

    init(id: UUID = UUID(), title: String, detail: String, isFailure: Bool = false) {
        self.id = id; self.title = title; self.detail = detail; self.isFailure = isFailure
    }
}

struct ZiggyTraceDisclosure: View {
    let title: String
    let traces: [ZiggyTraceGroup]
    var initiallyExpanded = false
    @State private var isExpanded: Bool

    init(title: String = "Activity", traces: [ZiggyTraceGroup], initiallyExpanded: Bool = false) {
        self.title = title; self.traces = traces; self.initiallyExpanded = initiallyExpanded
        _isExpanded = State(initialValue: initiallyExpanded)
    }

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(traces) { trace in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(trace.title).font(.subheadline.weight(.medium))
                        Text(trace.detail).font(.caption).foregroundStyle(ZiggyPalette.mutedInk).textSelection(.enabled)
                    }
                    .foregroundStyle(trace.isFailure ? ZiggyPalette.coral : ZiggyPalette.ink)
                }
            }
            .padding(.top, 8)
        } label: {
            Label(title, systemImage: "list.bullet.rectangle")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(ZiggyPalette.mutedInk)
        }
        .tint(ZiggyPalette.teal)
        .padding(12)
        .background(ZiggyPalette.panel, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(ZiggyPalette.line, lineWidth: 1))
    }
}

struct ZiggyStreamingIndicator: View {
    var body: some View {
        ProgressView().controlSize(.small).tint(ZiggyPalette.teal)
            .accessibilityLabel("Assistant is responding")
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
                    attachment.image.resizable().scaledToFill()
                        .frame(width: 64, height: 64).clipShape(RoundedRectangle(cornerRadius: 6))
                        .overlay(alignment: .topTrailing) {
                            Button { attachments.removeAll { $0.id == attachment.id } } label: {
                                Image(systemName: "xmark.circle.fill").symbolRenderingMode(.palette).foregroundStyle(.white, .black.opacity(0.65))
                            }
                            .buttonStyle(.plain).padding(3)
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
    var onSend: () -> Void = {}
    var onToggleBackgroundWork: () -> Void = {}
    var onMic: () -> Void = {}

    var body: some View {
        VStack(spacing: 8) {
            if !attachments.isEmpty { ZiggyImageAttachmentStrip(attachments: $attachments) }
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Message Ziggy", text: $text, axis: .vertical)
                    .lineLimit(1...6).textFieldStyle(.plain).padding(.vertical, 10)
                    .accessibilityLabel("Message")
                Button(action: onToggleBackgroundWork) {
                    Image(systemName: isBackgroundWork ? "bolt.fill" : "bolt")
                }
                .buttonStyle(.borderless).foregroundStyle(isBackgroundWork ? ZiggyPalette.amber : ZiggyPalette.mutedInk)
                .accessibilityLabel(isBackgroundWork ? "Send as background work" : "Send as chat message")
                .accessibilityHint("Toggles background work mode")
                Button(action: onMic) { Image(systemName: "mic") }
                    .buttonStyle(.borderless).foregroundStyle(ZiggyPalette.mutedInk).accessibilityLabel("Dictate message")
                PhotosPicker(selection: $photoItems, maxSelectionCount: 6, matching: .images) {
                    Image(systemName: "photo")
                }
                .buttonStyle(.borderless).foregroundStyle(ZiggyPalette.mutedInk).accessibilityLabel("Attach photos")
                Button(action: onSend) {
                    if isSending { ProgressView().controlSize(.small) } else { Image(systemName: "arrow.up.circle.fill") }
                }
                .buttonStyle(.borderless).disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && attachments.isEmpty)
                .foregroundStyle(ZiggyPalette.teal).accessibilityLabel(isSending ? "Sending message" : "Send message")
            }
            .padding(.horizontal, 12).background(ZiggyPalette.panel, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(ZiggyPalette.line, lineWidth: 1))
        }
    }
}

#Preview("Chat states") {
    @Previewable @State var attachments: [ZiggyImageAttachment] = [
        ZiggyImageAttachment(image: Image(systemName: "photo"), title: "Status screenshot")
    ]
    @Previewable @State var text = ""
    @Previewable @State var photos: [PhotosPickerItem] = []
    ScrollView { VStack(alignment: .leading, spacing: 14) {
        ZiggyConnectionStatus(state: .connected)
        ZiggyConnectionStatus(state: .waiting("Waiting for server"))
        ZiggyMessageBubble(kind: .assistant, text: "**Ready.** I found the deployment notes and can summarize them.\n\n- Three services are healthy\n- One worker is waiting", author: "Ziggy")
        ZiggyMessageBubble(kind: .user, text: "Please keep the summary short.", author: "You")
        ZiggyMessageBubble(kind: .progress, text: "Checking the latest task status…", isStreaming: true)
        ZiggyTraceDisclosure(traces: [ZiggyTraceGroup(title: "Fetch status", detail: "Request timed out", isFailure: true)], initiallyExpanded: true)
        ZiggyComposer(text: $text, attachments: $attachments, photoItems: $photos)
    }.padding().background(ZiggyPalette.canvas) }
    .preferredColorScheme(.dark)
}
