import Foundation

public struct AssistantDelta: Codable, Hashable, Sendable {
    public let sessionKey: String?
    public let messageID: String?
    public let text: String
    public let role: MessageRole?
    public let sequence: Int?

    public init(sessionKey: String? = nil, messageID: String? = nil, text: String,
                role: MessageRole? = nil, sequence: Int? = nil) {
        self.sessionKey = sessionKey
        self.messageID = messageID
        self.text = text
        self.role = role
        self.sequence = sequence
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        sessionKey = try c.decodeIfPresent(String.self, forAny: ["chat_id", "session_key", "chat_key"])
        messageID = try c.decodeIfPresent(String.self, forAny: ["stream_id", "message_id", "id"])
        text = try c.decodeIfPresent(String.self, forAny: ["text", "content", "delta"]) ?? ""
        role = try c.decodeIfPresent(MessageRole.self, forAny: ["role"])
        sequence = try c.decodeIfPresent(Int.self, forAny: ["seq", "sequence"])
        guard (sessionKey ?? "").utf8.count <= RichContentLimits.maxChatIDBytes,
              (messageID ?? "").utf8.count <= RichContentLimits.maxMessageIDBytes,
              text.utf8.count <= ZiggyProtocolLimits.maxDeltaTextBytes,
              sequence.map({ $0 >= 0 }) ?? true else {
            throw RichContentDecodingError.limit("stream delta")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: AnyCodingKey.self)
        try c.encodeIfPresent(sessionKey, forKey: AnyCodingKey("chat_id"))
        try c.encodeIfPresent(messageID, forKey: AnyCodingKey("stream_id"))
        try c.encode(text, forKey: AnyCodingKey("text"))
        try c.encodeIfPresent(role, forKey: AnyCodingKey("role"))
        try c.encodeIfPresent(sequence, forKey: AnyCodingKey("seq"))
    }
}

public struct AssistantCompletion: Codable, Hashable, Sendable {
    public let sessionKey: String?
    public let messageID: String?
    public let message: StreamFinalMessage?
    public let finishReason: String?

    public init(sessionKey: String? = nil, messageID: String? = nil, message: StreamFinalMessage? = nil,
                finishReason: String? = nil) {
        self.sessionKey = sessionKey
        self.messageID = messageID
        self.message = message
        self.finishReason = finishReason
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        message = try c.decodeIfPresent(StreamFinalMessage.self, forAny: ["message"])
        sessionKey = try c.decodeIfPresent(String.self, forAny: ["chat_id", "session_key", "chat_key"])
            ?? message?.chatID
        messageID = try c.decodeIfPresent(String.self, forAny: ["stream_id", "message_id", "id"])
            ?? message?.id
        finishReason = try c.decodeIfPresent(String.self, forAny: ["finish_reason", "reason"])
        guard (sessionKey ?? "").utf8.count <= RichContentLimits.maxChatIDBytes,
              (messageID ?? "").utf8.count <= RichContentLimits.maxMessageIDBytes,
              (finishReason ?? "").utf8.count <= RichContentLimits.maxToolFieldBytes else {
            throw RichContentDecodingError.limit("stream completion")
        }
    }
}

public enum StreamFinalMessage: Codable, Hashable, Sendable {
    case rich(RichContentMessage)
    case legacy(ZiggyMessage)

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        if c.contains(AnyCodingKey("version")), c.contains(AnyCodingKey("blocks")) {
            self = .rich(try RichContentMessage(from: decoder))
        } else {
            self = .legacy(try ZiggyMessage(from: decoder))
        }
    }

    public func encode(to encoder: Encoder) throws {
        switch self {
        case .rich(let value): try value.encode(to: encoder)
        case .legacy(let value): try value.encode(to: encoder)
        }
    }

    public var id: String {
        switch self {
        case .rich(let value): value.id
        case .legacy(let value): value.id
        }
    }

    public var chatID: String? {
        switch self {
        case .rich(let value): value.chatID
        case .legacy(let value): value.sessionKey
        }
    }
}

public struct ConnectionInfo: Codable, Hashable, Sendable {
    public let clientID: String?
    public let chatID: String?
    public let model: String?

    public init(clientID: String? = nil, chatID: String? = nil, model: String? = nil) {
        self.clientID = clientID
        self.chatID = chatID
        self.model = model
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        clientID = try c.decodeIfPresent(String.self, forAny: ["client_id", "connection_id"])
        chatID = try c.decodeIfPresent(String.self, forAny: ["chat_id"])
        model = try c.decodeIfPresent(String.self, forAny: ["model", "model_name"])
    }
}

public struct InboundChatMessage: Codable, Hashable, Sendable {
    public let id: String?
    public let chatID: String
    public let text: String
    public let role: MessageRole
    public let richContent: RichContentMessage?
    public let replyTo: String?
    public let mediaURLs: [String]
    public let buttons: [String]
    public let buttonPrompt: String?
    public let kind: String?

    public init(id: String? = nil, chatID: String, text: String, role: MessageRole = .assistant,
                richContent: RichContentMessage? = nil, replyTo: String? = nil,
                mediaURLs: [String] = [], buttons: [String] = [], buttonPrompt: String? = nil,
                kind: String? = nil) {
        self.id = id
        self.chatID = chatID
        self.text = text
        self.role = role
        self.richContent = richContent
        self.replyTo = replyTo
        self.mediaURLs = mediaURLs
        self.buttons = buttons
        self.buttonPrompt = buttonPrompt
        self.kind = kind
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let directRich: RichContentMessage?
        if c.contains(AnyCodingKey("version")), c.contains(AnyCodingKey("blocks")) {
            directRich = try RichContentMessage(from: decoder)
        } else {
            let nested = try c.decodeIfPresent(JSONValue.self, forAny: ["message", "content"])
            if let object = nested?.objectValue,
               object["version"]?.stringValue == RichContentVersion.v1.rawValue,
               object["blocks"] != nil {
                let data = try JSONEncoder().encode(nested)
                directRich = try JSONDecoder.ziggy.decode(RichContentMessage.self, from: data)
            } else {
                directRich = nil
            }
        }
        richContent = directRich
        chatID = try directRich?.chatID ?? c.decode(String.self, forAny: ["chat_id"])
        id = try directRich?.id ?? c.decodeIfPresent(String.self, forAny: ["message_id", "id"])
        let legacyText = (try? c.decodeIfPresent(String.self, forAny: ["text", "content"])) ?? nil
        text = legacyText ?? directRich.map { LegacyContentAdapter.plainText(for: $0.blocks) } ?? ""
        role = try directRich?.role ?? c.decodeIfPresent(MessageRole.self, forAny: ["role"]) ?? .assistant
        replyTo = try c.decodeIfPresent(String.self, forAny: ["reply_to"])
        if let urls = try c.decodeIfPresent([String].self, forAny: ["media_urls"]) {
            mediaURLs = urls
        } else {
            let media = try c.decodeIfPresent([JSONValue].self, forAny: ["media"]) ?? []
            mediaURLs = media.compactMap { item in
                item.stringValue ?? item.objectString(for: ["url", "media_url", "data_url"])
            }
        }
        guard mediaURLs.count <= 16,
              mediaURLs.allSatisfy({ $0.utf8.count <= RichContentLimits.maxMediaURLBytes }) else {
            throw RichContentDecodingError.limit("legacy media")
        }
        if let labels = try? c.decodeIfPresent([String].self, forAny: ["buttons"]) {
            buttons = labels
        } else {
            let values = try c.decodeIfPresent([JSONValue].self, forAny: ["buttons"]) ?? []
            buttons = values.compactMap { item in
                item.stringValue ?? item.objectString(for: ["label", "title", "text"])
            }
        }
        buttonPrompt = try c.decodeIfPresent(String.self, forAny: ["button_prompt"])
        kind = try c.decodeIfPresent(String.self, forAny: ["kind"])
        guard !chatID.isEmpty, chatID.utf8.count <= RichContentLimits.maxChatIDBytes,
              (id ?? "").utf8.count <= RichContentLimits.maxMessageIDBytes,
              text.utf8.count <= ZiggyProtocolLimits.maxLegacyTextBytes,
              (replyTo ?? "").utf8.count <= RichContentLimits.maxMessageIDBytes,
              buttons.count <= 32,
              buttons.allSatisfy({ $0.utf8.count <= 256 }),
              (buttonPrompt ?? "").utf8.count <= 1_024,
              (kind ?? "").utf8.count <= 128 else {
            throw RichContentDecodingError.limit("legacy message")
        }
    }
}

public struct OutboundMedia: Codable, Hashable, Sendable {
    public let dataURL: String
    public let name: String

    public init(dataURL: String, name: String) {
        self.dataURL = dataURL
        self.name = name
    }

    enum CodingKeys: String, CodingKey {
        case dataURL = "data_url"
        case name
    }
}

public struct AssistantStreamFailure: Error, Codable, Hashable, Sendable {
    public let message: String
    public let code: String?

    public init(message: String, code: String? = nil) {
        self.message = message
        self.code = code
    }
}

public enum InboundWebSocketEvent: Decodable, Hashable, Sendable {
    case ready(ConnectionInfo)
    case attached(chatID: String)
    case message(InboundChatMessage)
    case delta(AssistantDelta)
    case streamEnd(AssistantCompletion)
    case error(WebSocketError)
    case workCreated(taskID: String, task: WorkTask?)
    case workSubscribed(taskID: String)
    case workEvent(WorkEvent)
    case unknown(type: String, payload: JSONValue)

    public var type: String {
        switch self {
        case .ready: "ready"
        case .attached: "attached"
        case .message: "message"
        case .delta: "delta"
        case .streamEnd: "stream_end"
        case .error: "error"
        case .workCreated: "work.created"
        case .workSubscribed: "work.subscribed"
        case .workEvent: "work.event"
        case .unknown(let type, _): type
        }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let type = try c.decode(String.self, forAny: ["event", "type"])
        guard !type.isEmpty, type.utf8.count <= RichContentLimits.maxBlockTypeBytes else {
            throw RichContentDecodingError.invalid("event type")
        }

        switch type {
        case "ready":
            self = .ready(try ConnectionInfo(from: decoder))
        case "attached":
            self = .attached(chatID: try c.decode(String.self, forAny: ["chat_id"]))
        case "message":
            self = .message(try InboundChatMessage(from: decoder))
        case "delta":
            self = .delta(try AssistantDelta(from: decoder))
        case "stream_end":
            self = .streamEnd(try AssistantCompletion(from: decoder))
        case "error":
            self = .error(try WebSocketError(from: decoder))
        case "work.created":
            let taskID = try c.decode(String.self, forAny: ["task_id"])
            self = .workCreated(
                taskID: taskID,
                task: try c.decodeIfPresent(WorkTask.self, forAny: ["task"])
            )
        case "work.subscribed":
            self = .workSubscribed(taskID: try c.decode(String.self, forAny: ["task_id"]))
        case "work.event":
            self = .workEvent(try WorkEvent(from: decoder))
        default:
            let payload = ((try? JSONValue(from: decoder)) ?? .object([:])).boundedForUnsupportedContent()
            self = .unknown(type: type, payload: payload)
        }
    }
}

public enum OutboundWebSocketEnvelope: Encodable, Hashable, Sendable {
    case newChat
    case attach(chatID: String)
    case message(chatID: String, content: String, media: [OutboundMedia])
    case workCreate(chatID: String, content: String, title: String?, media: [OutboundMedia])
    case workSubscribe(taskID: String, afterSequence: Int?)
    case workCancel(taskID: String)
    case workMessage(taskID: String, content: String)

    public var type: String {
        switch self {
        case .newChat: "new_chat"
        case .attach: "attach"
        case .message: "message"
        case .workCreate: "work.create"
        case .workSubscribe: "work.subscribe"
        case .workCancel: "work.cancel"
        case .workMessage: "work.message"
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: AnyCodingKey.self)
        try c.encode(type, forKey: AnyCodingKey("type"))

        switch self {
        case .newChat:
            break
        case .attach(let chatID):
            try c.encode(chatID, forKey: AnyCodingKey("chat_id"))
        case .message(let chatID, let content, let media):
            try c.encode(chatID, forKey: AnyCodingKey("chat_id"))
            try c.encode(content, forKey: AnyCodingKey("content"))
            if !media.isEmpty { try c.encode(media, forKey: AnyCodingKey("media")) }
        case .workCreate(let chatID, let content, let title, let media):
            try c.encode(chatID, forKey: AnyCodingKey("chat_id"))
            try c.encode(content, forKey: AnyCodingKey("content"))
            try c.encodeIfPresent(title, forKey: AnyCodingKey("title"))
            if !media.isEmpty { try c.encode(media, forKey: AnyCodingKey("media")) }
        case .workSubscribe(let taskID, let afterSequence):
            try c.encode(taskID, forKey: AnyCodingKey("task_id"))
            try c.encodeIfPresent(afterSequence, forKey: AnyCodingKey("after_seq"))
        case .workCancel(let taskID):
            try c.encode(taskID, forKey: AnyCodingKey("task_id"))
        case .workMessage(let taskID, let content):
            try c.encode(taskID, forKey: AnyCodingKey("task_id"))
            try c.encode(content, forKey: AnyCodingKey("content"))
        }
    }
}

extension JSONDecoder {
    static var ziggy: JSONDecoder { JSONDecoder() }
}
