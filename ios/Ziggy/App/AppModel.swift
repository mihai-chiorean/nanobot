import Foundation
import Observation

struct ZiggyIdentity: Sendable {
    let name: String
    let email: String

    static let owner = ZiggyIdentity(
        name: "Mihai Chiorean",
        email: "mihai.v.chiorean@gmail.com"
    )
}

struct ChatItem: Identifiable, Hashable, Sendable {
    let id: String
    let chatID: String
    let role: MessageRole
    var text: String
    var isStreaming: Bool

    init(id: String = UUID().uuidString, chatID: String, role: MessageRole,
         text: String, isStreaming: Bool = false) {
        self.id = id
        self.chatID = chatID
        self.role = role
        self.text = text
        self.isStreaming = isStreaming
    }
}

@MainActor
@Observable
final class AppModel {
    enum Tab: Hashable {
        case chats
        case work
        case settings
    }

    enum Phase: Equatable {
        case launching
        case needsEnrollment
        case connecting
        case ready
        case failed(String)
    }

    let identity = ZiggyIdentity.owner
    var selectedTab: Tab = .chats
    var chatNavigationPath: [String] = []
    var phase: Phase = .launching
    var serverURLText = "https://chat.mihaichiorean.com"
    var accessCode = ""
    var connectionState: ZiggySocketConnectionState = .idle
    var modelName = "Ziggy"
    var sessions: [SessionSummary] = []
    var selectedSessionKey: String?
    var messagesByChatID: [String: [ChatItem]] = [:]
    var workTasks: [WorkTask] = []
    var workEventsByTaskID: [String: [WorkEvent]] = [:]
    var isRefreshing = false
    var isSending = false
    var bannerMessage: String?

    var isConfigured: Bool {
        if case .ready = phase { return true }
        if case .connecting = phase { return true }
        return false
    }

    var selectedChatID: String? {
        selectedSessionKey.map(Self.chatID(from:))
    }

    var connectionLabel: String {
        switch connectionState {
        case .idle: "Idle"
        case .connecting: "Connecting"
        case .connected: "Connected"
        case .reconnecting: "Reconnecting"
        case .stopped: "Stopped"
        case .failed(let message): message
        }
    }

    private let credentialStore: any CredentialStoring
    private var restClient: ZiggyRESTClient?
    private var restTokenExpiresAt: Date?
    private var socket: ZiggyWebSocketClient?
    private var socketEventTask: Task<Void, Never>?
    private var configuredServerURL: URL?
    private var configuredAccessCode: String?
    private var pendingNewChat = false
    private var hasStarted = false

    init(credentialStore: any CredentialStoring = KeychainCredentialStore()) {
        self.credentialStore = credentialStore
    }

    func start() async {
        guard !hasStarted else { return }
        hasStarted = true

        let environment = ProcessInfo.processInfo.environment
        if let server = environment["ZIGGY_SERVER_URL"], !server.isEmpty {
            serverURLText = server
        }
        if let code = environment["ZIGGY_GUEST_CODE"], !code.isEmpty {
            accessCode = code
            await connect(persist: false)
            return
        }

        do {
            if let savedURL = try await credentialStore.serverURL(),
               let savedCode = try await credentialStore.guestEnrollmentCode() {
                serverURLText = savedURL.absoluteString
                accessCode = savedCode
                await connect(persist: false)
            } else {
                phase = .needsEnrollment
            }
        } catch {
            phase = .failed(Self.message(for: error))
        }
    }

    func connect(persist: Bool = true) async {
        let trimmedCode = accessCode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let serverURL = URL(string: serverURLText),
              ["http", "https"].contains(serverURL.scheme?.lowercased() ?? ""),
              serverURL.host?.isEmpty == false else {
            phase = .failed("Enter a valid Ziggy server URL.")
            return
        }
        guard !trimmedCode.isEmpty else {
            phase = .failed("Enter the private access code.")
            return
        }

        phase = .connecting
        bannerMessage = nil
        await stopSocket()

        do {
            let bootstrapClient = ZiggyRESTClient(baseURL: serverURL)
            let bootstrap = try await bootstrapClient.bootstrapGuest(code: trimmedCode)
            configuredServerURL = serverURL
            configuredAccessCode = trimmedCode
            installRESTClient(from: bootstrap, serverURL: serverURL)
            modelName = bootstrap.model ?? "Ziggy"

            if persist {
                try await credentialStore.save(serverURL: serverURL)
                try await credentialStore.save(guestEnrollmentCode: trimmedCode)
            }

            let webSocketURL = Self.webSocketURL(baseURL: serverURL, path: bootstrap.webSocketPath)
            let socket = ZiggyWebSocketClient(baseURL: webSocketURL) {
                let fresh = try await bootstrapClient.bootstrapGuest(code: trimmedCode)
                return fresh.webSocketToken ?? fresh.restToken
            }
            self.socket = socket
            observe(socket)
            await socket.start()

            async let sessionsLoad: Void = loadSessions(showSpinner: false)
            async let workLoad: Void = loadWork(showSpinner: false)
            _ = await (sessionsLoad, workLoad)
        } catch {
            connectionState = .failed(Self.message(for: error))
            phase = .failed(Self.message(for: error))
        }
    }

    func retryConnection() async {
        await connect(persist: false)
    }

    func disconnectAndForget() async {
        await stopSocket()
        try? await credentialStore.removeServerURL()
        try? await credentialStore.removeGuestEnrollmentCode()
        restClient = nil
        configuredServerURL = nil
        configuredAccessCode = nil
        accessCode = ""
        sessions = []
        workTasks = []
        messagesByChatID = [:]
        selectedSessionKey = nil
        chatNavigationPath = []
        phase = .needsEnrollment
    }

    func refreshAll() async {
        isRefreshing = true
        async let sessionsLoad: Void = loadSessions(showSpinner: false)
        async let workLoad: Void = loadWork(showSpinner: false)
        _ = await (sessionsLoad, workLoad)
        isRefreshing = false
    }

    func loadSessions(showSpinner: Bool = true) async {
        if showSpinner { isRefreshing = true }
        defer { if showSpinner { isRefreshing = false } }
        do {
            let response = try await authenticatedRESTClient().fetchSessions()
            var refreshed = response.items.sorted { lhs, rhs in
                (lhs.updatedAt?.date ?? .distantPast) > (rhs.updatedAt?.date ?? .distantPast)
            }
            if let selectedSessionKey,
               !refreshed.contains(where: { $0.key == selectedSessionKey }),
               let active = sessions.first(where: { $0.key == selectedSessionKey }) {
                refreshed.insert(active, at: 0)
            }
            sessions = refreshed
        } catch {
            bannerMessage = Self.message(for: error)
        }
    }

    func selectSession(_ session: SessionSummary) async {
        selectedSessionKey = session.key
        let chatID = Self.chatID(from: session.key)
        await socket?.attach(chatID: chatID)
        await loadMessages(sessionKey: session.key)
    }

    func loadMessages(sessionKey: String) async {
        do {
            let response = try await authenticatedRESTClient().fetchMessages(sessionKey: sessionKey)
            let chatID = Self.chatID(from: sessionKey)
            messagesByChatID[chatID] = response.items.map { message in
                ChatItem(
                    id: message.id,
                    chatID: chatID,
                    role: message.role,
                    text: message.content.text ?? "",
                    isStreaming: false
                )
            }
        } catch ZiggyRESTError.http(let statusCode, _) where statusCode == 404 {
            messagesByChatID[Self.chatID(from: sessionKey)] = []
        } catch {
            bannerMessage = Self.message(for: error)
        }
    }

    func newChat() async {
        pendingNewChat = true
        await socket?.send(.newChat)
    }

    func sendMessage(_ content: String, media: [OutboundMedia] = [], asBackgroundWork: Bool = false) async {
        let trimmed = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let chatID = selectedChatID, !trimmed.isEmpty || !media.isEmpty else { return }
        isSending = true
        defer { isSending = false }

        messagesByChatID[chatID, default: []].append(
            ChatItem(chatID: chatID, role: .user, text: trimmed.isEmpty ? "Image attachment" : trimmed)
        )
        if asBackgroundWork {
            await socket?.send(.workCreate(chatID: chatID, content: trimmed, title: nil, media: media))
            selectedTab = .work
        } else {
            await socket?.send(.message(chatID: chatID, content: trimmed, media: media))
        }
    }

    func loadWork(showSpinner: Bool = true) async {
        if showSpinner { isRefreshing = true }
        defer { if showSpinner { isRefreshing = false } }
        do {
            let response = try await authenticatedRESTClient().fetchWork()
            workTasks = response.items.sorted { lhs, rhs in
                (lhs.updatedAt?.date ?? lhs.createdAt?.date ?? .distantPast)
                    > (rhs.updatedAt?.date ?? rhs.createdAt?.date ?? .distantPast)
            }
        } catch {
            bannerMessage = Self.message(for: error)
        }
    }

    func subscribe(to task: WorkTask) async {
        do {
            let response = try await authenticatedRESTClient().fetchWorkEvents(taskID: task.id)
            workEventsByTaskID[task.id] = response.items.sorted { ($0.sequence ?? 0) < ($1.sequence ?? 0) }
            let after = response.items.compactMap(\.sequence).max()
            await socket?.send(.workSubscribe(taskID: task.id, afterSequence: after))
        } catch {
            bannerMessage = Self.message(for: error)
        }
    }

    func cancel(task: WorkTask) async {
        await socket?.send(.workCancel(taskID: task.id))
    }

    func sendFollowUp(_ content: String, to task: WorkTask) async {
        let trimmed = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        await socket?.send(.workMessage(taskID: task.id, content: trimmed))
    }

    private func observe(_ socket: ZiggyWebSocketClient) {
        socketEventTask?.cancel()
        socketEventTask = Task { [weak self] in
            for await event in socket.events {
                guard !Task.isCancelled else { break }
                self?.handle(event)
            }
        }
    }

    private func handle(_ event: ZiggyWebSocketEvent) {
        switch event {
        case .state(let state):
            connectionState = state
            if state == .connected, phase == .connecting { phase = .ready }
            if case .failed(let message) = state { bannerMessage = message }
        case .outboundQueueFull:
            bannerMessage = "The outgoing queue is full. Wait for Ziggy to reconnect."
        case .decodingFailure(let message):
            bannerMessage = message
        case .inbound(let inbound):
            handle(inbound)
        }
    }

    private func handle(_ event: InboundWebSocketEvent) {
        switch event {
        case .ready(let info):
            phase = .ready
            if let chatID = info.chatID, selectedSessionKey == nil {
                selectLocalChat(chatID: chatID)
            }
        case .attached(let chatID):
            if pendingNewChat || selectedSessionKey == nil {
                pendingNewChat = false
                selectLocalChat(chatID: chatID)
                Task { await loadSessions(showSpinner: false) }
            }
        case .message(let message):
            messagesByChatID[message.chatID, default: []].append(
                ChatItem(chatID: message.chatID, role: .assistant, text: message.text)
            )
        case .delta(let delta):
            guard let chatID = delta.sessionKey else { return }
            let id = "stream-\(delta.messageID ?? chatID)"
            if let index = messagesByChatID[chatID, default: []].firstIndex(where: { $0.id == id }) {
                messagesByChatID[chatID]?[index].text += delta.text
            } else {
                messagesByChatID[chatID, default: []].append(
                    ChatItem(id: id, chatID: chatID, role: .assistant, text: delta.text, isStreaming: true)
                )
            }
        case .streamEnd(let completion):
            guard let chatID = completion.sessionKey else { return }
            let id = "stream-\(completion.messageID ?? chatID)"
            if let index = messagesByChatID[chatID, default: []].firstIndex(where: { $0.id == id }) {
                messagesByChatID[chatID]?[index].isStreaming = false
            }
            Task { await loadSessions(showSpinner: false) }
        case .error(let error):
            bannerMessage = error.message
        case .workCreated(_, let task):
            if let task { upsert(task: task) }
            Task { await loadWork(showSpinner: false) }
        case .workSubscribed:
            break
        case .workEvent(let event):
            guard let taskID = event.taskID else { return }
            if !workEventsByTaskID[taskID, default: []].contains(where: { $0.sequence == event.sequence }) {
                workEventsByTaskID[taskID, default: []].append(event)
            }
            if ["completed", "failed", "cancelled", "canceled"].contains(event.type.lowercased()) {
                Task { await loadWork(showSpinner: false) }
            }
        case .unknown:
            break
        }
    }

    private func selectLocalChat(chatID: String) {
        let key = "websocket:\(chatID)"
        selectedSessionKey = key
        if !sessions.contains(where: { $0.key == key }) {
            sessions.insert(SessionSummary(key: key, title: "New conversation", preview: "Ready for a prompt"), at: 0)
        }
    }

    private func upsert(task: WorkTask) {
        if let index = workTasks.firstIndex(where: { $0.id == task.id }) {
            workTasks[index] = task
        } else {
            workTasks.insert(task, at: 0)
        }
    }

    private func authenticatedRESTClient() async throws -> ZiggyRESTClient {
        if let client = restClient,
           restTokenExpiresAt.map({ $0 > Date() }) ?? true {
            return client
        }
        guard let serverURL = configuredServerURL, let code = configuredAccessCode else {
            throw ZiggyRESTError.invalidURL
        }
        let bootstrap = try await ZiggyRESTClient(baseURL: serverURL).bootstrapGuest(code: code)
        installRESTClient(from: bootstrap, serverURL: serverURL)
        guard let restClient else { throw ZiggyRESTError.invalidResponse }
        return restClient
    }

    private func installRESTClient(from bootstrap: BootstrapResponse, serverURL: URL) {
        restClient = ZiggyRESTClient(baseURL: serverURL, bearerToken: bootstrap.restToken)
        restTokenExpiresAt = bootstrap.expiresIn.map { Date().addingTimeInterval(TimeInterval(max(5, $0 - 15))) }
    }

    private func stopSocket() async {
        socketEventTask?.cancel()
        socketEventTask = nil
        await socket?.stop()
        socket = nil
        connectionState = .idle
    }

    private static func chatID(from sessionKey: String) -> String {
        sessionKey.hasPrefix("websocket:") ? String(sessionKey.dropFirst("websocket:".count)) : sessionKey
    }

    private static func webSocketURL(baseURL: URL, path: String) -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { return baseURL }
        components.path = path.isEmpty ? "/" : path
        components.query = nil
        return components.url ?? baseURL
    }

    private static func message(for error: Error) -> String {
        if let localized = error as? LocalizedError, let description = localized.errorDescription {
            return description
        }
        if let restError = error as? ZiggyRESTError {
            switch restError {
            case .http(let statusCode, _):
                return statusCode == 401 || statusCode == 403
                    ? "The private access code was rejected."
                    : "Ziggy returned HTTP \(statusCode)."
            case .invalidURL: return "The Ziggy server URL is invalid."
            case .invalidResponse: return "Ziggy returned an invalid response."
            case .decoding: return "Ziggy returned data this app could not read."
            }
        }
        return error.localizedDescription
    }
}
