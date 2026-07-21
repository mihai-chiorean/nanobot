import Foundation
import XCTest
@testable import Ziggy

final class RichTransportTests: XCTestCase {
    func testDirectV1WebSocketMessageRequiresCapabilityAndGatesMediaSeparately() throws {
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
        guard case .message(let message) = enabled, let rich = message.richContent else {
            return XCTFail("expected typed message")
        }
        XCTAssertEqual(rich.id, "msg-1")
        XCTAssertEqual(rich.blocks.count, 3)
        guard case .unsupported(let type, _) = rich.blocks[1] else { return XCTFail("media must remain disabled") }
        XCTAssertEqual(type, "media")
        guard case .mermaid = rich.blocks[2] else { return XCTFail("Mermaid capability is independent") }

        let fallback = try ZiggyWebSocketClient.decodeFrame(data, capabilities: .legacyOnly)
        guard case .message(let legacy) = fallback else { return XCTFail("expected legacy fallback") }
        XCTAssertNil(legacy.richContent)
        XCTAssertTrue(legacy.text.contains("Hello"))
    }

    func testLegacyWebSocketAndRichSSEPaths() throws {
        let legacyData = Data(#"{"event":"message","id":"legacy-1","chat_id":"chat-1","text":"hello","kind":"assistant"}"#.utf8)
        let legacy = try ZiggyWebSocketClient.decodeFrame(legacyData, capabilities: .legacyOnly)
        guard case .message(let message) = legacy else { return XCTFail("expected legacy message") }
        XCTAssertEqual(message.id, "legacy-1")
        XCTAssertEqual(message.text, "hello")

        var decoder = AssistantSSEEventDecoder(
            capabilities: RichContentCapabilities(richContentV1: true)
        )
        let richData = #"{"event":"message","version":"1","id":"msg-sse","chat_id":"chat-sse","role":"assistant","blocks":[{"type":"code","code":"42"}]}"#
        let result = try decoder.decode(SSEEvent(data: richData))
        guard case .message(let rich)? = result.events.first else { return XCTFail("expected SSE rich message") }
        XCTAssertEqual(rich.id, "msg-sse")
        XCTAssertEqual(rich.blocks, [.code(CodeBlock(code: "42"))])
    }

    func testOversizedWebSocketFrameAndSSEAccumulationAreRejected() throws {
        let frame = Data(repeating: 0x20, count: ZiggyProtocolLimits.maxWebSocketFrameBytes + 1)
        XCTAssertThrowsError(try ZiggyWebSocketClient.decodeFrame(frame, capabilities: .legacyOnly)) { error in
            XCTAssertEqual(error as? ZiggyWebSocketClientError, .frameTooLarge(limit: ZiggyProtocolLimits.maxWebSocketFrameBytes))
        }

        var decoder = AssistantSSEEventDecoder()
        let chunk = String(repeating: "x", count: ZiggyProtocolLimits.maxDeltaTextBytes)
        for _ in 0..<(ZiggyProtocolLimits.maxStreamTextBytes / ZiggyProtocolLimits.maxDeltaTextBytes) {
            let json = "{\"id\":\"stream\",\"choices\":[{\"delta\":{\"content\":\"\(chunk)\"}}]}"
            _ = try decoder.decode(SSEEvent(data: json))
        }
        let overflow = "{\"id\":\"stream\",\"choices\":[{\"delta\":{\"content\":\"x\"}}]}"
        XCTAssertThrowsError(try decoder.decode(SSEEvent(data: overflow)))
    }

    func testWebSocketAuthorizationNeverUsesURLQuery() throws {
        let request = try ZiggyWebSocketClient.makeWebSocketRequest(
            baseURL: try XCTUnwrap(URL(string: "https://example.test/ws?token=old&client_id=ios")),
            credential: WebSocketCredential(bearerToken: "secret")
        )
        XCTAssertEqual(request.url?.scheme, "wss")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer secret")
        XCTAssertEqual(URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.queryItems,
                       [URLQueryItem(name: "client_id", value: "ios")])
        XCTAssertFalse(request.url?.absoluteString.contains("secret") ?? true)
    }
}
