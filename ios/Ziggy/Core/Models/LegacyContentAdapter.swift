import Foundation

/// Converts the current string/JSON message shapes into the versioned content model.
/// It deliberately does not parse HTML or remove arbitrary tags from assistant text.
enum LegacyContentAdapter {
    static func content(for message: ZiggyMessage,
                        capabilities: RichContentCapabilities = .legacyOnly) -> [RichBlock] {
        if let rich = message.richContent {
            return capabilities.richContentV1
                ? capabilities.sanitize(rich).blocks
                : blocks(text: plainText(for: rich.blocks), role: rich.role)
        }
        if case .structured(let value) = message.content,
           let blocks = decodeBlocks(from: value, capabilities: capabilities) {
            return blocks
        }

        let text = legacyText(from: message.content)
        let kind: String?
        if case .boolean(true) = message.metadata?["_tool_hint"] {
            kind = "tool_hint"
        } else if case .boolean(true) = message.metadata?["_progress"] {
            kind = "progress"
        } else {
            kind = nil
        }
        return blocks(text: text, role: message.role, kind: kind)
    }

    static func blocks(text: String, role: MessageRole = .assistant, kind: String? = nil) -> [RichBlock] {
        let text = text.ziggyTruncatedUTF8(maxBytes: ZiggyProtocolLimits.maxLegacyTextBytes)
        let normalizedKind = kind?.lowercased()
        if normalizedKind == "tool_hint" || role == .tool || normalizedKind == "tool" {
            return [.tool(ToolBlock(text: text))]
        }
        if normalizedKind == "progress" || role == .progress {
            return [.progress(ProgressBlock(text: text))]
        }
        return [.markdown(MarkdownBlock(text: text))]
    }

    static func plainText(for blocks: [RichBlock]) -> String {
        blocks.map(\.plainText).filter { !$0.isEmpty }.joined(separator: "\n\n")
    }

    private static func legacyText(from content: MessageContent) -> String {
        switch content {
        case .text(let value):
            return value
        case .structured(let value):
            if let text = value.objectString(for: ["text", "content", "body", "message"]) {
                return text
            }
            if let blocks = decodeVersionedBlocks(from: value) {
                return plainText(for: blocks)
            }
            guard let data = try? JSONEncoder().encode(value) else { return "" }
            return String(decoding: data, as: UTF8.self)
                .ziggyTruncatedUTF8(maxBytes: ZiggyProtocolLimits.maxUnsupportedPayloadBytes)
        }
    }

    private static func decodeBlocks(from value: JSONValue,
                                     capabilities: RichContentCapabilities) -> [RichBlock]? {
        guard case .object(let object) = value, object["blocks"] != nil,
              let data = try? JSONEncoder().encode(value) else { return nil }

        // Accept only the versioned envelope. An unversioned object remains literal legacy text.
        guard object["version"]?.stringValue == RichContentVersion.v1.rawValue else { return nil }
        guard capabilities.richContentV1 else {
            return blocks(text: decodeVersionedBlocks(from: value).map(plainText(for:)) ?? "Content unavailable")
        }
        if let payload = try? JSONDecoder.ziggy.decode(VersionedBlockPayload.self, from: data) {
            return payload.blocks.map(capabilities.sanitize)
        }
        // A versioned payload that exceeds envelope limits is still typed content;
        // do not turn it back into renderable literal JSON.
        return [.unsupported(type: "message", payload: value.boundedForUnsupportedContent())]
    }

    private static func decodeVersionedBlocks(from value: JSONValue) -> [RichBlock]? {
        guard case .object(let object) = value,
              object["version"]?.stringValue == RichContentVersion.v1.rawValue,
              let data = try? JSONEncoder().encode(value),
              let payload = try? JSONDecoder.ziggy.decode(VersionedBlockPayload.self, from: data) else {
            return nil
        }
        return payload.blocks
    }

    private struct VersionedBlockPayload: Decodable {
        let version: RichContentVersion
        let blocks: [RichBlock]

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: AnyCodingKey.self)
            version = try c.decode(RichContentVersion.self, forAny: ["version"])
            let rawBlocks = try c.decode([JSONValue].self, forAny: ["blocks"])
            blocks = try RichContentMessage.decodeBlocks(rawBlocks)
        }
    }
}
