import PhotosUI
import SwiftUI
import UIKit

struct ChatListView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        @Bindable var appModel = appModel

        NavigationStack(path: $appModel.chatNavigationPath) {
            Group {
                if appModel.sessions.isEmpty {
                    ContentUnavailableView {
                        Label("No conversations", systemImage: "bubble.left.and.bubble.right")
                    } actions: {
                        Button("New Conversation") { Task { await appModel.newChat() } }
                            .buttonStyle(.borderedProminent)
                    }
                } else {
                    List(appModel.sessions, id: \.key) { session in
                        Button {
                            appModel.chatNavigationPath.append(session.key)
                        } label: {
                            HStack(spacing: 10) {
                                ChatSessionRow(session: session)
                                Spacer(minLength: 4)
                                Image(systemName: "chevron.right")
                                    .font(.caption.bold())
                                    .foregroundStyle(ZiggyPalette.mutedInk)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                    .listStyle(.plain)
                    .refreshable { await appModel.loadSessions() }
                }
            }
            .background(ZiggyPalette.canvas)
            .navigationTitle("Ziggy")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    ZiggyConnectionStatus(state: connectionStatus, showsLabel: false)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { Task { await appModel.newChat() } } label: {
                        Image(systemName: "square.and.pencil")
                    }
                    .accessibilityLabel("New conversation")
                }
            }
            .navigationDestination(for: String.self) { sessionKey in
                ChatConversationView(
                    sessionKey: sessionKey,
                    title: appModel.sessions.first(where: { $0.key == sessionKey })?.title
                )
            }
        }
    }

    private var connectionStatus: ZiggyConnectionState {
        switch appModel.connectionState {
        case .connected: .connected
        case .connecting, .reconnecting: .connecting
        case .failed(let message): .failed(message)
        case .idle, .stopped: .offline
        }
    }
}

private struct ChatSessionRow: View {
    let session: SessionSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(session.title?.nilIfEmpty ?? session.preview?.nilIfEmpty ?? "Conversation")
                    .font(.headline)
                    .foregroundStyle(ZiggyPalette.ink)
                    .lineLimit(1)
                Spacer(minLength: 8)
                if let date = session.updatedAt?.date ?? session.createdAt?.date {
                    Text(date, format: .relative(presentation: .named))
                        .font(.caption)
                        .foregroundStyle(ZiggyPalette.mutedInk)
                }
            }
            if let preview = session.preview?.nilIfEmpty ?? session.lastMessage?.nilIfEmpty {
                Text(preview)
                    .font(.subheadline)
                    .foregroundStyle(ZiggyPalette.mutedInk)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 7)
    }
}

struct ChatConversationView: View {
    @Environment(AppModel.self) private var appModel
    let sessionKey: String
    let title: String?

    @State private var composerText = ""
    @State private var attachments: [ZiggyImageAttachment] = []
    @State private var photoItems: [PhotosPickerItem] = []
    @State private var sendsAsBackgroundWork = false
    @State private var speechService = AppleSpeechDictationService()
    @State private var dictationTask: Task<Void, Never>?
    @State private var isDictating = false
    @State private var dictationPrefix = ""

    private var chatID: String {
        sessionKey.hasPrefix("websocket:")
            ? String(sessionKey.dropFirst("websocket:".count))
            : sessionKey
    }

    private var messages: [ChatItem] {
        appModel.messagesByChatID[chatID] ?? []
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 12) {
                    if messages.isEmpty {
                        ContentUnavailableView(
                            "Ready when you are",
                            systemImage: "sparkles",
                            description: Text("Send Ziggy a message.")
                        )
                        .padding(.top, 90)
                    } else {
                        ForEach(messages) { message in
                            ZiggyMessageBubble(
                                kind: kind(for: message.role),
                                text: message.text,
                                author: author(for: message.role),
                                isStreaming: message.isStreaming
                            )
                            .id(message.id)
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 16)
            }
            .background(ZiggyPalette.canvas)
            .onChange(of: messages.count) { _, _ in scrollToBottom(proxy) }
            .onChange(of: messages.last?.text) { _, _ in scrollToBottom(proxy) }
        }
        .navigationTitle(title?.nilIfEmpty ?? "Ziggy")
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 5) {
                if isDictating {
                    Label("Listening", systemImage: "waveform")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(ZiggyPalette.coral)
                }
                ZiggyComposer(
                    text: $composerText,
                    attachments: $attachments,
                    photoItems: $photoItems,
                    isSending: appModel.isSending,
                    isBackgroundWork: sendsAsBackgroundWork,
                    onSend: send,
                    onToggleBackgroundWork: { sendsAsBackgroundWork.toggle() },
                    onMic: toggleDictation
                )
            }
            .padding(.horizontal, 10)
            .padding(.top, 8)
            .background(.bar)
        }
        .task {
            let session = appModel.sessions.first(where: { $0.key == sessionKey })
                ?? SessionSummary(key: sessionKey)
            await appModel.selectSession(session)
        }
        .onChange(of: photoItems) { _, items in
            guard !items.isEmpty else { return }
            Task { await loadPhotos(items) }
        }
        .onDisappear {
            dictationTask?.cancel()
            speechService.cancel()
        }
    }

    private func send() {
        let text = composerText
        let media = attachments.compactMap(\.outboundMedia)
        composerText = ""
        attachments = []
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        Task {
            await appModel.sendMessage(text, media: media, asBackgroundWork: sendsAsBackgroundWork)
            sendsAsBackgroundWork = false
        }
    }

    private func toggleDictation() {
        if isDictating {
            speechService.stop()
            dictationTask?.cancel()
            dictationTask = nil
            isDictating = false
            return
        }

        dictationPrefix = composerText
        isDictating = true
        dictationTask = Task {
            do {
                let stream = try await speechService.start()
                for try await event in stream {
                    let transcript: String
                    switch event {
                    case .partial(let value), .final(let value): transcript = value
                    }
                    composerText = [dictationPrefix, transcript]
                        .filter { !$0.isEmpty }
                        .joined(separator: dictationPrefix.isEmpty ? "" : " ")
                }
            } catch is CancellationError {
                return
            } catch {
                appModel.bannerMessage = error.localizedDescription
            }
            isDictating = false
            dictationTask = nil
        }
    }

    private func loadPhotos(_ items: [PhotosPickerItem]) async {
        for (index, item) in items.enumerated() {
            guard let source = try? await item.loadTransferable(type: Data.self),
                  let prepared = ImageAttachmentFactory.prepare(source) else { continue }
            attachments.append(
                ZiggyImageAttachment(
                    image: Image(uiImage: prepared.image),
                    title: "ziggy-photo-\(index + 1).jpg",
                    data: prepared.data,
                    mimeType: "image/jpeg"
                )
            )
        }
        photoItems = []
    }

    private func scrollToBottom(_ proxy: ScrollViewProxy) {
        guard let id = messages.last?.id else { return }
        withAnimation(.easeOut(duration: 0.18)) { proxy.scrollTo(id, anchor: .bottom) }
    }

    private func kind(for role: MessageRole) -> ZiggyMessageKind {
        switch role {
        case .user: .user
        case .progress, .tool: .progress
        default: .assistant
        }
    }

    private func author(for role: MessageRole) -> String {
        role == .user ? "You" : "Ziggy"
    }
}

private enum ImageAttachmentFactory {
    struct PreparedImage {
        let image: UIImage
        let data: Data
    }

    static func prepare(_ data: Data) -> PreparedImage? {
        guard let image = UIImage(data: data) else { return nil }
        let maxDimension: CGFloat = 1_600
        let currentMax = max(image.size.width, image.size.height)
        let scale = min(1, maxDimension / max(currentMax, 1))
        let size = CGSize(width: image.size.width * scale, height: image.size.height * scale)
        let renderer = UIGraphicsImageRenderer(size: size)
        let rendered = renderer.image { _ in image.draw(in: CGRect(origin: .zero, size: size)) }
        guard let compressed = rendered.jpegData(compressionQuality: 0.82) else { return nil }
        return PreparedImage(image: rendered, data: compressed)
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
