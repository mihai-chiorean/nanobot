import XCTest
@testable import Ziggy

final class SSEParserTests: XCTestCase {
    func testParserHandlesCommentsCRLFMultilineAndFragmentedBytes() throws {
        var parser = SSEParser()
        let source = ": keep-alive\r\nevent: message\r\nid: 9\r\ndata: {\"a\":\r\ndata: 1}\r\n\r\ndata: [DONE]\r\n\r\n"
        var events: [SSEEvent] = []
        for byte in source.utf8 { events.append(contentsOf: try parser.feed([byte])) }

        XCTAssertEqual(events, [
            SSEEvent(event: "message", id: "9", data: "{\"a\":\n1}"),
            SSEEvent(data: "[DONE]")
        ])
    }

    func testParserFlushesAnUnterminatedFinalFrame() throws {
        var parser = SSEParser()
        _ = try parser.feed(Array("data: final".utf8))
        XCTAssertEqual(try parser.finish(), [SSEEvent(data: "final")])
    }

    func testParserIgnoresUnknownFieldsAndSupportsEventWithoutValue() throws {
        var parser = SSEParser()
        let events = try parser.feed(Array("event\ndata: value\n\n".utf8))
        XCTAssertEqual(events.first?.event, "")
        XCTAssertEqual(events.first?.data, "value")
    }

    func testParserRejectsOversizedLineAndEventBeforeDecode() throws {
        var lineParser = SSEParser()
        XCTAssertThrowsError(try lineParser.feed(Array(repeating: UInt8(ascii: "x"), count: ZiggyProtocolLimits.maxSSELineBytes + 1)))

        var eventParser = SSEParser()
        let line = "data: " + String(repeating: "a", count: ZiggyProtocolLimits.maxSSELineBytes - 6) + "\n"
        for _ in 0..<4 { _ = try eventParser.feed(line.utf8) }
        XCTAssertThrowsError(try eventParser.feed("data: overflow-overflow-overflow\n".utf8))
    }
}
