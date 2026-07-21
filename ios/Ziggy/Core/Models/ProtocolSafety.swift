import Foundation

public enum ZiggyProtocolLimits {
    public static let maxRESTResponseBytes = 4 * 1024 * 1024
    public static let maxWebSocketFrameBytes = 2 * 1024 * 1024
    public static let maxSSEEventBytes = 1024 * 1024
    public static let maxSSELineBytes = 256 * 1024
    public static let maxStreamTextBytes = 1024 * 1024
    public static let maxDeltaTextBytes = 64 * 1024
    public static let maxLegacyTextBytes = 1024 * 1024
    public static let maxUnsupportedPayloadBytes = 16 * 1024
    public static let maxUnsupportedDepth = 8
    public static let maxUnsupportedCollectionItems = 64
    public static let maxUnsupportedNodes = 256
    public static let maxTrackedStreamSequences = 4_096
    public static let maxActiveStreams = 32
    public static let maxRememberedStreams = 256
}

public struct RichContentCapabilities: Hashable, Sendable {
    public let richContentV1: Bool
    public let mediaV1: Bool
    public let mermaidV1: Bool

    public static let legacyOnly = RichContentCapabilities(
        richContentV1: false,
        mediaV1: false,
        mermaidV1: false
    )

    public init(richContentV1: Bool, mediaV1: Bool = false, mermaidV1: Bool = false) {
        self.richContentV1 = richContentV1
        self.mediaV1 = richContentV1 && mediaV1
        self.mermaidV1 = richContentV1 && mermaidV1
    }

    public init(advertised: [String: JSONValue]?) {
        let advertised = advertised ?? [:]
        let richValue = advertised["rich_content_v1"]
        let rich = Self.isEnabled(richValue)
        let nested = richValue?.objectValue
        self.init(
            richContentV1: rich,
            mediaV1: Self.isEnabled(advertised["rich_media_v1"])
                || Self.isEnabled(advertised["media_v1"])
                || Self.isEnabled(advertised["rich_content_media_v1"])
                || Self.isEnabled(nested?["media"]),
            mermaidV1: Self.isEnabled(advertised["mermaid_v1"])
                || Self.isEnabled(advertised["rich_mermaid_v1"])
                || Self.isEnabled(advertised["rich_content_mermaid_v1"])
                || Self.isEnabled(nested?["mermaid"])
        )
    }

    private static func isEnabled(_ value: JSONValue?) -> Bool {
        switch value {
        case .boolean(true), .number(1): true
        case .string(let value): ["1", "true", "enabled", "v1"].contains(value.lowercased())
        case .object(let value): isEnabled(value["enabled"])
            || isEnabled(value["supported"])
            || value["version"]?.stringValue == RichContentVersion.v1.rawValue
        case .array(let values): values.contains { isEnabled($0) }
        default: false
        }
    }

    public func sanitize(_ message: RichContentMessage) -> RichContentMessage {
        RichContentMessage(
            version: message.version,
            id: message.id,
            chatID: message.chatID,
            role: message.role,
            blocks: message.blocks.map(sanitize),
            createdAt: message.createdAt
        )
    }

    public func sanitize(_ block: RichBlock) -> RichBlock {
        switch block {
        case .media where !mediaV1:
            .unsupported(type: "media", payload: .object(["disabled": .boolean(true)]))
        case .mermaid where !mermaidV1:
            .unsupported(type: "mermaid", payload: .object(["disabled": .boolean(true)]))
        case .unsupported(let type, let payload):
            .unsupported(
                type: type.ziggyTruncatedUTF8(maxBytes: RichContentLimits.maxBlockTypeBytes),
                payload: payload.boundedForUnsupportedContent()
            )
        default:
            block
        }
    }
}

extension JSONValue {
    func boundedForUnsupportedContent() -> JSONValue {
        var budget = JSONTruncationBudget(
            stringBytes: ZiggyProtocolLimits.maxUnsupportedPayloadBytes,
            nodes: ZiggyProtocolLimits.maxUnsupportedNodes
        )
        return bounded(depth: 0, budget: &budget)
    }

    var aggregateStringBytes: Int {
        switch self {
        case .string(let value): value.utf8.count
        case .object(let value):
            value.reduce(0) { $0 + $1.key.utf8.count + $1.value.aggregateStringBytes }
        case .array(let value): value.reduce(0) { $0 + $1.aggregateStringBytes }
        case .number, .boolean, .null: 0
        }
    }

    private func bounded(depth: Int, budget: inout JSONTruncationBudget) -> JSONValue {
        guard budget.nodes > 0 else { return .null }
        budget.nodes -= 1
        guard depth < ZiggyProtocolLimits.maxUnsupportedDepth else {
            return .null
        }

        switch self {
        case .string(let value):
            guard budget.stringBytes > 0 else { return .null }
            let bounded = value.ziggyTruncatedUTF8(maxBytes: budget.stringBytes)
            budget.stringBytes -= min(budget.stringBytes, bounded.utf8.count)
            return .string(bounded)
        case .object(let value):
            var result: [String: JSONValue] = [:]
            for key in value.keys.sorted().prefix(ZiggyProtocolLimits.maxUnsupportedCollectionItems) {
                guard budget.nodes > 0 else { break }
                let boundedKey = key.ziggyTruncatedUTF8(maxBytes: 256)
                guard boundedKey.utf8.count <= budget.stringBytes else { break }
                budget.stringBytes -= boundedKey.utf8.count
                result[boundedKey] = value[key]?.bounded(depth: depth + 1, budget: &budget) ?? .null
            }
            if result.count < value.count, budget.stringBytes >= "_truncated".utf8.count {
                budget.stringBytes -= "_truncated".utf8.count
                result["_truncated"] = .boolean(true)
            }
            return .object(result)
        case .array(let value):
            var result: [JSONValue] = []
            for item in value.prefix(ZiggyProtocolLimits.maxUnsupportedCollectionItems) {
                guard budget.nodes > 0 else { break }
                result.append(item.bounded(depth: depth + 1, budget: &budget))
            }
            if result.count < value.count, budget.stringBytes >= "[truncated]".utf8.count {
                budget.stringBytes -= "[truncated]".utf8.count
                result.append(.string("[truncated]"))
            }
            return .array(result)
        case .number, .boolean, .null:
            return self
        }
    }
}

private struct JSONTruncationBudget {
    var stringBytes: Int
    var nodes: Int
}

extension String {
    func ziggyTruncatedUTF8(maxBytes: Int) -> String {
        guard maxBytes > 0 else { return "" }
        guard utf8.count > maxBytes else { return self }
        let marker = "..."
        let contentLimit = max(0, maxBytes - marker.utf8.count)
        var result = ""
        var used = 0
        for character in self {
            let string = String(character)
            let count = string.utf8.count
            guard used + count <= contentLimit else { break }
            result.append(character)
            used += count
        }
        return result + marker
    }
}

extension RichBlock {
    var decodedContentByteCount: Int {
        switch self {
        case .markdown(let value): value.text.utf8.count
        case .code(let value): (value.language?.utf8.count ?? 0) + value.code.utf8.count
        case .table(let value):
            value.columns.reduce(0) { $0 + $1.utf8.count }
                + value.rows.flatMap { $0 }.reduce(0) { $0 + $1.utf8.count }
                + (value.caption?.utf8.count ?? 0)
        case .taskList(let value):
            value.items.reduce(0) { $0 + $1.id.utf8.count + $1.text.utf8.count }
                + (value.title?.utf8.count ?? 0)
        case .quote(let value): value.text.utf8.count + (value.attribution?.utf8.count ?? 0)
        case .divider: 0
        case .media(let value):
            value.mediaType.utf8.count + (value.name?.utf8.count ?? 0) + (value.alt?.utf8.count ?? 0)
                + value.source.decodedContentByteCount
        case .chart(let value):
            (value.title?.utf8.count ?? 0) + value.series.reduce(0) { total, series in
                total + series.name.utf8.count + series.points.reduce(0) { $0 + $1.label.utf8.count }
            }
        case .mermaid(let value): value.source.utf8.count + (value.title?.utf8.count ?? 0)
        case .file(let value): value.id.utf8.count + value.name.utf8.count + (value.mediaType?.utf8.count ?? 0)
        case .progress(let value): value.text.utf8.count + (value.status?.utf8.count ?? 0)
        case .tool(let value):
            (value.name?.utf8.count ?? 0) + value.text.utf8.count + (value.status?.utf8.count ?? 0)
        case .unsupported(let type, let payload): type.utf8.count + payload.aggregateStringBytes
        }
    }
}

private extension MediaSource {
    var decodedContentByteCount: Int {
        switch self {
        case .authenticatedAsset(let id, _), .localFile(let id): id.utf8.count
        case .allowlistedURL(let url): url.absoluteString.utf8.count
        }
    }
}
