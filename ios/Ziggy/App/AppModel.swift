import Foundation
import Observation
import SwiftUI

enum ZiggyTheme: String, CaseIterable, Identifiable, Sendable {
    case system
    case light
    case dark

    var id: String { rawValue }
    var label: String { rawValue.capitalized }
    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

struct ChatItem: Identifiable, Hashable, Sendable {
    var id: String
    let chatID: String
    let role: MessageRole
    var text: String
    var blocks: [RichBlock]
    var isStreaming: Bool

    init(id: String = UUID().uuidString, chatID: String, role: MessageRole,
         text: String, blocks: [RichBlock]? = nil, isStreaming: Bool = false) {
        self.id = id
        self.chatID = chatID
        self.role = role
        self.text = text
        self.blocks = blocks ?? LegacyContentAdapter.blocks(text: text, role: role)
        self.isStreaming = isStreaming
    }

    @discardableResult
    mutating func append(delta: String) -> Bool {
        guard delta.utf8.count <= ZiggyProtocolLimits.maxDeltaTextBytes,
              text.utf8.count + delta.utf8.count <= ZiggyProtocolLimits.maxStreamTextBytes else {
            isStreaming = false
            return false
        }
        text += delta
        blocks = [.markdown(MarkdownBlock(text: text))]
        return true
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

    var identity = ZiggyIdentity.signedOut
    var selectedTab: Tab = .chats
    var theme: ZiggyTheme
    var chatNavigationPath: [String] = []
    var phase: Phase = .launching
    var serverURLText = "https://chat.mihaichiorean.com"
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

    var isServerURLAllowed: Bool {
        ZiggyServerURLValidation.url(from: serverURLText) != nil
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
    private let authSession: any AuthSessionProviding
    private var restClient: ZiggyRESTClient?
    private var restTokenExpiresAt: Date?
    private var currentBootstrap: BootstrapResponse?
    private var bootstrapRefreshTask: Task<BootstrapResponse, Error>?
    private var bootstrapRefreshGeneration = 0
    private var credentialGeneration = 0
    private var contentCapabilities = RichContentCapabilities.legacyOnly
    private var socket: ZiggyWebSocketClient?
    private var socketEventTask: Task<Void, Never>?
    private var configuredServerURL: URL?
    private var connectionAttempt = 0
    private var needsForegroundReconnect = false
    private var pendingNewChat = false
    private var hasStarted = false
    private var chatReconciler = ChatStreamReconciler()

    init(
        credentialStore: any CredentialStoring = KeychainCredentialStore(),
        authSession: any AuthSessionProviding = UnconfiguredAuthSession()
    ) {
        self.credentialStore = credentialStore
        self.authSession = authSession
        self.theme = ZiggyTheme(rawValue: UserDefaults.standard.string(forKey: "ziggy.theme") ?? "") ?? .system
    }

    func start() async {
        guard !hasStarted else { return }
        hasStarted = true

        let environment = ProcessInfo.processInfo.environment
        if let rawTheme = environment["ZIGGY_THEME"], let theme = ZiggyTheme(rawValue: rawTheme) {
            self.theme = theme
        }
        if let server = environment["ZIGGY_SERVER_URL"],
           let configuredURL = ZiggyServerURLValidation.url(from: server) {
            serverURLText = configuredURL.absoluteString
        }
        do {
            if let savedURL = try await credentialStore.serverURL() {
                serverURLText = savedURL.absoluteString
            }
            if authSession.isSignedIn {
                await connectAuthenticated()
            } else {
                phase = .needsEnrollment
            }
        } catch CredentialStoreError.invalidStoredValue(key: .serverURL) {
            do {
                try await credentialStore.removeServerURL()
                serverURLText = ZiggyServerURLValidation.productionURL.absoluteString
                bannerMessage = "The saved server URL was rejected and has been reset."
                phase = .needsEnrollment
            } catch {
                phase = .failed(Self.message(for: error))
            }
        } catch {
            phase = .failed(Self.message(for: error))
        }
    }

    func setTheme(_ theme: ZiggyTheme) {
        self.theme = theme
        UserDefaults.standard.set(theme.rawValue, forKey: "ziggy.theme")
    }

    func toggleTheme(currentScheme: ColorScheme) {
        setTheme(currentScheme == .dark ? .light : .dark)
    }

    func connectAuthenticated() async {
        connectionAttempt += 1
        let attempt = connectionAttempt

        guard let serverURL = ZiggyServerURLValidation.url(from: serverURLText) else {
            phase = .failed("Enter a valid Ziggy server URL.")
            return
        }
        guard authSession.isSignedIn else {
            phase = .needsEnrollment
            return
        }

        phase = .connecting
        bannerMessage = nil
        await stopSocket()
        invalidateBootstrapRefresh()
        restClient = nil
        currentBootstrap = nil
        restTokenExpiresAt = nil
        configuredServerURL = nil
        contentCapabilities = .legacyOnly
        credentialGeneration += 1

        do {
            let identityToken = try await authSession.sessionToken()
            guard attempt == connectionAttempt else { return }
            let bootstrap = try await ZiggyRESTClient(baseURL: serverURL)
                .bootstrapAuthenticated(identityToken: identityToken)
            guard attempt == connectionAttempt else { return }
            try await credentialStore.save(serverURL: serverURL)
            guard attempt == connectionAttempt else { return }
            identity = authSession.identity ?? .signedOut
            configuredServerURL = serverURL
            installRESTClient(from: bootstrap, serverURL: serverURL)
            modelName = bootstrap.model ?? "Ziggy"

            let webSocketURL = Self.webSocketURL(baseURL: serverURL, path: bootstrap.webSocketPath)
            let socket = ZiggyWebSocketClient(baseURL: webSocketURL) { [weak self] in
                guard let self else { throw ZiggyRESTError.invalidResponse }
                return try await self.webSocketCredential(for: attempt)
            }
            self.socket = socket
            observe(socket)
            await socket.start()
            guard attempt == connectionAttempt else {
                await socket.stop()
                return
            }

            async let sessionsLoad: Void = loadSessions(showSpinner: false)
            async let workLoad: Void = loadWork(showSpinner: false)
            _ = await (sessionsLoad, workLoad)
        } catch {
            guard attempt == connectionAttempt else { return }
            connectionState = .failed(Self.message(for: error))
            phase = .failed(Self.message(for: error))
        }
    }

    func authenticationDidChange() async {
        guard await clearConnectionState() else { return }
        guard authSession.isSignedIn else {
            phase = .needsEnrollment
            return
        }
        await connectAuthenticated()
    }

    func retryConnection() async {
        await connectAuthenticated()
    }

    func applicationDidEnterBackground() {
        connectionAttempt += 1
        needsForegroundReconnect = true
        invalidateBootstrapRefresh()
        let socketToStop = detachSocket()
        Task { await socketToStop?.stop() }
    }

    func applicationDidBecomeActive() async {
        guard needsForegroundReconnect else { return }
        needsForegroundReconnect = false
        guard hasStarted, authSession.isSignedIn else { return }
        await connectAuthenticated()
    }

    func signOut() async {
        await clearConnectionState()
        do {
            try await authSession.signOut()
            phase = .needsEnrollment
        } catch {
            bannerMessage = Self.message(for: error)
            if authSession.isSignedIn {
                await connectAuthenticated()
            }
        }
    }

    @discardableResult
    private func clearConnectionState() async -> Bool {
        connectionAttempt += 1
        let attempt = connectionAttempt
        await stopSocket()
        guard attempt == connectionAttempt else { return false }
        restClient = nil
        currentBootstrap = nil
        invalidateBootstrapRefresh()
        restTokenExpiresAt = nil
        contentCapabilities = .legacyOnly
        credentialGeneration += 1
        chatReconciler = ChatStreamReconciler()
        configuredServerURL = nil
        identity = .signedOut
        sessions = []
        workTasks = []
        workEventsByTaskID = [:]
        messagesByChatID = [:]
        selectedSessionKey = nil
        chatNavigationPath = []
        pendingNewChat = false
        isRefreshing = false
        isSending = false
        modelName = "Ziggy"
        bannerMessage = nil
        phase = .needsEnrollment
        return true
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
            let response = try await performAuthenticatedREST { try await $0.fetchSessions() }
            var refreshed = response.items.sorted { lhs, rhs in
                (lhs.updatedAt?.date ?? .distantPast) > (rhs.updatedAt?.date ?? .distantPast)
            }
            if let selectedSessionKey,
               !refreshed.contains(where: { $0.key == selectedSessionKey }),
               let active = sessions.first(where: { $0.key == selectedSessionKey }) {
                refreshed.insert(active, at: 0)
            }
            sessions = refreshed
        } catch is CancellationError {
            return
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
            let response = try await performAuthenticatedREST { try await $0.fetchMessages(sessionKey: sessionKey) }
            let chatID = Self.chatID(from: sessionKey)
            messagesByChatID[chatID] = response.items.map { message in
                let blocks = LegacyContentAdapter.content(for: message, capabilities: contentCapabilities)
                return ChatItem(
                    id: message.id,
                    chatID: chatID,
                    role: message.role,
                    text: LegacyContentAdapter.plainText(for: blocks),
                    blocks: blocks,
                    isStreaming: false
                )
            }
        } catch ZiggyRESTError.http(let statusCode, _) where statusCode == 404 {
            messagesByChatID[Self.chatID(from: sessionKey)] = []
        } catch is CancellationError {
            return
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
            let response = try await performAuthenticatedREST { try await $0.fetchWork() }
            workTasks = response.items.sorted { lhs, rhs in
                (lhs.updatedAt?.date ?? lhs.createdAt?.date ?? .distantPast)
                    > (rhs.updatedAt?.date ?? rhs.createdAt?.date ?? .distantPast)
            }
        } catch is CancellationError {
            return
        } catch {
            bannerMessage = Self.message(for: error)
        }
    }

    func subscribe(to task: WorkTask) async {
        do {
            let response = try await performAuthenticatedREST { try await $0.fetchWorkEvents(taskID: task.id) }
            workEventsByTaskID[task.id] = response.items.sorted { ($0.sequence ?? 0) < ($1.sequence ?? 0) }
            let after = response.items.compactMap(\.sequence).max()
            await socket?.send(.workSubscribe(taskID: task.id, afterSequence: after))
        } catch is CancellationError {
            return
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
            handle(chatReconciler.apply(
                message: message,
                capabilities: contentCapabilities,
                to: &messagesByChatID
            ))
        case .delta(let delta):
            handle(chatReconciler.apply(delta: delta, to: &messagesByChatID))
        case .streamEnd(let completion):
            handle(chatReconciler.apply(
                completion: completion,
                capabilities: contentCapabilities,
                to: &messagesByChatID
            ))
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
            if Self.shouldRefreshWork(for: event)
                || ["completed", "failed", "cancelled", "canceled"].contains(event.type.lowercased()) {
                Task { await loadWork(showSpinner: false) }
            }
        case .unknown:
            break
        }
    }

    nonisolated static func shouldRefreshWork(for event: WorkEvent) -> Bool {
        event.type.lowercased() == "status.changed" && event.status?.isTerminal == true
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

    private func handle(_ result: ChatReconciliationResult) {
        if case .rejected(let message) = result { bannerMessage = message }
    }

    private func authenticatedRESTClient() async throws -> ZiggyRESTClient {
        if let client = restClient,
           restTokenExpiresAt.map({ $0 > Date() }) ?? true {
            return client
        }
        _ = try await refreshBootstrap(force: false)
        guard let restClient else { throw ZiggyRESTError.invalidResponse }
        return restClient
    }

    private func performAuthenticatedREST<Value>(
        _ operation: (ZiggyRESTClient) async throws -> Value
    ) async throws -> Value {
        let attempt = connectionAttempt
        do {
            let client = try await authenticatedRESTClient()
            guard attempt == connectionAttempt else { throw CancellationError() }
            let generation = credentialGeneration
            do {
                let value = try await operation(client)
                guard attempt == connectionAttempt else { throw CancellationError() }
                return value
            } catch ZiggyRESTError.http(let statusCode, _) where statusCode == 401 {
                guard attempt == connectionAttempt else { throw CancellationError() }
                if generation == credentialGeneration {
                    restClient = nil
                    restTokenExpiresAt = nil
                    currentBootstrap = nil
                }
                let retryClient = try await authenticatedRESTClient()
                guard attempt == connectionAttempt else { throw CancellationError() }
                let value = try await operation(retryClient)
                guard attempt == connectionAttempt else { throw CancellationError() }
                return value
            }
        } catch {
            guard attempt == connectionAttempt else { throw CancellationError() }
            throw error
        }
    }

    private func webSocketCredential(for attempt: Int) async throws -> WebSocketCredential {
        guard attempt == connectionAttempt else { throw CancellationError() }
        let bootstrap = try await refreshBootstrap(force: true)
        guard attempt == connectionAttempt else { throw CancellationError() }
        return WebSocketCredential(
            bearerToken: bootstrap.webSocketToken ?? bootstrap.restToken,
            capabilities: RichContentCapabilities(advertised: bootstrap.capabilities)
        )
    }

    private func refreshBootstrap(force: Bool) async throws -> BootstrapResponse {
        if !force,
           let currentBootstrap,
           restTokenExpiresAt.map({ $0 > Date() }) ?? true {
            return currentBootstrap
        }
        if let bootstrapRefreshTask {
            let refreshGeneration = bootstrapRefreshGeneration
            let attempt = connectionAttempt
            let bootstrap = try await bootstrapRefreshTask.value
            guard refreshGeneration == bootstrapRefreshGeneration,
                  attempt == connectionAttempt else {
                throw CancellationError()
            }
            if currentBootstrap != bootstrap {
                guard let serverURL = configuredServerURL else { throw ZiggyRESTError.invalidURL }
                installRESTClient(from: bootstrap, serverURL: serverURL)
            }
            return bootstrap
        }
        guard let serverURL = configuredServerURL else {
            throw ZiggyRESTError.invalidURL
        }

        bootstrapRefreshGeneration += 1
        let refreshGeneration = bootstrapRefreshGeneration
        let attempt = connectionAttempt
        let task = Task { @MainActor in
            let identityToken = try await authSession.sessionToken()
            return try await ZiggyRESTClient(baseURL: serverURL)
                .bootstrapAuthenticated(identityToken: identityToken)
        }
        bootstrapRefreshTask = task
        do {
            let bootstrap = try await task.value
            guard refreshGeneration == bootstrapRefreshGeneration,
                  attempt == connectionAttempt else {
                throw CancellationError()
            }
            bootstrapRefreshTask = nil
            if currentBootstrap != bootstrap {
                installRESTClient(from: bootstrap, serverURL: serverURL)
            }
            return bootstrap
        } catch {
            if refreshGeneration == bootstrapRefreshGeneration {
                bootstrapRefreshTask = nil
            }
            throw error
        }
    }

    private func invalidateBootstrapRefresh() {
        bootstrapRefreshGeneration += 1
        bootstrapRefreshTask?.cancel()
        bootstrapRefreshTask = nil
    }

    private func installRESTClient(from bootstrap: BootstrapResponse, serverURL: URL) {
        restClient = ZiggyRESTClient(baseURL: serverURL, bearerToken: bootstrap.restToken)
        currentBootstrap = bootstrap
        restTokenExpiresAt = Self.credentialRefreshDate(for: bootstrap)
        contentCapabilities = RichContentCapabilities(advertised: bootstrap.capabilities)
        credentialGeneration += 1
    }

    static func credentialRefreshDate(for bootstrap: BootstrapResponse, now: Date = Date()) -> Date? {
        guard let expiration = bootstrap.expirationDate(relativeTo: now) else { return nil }
        let remaining = max(0, expiration.timeIntervalSince(now))
        let leeway = min(15, max(1, remaining * 0.1))
        return expiration.addingTimeInterval(-leeway)
    }

    private func stopSocket() async {
        let socketToStop = detachSocket()
        await socketToStop?.stop()
    }

    private func detachSocket() -> ZiggyWebSocketClient? {
        socketEventTask?.cancel()
        socketEventTask = nil
        let socketToStop = socket
        socket = nil
        connectionState = .idle
        return socketToStop
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
                    ? "This account is not authorized for Ziggy."
                    : "Ziggy returned HTTP \(statusCode)."
            case .invalidURL: return "The Ziggy server URL is invalid."
            case .invalidResponse: return "Ziggy returned an invalid response."
            case .responseTooLarge: return "Ziggy returned more data than this app can safely display."
            case .decoding: return "Ziggy returned data this app could not read."
            }
        }
        return error.localizedDescription
    }
}
