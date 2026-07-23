import Foundation

public enum RichContentVersion: String, Codable, Hashable, Sendable {
    case v1 = "1"
}

public enum RichContentLimits {
    public static let maxBlockTextBytes = 512 * 1024
    public static let maxMessageContentBytes = 1024 * 1024
    public static let maxMessageIDBytes = 256
    public static let maxChatIDBytes = 256
    public static let maxBlockTypeBytes = 128
    public static let maxLanguageBytes = 128
    public static let maxTaskItems = 128
    public static let maxTaskIDBytes = 128
    public static let maxTaskTextBytes = 32 * 1024
    public static let maxTaskTitleBytes = 512
    public static let maxAttributionBytes = 512
    public static let maxChartSeriesNameBytes = 256
    public static let maxMermaidSourceBytes = 128 * 1024
    public static let maxMermaidTitleBytes = 512
    public static let maxToolFieldBytes = 512
    public static let maxTableRows = 100
    public static let maxTableColumns = 20
    public static let maxTableCells = 2_000
    public static let maxChartSeries = 12
    public static let maxChartPoints = 500
    public static let maxChartLabelBytes = 256
    public static let maxMessageBlocks = 128
    public static let maxMediaIDBytes = 256
    public static let maxMediaURLBytes = 2_048
    public static let maxMediaTypeBytes = 128
    public static let maxMediaNameBytes = 1_024
    public static let maxMediaAltBytes = 4_096
    public static let maxMediaDimension = 32_768
    public static let maxMediaPixels = 100_000_000
    public static let maxFileSizeBytes: Int64 = 2 * 1_024 * 1_024 * 1_024
}

public struct RichContentMessage: Codable, Hashable, Sendable {
    public let version: RichContentVersion
    public let id: String
    public let chatID: String
    public let role: MessageRole
    public let blocks: [RichBlock]
    public let createdAt: ZiggyTimestamp?

    public init(version: RichContentVersion = .v1, id: String, chatID: String,
                role: MessageRole, blocks: [RichBlock], createdAt: ZiggyTimestamp? = nil) {
        self.version = version
        self.id = id
        self.chatID = chatID
        self.role = role
        self.blocks = blocks
        self.createdAt = createdAt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(RichContentVersion.self, forKey: .version)
        let id = try c.decode(String.self, forKey: .id)
        let chatID = try c.decode(String.self, forKey: .chatID)
        guard !id.isEmpty, id.utf8.count <= RichContentLimits.maxMessageIDBytes else {
            throw RichContentDecodingError.invalid("message id")
        }
        guard !chatID.isEmpty, chatID.utf8.count <= RichContentLimits.maxChatIDBytes else {
            throw RichContentDecodingError.invalid("chat id")
        }
        self.id = id
        self.chatID = chatID
        role = try c.decode(MessageRole.self, forKey: .role)
        let rawBlocks = try c.decode([JSONValue].self, forKey: .blocks)
        blocks = try Self.decodeBlocks(rawBlocks)
        createdAt = try c.decodeIfPresent(ZiggyTimestamp.self, forKey: .createdAt)
    }

    static func decodeBlocks(_ rawBlocks: [JSONValue]) throws -> [RichBlock] {
        guard rawBlocks.count <= RichContentLimits.maxMessageBlocks else {
            throw RichContentDecodingError.limit("message blocks")
        }
        guard rawBlocks.reduce(0, { $0 + $1.aggregateStringBytes }) <= RichContentLimits.maxMessageContentBytes else {
            throw RichContentDecodingError.limit("message content")
        }
        let blocks = rawBlocks.map { rawBlock in
            guard let data = try? JSONEncoder().encode(rawBlock) else {
                return RichBlock.unsupported(type: "malformed", payload: rawBlock.boundedForUnsupportedContent())
            }
            do {
                // Keep direct RichBlock decoding strict. The envelope is the
                // compatibility boundary that isolates malformed neighbors.
                return try JSONDecoder.ziggy.decode(RichBlock.self, from: data)
            } catch {
                let type = (rawBlock.objectValue?["type"]?.stringValue ?? "malformed")
                    .ziggyTruncatedUTF8(maxBytes: RichContentLimits.maxBlockTypeBytes)
                return .unsupported(type: type, payload: rawBlock.boundedForUnsupportedContent())
            }
        }
        guard blocks.reduce(0, { $0 + $1.decodedContentByteCount }) <= RichContentLimits.maxMessageContentBytes else {
            throw RichContentDecodingError.limit("message content")
        }
        return blocks
    }

    enum CodingKeys: String, CodingKey {
        case version, id, role, blocks
        case chatID = "chat_id"
        case createdAt = "created_at"
    }
}

public struct MarkdownBlock: Codable, Hashable, Sendable {
    public let text: String

    public init(text: String) {
        self.text = text
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let text = try c.decode(String.self, forAny: ["text"])
        guard text.utf8.count <= RichContentLimits.maxBlockTextBytes else {
            throw RichContentDecodingError.limit("markdown text")
        }
        self.text = text
    }
}

public struct CodeBlock: Codable, Hashable, Sendable {
    public let language: String?
    public let code: String

    public init(language: String? = nil, code: String) {
        self.language = language
        self.code = code
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let language = try c.decodeIfPresent(String.self, forAny: ["language", "lang"])
        let code = try c.decode(String.self, forAny: ["code", "text"])
        guard (language ?? "").utf8.count <= RichContentLimits.maxLanguageBytes else {
            throw RichContentDecodingError.limit("code language")
        }
        guard code.utf8.count <= RichContentLimits.maxBlockTextBytes else {
            throw RichContentDecodingError.limit("code text")
        }
        self.language = language
        self.code = code
    }
}

public struct TableBlock: Codable, Hashable, Sendable {
    public let columns: [String]
    public let rows: [[String]]
    public let caption: String?

    public init(columns: [String], rows: [[String]], caption: String? = nil) throws {
        try Self.validate(columns: columns, rows: rows, caption: caption)
        self.columns = columns
        self.rows = rows
        self.caption = caption
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let columns = try c.decode([String].self, forAny: ["columns", "headers"])
        let rows = try c.decode([[String]].self, forAny: ["rows", "data"])
        let caption = try c.decodeIfPresent(String.self, forAny: ["caption", "title"])
        try Self.validate(columns: columns, rows: rows, caption: caption)
        self.columns = columns
        self.rows = rows
        self.caption = caption
    }

    private static func validate(columns: [String], rows: [[String]], caption: String?) throws {
        guard !columns.isEmpty, columns.count <= RichContentLimits.maxTableColumns else {
            throw RichContentDecodingError.limit("table columns")
        }
        guard rows.count <= RichContentLimits.maxTableRows,
              rows.count * columns.count <= RichContentLimits.maxTableCells else {
            throw RichContentDecodingError.limit("table rows")
        }
        guard rows.allSatisfy({ $0.count <= columns.count }) else {
            throw RichContentDecodingError.invalid("table row has too many cells")
        }
        let strings = columns + rows.flatMap { $0 } + (caption.map { [$0] } ?? [])
        guard strings.allSatisfy({ $0.utf8.count <= RichContentLimits.maxBlockTextBytes }) else {
            throw RichContentDecodingError.limit("table cell text")
        }
    }
}

public struct TaskItem: Codable, Hashable, Sendable, Identifiable {
    public let id: String
    public let text: String
    public let isCompleted: Bool

    public init(id: String, text: String, isCompleted: Bool = false) {
        self.id = id
        self.text = text
        self.isCompleted = isCompleted
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let id = try c.decode(String.self, forAny: ["id"])
        let text = try c.decode(String.self, forAny: ["text"])
        let isCompleted = try c.decodeIfPresent(Bool.self, forAny: ["completed", "is_completed"]) ?? false
        guard !id.isEmpty, id.utf8.count <= RichContentLimits.maxTaskIDBytes else {
            throw RichContentDecodingError.invalid("task item id")
        }
        guard text.utf8.count <= RichContentLimits.maxTaskTextBytes else {
            throw RichContentDecodingError.limit("task item text")
        }
        self.id = id
        self.text = text
        self.isCompleted = isCompleted
    }

    enum CodingKeys: String, CodingKey {
        case id, text
        case isCompleted = "completed"
    }
}

public struct TaskListBlock: Codable, Hashable, Sendable {
    public let items: [TaskItem]
    public let title: String?

    public init(items: [TaskItem], title: String? = nil) {
        self.items = items
        self.title = title
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let items = try c.decode([TaskItem].self, forAny: ["items", "tasks"])
        let title = try c.decodeIfPresent(String.self, forAny: ["title"])
        guard items.count <= RichContentLimits.maxTaskItems else {
            throw RichContentDecodingError.limit("task items")
        }
        guard Set(items.map(\.id)).count == items.count else {
            throw RichContentDecodingError.invalid("duplicate task item id")
        }
        guard (title ?? "").utf8.count <= RichContentLimits.maxTaskTitleBytes else {
            throw RichContentDecodingError.limit("task list title")
        }
        self.items = items
        self.title = title
    }
}

public struct QuoteBlock: Codable, Hashable, Sendable {
    public let text: String
    public let attribution: String?

    public init(text: String, attribution: String? = nil) {
        self.text = text
        self.attribution = attribution
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let text = try c.decode(String.self, forAny: ["text"])
        let attribution = try c.decodeIfPresent(String.self, forAny: ["attribution", "author"])
        guard text.utf8.count <= RichContentLimits.maxBlockTextBytes else {
            throw RichContentDecodingError.limit("quote text")
        }
        guard (attribution ?? "").utf8.count <= RichContentLimits.maxAttributionBytes else {
            throw RichContentDecodingError.limit("quote attribution")
        }
        self.text = text
        self.attribution = attribution
    }
}

public enum MediaSource: Codable, Hashable, Sendable {
    case authenticatedAsset(id: String, expiresAt: ZiggyTimestamp?)
    case allowlistedURL(URL)
    case localFile(id: String)

    enum CodingKeys: String, CodingKey { case type, id, expiresAt = "expires_at", url }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .type) {
        case "authenticated_asset":
            let id = try c.decode(String.self, forKey: .id)
            try Self.validateID(id)
            self = .authenticatedAsset(
                id: id,
                expiresAt: try c.decodeIfPresent(ZiggyTimestamp.self, forKey: .expiresAt)
            )
        case "allowlisted_url":
            let url = try c.decode(URL.self, forKey: .url)
            guard url.scheme?.lowercased() == "https",
                  url.host?.isEmpty == false,
                  url.user == nil,
                  url.password == nil,
                  url.absoluteString.utf8.count <= RichContentLimits.maxMediaURLBytes else {
                throw RichContentDecodingError.invalid("media URL scheme")
            }
            self = .allowlistedURL(url)
        case "local_file":
            let id = try c.decode(String.self, forKey: .id)
            try Self.validateID(id)
            self = .localFile(id: id)
        default:
            throw RichContentDecodingError.invalid("media source type")
        }
    }

    private static func validateID(_ id: String) throws {
        guard !id.isEmpty, id.utf8.count <= RichContentLimits.maxMediaIDBytes else {
            throw RichContentDecodingError.invalid("media id")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .authenticatedAsset(let id, let expiresAt):
            try c.encode("authenticated_asset", forKey: .type)
            try c.encode(id, forKey: .id)
            try c.encodeIfPresent(expiresAt, forKey: .expiresAt)
        case .allowlistedURL(let url):
            try c.encode("allowlisted_url", forKey: .type)
            try c.encode(url, forKey: .url)
        case .localFile(let id):
            try c.encode("local_file", forKey: .type)
            try c.encode(id, forKey: .id)
        }
    }
}

public struct MediaBlock: Codable, Hashable, Sendable {
    public let source: MediaSource
    public let mediaType: String
    public let name: String?
    public let width: Int?
    public let height: Int?
    public let alt: String?

    public init(source: MediaSource, mediaType: String, name: String? = nil,
                width: Int? = nil, height: Int? = nil, alt: String? = nil) {
        self.source = source
        self.mediaType = mediaType
        self.name = name
        self.width = width
        self.height = height
        self.alt = alt
    }

    enum CodingKeys: String, CodingKey {
        case source
        case mediaType = "media_type"
        case name, width, height, alt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let source = try c.decode(MediaSource.self, forKey: .source)
        let mediaType = try c.decode(String.self, forKey: .mediaType)
        let name = try c.decodeIfPresent(String.self, forKey: .name)
        let width = try c.decodeIfPresent(Int.self, forKey: .width)
        let height = try c.decodeIfPresent(Int.self, forKey: .height)
        let alt = try c.decodeIfPresent(String.self, forKey: .alt)
        try Self.validate(mediaType: mediaType, name: name, width: width, height: height, alt: alt)
        self.source = source
        self.mediaType = mediaType
        self.name = name
        self.width = width
        self.height = height
        self.alt = alt
    }

    private static func validate(mediaType: String, name: String?, width: Int?, height: Int?, alt: String?) throws {
        guard RichContentStructuralValidation.isValidMediaType(mediaType) else {
            throw RichContentDecodingError.invalid("media type")
        }
        guard (name ?? "").utf8.count <= RichContentLimits.maxMediaNameBytes,
              (alt ?? "").utf8.count <= RichContentLimits.maxMediaAltBytes else {
            throw RichContentDecodingError.limit("media metadata")
        }
        guard [width, height].compactMap({ $0 }).allSatisfy({
            $0 > 0 && $0 <= RichContentLimits.maxMediaDimension
        }) else {
            throw RichContentDecodingError.invalid("media dimensions")
        }
        if let width, let height,
           width > RichContentLimits.maxMediaPixels / height {
            throw RichContentDecodingError.limit("media pixels")
        }
    }
}

public struct ChartPoint: Codable, Hashable, Sendable {
    public let label: String
    public let value: Double

    public init(label: String, value: Double) throws {
        guard value.isFinite else { throw RichContentDecodingError.invalid("chart value") }
        guard label.utf8.count <= RichContentLimits.maxChartLabelBytes else {
            throw RichContentDecodingError.limit("chart label")
        }
        self.label = label
        self.value = value
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let label: String
        if let value = try c.decodeIfPresent(String.self, forAny: ["label", "x", "name"]) {
            label = value
        } else {
            label = try c.decodeIfPresent(String.self, forAny: ["category"]) ?? ""
        }
        try self.init(label: label, value: c.decode(Double.self, forAny: ["value", "y"]))
    }
}

public struct ChartSeries: Codable, Hashable, Sendable {
    public let name: String
    public let points: [ChartPoint]

    public init(name: String, points: [ChartPoint]) {
        self.name = name
        self.points = points
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let name = try c.decode(String.self, forAny: ["name", "label"])
        let points = try c.decode([ChartPoint].self, forAny: ["points", "data"])
        guard name.utf8.count <= RichContentLimits.maxChartSeriesNameBytes else {
            throw RichContentDecodingError.limit("chart series name")
        }
        self.name = name
        self.points = points
    }
}

public struct ChartBlock: Codable, Hashable, Sendable {
    public enum ChartType: String, Codable, Hashable, Sendable {
        case line, bar, area
    }

    public let chartType: ChartType
    public let title: String?
    public let series: [ChartSeries]

    enum CodingKeys: String, CodingKey {
        case chartType = "chart_type"
        case title, series
    }

    public init(chartType: ChartType, title: String? = nil, series: [ChartSeries]) throws {
        try Self.validate(series: series, title: title)
        self.chartType = chartType
        self.title = title
        self.series = series
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let chartType = try c.decode(ChartType.self, forAny: ["chart_type", "type"])
        let title = try c.decodeIfPresent(String.self, forAny: ["title"])
        let series = try c.decodeIfPresent([ChartSeries].self, forAny: ["series"]) ?? []
        try Self.validate(series: series, title: title)
        self.chartType = chartType
        self.title = title
        self.series = series
    }

    private static func validate(series: [ChartSeries], title: String?) throws {
        guard series.count <= RichContentLimits.maxChartSeries else {
            throw RichContentDecodingError.limit("chart series")
        }
        guard series.allSatisfy({ $0.points.count <= RichContentLimits.maxChartPoints }) else {
            throw RichContentDecodingError.limit("chart points")
        }
        let totalPoints = series.reduce(0) { $0 + $1.points.count }
        guard totalPoints <= RichContentLimits.maxChartPoints else {
            throw RichContentDecodingError.limit("chart points")
        }
        guard (title ?? "").utf8.count <= RichContentLimits.maxBlockTextBytes else {
            throw RichContentDecodingError.limit("chart title")
        }
    }
}

public struct MermaidBlock: Codable, Hashable, Sendable {
    public let source: String
    public let title: String?

    public init(source: String, title: String? = nil) {
        self.source = source
        self.title = title
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let source = try c.decode(String.self, forAny: ["source", "code"])
        let title = try c.decodeIfPresent(String.self, forAny: ["title"])
        guard source.utf8.count <= RichContentLimits.maxMermaidSourceBytes else {
            throw RichContentDecodingError.limit("Mermaid source")
        }
        guard (title ?? "").utf8.count <= RichContentLimits.maxMermaidTitleBytes else {
            throw RichContentDecodingError.limit("Mermaid title")
        }
        self.source = source
        self.title = title
    }

    enum CodingKeys: String, CodingKey { case source, title }
}

public struct FileBlock: Codable, Hashable, Sendable {
    public let id: String
    public let name: String
    public let mediaType: String?
    public let sizeBytes: Int64?

    public init(id: String, name: String, mediaType: String? = nil, sizeBytes: Int64? = nil) {
        self.id = id
        self.name = name
        self.mediaType = mediaType
        self.sizeBytes = sizeBytes
    }

    enum CodingKeys: String, CodingKey {
        case id, name
        case mediaType = "media_type"
        case sizeBytes = "size_bytes"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let id = try c.decode(String.self, forKey: .id)
        let name = try c.decode(String.self, forKey: .name)
        let mediaType = try c.decodeIfPresent(String.self, forKey: .mediaType)
        let sizeBytes = try c.decodeIfPresent(Int64.self, forKey: .sizeBytes)
        guard !id.isEmpty, id.utf8.count <= RichContentLimits.maxMediaIDBytes else {
            throw RichContentDecodingError.invalid("file id")
        }
        guard !name.isEmpty, name.utf8.count <= RichContentLimits.maxMediaNameBytes else {
            throw RichContentDecodingError.invalid("file name")
        }
        guard mediaType.map(RichContentStructuralValidation.isValidMediaType) ?? true else {
            throw RichContentDecodingError.invalid("file media type")
        }
        guard sizeBytes.map({ $0 >= 0 && $0 <= RichContentLimits.maxFileSizeBytes }) ?? true else {
            throw RichContentDecodingError.limit("file size")
        }
        self.id = id
        self.name = name
        self.mediaType = mediaType
        self.sizeBytes = sizeBytes
    }
}

public struct ProgressBlock: Codable, Hashable, Sendable {
    public let text: String
    public let status: String?

    public init(text: String, status: String? = nil) {
        self.text = text
        self.status = status
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let text = try c.decode(String.self, forAny: ["text"])
        let status = try c.decodeIfPresent(String.self, forAny: ["status"])
        guard text.utf8.count <= RichContentLimits.maxBlockTextBytes else {
            throw RichContentDecodingError.limit("progress text")
        }
        guard (status ?? "").utf8.count <= RichContentLimits.maxToolFieldBytes else {
            throw RichContentDecodingError.limit("progress status")
        }
        self.text = text
        self.status = status
    }
}

public struct ToolBlock: Codable, Hashable, Sendable {
    public let name: String?
    public let text: String
    public let status: String?

    public init(name: String? = nil, text: String, status: String? = nil) {
        self.name = name
        self.text = text
        self.status = status
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        let name = try c.decodeIfPresent(String.self, forAny: ["name"])
        let text = try c.decode(String.self, forAny: ["text"])
        let status = try c.decodeIfPresent(String.self, forAny: ["status"])
        guard (name ?? "").utf8.count <= RichContentLimits.maxToolFieldBytes else {
            throw RichContentDecodingError.limit("tool name")
        }
        guard text.utf8.count <= RichContentLimits.maxBlockTextBytes else {
            throw RichContentDecodingError.limit("tool text")
        }
        guard (status ?? "").utf8.count <= RichContentLimits.maxToolFieldBytes else {
            throw RichContentDecodingError.limit("tool status")
        }
        self.name = name
        self.text = text
        self.status = status
    }
}

public enum RichBlock: Codable, Hashable, Sendable {
    case markdown(MarkdownBlock)
    case code(CodeBlock)
    case table(TableBlock)
    case taskList(TaskListBlock)
    case quote(QuoteBlock)
    case divider
    case media(MediaBlock)
    case chart(ChartBlock)
    case mermaid(MermaidBlock)
    case file(FileBlock)
    case progress(ProgressBlock)
    case tool(ToolBlock)
    case unsupported(type: String, payload: JSONValue)

    enum CodingKeys: String, CodingKey { case type }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let type = try c.decode(String.self, forKey: .type)
        guard !type.isEmpty, type.utf8.count <= RichContentLimits.maxBlockTypeBytes else {
            throw RichContentDecodingError.invalid("block type")
        }
        switch type {
        case "markdown": self = .markdown(try MarkdownBlock(from: decoder))
        case "code": self = .code(try CodeBlock(from: decoder))
        case "table": self = .table(try TableBlock(from: decoder))
        case "task_list": self = .taskList(try TaskListBlock(from: decoder))
        case "quote": self = .quote(try QuoteBlock(from: decoder))
        case "divider": self = .divider
        case "media": self = .media(try MediaBlock(from: decoder))
        case "chart": self = .chart(try ChartBlock(from: decoder))
        case "mermaid": self = .mermaid(try MermaidBlock(from: decoder))
        case "file": self = .file(try FileBlock(from: decoder))
        case "progress": self = .progress(try ProgressBlock(from: decoder))
        case "tool": self = .tool(try ToolBlock(from: decoder))
        default:
            let payload = ((try? JSONValue(from: decoder)) ?? .object([:])).boundedForUnsupportedContent()
            self = .unsupported(type: type, payload: payload)
        }
    }

    public func encode(to encoder: Encoder) throws {
        switch self {
        case .unsupported(let type, let payload):
            var object = payload.objectValue ?? [:]
            object["type"] = .string(type)
            try JSONValue.object(object).encode(to: encoder)
        case .markdown(let value): try Self.encodeKnown(value, type: "markdown", to: encoder)
        case .code(let value): try Self.encodeKnown(value, type: "code", to: encoder)
        case .table(let value): try Self.encodeKnown(value, type: "table", to: encoder)
        case .taskList(let value): try Self.encodeKnown(value, type: "task_list", to: encoder)
        case .quote(let value): try Self.encodeKnown(value, type: "quote", to: encoder)
        case .divider:
            var c = encoder.container(keyedBy: AnyCodingKey.self)
            try c.encode("divider", forKey: AnyCodingKey("type"))
        case .media(let value): try Self.encodeKnown(value, type: "media", to: encoder)
        case .chart(let value): try Self.encodeKnown(value, type: "chart", to: encoder)
        case .mermaid(let value): try Self.encodeKnown(value, type: "mermaid", to: encoder)
        case .file(let value): try Self.encodeKnown(value, type: "file", to: encoder)
        case .progress(let value): try Self.encodeKnown(value, type: "progress", to: encoder)
        case .tool(let value): try Self.encodeKnown(value, type: "tool", to: encoder)
        }
    }

    private static func encodeKnown<Value: Encodable>(_ value: Value, type: String, to encoder: Encoder) throws {
        let data = try JSONEncoder().encode(value)
        var object = try JSONDecoder().decode(JSONValue.self, from: data).objectValue ?? [:]
        object["type"] = .string(type)
        try JSONValue.object(object).encode(to: encoder)
    }

    public var plainText: String {
        switch self {
        case .markdown(let value): value.text
        case .code(let value): value.code
        case .table(let value): ([value.columns] + value.rows).map { $0.joined(separator: " | ") }.joined(separator: "\n")
        case .taskList(let value): value.items.map { "\($0.isCompleted ? "[x]" : "[ ]") \($0.text)" }.joined(separator: "\n")
        case .quote(let value): value.text
        case .divider: ""
        case .media(let value): value.alt ?? value.name ?? "Media attachment"
        case .chart(let value): value.title ?? "Chart"
        case .mermaid(let value): value.title ?? "Diagram"
        case .file(let value): value.name
        case .progress(let value): value.text
        case .tool(let value): value.text
        case .unsupported(let type, _): "Content unavailable: \(type)"
        }
    }
}

public enum RichContentDecodingError: Error, Equatable, Sendable {
    case invalid(String)
    case limit(String)
}

private enum RichContentStructuralValidation {
    static func isValidMediaType(_ value: String) -> Bool {
        !value.isEmpty
            && value.utf8.count <= RichContentLimits.maxMediaTypeBytes
            && value.range(
                of: #"^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$"#,
                options: .regularExpression
            ) != nil
    }
}

public enum RichContentSafety {
    /// Structured Markdown is used only when the source contains neither raw
    /// HTML nor Markdown image attachments. Those inputs stay literal text.
    public static func allowsStructuredMarkdown(_ markdown: String) -> Bool {
        let htmlPattern = #"(?is)<\s*(?:/\s*)?[a-z][^>]*>|<!--|<!DOCTYPE|<\?xml"#
        let imagePattern = #"!\s*\["#
        return markdown.range(of: htmlPattern, options: .regularExpression) == nil
            && markdown.range(of: imagePattern, options: .regularExpression) == nil
    }
}

extension JSONValue {
    var objectValue: [String: JSONValue]? {
        guard case .object(let value) = self else { return nil }
        return value
    }
}
