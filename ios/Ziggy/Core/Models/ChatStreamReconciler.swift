import Foundation

enum ChatReconciliationResult: Equatable, Sendable {
    case applied
    case duplicate
    case ignored
    case rejected(String)
}

struct ChatStreamReconciler: Sendable {
    private struct StreamKey: Hashable, Sendable {
        let chatID: String
        let streamID: String
    }

    private struct StreamState: Sendable {
        let itemID: String
        var lastSequence: Int?
        var signatures: [Int: UInt64] = [:]
    }

    private var streams: [StreamKey: StreamState] = [:]
    private var finalizedStreams: Set<StreamKey> = []
    private var finalizedOrder: [StreamKey] = []

    mutating func apply(message: InboundChatMessage,
                        capabilities: RichContentCapabilities,
                        to messages: inout [String: [ChatItem]]) -> ChatReconciliationResult {
        let role: MessageRole = ["trace", "progress", "tool"].contains(message.kind?.lowercased() ?? "")
            ? .progress
            : message.role
        let rich = message.richContent.flatMap {
            capabilities.richContentV1 ? capabilities.sanitize($0) : nil
        }
        let blocks = rich?.blocks
            ?? LegacyContentAdapter.blocks(text: message.text, role: role, kind: message.kind)
        let text = rich.map { LegacyContentAdapter.plainText(for: $0.blocks) } ?? message.text
        let stableID = rich?.id ?? message.id
        let item = ChatItem(
            id: stableID ?? UUID().uuidString,
            chatID: message.chatID,
            role: rich?.role ?? role,
            text: text,
            blocks: blocks,
            isStreaming: false
        )
        return upsert(item, stableID: stableID, in: &messages)
    }

    mutating func apply(delta: AssistantDelta,
                        to messages: inout [String: [ChatItem]]) -> ChatReconciliationResult {
        guard let chatID = delta.sessionKey, !chatID.isEmpty else { return .ignored }
        guard delta.text.utf8.count <= ZiggyProtocolLimits.maxDeltaTextBytes else {
            return .rejected("Ziggy sent an oversized stream update.")
        }

        let streamID = delta.messageID ?? chatID
        let key = StreamKey(chatID: chatID, streamID: streamID)
        guard !finalizedStreams.contains(key) else { return .duplicate }
        guard streams[key] != nil || streams.count < ZiggyProtocolLimits.maxActiveStreams else {
            return .rejected("Ziggy sent too many concurrent streams.")
        }
        var state = streams[key] ?? StreamState(itemID: "stream-\(streamID)")

        if let sequence = delta.sequence {
            let signature = Self.signature(delta.text)
            if let existing = state.signatures[sequence] {
                return existing == signature ? .duplicate : .rejected("Ziggy sent conflicting stream updates.")
            }
            if let last = state.lastSequence, sequence <= last { return .duplicate }
            state.signatures[sequence] = signature
            state.lastSequence = sequence
            trimSignatures(&state.signatures)
        }

        var items = messages[chatID, default: []]
        if let index = items.firstIndex(where: { $0.id == state.itemID }) {
            guard items[index].append(delta: delta.text) else {
                messages[chatID] = items
                streams.removeValue(forKey: key)
                rememberFinalized(key)
                return .rejected("Ziggy's streamed response exceeded the display limit.")
            }
            items[index].isStreaming = true
        } else {
            guard delta.text.utf8.count <= ZiggyProtocolLimits.maxStreamTextBytes else {
                return .rejected("Ziggy's streamed response exceeded the display limit.")
            }
            items.append(ChatItem(
                id: state.itemID,
                chatID: chatID,
                role: delta.role ?? .assistant,
                text: delta.text,
                isStreaming: true
            ))
        }
        streams[key] = state
        messages[chatID] = items
        return .applied
    }

    mutating func apply(completion: AssistantCompletion,
                        capabilities: RichContentCapabilities,
                        to messages: inout [String: [ChatItem]]) -> ChatReconciliationResult {
        guard let chatID = completion.message?.chatID ?? completion.sessionKey else { return .ignored }
        let streamID = completion.messageID ?? completion.message?.id ?? chatID
        let key = StreamKey(chatID: chatID, streamID: streamID)
        let state = streams.removeValue(forKey: key)
        let wasFinalized = finalizedStreams.contains(key)
        rememberFinalized(key)

        guard let final = completion.message else {
            guard let state else { return wasFinalized ? .duplicate : .ignored }
            var items = messages[chatID, default: []]
            if let index = items.firstIndex(where: { $0.id == state.itemID }) {
                items[index].isStreaming = false
                messages[chatID] = items
                return .applied
            }
            return .ignored
        }

        let item: ChatItem
        switch final {
        case .rich(let rich):
            let sanitized = capabilities.richContentV1 ? capabilities.sanitize(rich) : nil
            let blocks = sanitized?.blocks
                ?? LegacyContentAdapter.blocks(text: LegacyContentAdapter.plainText(for: rich.blocks), role: rich.role)
            item = ChatItem(
                id: rich.id,
                chatID: rich.chatID,
                role: rich.role,
                text: LegacyContentAdapter.plainText(for: blocks),
                blocks: blocks
            )
        case .legacy(let legacy):
            let finalChatID = legacy.sessionKey ?? chatID
            let blocks = LegacyContentAdapter.content(for: legacy, capabilities: capabilities)
            item = ChatItem(
                id: legacy.id,
                chatID: finalChatID,
                role: legacy.role,
                text: LegacyContentAdapter.plainText(for: blocks),
                blocks: blocks
            )
        }

        var items = messages[item.chatID, default: []]
        if let stableIndex = items.firstIndex(where: { $0.id == item.id }) {
            items[stableIndex] = item
            if let temporaryID = state?.itemID, temporaryID != item.id {
                items.removeAll { $0.id == temporaryID }
            }
            messages[item.chatID] = items
            return wasFinalized ? .duplicate : .applied
        }
        if let temporaryID = state?.itemID,
           let temporaryIndex = items.firstIndex(where: { $0.id == temporaryID }) {
            items[temporaryIndex] = item
        } else {
            items.append(item)
        }
        messages[item.chatID] = items
        return .applied
    }

    private mutating func upsert(_ item: ChatItem, stableID: String?,
                                 in messages: inout [String: [ChatItem]]) -> ChatReconciliationResult {
        var items = messages[item.chatID, default: []]
        if let stableID, let index = items.firstIndex(where: { $0.id == stableID }) {
            let duplicate = items[index] == item
            items[index] = item
            messages[item.chatID] = items
            return duplicate ? .duplicate : .applied
        }
        items.append(item)
        messages[item.chatID] = items
        return .applied
    }

    private mutating func rememberFinalized(_ key: StreamKey) {
        guard finalizedStreams.insert(key).inserted else { return }
        finalizedOrder.append(key)
        if finalizedOrder.count > ZiggyProtocolLimits.maxRememberedStreams {
            finalizedStreams.remove(finalizedOrder.removeFirst())
        }
    }

    private func trimSignatures(_ signatures: inout [Int: UInt64]) {
        guard signatures.count > ZiggyProtocolLimits.maxTrackedStreamSequences else { return }
        for sequence in signatures.keys.sorted().prefix(
            signatures.count - ZiggyProtocolLimits.maxTrackedStreamSequences
        ) {
            signatures.removeValue(forKey: sequence)
        }
    }

    private static func signature(_ text: String) -> UInt64 {
        var value: UInt64 = 14_695_981_039_346_656_037
        for byte in text.utf8 {
            value ^= UInt64(byte)
            value &*= 1_099_511_628_211
        }
        return value
    }
}
