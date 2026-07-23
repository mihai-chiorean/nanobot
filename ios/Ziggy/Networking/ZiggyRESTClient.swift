import Foundation

public struct ZiggyRESTClient: Sendable {
    public let baseURL: URL
    public let session: URLSession
    public let maxResponseBytes: Int
    private let bearerToken: String?

    public init(baseURL: URL, bearerToken: String? = nil, session: URLSession = .shared,
                maxResponseBytes: Int = ZiggyProtocolLimits.maxRESTResponseBytes) {
        self.baseURL = baseURL
        self.bearerToken = bearerToken
        self.session = session
        self.maxResponseBytes = max(1, maxResponseBytes)
    }

    public func withBearerToken(_ token: String?) -> ZiggyRESTClient {
        ZiggyRESTClient(baseURL: baseURL, bearerToken: token, session: session,
                        maxResponseBytes: maxResponseBytes)
    }

    public func bootstrapAuthenticated(identityToken: String) async throws -> BootstrapResponse {
        guard ZiggyServerURLValidation.isTrustedForIdentityToken(baseURL) else {
            throw ZiggyRESTError.invalidURL
        }
        return try await request(path: ["auth", "bootstrap"], token: identityToken)
    }

    public func fetchSessions() async throws -> RESTListResponse<SessionSummary> {
        try await list(path: ["api", "sessions"])
    }

    public func fetchMessages(sessionKey: String) async throws -> RESTListResponse<ZiggyMessage> {
        try await list(path: ["api", "sessions", sessionKey, "messages"])
    }

    public func fetchWork() async throws -> RESTListResponse<WorkTask> {
        try await list(path: ["api", "work"])
    }

    public func fetchWorkTask(taskID: String) async throws -> WorkTask {
        let data = try await data(path: ["api", "work", taskID])
        let decoder = JSONDecoder.ziggy
        if let direct = try? decoder.decode(WorkTask.self, from: data) { return direct }
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let task = object["task"] {
            return try decoder.decode(WorkTask.self, from: JSONSerialization.data(withJSONObject: task))
        }
        throw ZiggyRESTError.decoding
    }

    public func fetchWorkEvents(taskID: String, afterSequence: Int? = nil) async throws -> RESTListResponse<WorkEvent> {
        let query = afterSequence.map { [URLQueryItem(name: "after_seq", value: String($0))] } ?? []
        return try await list(path: ["api", "work", taskID, "events"], query: query)
    }

    public func createWork(chatID: String, content: String, title: String? = nil,
                           media: [OutboundMedia] = [],
                           idempotencyKey: String = UUID().uuidString) async throws -> WorkTask {
        let body = try JSONEncoder().encode(WorkCreateRequest(
            chatID: chatID,
            content: content,
            title: title,
            media: media.isEmpty ? nil : media
        ))
        let data = try await data(
            path: ["api", "work"],
            headers: [
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Idempotency-Key": idempotencyKey
            ],
            method: "POST",
            body: body
        )
        return try decodeWorkTask(from: data)
    }

    public func cancelWork(taskID: String, idempotencyKey: String = UUID().uuidString) async throws -> WorkTask {
        let data = try await data(
            path: ["api", "work", taskID, "cancel"],
            headers: ["Accept": "application/json", "Idempotency-Key": idempotencyKey],
            method: "POST"
        )
        return try decodeWorkTask(from: data)
    }

    public func sendWorkFollowUp(taskID: String, content: String,
                                 idempotencyKey: String = UUID().uuidString) async throws {
        let body = try JSONEncoder().encode(WorkFollowUpRequest(content: content))
        do {
            _ = try await data(
                path: ["api", "work", taskID, "messages"],
                headers: [
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": idempotencyKey
                ],
                method: "POST",
                body: body
            )
        } catch ZiggyRESTError.http(let statusCode, _) where statusCode == 404 || statusCode == 501 {
            _ = try await data(
                path: ["api", "work", taskID, "message"],
                headers: [
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": idempotencyKey
                ],
                method: "POST",
                body: body
            )
        }
    }

    /// Opens a bounded, authenticated SSE stream. Reconnects resume from the last
    /// server event id and are deduplicated by the durable event sequence.
    public func streamWorkEvents(taskID: String, lastEventID: String? = nil,
                                 maxReconnectAttempts: Int = 3) -> AsyncThrowingStream<WorkEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var cursor = lastEventID
                    var seenSequences = Set<Int>()
                    var reconnectAttempts = 0
                    let reconnectLimit = min(max(0, maxReconnectAttempts), 3)

                    func waitForReconnect() async throws {
                        guard reconnectAttempts < reconnectLimit else {
                            throw ZiggyRESTError.streamReconnectLimit(limit: reconnectLimit)
                        }
                        reconnectAttempts += 1
                        try await Task.sleep(for: .milliseconds(
                            min(250 * (1 << (reconnectAttempts - 1)), 2_000)
                        ))
                    }

                    while !Task.isCancelled {
                        do {
                            var request = try makeRequest(
                                path: ["api", "work", taskID, "events", "stream"],
                                headers: [
                                    "Accept": "text/event-stream",
                                    "Cache-Control": "no-cache"
                                ]
                            )
                            request.httpMethod = "GET"
                            if let cursor, !cursor.isEmpty {
                                request.setValue(cursor, forHTTPHeaderField: "Last-Event-ID")
                            }
                            let (bytes, response) = try await session.bytes(for: request)
                            guard let response = response as? HTTPURLResponse else {
                                throw ZiggyRESTError.invalidResponse
                            }
                            guard (200..<300).contains(response.statusCode) else {
                                throw ZiggyRESTError.http(statusCode: response.statusCode, body: nil)
                            }

                            var parser = SSEParser()

                            func emit(_ sseEvent: SSEEvent) throws {
                                guard let workEvent = try Self.decodeWorkEvent(
                                    sseEvent,
                                    taskID: taskID
                                ) else { return }
                                if let sequence = workEvent.sequence {
                                    guard !seenSequences.contains(sequence) else {
                                        cursor = sseEvent.id ?? String(sequence)
                                        return
                                    }
                                    seenSequences.insert(sequence)
                                    if seenSequences.count > ZiggyProtocolLimits.maxTrackedStreamSequences {
                                        seenSequences.remove(seenSequences.min()!)
                                    }
                                    cursor = sseEvent.id ?? String(sequence)
                                } else if let id = sseEvent.id, !id.isEmpty {
                                    cursor = id
                                }
                                continuation.yield(workEvent)
                            }

                            for try await byte in bytes {
                                try Task.checkCancellation()
                                for sseEvent in try parser.feed([byte]) {
                                    try emit(sseEvent)
                                }
                            }
                            for sseEvent in try parser.finish() {
                                try emit(sseEvent)
                            }
                            // The budget is bounded across this stream lifetime. A clean
                            // EOF still represents a reconnect and must not spin forever.
                            try await waitForReconnect()
                        } catch is CancellationError {
                            throw CancellationError()
                        } catch let error as ZiggyRESTError {
                            if case .http(let statusCode, _) = error {
                                if Self.shouldSurfaceHTTPStatus(statusCode) || statusCode == 501 {
                                    throw error
                                }
                            }
                            try await waitForReconnect()
                        } catch {
                            try await waitForReconnect()
                        }
                    }
                    throw CancellationError()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    public func fetchSettings() async throws -> SettingsSnapshot {
        try await request(path: ["api", "settings"])
    }

    private func list<Value: Codable & Sendable>(path: [String], query: [URLQueryItem] = []) async throws -> RESTListResponse<Value> {
        let data = try await data(path: path, query: query)
        return try decodeList(Value.self, from: data)
    }

    private func request<Value: Codable & Sendable>(path: [String], query: [URLQueryItem] = [],
                                                    headers: [String: String] = [:], token: String? = nil,
                                                    method: String = "GET", body: Data? = nil) async throws -> Value {
        let data = try await data(path: path, query: query, headers: headers, token: token,
                                  method: method, body: body)
        let decoder = JSONDecoder.ziggy
        if let direct = try? decoder.decode(Value.self, from: data) { return direct }
        if let envelope = try? decoder.decode(RESTEnvelope<Value>.self, from: data), let value = envelope.value { return value }
        throw ZiggyRESTError.decoding
    }

    private func data(path: [String], query: [URLQueryItem] = [], headers: [String: String] = [:],
                      token: String? = nil, method: String = "GET", body: Data? = nil) async throws -> Data {
        var request = try makeRequest(path: path, query: query, headers: headers, token: token)
        request.httpMethod = method
        request.httpBody = body
        try Task.checkCancellation()
        let (bytes, response) = try await session.bytes(for: request)
        guard let response = response as? HTTPURLResponse else { throw ZiggyRESTError.invalidResponse }
        if response.expectedContentLength > Int64(maxResponseBytes) {
            throw ZiggyRESTError.responseTooLarge(limit: maxResponseBytes)
        }

        var responseData = Data()
        if response.expectedContentLength > 0 {
            responseData.reserveCapacity(min(Int(response.expectedContentLength), maxResponseBytes))
        }
        for try await byte in bytes {
            try Task.checkCancellation()
            guard responseData.count < maxResponseBytes else {
                throw ZiggyRESTError.responseTooLarge(limit: maxResponseBytes)
            }
            responseData.append(byte)
        }
        try Task.checkCancellation()
        guard (200..<300).contains(response.statusCode) else {
            let boundedBody = String(data: responseData, encoding: .utf8)?
                .ziggyTruncatedUTF8(maxBytes: ZiggyProtocolLimits.maxUnsupportedPayloadBytes)
            throw ZiggyRESTError.http(statusCode: response.statusCode, body: boundedBody)
        }
        return responseData
    }

    private func makeRequest(path: [String], query: [URLQueryItem] = [],
                             headers: [String: String] = [:], token: String? = nil) throws -> URLRequest {
        guard ZiggyServerURLValidation.isValid(baseURL) else { throw ZiggyRESTError.invalidURL }
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        let basePath = components?.percentEncodedPath ?? baseURL.path
        let encodedPath = path.map(Self.encodePathSegment).joined(separator: "/")
        components?.percentEncodedPath = basePath.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/" + encodedPath
        components?.queryItems = query.isEmpty ? nil : query
        guard let url = components?.url else { throw ZiggyRESTError.invalidURL }

        var request = URLRequest(url: url)
        for (header, value) in headers { request.setValue(value, forHTTPHeaderField: header) }
        if let token = token ?? bearerToken { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        return request
    }

    private static func encodePathSegment(_ segment: String) -> String {
        segment.addingPercentEncoding(withAllowedCharacters: CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))) ?? segment
    }

    private func decodeList<Value: Codable & Sendable>(_ type: Value.Type, from data: Data) throws -> RESTListResponse<Value> {
        let decoder = JSONDecoder.ziggy
        if let array = try? decoder.decode([Value].self, from: data) { return RESTListResponse(items: array) }
        if let envelope = try? decoder.decode(RESTEnvelope<[Value]>.self, from: data), let items = envelope.value {
            return RESTListResponse(items: items)
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw ZiggyRESTError.decoding }
        var itemsData: Data
        if let value = object["sessions"] ?? object["messages"] ?? object["work"] ?? object["tasks"] ?? object["events"] ?? object["items"] ?? object["data"] {
            itemsData = try JSONSerialization.data(withJSONObject: value)
        } else { throw ZiggyRESTError.decoding }
        if let nested = try? decoder.decode([Value].self, from: itemsData) {
            let nextCursor = object["next_cursor"] as? String ?? object["nextCursor"] as? String
            let hasMore = object["has_more"] as? Bool ?? object["hasMore"] as? Bool
            return RESTListResponse(items: nested, nextCursor: nextCursor, hasMore: hasMore)
        }
        if let nestedObject = try? JSONSerialization.jsonObject(with: itemsData) as? [String: Any],
           let nestedValue = nestedObject["items"] ?? nestedObject["sessions"] ?? nestedObject["messages"] ?? nestedObject["data"] {
            itemsData = try JSONSerialization.data(withJSONObject: nestedValue)
        }
        let items = try decoder.decode([Value].self, from: itemsData)
        let nextCursor = object["next_cursor"] as? String ?? object["nextCursor"] as? String
        let hasMore = object["has_more"] as? Bool ?? object["hasMore"] as? Bool
        return RESTListResponse(items: items, nextCursor: nextCursor, hasMore: hasMore)
    }

    private func decodeWorkTask(from data: Data) throws -> WorkTask {
        let decoder = JSONDecoder.ziggy
        if let task = try? decoder.decode(WorkTask.self, from: data) { return task }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let taskObject = object["task"] else {
            throw ZiggyRESTError.decoding
        }
        return try decoder.decode(WorkTask.self, from: JSONSerialization.data(withJSONObject: taskObject))
    }

    private static func decodeWorkEvent(_ event: SSEEvent, taskID: String) throws -> WorkEvent? {
        guard event.data != "[DONE]" else { return nil }
        guard var object = try JSONSerialization.jsonObject(with: Data(event.data.utf8)) as? [String: Any] else {
            throw ZiggyRESTError.decoding
        }
        if object["task_id"] == nil { object["task_id"] = taskID }
        if object["seq"] == nil, let id = event.id, let sequence = Int(id) { object["seq"] = sequence }
        if object["event"] == nil, let eventName = event.event, !eventName.isEmpty {
            object["event"] = eventName
        }
        let data = try JSONSerialization.data(withJSONObject: object)
        return try JSONDecoder.ziggy.decode(WorkEvent.self, from: data)
    }

    private static func shouldSurfaceHTTPStatus(_ statusCode: Int) -> Bool {
        guard (400..<500).contains(statusCode) else { return false }
        return ![408, 425, 429].contains(statusCode)
    }
}

private struct WorkCreateRequest: Codable, Sendable {
    let chatID: String
    let content: String
    let title: String?
    let media: [OutboundMedia]?

    enum CodingKeys: String, CodingKey {
        case chatID = "chat_id"
        case content
        case title
        case media
    }
}

private struct WorkFollowUpRequest: Codable, Sendable {
    let content: String
}

public enum ZiggyRESTError: Error, Equatable, Sendable {
    case invalidURL
    case invalidResponse
    case http(statusCode: Int, body: String?)
    case responseTooLarge(limit: Int)
    case streamReconnectLimit(limit: Int)
    case decoding
}
