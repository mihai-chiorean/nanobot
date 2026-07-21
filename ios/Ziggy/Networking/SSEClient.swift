import Foundation

public struct SSEEvent: Equatable, Sendable {
    public let event: String?
    public let id: String?
    public let retry: Int?
    public let data: String
    public init(event: String? = nil, id: String? = nil, retry: Int? = nil, data: String) {
        self.event = event; self.id = id; self.retry = retry; self.data = data
    }
}

/// Incremental SSE framing. Feed arbitrary byte fragments; UTF-8 and CRLF are handled at line boundaries.
public struct SSEParser: Sendable {
    private var buffer: [UInt8] = []
    private var eventName: String?
    private var eventID: String?
    private var retry: Int?
    private var dataLines: [String] = []
    private var eventDataBytes = 0

    public init() {}

    public mutating func feed(_ bytes: some Sequence<UInt8>) throws -> [SSEEvent] {
        buffer.append(contentsOf: bytes)
        var output: [SSEEvent] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            guard buffer.distance(from: buffer.startIndex, to: newline) <= ZiggyProtocolLimits.maxSSELineBytes else {
                throw SSEClientError.lineTooLarge(limit: ZiggyProtocolLimits.maxSSELineBytes)
            }
            var line = Array(buffer[buffer.startIndex..<newline])
            buffer.removeSubrange(buffer.startIndex...newline)
            if line.last == 0x0D { line.removeLast() }
            try process(line: String(decoding: line, as: UTF8.self), output: &output)
        }
        guard buffer.count <= ZiggyProtocolLimits.maxSSELineBytes else {
            throw SSEClientError.lineTooLarge(limit: ZiggyProtocolLimits.maxSSELineBytes)
        }
        return output
    }

    public mutating func finish() throws -> [SSEEvent] {
        var output: [SSEEvent] = []
        if !buffer.isEmpty {
            var remaining = buffer
            if remaining.last == 0x0D { remaining.removeLast() }
            let line = String(decoding: remaining, as: UTF8.self)
            buffer.removeAll(keepingCapacity: false)
            try process(line: line, output: &output)
        }
        dispatch(output: &output)
        return output
    }

    private mutating func process(line: String, output: inout [SSEEvent]) throws {
        if line.isEmpty { dispatch(output: &output); return }
        if line.first == ":" { return }
        let separator = line.firstIndex(of: ":")
        let field = String(line[..<(separator ?? line.endIndex)])
        var value = separator.map { String(line[line.index(after: $0)...]) } ?? ""
        if value.first == " " { value.removeFirst() }
        switch field {
        case "event": eventName = value
        case "id": eventID = value
        case "retry": retry = Int(value)
        case "data":
            eventDataBytes += value.utf8.count + (dataLines.isEmpty ? 0 : 1)
            guard eventDataBytes <= ZiggyProtocolLimits.maxSSEEventBytes else {
                throw SSEClientError.eventTooLarge(limit: ZiggyProtocolLimits.maxSSEEventBytes)
            }
            dataLines.append(value)
        default: break
        }
    }

    private mutating func dispatch(output: inout [SSEEvent]) {
        guard !dataLines.isEmpty else {
            eventName = nil; eventID = nil; retry = nil
            return
        }
        output.append(SSEEvent(event: eventName, id: eventID, retry: retry, data: dataLines.joined(separator: "\n")))
        eventName = nil; eventID = nil; retry = nil; dataLines.removeAll(keepingCapacity: true)
        eventDataBytes = 0
    }
}

public struct OpenAIChatCompletionRequest: Codable, Sendable, Hashable {
    public struct Message: Codable, Sendable, Hashable {
        public let role: String
        public let content: String
        public init(role: String, content: String) { self.role = role; self.content = content }
    }
    public let model: String?
    public let messages: [Message]
    public let stream: Bool
    public init(model: String? = nil, messages: [Message], stream: Bool = true) {
        self.model = model; self.messages = messages; self.stream = stream
    }
}

public struct SSEClient: Sendable {
    public let endpoint: URL
    public let session: URLSession

    public init(endpoint: URL, session: URLSession = .shared) {
        self.endpoint = endpoint; self.session = session
    }

    public func stream(request body: OpenAIChatCompletionRequest, bearerToken: String? = nil,
                       capabilities: RichContentCapabilities = .legacyOnly) -> AsyncThrowingStream<AssistantStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = URLRequest(url: endpoint)
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    if let bearerToken { request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization") }
                    request.httpBody = try JSONEncoder().encode(body)

                    let (bytes, response) = try await session.bytes(for: request)
                    guard let response = response as? HTTPURLResponse else {
                        throw SSEClientError.invalidResponse
                    }
                    guard (200..<300).contains(response.statusCode) else {
                        throw SSEClientError.http(statusCode: response.statusCode)
                    }
                    continuation.yield(.connected(nil))

                    var parser = SSEParser()
                    var eventDecoder = AssistantSSEEventDecoder(capabilities: capabilities)
                    for try await byte in bytes {
                        for event in try parser.feed([byte]) {
                            let decoded = try eventDecoder.decode(event)
                            for item in decoded.events { continuation.yield(item) }
                            if decoded.isDone {
                                continuation.finish()
                                return
                            }
                        }
                    }
                    for event in try parser.finish() {
                        let decoded = try eventDecoder.decode(event)
                        for item in decoded.events { continuation.yield(item) }
                        if decoded.isDone { break }
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

public enum SSEClientError: Error, Equatable, Sendable {
    case invalidResponse
    case http(statusCode: Int)
    case lineTooLarge(limit: Int)
    case eventTooLarge(limit: Int)
    case streamTooLarge(limit: Int)
    case tooManyStreams(limit: Int)
}

public struct AssistantSSEEventDecoder: Sendable {
    public struct Result: Sendable {
        public let events: [AssistantStreamEvent]
        public let isDone: Bool
    }

    private let capabilities: RichContentCapabilities
    private var accumulatedTextBytes: [String: Int] = [:]

    public init(capabilities: RichContentCapabilities = .legacyOnly) {
        self.capabilities = capabilities
    }

    public mutating func decode(_ event: SSEEvent) throws -> Result {
        guard event.data.utf8.count <= ZiggyProtocolLimits.maxSSEEventBytes else {
            throw SSEClientError.eventTooLarge(limit: ZiggyProtocolLimits.maxSSEEventBytes)
        }
        if event.data == "[DONE]" { return Result(events: [], isDone: true) }

        let data = Data(event.data.utf8)
        if let inbound = try decodeInboundEvent(event: event, data: data) {
            return try map(inbound.applying(capabilities: capabilities))
        }
        if let rich = try? JSONDecoder.ziggy.decode(RichContentMessage.self, from: data) {
            if capabilities.richContentV1 {
                return Result(events: [.message(capabilities.sanitize(rich))], isDone: false)
            }
            let text = LegacyContentAdapter.plainText(for: rich.blocks)
            let delta = AssistantDelta(sessionKey: rich.chatID, messageID: rich.id, text: text, role: rich.role)
            return try mapLegacyMessage(delta: delta, finishReason: "stop")
        }

        let chunk = try JSONDecoder.ziggy.decode(OpenAIChunk.self, from: data)
        guard let choice = chunk.choices.first else { return Result(events: [], isDone: false) }
        let delta = AssistantDelta(
            sessionKey: nil,
            messageID: chunk.id,
            text: choice.delta.content ?? "",
            role: choice.delta.role.map(MessageRole.init)
        )
        var events: [AssistantStreamEvent] = []
        if !delta.text.isEmpty || delta.role != nil {
            try account(for: delta)
            events.append(.delta(delta))
        }
        if let finishReason = choice.finishReason {
            accumulatedTextBytes.removeValue(forKey: streamKey(for: delta))
            events.append(.completed(AssistantCompletion(messageID: chunk.id, finishReason: finishReason)))
        }
        return Result(events: events, isDone: false)
    }

    private func decodeInboundEvent(event: SSEEvent, data: Data) throws -> InboundWebSocketEvent? {
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           object["event"] != nil || object["type"] != nil {
            return try JSONDecoder.ziggy.decode(InboundWebSocketEvent.self, from: data)
        }
        guard let eventName = event.event, !eventName.isEmpty,
              var object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        object["event"] = eventName
        let framed = try JSONSerialization.data(withJSONObject: object)
        guard framed.count <= ZiggyProtocolLimits.maxSSEEventBytes else {
            throw SSEClientError.eventTooLarge(limit: ZiggyProtocolLimits.maxSSEEventBytes)
        }
        return try JSONDecoder.ziggy.decode(InboundWebSocketEvent.self, from: framed)
    }

    private mutating func map(_ event: InboundWebSocketEvent) throws -> Result {
        switch event {
        case .ready(let info):
            return Result(events: [.connected(info)], isDone: false)
        case .message(let message):
            if let rich = message.richContent {
                return Result(events: [.message(rich)], isDone: false)
            }
            return try mapLegacyMessage(
                delta: AssistantDelta(sessionKey: message.chatID, messageID: message.id,
                                      text: message.text, role: message.role),
                finishReason: "stop"
            )
        case .delta(let delta):
            try account(for: delta)
            return Result(events: [.delta(delta)], isDone: false)
        case .streamEnd(let completion):
            accumulatedTextBytes.removeValue(forKey: completion.messageID ?? completion.sessionKey ?? "default")
            var events: [AssistantStreamEvent] = []
            if case .rich(let rich) = completion.message { events.append(.message(rich)) }
            events.append(.completed(completion))
            return Result(events: events, isDone: false)
        case .error(let error):
            return Result(events: [.failed(AssistantStreamFailure(message: error.message, code: error.code))], isDone: false)
        case .attached, .workCreated, .workSubscribed, .workEvent, .unknown:
            return Result(events: [], isDone: false)
        }
    }

    private mutating func mapLegacyMessage(delta: AssistantDelta, finishReason: String) throws -> Result {
        try account(for: delta)
        accumulatedTextBytes.removeValue(forKey: streamKey(for: delta))
        return Result(events: [
            .delta(delta),
            .completed(AssistantCompletion(
                sessionKey: delta.sessionKey,
                messageID: delta.messageID,
                finishReason: finishReason
            ))
        ], isDone: false)
    }

    private mutating func account(for delta: AssistantDelta) throws {
        let key = streamKey(for: delta)
        guard accumulatedTextBytes[key] != nil
                || accumulatedTextBytes.count < ZiggyProtocolLimits.maxActiveStreams else {
            throw SSEClientError.tooManyStreams(limit: ZiggyProtocolLimits.maxActiveStreams)
        }
        let total = accumulatedTextBytes[key, default: 0] + delta.text.utf8.count
        guard total <= ZiggyProtocolLimits.maxStreamTextBytes else {
            throw SSEClientError.streamTooLarge(limit: ZiggyProtocolLimits.maxStreamTextBytes)
        }
        accumulatedTextBytes[key] = total
    }

    private func streamKey(for delta: AssistantDelta) -> String {
        delta.messageID ?? delta.sessionKey ?? "default"
    }
}

private struct OpenAIChunk: Decodable {
    struct Choice: Decodable {
        struct Delta: Decodable { let role: String?; let content: String? }
        let delta: Delta
        let finishReason: String?
        enum CodingKeys: String, CodingKey { case delta; case finishReason = "finish_reason" }
    }
    let id: String
    let choices: [Choice]
}
