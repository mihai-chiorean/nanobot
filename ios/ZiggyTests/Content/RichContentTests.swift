import Foundation
import Testing
@testable import Ziggy

@Suite
struct RichContentTests {
    @Test
    func `V1 decodes every block type`() throws {
        let json = #"""
        {
          "version":"1", "id":"m-1", "chat_id":"chat-1", "role":"assistant",
          "blocks":[
            {"type":"markdown","text":"## Result"},
            {"type":"code","language":"swift","code":"let answer = 42"},
            {"type":"table","columns":["Name","Value"],"rows":[["Latency","42ms"]]},
            {"type":"task_list","title":"Ship it","items":[{"id":"task-1","text":"Verify","completed":true}]},
            {"type":"quote","text":"Keep it small","attribution":"Review"},
            {"type":"divider"},
            {"type":"media","source":{"type":"authenticated_asset","id":"asset-1"},"media_type":"image/png","alt":"Screenshot"},
            {"type":"chart","chart_type":"line","title":"Latency","series":[{"name":"p95","points":[{"label":"A","value":1.5}]}]},
            {"type":"mermaid","source":"graph TD; A-->B"},
            {"type":"file","id":"file-1","name":"report.pdf","media_type":"application/pdf","size_bytes":12},
            {"type":"progress","text":"Checking"},
            {"type":"tool","name":"search","text":"Searching","status":"running"}
          ]
        }
        """#

        let message = try JSONDecoder.ziggy.decode(RichContentMessage.self, from: Data(json.utf8))
        #expect(message.version == .v1)
        #expect(message.blocks.count == 12)
        if case .chart(let chart) = message.blocks[7] {
            #expect(chart.series[0].points[0].value == 1.5)
        } else {
            Issue.record("expected chart block")
        }
        let encoded = try JSONEncoder().encode(message)
        let roundTrip = try JSONDecoder().decode(RichContentMessage.self, from: encoded)
        #expect(roundTrip.blocks == message.blocks)
    }

    @Test
    func `unknown block survives as placeholder and round trips`() throws {
        let data = Data(#"{"type":"future_block","label":"Later","payload":{"answer":3}}"#.utf8)
        let block = try JSONDecoder().decode(RichBlock.self, from: data)
        guard case .unsupported(let type, let payload) = block else {
            Issue.record("unknown block should be preserved")
            return
        }
        #expect(type == "future_block")
        #expect(payload.objectValue?["payload"]?.objectValue?["answer"] == .number(3))
        let encoded = try JSONEncoder().encode(block)
        let object = try #require(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        #expect(object["type"] as? String == "future_block")
    }

    @Test
    func `malformed known block is isolated inside message and legacy payload`() throws {
        let json = #"""
        {"version":"1","id":"m-1","chat_id":"chat-1","role":"assistant","blocks":[
          {"type":"markdown","text":42},
          {"type":"divider"},
          {"type":"markdown","text":"kept"}
        ]}
        """#
        let data = Data(json.utf8)
        let message = try JSONDecoder.ziggy.decode(RichContentMessage.self, from: data)
        #expect(message.blocks.count == 3)
        guard case .unsupported(let type, let payload) = message.blocks[0] else {
            Issue.record("malformed known block should become unsupported")
            return
        }
        #expect(type == "markdown")
        #expect(payload.objectValue?["text"] == .number(42))
        #expect(message.blocks[1] == .divider)
        #expect(message.blocks[2] == .markdown(MarkdownBlock(text: "kept")))

        let structured = JSONValue.object([
            "version": .string("1"),
            "blocks": .array([
                .object(["type": .string("markdown"), "text": .number(42)]),
                .object(["type": .string("markdown"), "text": .string("kept")])
            ])
        ])
        let legacy = ZiggyMessage(id: "legacy", role: .assistant, content: .structured(structured))
        let adapted = LegacyContentAdapter.content(
            for: legacy,
            capabilities: RichContentCapabilities(richContentV1: true)
        )
        guard case .unsupported(let adaptedType, _) = adapted[0] else {
            Issue.record("legacy rich payload should use envelope isolation")
            return
        }
        #expect(adaptedType == "markdown")
        #expect(adapted[1] == .markdown(MarkdownBlock(text: "kept")))
    }

    @Test
    func `message rejects too many blocks at envelope boundary`() throws {
        let blocks = Array(repeating: #"{"type":"divider"}"#, count: RichContentLimits.maxMessageBlocks + 1).joined(separator: ",")
        let json = "{\"version\":\"1\",\"id\":\"m\",\"chat_id\":\"c\",\"role\":\"assistant\",\"blocks\":[\(blocks)]}"
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichContentMessage.self, from: Data(json.utf8))
        }
    }

    @Test
    func `task items reject duplicates and oversized values`() throws {
        let duplicate = #"{"type":"task_list","items":[{"id":"same","text":"one"},{"id":"same","text":"two"}]}"#
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichBlock.self, from: Data(duplicate.utf8))
        }

        let oversizedText = String(repeating: "x", count: RichContentLimits.maxTaskTextBytes + 1)
        let oversized = "{\"type\":\"task_list\",\"items\":[{\"id\":\"one\",\"text\":\"\(oversizedText)\"}]}"
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichBlock.self, from: Data(oversized.utf8))
        }
    }

    @Test
    func `markdown and code reject oversized decoded text`() throws {
        let text = String(repeating: "x", count: RichContentLimits.maxBlockTextBytes + 1)
        let markdown = "{\"type\":\"markdown\",\"text\":\"\(text)\"}"
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichBlock.self, from: Data(markdown.utf8))
        }

        let code = "{\"type\":\"code\",\"language\":\"swift\",\"code\":\"\(text)\"}"
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichBlock.self, from: Data(code.utf8))
        }
    }

    @Test
    func `legacy text maps to markdown without interpreting HTML or images`() throws {
        let raw = "<b>literal</b> **emphasis** ![remote](https://example.test/image.png)"
        let message = ZiggyMessage(id: "m-1", role: .assistant, content: .text(raw))
        let blocks = LegacyContentAdapter.content(for: message)
        guard case .markdown(let markdown) = try #require(blocks.first) else {
            Issue.record("legacy assistant text should become markdown")
            return
        }
        #expect(markdown.text == raw)
        #expect(!RichContentSafety.allowsStructuredMarkdown(raw))
        #expect(RichContentSafety.allowsStructuredMarkdown("**safe** [link](https://example.test)"))
    }

    @Test
    func `legacy progress and tool metadata remain timeline blocks`() {
        let progress = ZiggyMessage(id: "p", role: .assistant, content: .text("Working"), metadata: ["_progress": .boolean(true)])
        let tool = ZiggyMessage(id: "t", role: .assistant, content: .text("read_file"), metadata: ["_tool_hint": .boolean(true)])

        guard case .progress = LegacyContentAdapter.content(for: progress).first else {
            Issue.record("progress metadata should map to progress")
            return
        }
        guard case .tool = LegacyContentAdapter.content(for: tool).first else {
            Issue.record("tool metadata should map to tool")
            return
        }
    }

    @Test
    func `chart rejects non-finite values and limits`() throws {
        #expect(throws: (any Error).self) {
            try ChartPoint(label: "bad", value: .infinity)
        }

        let points = Array(repeating: #"{"label":"x","value":1}"#, count: RichContentLimits.maxChartPoints + 1).joined(separator: ",")
        let jsonString = "{\"type\":\"chart\",\"chart_type\":\"line\",\"series\":[{\"name\":\"too-many\",\"points\":[\(points)]}]}"
        let json = Data(jsonString.utf8)
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichBlock.self, from: json)
        }
    }

    @Test
    func `streaming delta keeps concatenation and typed markdown`() {
        var item = ChatItem(id: "stream-1", chatID: "chat-1", role: .assistant, text: "Hel", isStreaming: true)
        item.append(delta: "lo")
        #expect(item.text == "Hello")
        #expect(item.isStreaming)
        guard case .markdown(let markdown) = item.blocks.first else {
            Issue.record("streaming content should remain markdown")
            return
        }
        #expect(markdown.text == "Hello")
    }

    @Test
    func `aggregate content and media structure are bounded`() throws {
        let half = String(repeating: "x", count: RichContentLimits.maxMessageContentBytes / 2 + 1)
        let aggregate = #"{"version":"1","id":"m","chat_id":"c","role":"assistant","blocks":[{"type":"markdown","text":"\#(half)"},{"type":"markdown","text":"\#(half)"}]}"#
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichContentMessage.self, from: Data(aggregate.utf8))
        }

        let badMedia = #"{"type":"media","source":{"type":"allowlisted_url","url":"https://user:secret@example.test/a"},"media_type":"image/png","width":40000,"height":1}"#
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichBlock.self, from: Data(badMedia.utf8))
        }

        let badFile = #"{"type":"file","id":"f","name":"x","size_bytes":-1}"#
        #expect(throws: (any Error).self) {
            try JSONDecoder().decode(RichBlock.self, from: Data(badFile.utf8))
        }
    }

    @Test
    func `unsupported payload is truncated`() throws {
        let payload = String(repeating: "x", count: ZiggyProtocolLimits.maxUnsupportedPayloadBytes * 2)
        let data = Data("{\"type\":\"future\",\"payload\":\"\(payload)\"}".utf8)
        let block = try JSONDecoder().decode(RichBlock.self, from: data)
        guard case .unsupported(_, let bounded) = block else {
            Issue.record("expected placeholder")
            return
        }
        #expect(bounded.aggregateStringBytes <= ZiggyProtocolLimits.maxUnsupportedPayloadBytes)
    }

    @Test
    func `accessibility models expose table and chart values`() throws {
        let table = try TableBlock(columns: ["Region", "Count"], rows: [["West", "7"]])
        #expect(table.accessibilityRows == ["Row 1, Region: West, Count: 7"])

        let chart = try ChartBlock(
            chartType: .bar,
            title: "Requests",
            series: [ChartSeries(name: "Success", points: [try ChartPoint(label: "Monday", value: 42)])]
        )
        #expect(chart.accessibilityRows.first?.series == "Success")
        #expect(chart.accessibilitySummary.contains("Monday"))
        #expect(chart.accessibilitySummary.contains("42"))
    }
}
