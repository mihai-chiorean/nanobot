import Foundation

public enum ZiggySocketConnectionState: Sendable, Equatable, Hashable {
    case idle
    case connecting
    case connected
    case reconnecting
    case stopped
    case failed(String)
}

public enum ZiggyWebSocketEvent: Sendable, Hashable {
    case state(ZiggySocketConnectionState)
    case inbound(InboundWebSocketEvent)
    case decodingFailure(String)
    case outboundQueueFull
}

public struct WebSocketCredential: Sendable, Hashable {
    public let bearerToken: String
    public let capabilities: RichContentCapabilities

    public init(bearerToken: String, capabilities: RichContentCapabilities = .legacyOnly) {
        self.bearerToken = bearerToken
        self.capabilities = capabilities
    }
}

public enum ZiggyWebSocketClientError: Error, Equatable, Sendable {
    case frameTooLarge(limit: Int)
    case upgradeFailed(statusCode: Int?)
}

protocol ZiggyWebSocketConnection: AnyObject, Sendable {
    var response: URLResponse? { get }
    func resume()
    func ping() async throws
    func send(_ message: URLSessionWebSocketTask.Message) async throws
    func receive() async throws -> URLSessionWebSocketTask.Message
    func cancel(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?)
}

final class URLSessionWebSocketConnection: ZiggyWebSocketConnection, @unchecked Sendable {
    private let task: URLSessionWebSocketTask

    init(task: URLSessionWebSocketTask) {
        self.task = task
    }

    var response: URLResponse? { task.response }

    func resume() { task.resume() }

    func ping() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            task.sendPing { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }

    func send(_ message: URLSessionWebSocketTask.Message) async throws {
        try await task.send(message)
    }

    func receive() async throws -> URLSessionWebSocketTask.Message {
        try await task.receive()
    }

    func cancel(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        task.cancel(with: closeCode, reason: reason)
    }
}

public actor ZiggyWebSocketClient {
    public nonisolated let events: AsyncStream<ZiggyWebSocketEvent>

    private let baseURL: URL
    private let session: URLSession
    private let credentialProvider: @Sendable () async throws -> WebSocketCredential
    private let maxOutboundQueue: Int
    private let heartbeatInterval: Duration
    private let eventContinuation: AsyncStream<ZiggyWebSocketEvent>.Continuation
    private var assistantContinuations: [UUID: AsyncStream<AssistantStreamEvent>.Continuation] = [:]
    private var socket: (any ZiggyWebSocketConnection)?
    private var connectionTask: Task<Void, Never>?
    private var outboundQueue: [OutboundWebSocketEnvelope] = []
    private var attachedChatIDs: Set<String> = []
    private var shouldRun = false
    private var backoffNanoseconds: UInt64 = 500_000_000
    private let connectionFactory: @Sendable (URLRequest) -> any ZiggyWebSocketConnection

    public init(baseURL: URL, session: URLSession = .shared, maxOutboundQueue: Int = 100,
                tokenProvider: @escaping @Sendable () async throws -> String) {
        self.baseURL = baseURL
        self.session = session
        self.maxOutboundQueue = max(1, maxOutboundQueue)
        self.heartbeatInterval = .seconds(10)
        self.credentialProvider = {
            WebSocketCredential(bearerToken: try await tokenProvider())
        }
        self.connectionFactory = { request in
            URLSessionWebSocketConnection(task: session.webSocketTask(with: request))
        }
        var continuation: AsyncStream<ZiggyWebSocketEvent>.Continuation!
        self.events = AsyncStream { continuation = $0 }
        self.eventContinuation = continuation
    }

    public init(baseURL: URL, session: URLSession = .shared, maxOutboundQueue: Int = 100,
                credentialProvider: @escaping @Sendable () async throws -> WebSocketCredential) {
        self.baseURL = baseURL
        self.session = session
        self.maxOutboundQueue = max(1, maxOutboundQueue)
        self.heartbeatInterval = .seconds(10)
        self.credentialProvider = credentialProvider
        self.connectionFactory = { request in
            URLSessionWebSocketConnection(task: session.webSocketTask(with: request))
        }
        var continuation: AsyncStream<ZiggyWebSocketEvent>.Continuation!
        self.events = AsyncStream { continuation = $0 }
        self.eventContinuation = continuation
    }

    init(baseURL: URL, maxOutboundQueue: Int = 100,
         heartbeatInterval: Duration = .seconds(10),
         credentialProvider: @escaping @Sendable () async throws -> WebSocketCredential,
         connectionFactory: @escaping @Sendable (URLRequest) -> any ZiggyWebSocketConnection) {
        self.baseURL = baseURL
        self.session = .shared
        self.maxOutboundQueue = max(1, maxOutboundQueue)
        self.heartbeatInterval = heartbeatInterval
        self.credentialProvider = credentialProvider
        self.connectionFactory = connectionFactory
        var continuation: AsyncStream<ZiggyWebSocketEvent>.Continuation!
        self.events = AsyncStream { continuation = $0 }
        self.eventContinuation = continuation
    }

    public func start() {
        guard connectionTask == nil else { return }
        shouldRun = true
        connectionTask = Task { [weak self] in await self?.connectionLoop() }
    }

    public func stop() {
        shouldRun = false
        connectionTask?.cancel()
        connectionTask = nil
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
        emit(.state(.stopped))
        for continuation in assistantContinuations.values { continuation.finish() }
        assistantContinuations.removeAll()
    }

    public func attach(chatID: String) {
        attachedChatIDs.insert(chatID)
        enqueueOrSend(.attach(chatID: chatID))
    }

    public func detach(chatID: String) {
        attachedChatIDs.remove(chatID)
    }

    public func send(_ envelope: OutboundWebSocketEnvelope) {
        enqueueOrSend(envelope)
    }

    public func makeAssistantStream() -> BufferedAssistantStream {
        let id = UUID()
        var continuation: AsyncStream<AssistantStreamEvent>.Continuation!
        let stream = AsyncStream<AssistantStreamEvent> { continuation = $0 }
        assistantContinuations[id] = continuation
        continuation.onTermination = { [weak self] _ in
            Task { await self?.removeAssistantContinuation(id) }
        }
        return BufferedAssistantStream(events: stream) { [weak self] in
            Task { await self?.removeAssistantContinuation(id) }
        }
    }

    private func removeAssistantContinuation(_ id: UUID) { assistantContinuations.removeValue(forKey: id) }

    private func connectionLoop() async {
        var hasOpened = false
        while shouldRun && !Task.isCancelled {
            emit(.state(hasOpened ? .reconnecting : .connecting))
            var connection: (any ZiggyWebSocketConnection)?
            do {
                let credential = try await credentialProvider()
                let request = try Self.makeWebSocketRequest(baseURL: baseURL, credential: credential)
                let task = connectionFactory(request)
                connection = task
                socket = task
                task.resume()
                try await task.ping()
                hasOpened = true
                backoffNanoseconds = 500_000_000
                emit(.state(.connected))
                publishAssistant(.connected(nil))
                try await flushAttachedChats(on: task)
                try await flushQueue(on: task)
                try await maintainConnection(on: task, capabilities: credential.capabilities)
            } catch is CancellationError {
                break
            } catch {
                let message = Self.failureMessage(for: error, response: connection?.response)
                emit(.state(.failed(message)))
                let terminal = Self.isTerminalFailure(error, response: connection?.response)
                connection?.cancel(with: .abnormalClosure, reason: nil)
                if terminal { shouldRun = false }
            }
            socket = nil
            guard shouldRun && !Task.isCancelled else { break }
            emit(.state(.reconnecting))
            await sleepBackoff()
            backoffNanoseconds = min(backoffNanoseconds * 2, 20_000_000_000)
        }
    }

    private func receiveLoop(on task: any ZiggyWebSocketConnection,
                             capabilities: RichContentCapabilities) async throws {
        while shouldRun && !Task.isCancelled {
            let message = try await task.receive()
            _ = try await handle(message, capabilities: capabilities)
        }
    }

    private func maintainConnection(on task: any ZiggyWebSocketConnection,
                                    capabilities: RichContentCapabilities) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { [weak self] in
                guard let self else { return }
                try await self.receiveLoop(on: task, capabilities: capabilities)
            }
            group.addTask { [weak self] in
                guard let self else { return }
                try await self.heartbeatLoop(on: task)
            }
            defer { group.cancelAll() }
            _ = try await group.next()
        }
    }

    private func heartbeatLoop(on task: any ZiggyWebSocketConnection) async throws {
        while shouldRun && !Task.isCancelled {
            try await Task.sleep(for: heartbeatInterval)
            guard shouldRun && !Task.isCancelled else { return }
            do {
                try await task.ping()
            } catch {
                task.cancel(with: .abnormalClosure, reason: nil)
                throw error
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message,
                        capabilities: RichContentCapabilities) async throws -> Bool {
            let data: Data
            switch message {
            case .string(let string): data = Data(string.utf8)
            case .data(let value): data = value
            @unknown default: return true
            }
            let event: InboundWebSocketEvent
            do {
                event = try Self.decodeFrame(data, capabilities: capabilities)
            } catch {
                emit(.decodingFailure("Ziggy sent an event this app could not read."))
                return true
            }
            emit(.inbound(event))
            switch event {
            case .message(let message):
                if let rich = message.richContent { publishAssistant(.message(rich)) }
            case .delta(let delta): publishAssistant(.delta(delta))
            case .streamEnd(let completion): publishAssistant(.completed(completion))
            case .error(let error): publishAssistant(.failed(AssistantStreamFailure(message: error.message, code: error.code)))
            default: break
            }
        return true
    }

    private func flushAttachedChats(on task: any ZiggyWebSocketConnection) async throws {
        for chatID in attachedChatIDs.sorted() {
            try await task.send(.string(try encode(.attach(chatID: chatID))))
        }
        outboundQueue.removeAll { envelope in
            if case .attach = envelope { return true }
            return false
        }
    }

    private func enqueueOrSend(_ envelope: OutboundWebSocketEnvelope) {
        guard let task = socket else {
            enqueue(envelope)
            return
        }
        Task { [weak self] in
            do {
                try await task.send(.string(try Self.encode(envelope)))
            } catch {
                await self?.enqueueAfterSendFailure(envelope, task: task)
            }
        }
    }

    private func enqueueAfterSendFailure(_ envelope: OutboundWebSocketEnvelope,
                                         task: any ZiggyWebSocketConnection) {
        if socket === task { socket = nil; task.cancel(with: .abnormalClosure, reason: nil) }
        enqueue(envelope)
    }

    private func enqueue(_ envelope: OutboundWebSocketEnvelope) {
        guard outboundQueue.count < maxOutboundQueue else {
            emit(.outboundQueueFull)
            return
        }
        outboundQueue.append(envelope)
    }

    private func flushQueue(on task: any ZiggyWebSocketConnection) async throws {
        while !outboundQueue.isEmpty {
            let envelope = outboundQueue.removeFirst()
            do { try await task.send(.string(try encode(envelope))) }
            catch { outboundQueue.insert(envelope, at: 0); throw error }
        }
    }

    private static func failureMessage(for error: Error, response: URLResponse?) -> String {
        if let statusCode = (response as? HTTPURLResponse)?.statusCode {
            return "WebSocket upgrade failed (HTTP \(statusCode))."
        }
        if case let ZiggyWebSocketClientError.upgradeFailed(statusCode) = error {
            if let statusCode { return "WebSocket upgrade failed (HTTP \(statusCode))." }
            return "WebSocket upgrade failed."
        }
        if case let ZiggyRESTError.http(statusCode, _) = error {
            return statusCode == 401
                ? "WebSocket authentication failed (HTTP 401)."
                : "WebSocket upgrade failed (HTTP \(statusCode))."
        }
        return "WebSocket connection failed."
    }

    private static func isTerminalFailure(_ error: Error, response: URLResponse?) -> Bool {
        if let statusCode = (response as? HTTPURLResponse)?.statusCode { return statusCode != 101 }
        if let socketError = error as? ZiggyWebSocketClientError,
           case .upgradeFailed = socketError { return true }
        if case let ZiggyRESTError.http(statusCode, _) = error { return statusCode == 401 }
        return false
    }

    static func makeWebSocketRequest(baseURL: URL, credential: WebSocketCredential) throws -> URLRequest {
        guard ZiggyServerURLValidation.isValid(baseURL),
              var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw ZiggyRESTError.invalidURL
        }
        if components.scheme == "https" { components.scheme = "wss" }
        else if components.scheme == "http" { components.scheme = "ws" }
        let secretQueryNames: Set<String> = ["token", "access_token", "authorization", "code"]
        components.queryItems = components.queryItems?.filter {
            !secretQueryNames.contains($0.name.lowercased())
        }
        if components.queryItems?.isEmpty == true { components.queryItems = nil }
        guard let url = components.url else { throw ZiggyRESTError.invalidURL }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(credential.bearerToken)", forHTTPHeaderField: "Authorization")
        return request
    }

    static func decodeFrame(_ data: Data,
                            capabilities: RichContentCapabilities) throws -> InboundWebSocketEvent {
        guard data.count <= ZiggyProtocolLimits.maxWebSocketFrameBytes else {
            throw ZiggyWebSocketClientError.frameTooLarge(limit: ZiggyProtocolLimits.maxWebSocketFrameBytes)
        }
        return try JSONDecoder.ziggy.decode(InboundWebSocketEvent.self, from: data)
            .applying(capabilities: capabilities)
    }

    private func sleepBackoff() async {
        do { try await Task.sleep(nanoseconds: backoffNanoseconds) } catch { }
    }

    private func emit(_ event: ZiggyWebSocketEvent) { eventContinuation.yield(event) }
    private func publishAssistant(_ event: AssistantStreamEvent) {
        for continuation in assistantContinuations.values { continuation.yield(event) }
    }

    private static func encode(_ envelope: OutboundWebSocketEnvelope) throws -> String {
        let data = try JSONEncoder().encode(envelope)
        guard let string = String(data: data, encoding: .utf8) else { throw ZiggyRESTError.decoding }
        return string
    }

    private func encode(_ envelope: OutboundWebSocketEnvelope) throws -> String { try Self.encode(envelope) }
}

extension InboundWebSocketEvent {
    func applying(capabilities: RichContentCapabilities) -> InboundWebSocketEvent {
        switch self {
        case .message(let message):
            let rich = message.richContent.flatMap {
                capabilities.richContentV1 ? capabilities.sanitize($0) : nil
            }
            return .message(InboundChatMessage(
                id: message.id,
                chatID: message.chatID,
                text: message.text,
                role: message.role,
                richContent: rich,
                replyTo: message.replyTo,
                mediaURLs: message.mediaURLs,
                buttons: message.buttons,
                buttonPrompt: message.buttonPrompt,
                kind: message.kind
            ))
        case .streamEnd(let completion):
            guard let message = completion.message else { return self }
            let normalized: StreamFinalMessage
            switch message {
            case .rich(let rich) where capabilities.richContentV1:
                normalized = .rich(capabilities.sanitize(rich))
            case .rich(let rich):
                normalized = .legacy(ZiggyMessage(
                    id: rich.id,
                    sessionKey: rich.chatID,
                    role: rich.role,
                    content: .text(LegacyContentAdapter.plainText(for: rich.blocks)),
                    createdAt: rich.createdAt
                ))
            case .legacy(let legacy):
                normalized = .legacy(legacy)
            }
            return .streamEnd(AssistantCompletion(
                sessionKey: completion.sessionKey,
                messageID: completion.messageID,
                message: normalized,
                finishReason: completion.finishReason
            ))
        default:
            return self
        }
    }
}
