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
    public let message: ZiggyMessage?
    public let finishReason: String?

    public init(sessionKey: String? = nil, messageID: String? = nil, message: ZiggyMessage? = nil,
                finishReason: String? = nil) {
        self.sessionKey = sessionKey
        self.messageID = messageID
        self.message = message
        self.finishReason = finishReason
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        sessionKey = try c.decodeIfPresent(String.self, forAny: ["chat_id", "session_key", "chat_key"])
        messageID = try c.decodeIfPresent(String.self, forAny: ["stream_id", "message_id", "id"])
        message = try c.decodeIfPresent(ZiggyMessage.self, forAny: ["message"])
        finishReason = try c.decodeIfPresent(String.self, forAny: ["finish_reason", "reason"])
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
    public let chatID: String
    public let text: String
    public let replyTo: String?
    public let mediaURLs: [String]
    public let buttons: [String]
    public let buttonPrompt: String?
    public let kind: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        chatID = try c.decode(String.self, forAny: ["chat_id"])
        text = try c.decodeIfPresent(String.self, forAny: ["text", "content"]) ?? ""
        replyTo = try c.decodeIfPresent(String.self, forAny: ["reply_to"])
        if let urls = try c.decodeIfPresent([String].self, forAny: ["media_urls"]) {
            mediaURLs = urls
        } else {
            let media = try c.decodeIfPresent([JSONValue].self, forAny: ["media"]) ?? []
            mediaURLs = media.compactMap { item in
                item.stringValue ?? item.objectString(for: ["url", "media_url", "data_url"])
            }
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
            let payload = (try? JSONValue(from: decoder)) ?? .object([:])
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
