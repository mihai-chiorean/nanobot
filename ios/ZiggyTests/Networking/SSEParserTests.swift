import Testing
@testable import Ziggy

@Suite
struct SSEParserTests {
    @Test
    func `parser handles comments CRLF multiline and fragmented bytes`() throws {
        var parser = SSEParser()
        let source = ": keep-alive\r\nevent: message\r\nid: 9\r\ndata: {\"a\":\r\ndata: 1}\r\n\r\ndata: [DONE]\r\n\r\n"
        var events: [SSEEvent] = []
        for byte in source.utf8 { events.append(contentsOf: try parser.feed([byte])) }

        #expect(events == [
            SSEEvent(event: "message", id: "9", data: "{\"a\":\n1}"),
            SSEEvent(data: "[DONE]")
        ])
    }

    @Test
    func `parser flushes an unterminated final frame`() throws {
        var parser = SSEParser()
        _ = try parser.feed(Array("data: final".utf8))
        #expect(try parser.finish() == [SSEEvent(data: "final")])
    }

    @Test
    func `parser ignores unknown fields and supports event without value`() throws {
        var parser = SSEParser()
        let events = try parser.feed(Array("event\ndata: value\n\n".utf8))
        #expect(events.first?.event == "")
        #expect(events.first?.data == "value")
    }

    @Test
    func `parser rejects oversized line and event before decode`() throws {
        var lineParser = SSEParser()
        #expect(throws: (any Error).self) {
            try lineParser.feed(Array(
                repeating: UInt8(ascii: "x"),
                count: ZiggyProtocolLimits.maxSSELineBytes + 1
            ))
        }

        var eventParser = SSEParser()
        let line = "data: " + String(repeating: "a", count: ZiggyProtocolLimits.maxSSELineBytes - 6) + "\n"
        for _ in 0..<4 { _ = try eventParser.feed(line.utf8) }
        #expect(throws: (any Error).self) {
            try eventParser.feed("data: overflow-overflow-overflow\n".utf8)
        }
    }
}
