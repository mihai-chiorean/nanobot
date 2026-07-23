import Foundation
import Testing
@testable import Ziggy

@Suite
struct RichTransportTests {
    @Test
    func `direct V1 WebSocket message requires capability and gates media separately`() throws {
        let data = Data(#"""
        {
          "event":"message","version":"1","id":"msg-1","chat_id":"chat-1","role":"assistant",
          "blocks":[
            {"type":"markdown","text":"Hello"},
            {"type":"media","source":{"type":"authenticated_asset","id":"asset-1"},"media_type":"image/png"},
            {"type":"mermaid","source":"graph TD; A-->B"}
          ]
        }
        """#.utf8)

        let enabled = try ZiggyWebSocketClient.decodeFrame(
            data,
            capabilities: RichContentCapabilities(richContentV1: true, mediaV1: false, mermaidV1: true)
        )
        guard case .message(let message) = enabled else {
            Issue.record("expected typed message")
            return
        }
        let rich = try #require(message.richContent)
        #expect(rich.id == "msg-1")
        #expect(rich.blocks.count == 3)
        guard case .unsupported(let type, _) = rich.blocks[1] else {
            Issue.record("media must remain disabled")
            return
        }
        #expect(type == "media")
        guard case .mermaid = rich.blocks[2] else {
            Issue.record("Mermaid capability is independent")
            return
        }

        let fallback = try ZiggyWebSocketClient.decodeFrame(data, capabilities: .legacyOnly)
        guard case .message(let legacy) = fallback else {
            Issue.record("expected legacy fallback")
            return
        }
        #expect(legacy.richContent == nil)
        #expect(legacy.text.contains("Hello"))
    }

    @Test
    func `legacy WebSocket and rich SSE paths decode`() throws {
        let legacyData = Data(#"{"event":"message","id":"legacy-1","chat_id":"chat-1","text":"hello","kind":"assistant"}"#.utf8)
        let legacy = try ZiggyWebSocketClient.decodeFrame(legacyData, capabilities: .legacyOnly)
        guard case .message(let message) = legacy else {
            Issue.record("expected legacy message")
            return
        }
        #expect(message.id == "legacy-1")
        #expect(message.text == "hello")

        var decoder = AssistantSSEEventDecoder(
            capabilities: RichContentCapabilities(richContentV1: true)
        )
        let richData = #"{"event":"message","version":"1","id":"msg-sse","chat_id":"chat-sse","role":"assistant","blocks":[{"type":"code","code":"42"}]}"#
        let result = try decoder.decode(SSEEvent(data: richData))
        guard case .message(let rich)? = result.events.first else {
            Issue.record("expected SSE rich message")
            return
        }
        #expect(rich.id == "msg-sse")
        #expect(rich.blocks == [.code(CodeBlock(code: "42"))])
    }

    @Test
    func `oversized WebSocket frame and SSE accumulation are rejected`() throws {
        let frame = Data(repeating: 0x20, count: ZiggyProtocolLimits.maxWebSocketFrameBytes + 1)
        #expect(throws: ZiggyWebSocketClientError.frameTooLarge(
            limit: ZiggyProtocolLimits.maxWebSocketFrameBytes
        )) {
            try ZiggyWebSocketClient.decodeFrame(frame, capabilities: .legacyOnly)
        }

        var decoder = AssistantSSEEventDecoder()
        let chunk = String(repeating: "x", count: ZiggyProtocolLimits.maxDeltaTextBytes)
        for _ in 0..<(ZiggyProtocolLimits.maxStreamTextBytes / ZiggyProtocolLimits.maxDeltaTextBytes) {
            let json = "{\"id\":\"stream\",\"choices\":[{\"delta\":{\"content\":\"\(chunk)\"}}]}"
            _ = try decoder.decode(SSEEvent(data: json))
        }
        let overflow = "{\"id\":\"stream\",\"choices\":[{\"delta\":{\"content\":\"x\"}}]}"
        #expect(throws: (any Error).self) {
            try decoder.decode(SSEEvent(data: overflow))
        }
    }

    @Test
    func `WebSocket authorization never uses URL query`() throws {
        let request = try ZiggyWebSocketClient.makeWebSocketRequest(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com/ws")),
            credential: WebSocketCredential(bearerToken: "secret")
        )
        #expect(request.url?.scheme == "wss")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer secret")
        #expect(URLComponents(
            url: try #require(request.url),
            resolvingAgainstBaseURL: false
        )?.query == nil)
        #expect(!(request.url?.absoluteString.contains("secret") ?? true))
    }

    @Test
    func `WebSocket request rejects server URL query`() throws {
        #expect(throws: ZiggyRESTError.invalidURL) {
            try ZiggyWebSocketClient.makeWebSocketRequest(
                baseURL: try #require(URL(string: "https://chat.mihaichiorean.com/ws?client_id=ios")),
                credential: WebSocketCredential(bearerToken: "secret")
            )
        }
    }
}
