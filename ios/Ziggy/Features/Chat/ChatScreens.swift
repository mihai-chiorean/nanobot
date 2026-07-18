import PhotosUI
import SwiftUI
import UIKit

struct ChatListView: View {
    @Environment(AppModel.self) private var appModel
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        @Bindable var appModel = appModel

        NavigationStack(path: $appModel.chatNavigationPath) {
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Image("ZiggyAvatar")
                        .resizable()
                        .scaledToFit()
                        .frame(width: 27, height: 27)
                        .clipShape(RoundedRectangle(cornerRadius: 6))
                    Text("Ziggy")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(ZiggyPalette.foreground)
                    ZiggyConnectionStatus(state: connectionStatus, showsLabel: false)
                    Spacer()
                    Button {
                        appModel.toggleTheme(currentScheme: colorScheme)
                    } label: {
                        Image(systemName: colorScheme == .dark ? "sun.max" : "moon")
                    }
                    .buttonStyle(PWAIconButton(size: 34))
                    .accessibilityLabel("Toggle theme")
                    Button { Task { await appModel.newChat() } } label: {
                        Image(systemName: "square.and.pencil")
                    }
                    .buttonStyle(PWAIconButton(size: 34))
                    .accessibilityLabel("New conversation")
                }
                .padding(.horizontal, 14)
                .padding(.top, 10)
                .padding(.bottom, 8)

                Button {
                    Task { await appModel.newChat() }
                } label: {
                    Label("New chat", systemImage: "square.and.pencil")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(ZiggyPalette.foreground.opacity(0.9))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 13)
                        .frame(height: 42)
                        .background(ZiggyPalette.accent.opacity(0.5))
                        .clipShape(RoundedRectangle(cornerRadius: 7))
                }
                .buttonStyle(.plain)
                .padding(.horizontal, 10)
                .padding(.bottom, 8)

                HStack {
                    ZiggySectionLabel(title: "Recent")
                    Spacer()
                    Button { Task { await appModel.loadSessions() } } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(PWAIconButton(size: 28))
                    .accessibilityLabel("Refresh conversations")
                }
                .padding(.leading, 14)
                .padding(.trailing, 10)
                .padding(.top, 5)
                .padding(.bottom, 3)

                ScrollView {
                    LazyVStack(spacing: 2) {
                        if appModel.sessions.isEmpty {
                            Text("No conversations yet")
                                .font(.caption)
                                .foregroundStyle(ZiggyPalette.mutedForeground)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 20)
                        } else {
                            ForEach(appModel.sessions, id: \.key) { session in
                                Button {
                                    appModel.chatNavigationPath.append(session.key)
                                } label: {
                                    ChatSessionRow(session: session)
                                }
                                .buttonStyle(.plain)
                                .accessibilityIdentifier("chat-session")
                            }
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.bottom, 12)
                }
                .refreshable { await appModel.loadSessions() }
            }
            .background(ZiggyPalette.sidebar)
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(for: String.self) { sessionKey in
                ChatConversationView(
                    sessionKey: sessionKey,
                    title: appModel.sessions.first(where: { $0.key == sessionKey }).map(sessionTitle)
                )
            }
        }
    }

    private func sessionTitle(_ session: SessionSummary) -> String {
        session.title?.nilIfEmpty ?? session.preview?.nilIfEmpty ?? "Conversation"
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
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(session.title?.nilIfEmpty ?? session.preview?.nilIfEmpty ?? "Conversation")
                    .font(.system(size: 13.5, weight: .medium))
                    .foregroundStyle(ZiggyPalette.foreground.opacity(0.9))
                    .lineLimit(1)
                Text(timestamp)
                    .font(.system(size: 11))
                    .foregroundStyle(ZiggyPalette.mutedForeground.opacity(0.85))
            }
            Spacer(minLength: 4)
            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(ZiggyPalette.mutedForeground.opacity(0.65))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .contentShape(Rectangle())
        .background(ZiggyPalette.accent.opacity(0.001))
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }

    private var timestamp: String {
        guard let date = session.updatedAt?.date ?? session.createdAt?.date else { return "-" }
        return date.formatted(.relative(presentation: .named))
    }
}

struct ChatConversationView: View {
    @Environment(AppModel.self) private var appModel
    @Environment(\.dismiss) private var dismiss
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
        VStack(spacing: 0) {
            conversationHeader

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 20) {
                        if messages.isEmpty {
                            VStack(alignment: .leading, spacing: 9) {
                                HStack(spacing: 7) {
                                    Image("ZiggyAvatar")
                                        .resizable()
                                        .frame(width: 18, height: 18)
                                        .clipShape(RoundedRectangle(cornerRadius: 4))
                                    Text("Ziggy")
                                        .font(.caption.weight(.medium))
                                        .foregroundStyle(ZiggyPalette.foreground.opacity(0.82))
                                }
                                Text("Ask about your workspace, start a task, or continue where you left off.")
                                    .font(.system(size: 14))
                                    .foregroundStyle(ZiggyPalette.mutedForeground)
                                    .lineSpacing(4)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 44)
                        } else {
                            ForEach(messages) { message in
                                ZiggyMessageBubble(
                                    kind: kind(for: message.role),
                                    text: message.text,
                                    isStreaming: message.isStreaming
                                )
                                .id(message.id)
                            }
                        }
                    }
                    .padding(.horizontal, 18)
                    .padding(.top, 14)
                    .padding(.bottom, 28)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: messages.count) { _, _ in scrollToBottom(proxy) }
                .onChange(of: messages.last?.text) { _, _ in scrollToBottom(proxy) }
            }

            VStack(spacing: 5) {
                if isDictating {
                    Label("Listening", systemImage: "waveform")
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(ZiggyPalette.destructive)
                }
                ZiggyComposer(
                    text: $composerText,
                    attachments: $attachments,
                    photoItems: $photoItems,
                    isSending: appModel.isSending,
                    isBackgroundWork: sendsAsBackgroundWork,
                    modelLabel: appModel.modelName,
                    onSend: send,
                    onToggleBackgroundWork: { sendsAsBackgroundWork.toggle() },
                    onMic: toggleDictation
                )
            }
            .padding(.horizontal, 14)
            .padding(.top, 7)
            .padding(.bottom, 8)
            .background(ZiggyPalette.background)
        }
        .background(ZiggyPalette.background)
        .toolbar(.hidden, for: .navigationBar)
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

    private var conversationHeader: some View {
        HStack(spacing: 7) {
            Button { dismiss() } label: {
                Image(systemName: "chevron.left")
            }
            .buttonStyle(PWAIconButton(size: 34))
            .accessibilityLabel("Back to chats")

            Image("ZiggyAvatar")
                .resizable()
                .frame(width: 18, height: 18)
                .clipShape(RoundedRectangle(cornerRadius: 4))
            Text(title?.nilIfEmpty ?? "Ziggy")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(ZiggyPalette.mutedForeground)
                .lineLimit(1)
            Spacer()
            ZiggyConnectionStatus(state: connectionStatus, showsLabel: false)
        }
        .padding(.horizontal, 10)
        .frame(height: 46)
    }

    private var connectionStatus: ZiggyConnectionState {
        switch appModel.connectionState {
        case .connected: .connected
        case .connecting, .reconnecting: .connecting
        case .failed(let message): .failed(message)
        case .idle, .stopped: .offline
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
