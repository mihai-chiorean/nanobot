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

    public init() {}

    public mutating func feed(_ bytes: some Sequence<UInt8>) -> [SSEEvent] {
        buffer.append(contentsOf: bytes)
        var output: [SSEEvent] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            var line = Array(buffer[buffer.startIndex..<newline])
            buffer.removeSubrange(buffer.startIndex...newline)
            if line.last == 0x0D { line.removeLast() }
            process(line: String(decoding: line, as: UTF8.self), output: &output)
        }
        return output
    }

    public mutating func finish() -> [SSEEvent] {
        var output: [SSEEvent] = []
        if !buffer.isEmpty {
            var remaining = buffer
            if remaining.last == 0x0D { remaining.removeLast() }
            let line = String(decoding: remaining, as: UTF8.self)
            buffer.removeAll(keepingCapacity: false)
            process(line: line, output: &output)
        }
        dispatch(output: &output)
        return output
    }

    private mutating func process(line: String, output: inout [SSEEvent]) {
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
        case "data": dataLines.append(value)
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

    public func stream(request body: OpenAIChatCompletionRequest, bearerToken: String? = nil) -> AsyncThrowingStream<AssistantStreamEvent, Error> {
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
                    for try await byte in bytes {
                        for event in parser.feed([byte]) {
                            if event.data == "[DONE]" { continuation.finish(); return }
                            let chunk = try JSONDecoder().decode(OpenAIChunk.self, from: Data(event.data.utf8))
                            if let choice = chunk.choices.first {
                                let delta = AssistantDelta(sessionKey: nil, messageID: chunk.id,
                                                           text: choice.delta.content ?? "", role: choice.delta.role.map(MessageRole.init))
                                if !delta.text.isEmpty || delta.role != nil { continuation.yield(.delta(delta)) }
                                if choice.finishReason != nil {
                                    continuation.yield(.completed(AssistantCompletion(messageID: chunk.id, finishReason: choice.finishReason)))
                                }
                            }
                        }
                    }
                    for event in parser.finish() where event.data != "[DONE]" {
                        let chunk = try JSONDecoder().decode(OpenAIChunk.self, from: Data(event.data.utf8))
                        if let choice = chunk.choices.first, let content = choice.delta.content, !content.isEmpty {
                            continuation.yield(.delta(AssistantDelta(messageID: chunk.id, text: content)))
                        }
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
