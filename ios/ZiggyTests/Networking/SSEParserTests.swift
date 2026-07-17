import XCTest
@testable import Ziggy

final class SSEParserTests: XCTestCase {
    func testParserHandlesCommentsCRLFMultilineAndFragmentedBytes() {
        var parser = SSEParser()
        let source = ": keep-alive\r\nevent: message\r\nid: 9\r\ndata: {\"a\":\r\ndata: 1}\r\n\r\ndata: [DONE]\r\n\r\n"
        var events: [SSEEvent] = []
        for byte in source.utf8 { events.append(contentsOf: parser.feed([byte])) }

        XCTAssertEqual(events, [
            SSEEvent(event: "message", id: "9", data: "{\"a\":\n1}"),
            SSEEvent(data: "[DONE]")
        ])
    }

    func testParserFlushesAnUnterminatedFinalFrame() {
        var parser = SSEParser()
        _ = parser.feed(Array("data: final".utf8))
        XCTAssertEqual(parser.finish(), [SSEEvent(data: "final")])
    }

    func testParserIgnoresUnknownFieldsAndSupportsEventWithoutValue() {
        var parser = SSEParser()
        let events = parser.feed(Array("event\ndata: value\n\n".utf8))
        XCTAssertEqual(events.first?.event, "")
        XCTAssertEqual(events.first?.data, "value")
    }
}
