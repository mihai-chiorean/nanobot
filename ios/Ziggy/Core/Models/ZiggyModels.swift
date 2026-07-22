import Foundation

public struct ZiggyTimestamp: Codable, Hashable, Sendable {
    public let date: Date

    public init(_ date: Date) { self.date = date }

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let seconds = try? value.decode(Double.self) {
            date = Date(timeIntervalSince1970: seconds)
            return
        }
        let string = try value.decode(String.self)
        if let date = Self.parse(string) {
            self.date = date
            return
        }
        if let seconds = Double(string) {
            self.date = Date(timeIntervalSince1970: seconds)
            return
        }
        throw DecodingError.dataCorruptedError(in: value, debugDescription: "Invalid timestamp")
    }

    private static func parse(_ value: String) -> Date? {
        guard let separator = value.firstIndex(of: "T") else { return nil }
        let time = value[value.index(after: separator)...]
        let hasOffset = time.contains("Z") || time.contains("+") || time.dropFirst().contains("-")
        if hasOffset {
            return ISO8601DateFormatter.ziggy(fractionalSeconds: true).date(from: value)
                ?? ISO8601DateFormatter.ziggy(fractionalSeconds: false).date(from: value)
        }

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = .current
        for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss.SSS", "yyyy-MM-dd'T'HH:mm:ss"] {
            formatter.dateFormat = format
            if let date = formatter.date(from: value) { return date }
        }
        return nil
    }

    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        try value.encode(ISO8601DateFormatter.ziggy(fractionalSeconds: true).string(from: date))
    }
}

private extension ISO8601DateFormatter {
    static func ziggy(fractionalSeconds: Bool) -> ISO8601DateFormatter {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = fractionalSeconds ? [.withInternetDateTime, .withFractionalSeconds] : [.withInternetDateTime]
        return formatter
    }
}

public enum ZiggyStatus: Codable, Hashable, Sendable {
    case queued, running, waiting, completed, failed, cancelled
    case unknown(String)

    public init(_ rawValue: String) {
        switch rawValue.lowercased() {
        case "queued", "pending": self = .queued
        case "running", "in_progress", "in-progress": self = .running
        case "waiting", "paused": self = .waiting
        case "completed", "complete", "done": self = .completed
        case "failed", "error": self = .failed
        case "cancelled", "canceled": self = .cancelled
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .queued: "queued"
        case .running: "running"
        case .waiting: "waiting"
        case .completed: "completed"
        case .failed: "failed"
        case .cancelled: "cancelled"
        case .unknown(let value): value
        }
    }

    public init(from decoder: Decoder) throws { self.init(try decoder.singleValueContainer().decode(String.self)) }
    public func encode(to encoder: Encoder) throws { var c = encoder.singleValueContainer(); try c.encode(rawValue) }
}

public struct BootstrapResponse: Codable, Hashable, Sendable {
    public let restToken: String
    public let webSocketToken: String?
    public let webSocketPath: String
    public let expiresIn: Int?
    public let expiresAt: ZiggyTimestamp?
    public let serverName: String?
    public let model: String?
    public let access: String?
    public let guestCode: String?
    public let isOwner: Bool?
    public let capabilities: [String: JSONValue]?

    /// Compatibility alias for callers that do not distinguish the two transport credentials yet.
    public var token: String { restToken }

    public func expirationDate(relativeTo now: Date = Date()) -> Date? {
        let relative = expiresIn.map { now.addingTimeInterval(TimeInterval(max(0, $0))) }
        switch (expiresAt?.date, relative) {
        case (.some(let absolute), .some(let relative)): return min(absolute, relative)
        case (.some(let absolute), .none): return absolute
        case (.none, .some(let relative)): return relative
        case (.none, .none): return nil
        }
    }

    public init(restToken: String, webSocketToken: String? = nil, webSocketPath: String = "/",
                expiresIn: Int? = nil, expiresAt: ZiggyTimestamp? = nil,
                serverName: String? = nil, model: String? = nil, access: String? = nil,
                guestCode: String? = nil, isOwner: Bool? = nil,
                capabilities: [String: JSONValue]? = nil) {
        self.restToken = restToken
        self.webSocketToken = webSocketToken
        self.webSocketPath = webSocketPath
        self.expiresIn = expiresIn
        self.expiresAt = expiresAt
        self.serverName = serverName
        self.model = model
        self.access = access
        self.guestCode = guestCode
        self.isOwner = isOwner ?? access.map { $0 == "owner" }
        self.capabilities = capabilities
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        restToken = try c.decode(String.self, forAny: ["rest_token", "restToken", "token", "access_token", "session_token"])
        webSocketToken = try c.decodeIfPresent(String.self, forAny: ["ws_token", "websocket_token", "webSocketToken"])
        webSocketPath = try c.decodeIfPresent(String.self, forAny: ["ws_path", "websocket_path"]) ?? "/"
        expiresIn = try c.decodeIfPresent(Int.self, forAny: ["expires_in", "expiresIn"])
        expiresAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["expires_at", "expiresAt"])
        serverName = try c.decodeIfPresent(String.self, forAny: ["server", "server_name", "name"])
        model = try c.decodeIfPresent(String.self, forAny: ["model", "model_name"])
        access = try c.decodeIfPresent(String.self, forAny: ["access", "access_level"])
        guestCode = try c.decodeIfPresent(String.self, forAny: ["guest_code"])
        isOwner = try c.decodeIfPresent(Bool.self, forAny: ["owner", "is_owner"]) ?? access.map { $0 == "owner" }
        if let values = try? c.decode([String: JSONValue].self, forAny: ["capabilities", "features"]) {
            capabilities = values
        } else if let names = try? c.decode([String].self, forAny: ["capabilities", "features"]) {
            capabilities = Dictionary(uniqueKeysWithValues: names.map { ($0, .boolean(true)) })
        } else {
            capabilities = nil
        }
    }
}

public struct SessionSummary: Codable, Hashable, Sendable {
    public let key: String
    public let title: String?
    public let preview: String?
    public let lastMessage: String?
    public let createdAt: ZiggyTimestamp?
    public let updatedAt: ZiggyTimestamp?
    public let messageCount: Int?
    public let archived: Bool?
    public let metadata: [String: JSONValue]?

    public init(key: String, title: String? = nil, preview: String? = nil, lastMessage: String? = nil,
                createdAt: ZiggyTimestamp? = nil, updatedAt: ZiggyTimestamp? = nil,
                messageCount: Int? = nil, archived: Bool? = nil, metadata: [String: JSONValue]? = nil) {
        self.key = key; self.title = title; self.preview = preview; self.lastMessage = lastMessage
        self.createdAt = createdAt; self.updatedAt = updatedAt; self.messageCount = messageCount
        self.archived = archived; self.metadata = metadata
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        key = try c.decode(String.self, forAny: ["key", "session_key", "id"])
        title = try c.decodeIfPresent(String.self, forAny: ["title", "name"])
        preview = try c.decodeIfPresent(String.self, forAny: ["preview", "summary"])
        lastMessage = try c.decodeIfPresent(String.self, forAny: ["last_message", "lastMessage"])
        createdAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["created_at", "createdAt"])
        updatedAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["updated_at", "updatedAt", "last_activity"])
        messageCount = try c.decodeIfPresent(Int.self, forAny: ["message_count", "messageCount"])
        archived = try c.decodeIfPresent(Bool.self, forAny: ["archived"])
        metadata = try c.decodeIfPresent([String: JSONValue].self, forAny: ["metadata", "meta"])
    }
}

public struct SessionHistory: Codable, Sendable {
    public let session: SessionSummary?
    public let messages: [ZiggyMessage]
    public let nextCursor: String?
    public let hasMore: Bool?

    public init(session: SessionSummary? = nil, messages: [ZiggyMessage], nextCursor: String? = nil, hasMore: Bool? = nil) {
        self.session = session; self.messages = messages; self.nextCursor = nextCursor; self.hasMore = hasMore
    }
}

public enum MessageRole: Codable, Hashable, Sendable {
    case user, assistant, system, tool, progress
    case unknown(String)

    public init(_ rawValue: String) {
        switch rawValue.lowercased() {
        case "user": self = .user
        case "assistant": self = .assistant
        case "system": self = .system
        case "tool", "function": self = .tool
        case "progress", "status": self = .progress
        default: self = .unknown(rawValue)
        }
    }
    public var rawValue: String {
        switch self { case .user: "user"; case .assistant: "assistant"; case .system: "system"; case .tool: "tool"; case .progress: "progress"; case .unknown(let value): value }
    }
    public init(from decoder: Decoder) throws { self.init(try decoder.singleValueContainer().decode(String.self)) }
    public func encode(to encoder: Encoder) throws { var c = encoder.singleValueContainer(); try c.encode(rawValue) }
}

public enum MessageContent: Codable, Hashable, Sendable {
    case text(String)
    case structured(JSONValue)

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let text = try? c.decode(String.self) {
            guard text.utf8.count <= ZiggyProtocolLimits.maxLegacyTextBytes else {
                throw RichContentDecodingError.limit("legacy message text")
            }
            self = .text(text)
        } else {
            let value = try c.decode(JSONValue.self)
            guard value.aggregateStringBytes <= RichContentLimits.maxMessageContentBytes else {
                throw RichContentDecodingError.limit("legacy message content")
            }
            self = .structured(value)
        }
    }
    public func encode(to encoder: Encoder) throws {
        switch self { case .text(let value): try value.encode(to: encoder); case .structured(let value): try value.encode(to: encoder) }
    }
    public var text: String? { if case .text(let value) = self { return value }; return nil }
}

public struct ZiggyMessage: Codable, Hashable, Sendable {
    public let id: String
    public let sessionKey: String?
    public let role: MessageRole
    public let content: MessageContent
    public let createdAt: ZiggyTimestamp?
    public let status: ZiggyStatus?
    public let sequence: Int?
    public let metadata: [String: JSONValue]?
    public let richContent: RichContentMessage?

    public init(id: String, sessionKey: String? = nil, role: MessageRole, content: MessageContent,
                createdAt: ZiggyTimestamp? = nil, status: ZiggyStatus? = nil, sequence: Int? = nil,
                metadata: [String: JSONValue]? = nil, richContent: RichContentMessage? = nil) {
        self.id = id; self.sessionKey = sessionKey; self.role = role; self.content = content
        self.createdAt = createdAt; self.status = status; self.sequence = sequence; self.metadata = metadata
        self.richContent = richContent
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        if c.contains(AnyCodingKey("version")), c.contains(AnyCodingKey("blocks")) {
            let rich = try RichContentMessage(from: decoder)
            id = rich.id
            sessionKey = rich.chatID
            role = rich.role
            content = .text(LegacyContentAdapter.plainText(for: rich.blocks))
            createdAt = rich.createdAt
            status = nil
            sequence = nil
            metadata = nil
            richContent = rich
            return
        }

        sessionKey = try c.decodeIfPresent(String.self, forAny: ["session_key", "sessionKey", "chat_key"])
        role = try c.decode(MessageRole.self, forAny: ["role", "author_role"])
        content = try c.decodeIfPresent(MessageContent.self, forAny: ["content", "text", "body"]) ?? .text("")
        createdAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["created_at", "createdAt", "timestamp"])
        status = try c.decodeIfPresent(ZiggyStatus.self, forAny: ["status"])
        sequence = try c.decodeIfPresent(Int.self, forAny: ["seq", "sequence"])
        metadata = try c.decodeIfPresent([String: JSONValue].self, forAny: ["metadata", "meta"])
        id = try c.decodeIfPresent(String.self, forAny: ["id", "message_id", "uuid"])
            ?? "message-\(sequence.map(String.init) ?? UUID().uuidString)"
        richContent = nil
        guard id.utf8.count <= RichContentLimits.maxMessageIDBytes,
              (sessionKey ?? "").utf8.count <= RichContentLimits.maxChatIDBytes else {
            throw RichContentDecodingError.limit("legacy message identity")
        }
    }
}

public struct WorkTask: Codable, Hashable, Sendable {
    public let id: String
    public let title: String?
    public let description: String?
    public let status: ZiggyStatus
    public let sessionKey: String?
    public let progress: Double?
    public let createdAt: ZiggyTimestamp?
    public let updatedAt: ZiggyTimestamp?
    public let startedAt: ZiggyTimestamp?
    public let completedAt: ZiggyTimestamp?
    public let metadata: [String: JSONValue]?

    public init(id: String, title: String? = nil, description: String? = nil, status: ZiggyStatus = .queued,
                sessionKey: String? = nil, progress: Double? = nil, createdAt: ZiggyTimestamp? = nil,
                updatedAt: ZiggyTimestamp? = nil, startedAt: ZiggyTimestamp? = nil,
                completedAt: ZiggyTimestamp? = nil, metadata: [String: JSONValue]? = nil) {
        self.id = id; self.title = title; self.description = description; self.status = status
        self.sessionKey = sessionKey; self.progress = progress; self.createdAt = createdAt
        self.updatedAt = updatedAt; self.startedAt = startedAt; self.completedAt = completedAt; self.metadata = metadata
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        id = try c.decode(String.self, forAny: ["id", "task_id", "key"])
        title = try c.decodeIfPresent(String.self, forAny: ["title", "name"])
        description = try c.decodeIfPresent(String.self, forAny: ["description", "prompt", "content"])
        status = try c.decodeIfPresent(ZiggyStatus.self, forAny: ["status", "state"]) ?? .queued
        sessionKey = try c.decodeIfPresent(String.self, forAny: ["session_key", "chat_key", "chat_id"])
        progress = try c.decodeIfPresent(Double.self, forAny: ["progress", "percent"])
        createdAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["created_at"])
        updatedAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["updated_at"])
        startedAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["started_at"])
        completedAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["completed_at"])
        metadata = try c.decodeIfPresent([String: JSONValue].self, forAny: ["metadata", "meta"])
    }
}

public struct WorkEvent: Codable, Hashable, Sendable {
    public let id: String?
    public let taskID: String?
    public let sequence: Int?
    public let type: String
    public let status: ZiggyStatus?
    public let message: String?
    public let data: JSONValue?
    public let actor: String?
    public let stepID: String?
    public let createdAt: ZiggyTimestamp?

    public init(id: String? = nil, taskID: String? = nil, sequence: Int? = nil, type: String,
                status: ZiggyStatus? = nil, message: String? = nil, data: JSONValue? = nil,
                actor: String? = nil, stepID: String? = nil,
                createdAt: ZiggyTimestamp? = nil) {
        self.id = id; self.taskID = taskID; self.sequence = sequence; self.type = type
        self.status = status; self.message = message; self.data = data; self.actor = actor
        self.stepID = stepID; self.createdAt = createdAt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        id = try c.decodeIfPresent(String.self, forAny: ["id", "event_id"])
        taskID = try c.decodeIfPresent(String.self, forAny: ["task_id"])
        sequence = try c.decodeIfPresent(Int.self, forAny: ["seq", "sequence"])
        type = try c.decodeIfPresent(String.self, forAny: ["type", "event_type"]) ?? "event"
        status = try c.decodeIfPresent(ZiggyStatus.self, forAny: ["status"])
        let payload = try c.decodeIfPresent(JSONValue.self, forAny: ["payload", "data"])
        data = payload
        message = try c.decodeIfPresent(String.self, forAny: ["message", "text", "detail"])
            ?? payload?.objectString(for: ["message", "text", "detail", "content"])
        actor = try c.decodeIfPresent(String.self, forAny: ["actor"])
        stepID = try c.decodeIfPresent(String.self, forAny: ["step_id"])
        createdAt = try c.decodeIfPresent(ZiggyTimestamp.self, forAny: ["created_at", "timestamp"])
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: AnyCodingKey.self)
        try c.encodeIfPresent(id, forKey: AnyCodingKey("id"))
        try c.encodeIfPresent(taskID, forKey: AnyCodingKey("task_id"))
        try c.encodeIfPresent(sequence, forKey: AnyCodingKey("seq"))
        try c.encode(type, forKey: AnyCodingKey("type"))
        try c.encodeIfPresent(status, forKey: AnyCodingKey("status"))
        try c.encodeIfPresent(message, forKey: AnyCodingKey("message"))
        try c.encodeIfPresent(data, forKey: AnyCodingKey("payload"))
        try c.encodeIfPresent(actor, forKey: AnyCodingKey("actor"))
        try c.encodeIfPresent(stepID, forKey: AnyCodingKey("step_id"))
        try c.encodeIfPresent(createdAt, forKey: AnyCodingKey("created_at"))
    }
}

public struct SettingsSnapshot: Codable, Hashable, Sendable {
    public let model: String?
    public let isOwner: Bool?
    public let values: [String: JSONValue]
    public init(model: String? = nil, isOwner: Bool? = nil, values: [String: JSONValue] = [:]) {
        self.model = model; self.isOwner = isOwner; self.values = values
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        model = try c.decodeIfPresent(String.self, forAny: ["model", "model_name"])
        isOwner = try c.decodeIfPresent(Bool.self, forAny: ["owner", "is_owner"])
        values = try c.decodeIfPresent([String: JSONValue].self, forAny: ["values", "settings"]) ?? [:]
    }
}

public struct RESTEnvelope<Value: Codable & Sendable>: Codable, Sendable {
    public let data: Value?
    public let result: Value?
    public let items: Value?
    public let ok: Bool?
    public let error: String?
    public let message: String?

    public init(data: Value? = nil, result: Value? = nil, items: Value? = nil, ok: Bool? = nil,
                error: String? = nil, message: String? = nil) {
        self.data = data; self.result = result; self.items = items; self.ok = ok; self.error = error; self.message = message
    }

    public var value: Value? { data ?? result ?? items }
}

public struct RESTListResponse<Value: Codable & Sendable>: Codable, Sendable {
    public let items: [Value]
    public let nextCursor: String?
    public let hasMore: Bool?
    public init(items: [Value], nextCursor: String? = nil, hasMore: Bool? = nil) {
        self.items = items; self.nextCursor = nextCursor; self.hasMore = hasMore
    }
}

public struct WebSocketError: Codable, Hashable, Sendable, Error {
    public let code: String?
    public let message: String
    public let details: JSONValue?
    public init(code: String? = nil, message: String, details: JSONValue? = nil) {
        self.code = code; self.message = message; self.details = details
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        code = try c.decodeIfPresent(String.self, forAny: ["code", "error_code"])
        message = try c.decodeIfPresent(String.self, forAny: ["message", "error", "detail"]) ?? "Unknown WebSocket error"
        details = try c.decodeIfPresent(JSONValue.self, forAny: ["details", "data"])
    }
}

struct AnyCodingKey: CodingKey, Hashable, Sendable {
    let stringValue: String
    let intValue: Int?
    init(_ string: String) { stringValue = string; intValue = nil }
    init?(stringValue: String) { self.init(stringValue) }
    init?(intValue: Int) { stringValue = String(intValue); self.intValue = intValue }
}

extension KeyedDecodingContainer where Key == AnyCodingKey {
    func decode<T: Decodable>(_ type: T.Type, forAny keys: [String]) throws -> T {
        for key in keys where contains(AnyCodingKey(key)) { return try decode(type, forKey: AnyCodingKey(key)) }
        throw DecodingError.keyNotFound(AnyCodingKey(keys[0]), DecodingError.Context(codingPath: codingPath, debugDescription: "Missing any of \(keys)"))
    }

    func decodeIfPresent<T: Decodable>(_ type: T.Type, forAny keys: [String]) throws -> T? {
        for key in keys where contains(AnyCodingKey(key)) { return try decodeIfPresent(type, forKey: AnyCodingKey(key)) }
        return nil
    }
}
