import XCTest
@testable import Ziggy

final class ChatStreamReconcilerTests: XCTestCase {
    func testFinalMessageReconcilesStableIDAndReplayIsIdempotent() throws {
        var reconciler = ChatStreamReconciler()
        var messages: [String: [ChatItem]] = [:]
        let first = AssistantDelta(sessionKey: "chat-1", messageID: "stream-1", text: "Hel", sequence: 1)
        let second = AssistantDelta(sessionKey: "chat-1", messageID: "stream-1", text: "lo", sequence: 2)

        XCTAssertEqual(reconciler.apply(delta: first, to: &messages), .applied)
        XCTAssertEqual(reconciler.apply(delta: first, to: &messages), .duplicate)
        XCTAssertEqual(reconciler.apply(delta: second, to: &messages), .applied)
        XCTAssertEqual(messages["chat-1"]?.first?.text, "Hello")

        let final = RichContentMessage(
            id: "message-42",
            chatID: "chat-1",
            role: .assistant,
            blocks: [.markdown(MarkdownBlock(text: "Hello, final"))]
        )
        let completion = AssistantCompletion(
            sessionKey: "chat-1",
            messageID: "stream-1",
            message: .rich(final),
            finishReason: "stop"
        )
        let capabilities = RichContentCapabilities(richContentV1: true)
        XCTAssertEqual(reconciler.apply(completion: completion, capabilities: capabilities, to: &messages), .applied)
        XCTAssertEqual(messages["chat-1"]?.map(\.id), ["message-42"])
        XCTAssertEqual(messages["chat-1"]?.first?.text, "Hello, final")
        XCTAssertFalse(messages["chat-1"]?.first?.isStreaming ?? true)

        XCTAssertEqual(reconciler.apply(completion: completion, capabilities: capabilities, to: &messages), .duplicate)
        XCTAssertEqual(reconciler.apply(delta: second, to: &messages), .duplicate)
        XCTAssertEqual(messages["chat-1"]?.count, 1)
    }

    func testConflictingDuplicateAndBoundedStreamAreRejected() {
        var reconciler = ChatStreamReconciler()
        var messages: [String: [ChatItem]] = [:]
        _ = reconciler.apply(
            delta: AssistantDelta(sessionKey: "chat", messageID: "s", text: "a", sequence: 1),
            to: &messages
        )
        XCTAssertEqual(
            reconciler.apply(
                delta: AssistantDelta(sessionKey: "chat", messageID: "s", text: "b", sequence: 1),
                to: &messages
            ),
            .rejected("Ziggy sent conflicting stream updates.")
        )

        var oversized = ChatItem(chatID: "chat", role: .assistant,
                                 text: String(repeating: "x", count: ZiggyProtocolLimits.maxStreamTextBytes),
                                 isStreaming: true)
        XCTAssertFalse(oversized.append(delta: "x"))
        XCTAssertLessThanOrEqual(oversized.text.utf8.count, ZiggyProtocolLimits.maxStreamTextBytes)
    }
}
