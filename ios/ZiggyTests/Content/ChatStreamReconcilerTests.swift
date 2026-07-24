import Testing
@testable import Ziggy

@Suite
struct ChatStreamReconcilerTests {
    @Test
    func `final message reconciles stable ID and replay is idempotent`() throws {
        var reconciler = ChatStreamReconciler()
        var messages: [String: [ChatItem]] = [:]
        let first = AssistantDelta(sessionKey: "chat-1", messageID: "stream-1", text: "Hel", sequence: 1)
        let second = AssistantDelta(sessionKey: "chat-1", messageID: "stream-1", text: "lo", sequence: 2)

        #expect(reconciler.apply(delta: first, to: &messages) == .applied)
        #expect(reconciler.apply(delta: first, to: &messages) == .duplicate)
        #expect(reconciler.apply(delta: second, to: &messages) == .applied)
        #expect(messages["chat-1"]?.first?.text == "Hello")

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
        #expect(reconciler.apply(completion: completion, capabilities: capabilities, to: &messages) == .applied)
        #expect(messages["chat-1"]?.map(\.id) == ["message-42"])
        #expect(messages["chat-1"]?.first?.text == "Hello, final")
        #expect(!(messages["chat-1"]?.first?.isStreaming ?? true))

        #expect(reconciler.apply(completion: completion, capabilities: capabilities, to: &messages) == .duplicate)
        #expect(reconciler.apply(delta: second, to: &messages) == .duplicate)
        #expect(messages["chat-1"]?.count == 1)
    }

    @Test
    func `conflicting duplicate and bounded stream are rejected`() {
        var reconciler = ChatStreamReconciler()
        var messages: [String: [ChatItem]] = [:]
        _ = reconciler.apply(
            delta: AssistantDelta(sessionKey: "chat", messageID: "s", text: "a", sequence: 1),
            to: &messages
        )
        #expect(
            reconciler.apply(
                delta: AssistantDelta(sessionKey: "chat", messageID: "s", text: "b", sequence: 1),
                to: &messages
            ) == .rejected("Ziggy sent conflicting stream updates.")
        )

        var oversized = ChatItem(chatID: "chat", role: .assistant,
                                 text: String(repeating: "x", count: ZiggyProtocolLimits.maxStreamTextBytes),
                                 isStreaming: true)
        let didAppend = oversized.append(delta: "x")
        #expect(!didAppend)
        #expect(oversized.text.utf8.count <= ZiggyProtocolLimits.maxStreamTextBytes)
    }
}
