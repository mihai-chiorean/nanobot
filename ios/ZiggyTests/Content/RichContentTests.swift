import Foundation
import XCTest
@testable import Ziggy

final class RichContentTests: XCTestCase {
    func testV1DecodesEveryBlockType() throws {
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
        XCTAssertEqual(message.version, .v1)
        XCTAssertEqual(message.blocks.count, 12)
        if case .chart(let chart) = message.blocks[7] {
            XCTAssertEqual(chart.series[0].points[0].value, 1.5)
        } else {
            XCTFail("expected chart block")
        }
        let encoded = try JSONEncoder().encode(message)
        let roundTrip = try JSONDecoder().decode(RichContentMessage.self, from: encoded)
        XCTAssertEqual(roundTrip.blocks, message.blocks)
    }

    func testUnknownBlockSurvivesAsPlaceholderAndRoundTrips() throws {
        let data = Data(#"{"type":"future_block","label":"Later","payload":{"answer":3}}"#.utf8)
        let block = try JSONDecoder().decode(RichBlock.self, from: data)
        guard case .unsupported(let type, let payload) = block else {
            return XCTFail("unknown block should be preserved")
        }
        XCTAssertEqual(type, "future_block")
        XCTAssertEqual(payload.objectValue?["payload"]?.objectValue?["answer"], .number(3))
        let encoded = try JSONEncoder().encode(block)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        XCTAssertEqual(object["type"] as? String, "future_block")
    }

    func testMalformedKnownBlockIsolatedInsideMessageAndLegacyPayload() throws {
        let json = #"""
        {"version":"1","id":"m-1","chat_id":"chat-1","role":"assistant","blocks":[
          {"type":"markdown","text":42},
          {"type":"divider"},
          {"type":"markdown","text":"kept"}
        ]}
        """#
        let data = Data(json.utf8)
        let message = try JSONDecoder.ziggy.decode(RichContentMessage.self, from: data)
        XCTAssertEqual(message.blocks.count, 3)
        guard case .unsupported(let type, let payload) = message.blocks[0] else {
            return XCTFail("malformed known block should become unsupported")
        }
        XCTAssertEqual(type, "markdown")
        XCTAssertEqual(payload.objectValue?["text"], .number(42))
        XCTAssertEqual(message.blocks[1], .divider)
        XCTAssertEqual(message.blocks[2], .markdown(MarkdownBlock(text: "kept")))

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
            return XCTFail("legacy rich payload should use envelope isolation")
        }
        XCTAssertEqual(adaptedType, "markdown")
        XCTAssertEqual(adapted[1], .markdown(MarkdownBlock(text: "kept")))
    }

    func testMessageRejectsTooManyBlocksAtEnvelopeBoundary() throws {
        let blocks = Array(repeating: #"{"type":"divider"}"#, count: RichContentLimits.maxMessageBlocks + 1).joined(separator: ",")
        let json = "{\"version\":\"1\",\"id\":\"m\",\"chat_id\":\"c\",\"role\":\"assistant\",\"blocks\":[\(blocks)]}"
        XCTAssertThrowsError(try JSONDecoder().decode(RichContentMessage.self, from: Data(json.utf8)))
    }

    func testTaskItemsRejectDuplicatesAndOversizedValues() throws {
        let duplicate = #"{"type":"task_list","items":[{"id":"same","text":"one"},{"id":"same","text":"two"}]}"#
        XCTAssertThrowsError(try JSONDecoder().decode(RichBlock.self, from: Data(duplicate.utf8)))

        let oversizedText = String(repeating: "x", count: RichContentLimits.maxTaskTextBytes + 1)
        let oversized = "{\"type\":\"task_list\",\"items\":[{\"id\":\"one\",\"text\":\"\(oversizedText)\"}]}"
        XCTAssertThrowsError(try JSONDecoder().decode(RichBlock.self, from: Data(oversized.utf8)))
    }

    func testMarkdownAndCodeRejectOversizedDecodedText() throws {
        let text = String(repeating: "x", count: RichContentLimits.maxBlockTextBytes + 1)
        let markdown = "{\"type\":\"markdown\",\"text\":\"\(text)\"}"
        XCTAssertThrowsError(try JSONDecoder().decode(RichBlock.self, from: Data(markdown.utf8)))

        let code = "{\"type\":\"code\",\"language\":\"swift\",\"code\":\"\(text)\"}"
        XCTAssertThrowsError(try JSONDecoder().decode(RichBlock.self, from: Data(code.utf8)))
    }

    func testLegacyTextMapsToMarkdownWithoutInterpretingHTMLOrImages() throws {
        let raw = "<b>literal</b> **emphasis** ![remote](https://example.test/image.png)"
        let message = ZiggyMessage(id: "m-1", role: .assistant, content: .text(raw))
        let blocks = LegacyContentAdapter.content(for: message)
        guard case .markdown(let markdown) = try XCTUnwrap(blocks.first) else {
            return XCTFail("legacy assistant text should become markdown")
        }
        XCTAssertEqual(markdown.text, raw)
        XCTAssertFalse(RichContentSafety.allowsStructuredMarkdown(raw))
        XCTAssertTrue(RichContentSafety.allowsStructuredMarkdown("**safe** [link](https://example.test)"))
    }

    func testLegacyProgressAndToolMetadataRemainTimelineBlocks() {
        let progress = ZiggyMessage(id: "p", role: .assistant, content: .text("Working"), metadata: ["_progress": .boolean(true)])
        let tool = ZiggyMessage(id: "t", role: .assistant, content: .text("read_file"), metadata: ["_tool_hint": .boolean(true)])

        guard case .progress = LegacyContentAdapter.content(for: progress).first else {
            return XCTFail("progress metadata should map to progress")
        }
        guard case .tool = LegacyContentAdapter.content(for: tool).first else {
            return XCTFail("tool metadata should map to tool")
        }
    }

    func testChartRejectsNonFiniteValuesAndLimits() throws {
        XCTAssertThrowsError(try ChartPoint(label: "bad", value: .infinity))

        let points = Array(repeating: #"{"label":"x","value":1}"#, count: RichContentLimits.maxChartPoints + 1).joined(separator: ",")
        let jsonString = "{\"type\":\"chart\",\"chart_type\":\"line\",\"series\":[{\"name\":\"too-many\",\"points\":[\(points)]}]}"
        let json = Data(jsonString.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(RichBlock.self, from: json))
    }

    func testStreamingDeltaKeepsConcatenationAndTypedMarkdown() {
        var item = ChatItem(id: "stream-1", chatID: "chat-1", role: .assistant, text: "Hel", isStreaming: true)
        item.append(delta: "lo")
        XCTAssertEqual(item.text, "Hello")
        XCTAssertTrue(item.isStreaming)
        guard case .markdown(let markdown) = item.blocks.first else {
            return XCTFail("streaming content should remain markdown")
        }
        XCTAssertEqual(markdown.text, "Hello")
    }

    func testAggregateContentAndMediaStructureAreBounded() throws {
        let half = String(repeating: "x", count: RichContentLimits.maxMessageContentBytes / 2 + 1)
        let aggregate = #"{"version":"1","id":"m","chat_id":"c","role":"assistant","blocks":[{"type":"markdown","text":"\#(half)"},{"type":"markdown","text":"\#(half)"}]}"#
        XCTAssertThrowsError(try JSONDecoder().decode(RichContentMessage.self, from: Data(aggregate.utf8)))

        let badMedia = #"{"type":"media","source":{"type":"allowlisted_url","url":"https://user:secret@example.test/a"},"media_type":"image/png","width":40000,"height":1}"#
        XCTAssertThrowsError(try JSONDecoder().decode(RichBlock.self, from: Data(badMedia.utf8)))

        let badFile = #"{"type":"file","id":"f","name":"x","size_bytes":-1}"#
        XCTAssertThrowsError(try JSONDecoder().decode(RichBlock.self, from: Data(badFile.utf8)))
    }

    func testUnsupportedPayloadIsTruncated() throws {
        let payload = String(repeating: "x", count: ZiggyProtocolLimits.maxUnsupportedPayloadBytes * 2)
        let data = Data("{\"type\":\"future\",\"payload\":\"\(payload)\"}".utf8)
        let block = try JSONDecoder().decode(RichBlock.self, from: data)
        guard case .unsupported(_, let bounded) = block else { return XCTFail("expected placeholder") }
        XCTAssertLessThanOrEqual(bounded.aggregateStringBytes, ZiggyProtocolLimits.maxUnsupportedPayloadBytes)
    }

    func testAccessibilityModelsExposeTableAndChartValues() throws {
        let table = try TableBlock(columns: ["Region", "Count"], rows: [["West", "7"]])
        XCTAssertEqual(table.accessibilityRows, ["Row 1, Region: West, Count: 7"])

        let chart = try ChartBlock(
            chartType: .bar,
            title: "Requests",
            series: [ChartSeries(name: "Success", points: [try ChartPoint(label: "Monday", value: 42)])]
        )
        XCTAssertEqual(chart.accessibilityRows.first?.series, "Success")
        XCTAssertTrue(chart.accessibilitySummary.contains("Monday"))
        XCTAssertTrue(chart.accessibilitySummary.contains("42"))
    }
}
