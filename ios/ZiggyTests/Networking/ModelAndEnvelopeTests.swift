import XCTest
@testable import Ziggy

final class ModelAndEnvelopeTests: XCTestCase {
    func testProductionBootstrapShape() throws {
        let data = Data(#"{"token":"nbwt_short-lived","ws_path":"/","expires_in":299,"model_name":"qwen3.6-35b","access":"guest","guest_code":"enrolled"}"#.utf8)
        let response = try JSONDecoder().decode(BootstrapResponse.self, from: data)
        XCTAssertEqual(response.restToken, "nbwt_short-lived")
        XCTAssertNil(response.webSocketToken)
        XCTAssertEqual(response.webSocketPath, "/")
        XCTAssertEqual(response.expiresIn, 299)
        XCTAssertEqual(response.model, "qwen3.6-35b")
        XCTAssertEqual(response.access, "guest")
        XCTAssertEqual(response.isOwner, false)
    }

    func testCapabilitiesRemainIndependent() {
        let capabilities = RichContentCapabilities(advertised: [
            "rich_content_v1": .boolean(true),
            "mermaid_v1": .boolean(true)
        ])
        XCTAssertTrue(capabilities.richContentV1)
        XCTAssertFalse(capabilities.mediaV1)
        XCTAssertTrue(capabilities.mermaidV1)
    }

    func testBootstrapAcceptsCapabilityNameArray() throws {
        let data = Data(#"{"token":"t","capabilities":["rich_content_v1","media_v1"]}"#.utf8)
        let response = try JSONDecoder().decode(BootstrapResponse.self, from: data)
        let capabilities = RichContentCapabilities(advertised: response.capabilities)
        XCTAssertTrue(capabilities.richContentV1)
        XCTAssertTrue(capabilities.mediaV1)
        XCTAssertFalse(capabilities.mermaidV1)
    }

    func testUnknownStatusAndFieldsDoNotBreakMessageDecoding() throws {
        let data = Data(#"{"id":"m1","role":"future_role","content":"hello","status":"future_state","ignored":{"x":1}}"#.utf8)
        let message = try JSONDecoder().decode(ZiggyMessage.self, from: data)
        XCTAssertEqual(message.id, "m1")
        XCTAssertEqual(message.content.text, "hello")
        if case .unknown("future_role") = message.role {} else { XCTFail("role should preserve unknown value") }
        if case .unknown("future_state") = message.status {} else { XCTFail("status should preserve unknown value") }
    }

    func testProductionHistoryMessageAllowsNoIDAndUTCWithoutSuffix() throws {
        let data = Data(#"{"role":"assistant","content":"IOS_NATIVE_OK","timestamp":"2026-07-17T16:14:57.836606"}"#.utf8)
        let message = try JSONDecoder().decode(ZiggyMessage.self, from: data)
        XCTAssertFalse(message.id.isEmpty)
        XCTAssertEqual(message.content.text, "IOS_NATIVE_OK")
        XCTAssertNotNil(message.createdAt)
    }

    func testOutboundEnvelopeUsesWireNames() throws {
        let envelope = OutboundWebSocketEnvelope.workSubscribe(taskID: "task/1", afterSequence: 7)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(envelope)) as? [String: Any])
        XCTAssertEqual(object["type"] as? String, "work.subscribe")
        XCTAssertEqual(object["task_id"] as? String, "task/1")
        XCTAssertEqual(object["after_seq"] as? Int, 7)
    }

    func testOutboundMessageUsesProductionChatAndMediaFields() throws {
        let envelope = OutboundWebSocketEnvelope.message(
            chatID: "chat-1",
            content: "hello",
            media: [OutboundMedia(dataURL: "data:image/jpeg;base64,AA==", name: "photo.jpg")]
        )
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(envelope)) as? [String: Any])
        XCTAssertEqual(object["type"] as? String, "message")
        XCTAssertEqual(object["chat_id"] as? String, "chat-1")
        XCTAssertNil(object["session_key"])
        XCTAssertEqual((object["media"] as? [[String: Any]])?.first?["name"] as? String, "photo.jpg")
    }

    func testInboundTypedEventAndUnknownEvent() throws {
        let message = try JSONDecoder().decode(InboundWebSocketEvent.self, from: Data(#"{"event":"message","chat_id":"chat-1","text":"hello","kind":"assistant","future":true}"#.utf8))
        if case .message(let value) = message {
            XCTAssertEqual(value.chatID, "chat-1")
            XCTAssertEqual(value.text, "hello")
        } else { XCTFail("expected typed message") }

        let unknown = try JSONDecoder().decode(InboundWebSocketEvent.self, from: Data(#"{"event":"future.event","value":3}"#.utf8))
        if case .unknown(let type, let payload) = unknown {
            XCTAssertEqual(type, "future.event")
            guard case .object(let object) = payload else { return XCTFail("unknown payload must be an object") }
            XCTAssertEqual(object["value"], .number(3))
        } else { XCTFail("unknown event must be preserved") }
    }

    func testProductionWorkEventUsesFlatEnvelopeAndPayload() throws {
        let data = Data(#"{"event":"work.event","task_id":"task-1","seq":4,"type":"progress","payload":{"message":"Checking deploy"},"actor":"ziggy","step_id":"step-2","created_at":"2026-07-17T12:00:00Z"}"#.utf8)
        let event = try JSONDecoder().decode(InboundWebSocketEvent.self, from: data)
        guard case .workEvent(let workEvent) = event else { return XCTFail("expected work event") }
        XCTAssertEqual(workEvent.taskID, "task-1")
        XCTAssertEqual(workEvent.sequence, 4)
        XCTAssertEqual(workEvent.message, "Checking deploy")
        XCTAssertEqual(workEvent.actor, "ziggy")
    }

    func testRESTListWrapperDecodesItems() throws {
        let data = Data(#"{"data":[{"key":"chat-1","title":"One"}],"next_cursor":"next","has_more":true}"#.utf8)
        let response = try JSONDecoder().decode(RESTEnvelope<[SessionSummary]>.self, from: data)
        XCTAssertEqual(response.value?.first?.key, "chat-1")
    }
}
