import Foundation
import XCTest
@testable import Ziggy

final class WebSocketLifecycleTests: XCTestCase {
    func testConnectedWaitsForFirstServerFrame() async throws {
        let connection = TestWebSocketConnection()
        let client = makeClient(connection: connection)
        let collector = EventCollector()
        let eventTask = collect(client.events, into: collector)

        await client.start()
        try await Task.sleep(for: .milliseconds(50))
        let beforeFrame = await collector.events
        XCTAssertEqual(beforeFrame, [.state(.connecting)])

        await connection.receive(.string(#"{"event":"ready","chat_id":"chat-1"}"#))
        try await Task.sleep(for: .milliseconds(50))
        let afterFrame = await collector.events
        XCTAssertEqual(afterFrame.first, .state(.connecting))
        XCTAssertTrue(afterFrame.contains(.state(.connected)))

        await client.stop()
        eventTask.cancel()
    }

    func testUnauthorizedUpgradeIsSurfacedAndDoesNotReportConnected() async throws {
        let connection = TestWebSocketConnection(statusCode: 401)
        let client = makeClient(connection: connection)
        let collector = EventCollector()
        let eventTask = collect(client.events, into: collector)

        await client.start()
        try await Task.sleep(for: .milliseconds(50))
        let beforeFrame = await collector.events
        XCTAssertEqual(beforeFrame, [.state(.connecting)])

        await connection.failReceive(with: .upgradeFailed(statusCode: 401))
        try await Task.sleep(for: .milliseconds(50))
        let events = await collector.events

        XCTAssertEqual(events.first, .state(.connecting))
        XCTAssertTrue(events.contains(.state(.failed("WebSocket upgrade failed (HTTP 401)."))))
        XCTAssertFalse(events.contains(.state(.connected)))

        await client.stop()
        eventTask.cancel()
    }

    func testConnectedSocketSendsPeriodicHeartbeatPings() async throws {
        let connection = TestWebSocketConnection()
        let client = makeClient(connection: connection, heartbeatInterval: .milliseconds(10))
        let collector = EventCollector()
        let eventTask = collect(client.events, into: collector)

        await client.start()
        await connection.completePing()
        await connection.receive(.string(#"{"event":"ready","chat_id":"chat-1"}"#))
        try await Task.sleep(for: .milliseconds(45))

        let pingCount = await connection.pingCount()
        XCTAssertGreaterThanOrEqual(pingCount, 3)

        await client.stop()
        eventTask.cancel()
    }

    private func makeClient(
        connection: TestWebSocketConnection,
        heartbeatInterval: Duration = .seconds(10)
    ) -> ZiggyWebSocketClient {
        ZiggyWebSocketClient(
            baseURL: URL(string: "https://ziggy.example.test")!,
            heartbeatInterval: heartbeatInterval,
            credentialProvider: { WebSocketCredential(bearerToken: "test-token") },
            connectionFactory: { _ in connection }
        )
    }

    private func collect(_ events: AsyncStream<ZiggyWebSocketEvent>, into collector: EventCollector) -> Task<Void, Never> {
        Task {
            for await event in events {
                await collector.append(event)
            }
        }
    }
}

private actor EventCollector {
    private(set) var events: [ZiggyWebSocketEvent] = []

    func append(_ event: ZiggyWebSocketEvent) {
        events.append(event)
    }
}

private final class TestWebSocketConnection: ZiggyWebSocketConnection, @unchecked Sendable {
    private actor State {
        enum PingResult: Sendable {
            case success
            case failure(ZiggyWebSocketClientError)
        }

        var messages: [URLSessionWebSocketTask.Message] = []
        var waiters: [CheckedContinuation<URLSessionWebSocketTask.Message, Error>] = []
        var receiveFailure: ZiggyWebSocketClientError?
        var pingResult: PingResult?
        var pingWaiter: CheckedContinuation<Void, Error>?
        var pingCount = 0

        func waitForPing() async throws {
            pingCount += 1
            if let pingResult {
                switch pingResult {
                case .success: return
                case .failure(let error): throw error
                }
            }
            try await withCheckedThrowingContinuation { continuation in
                pingWaiter = continuation
            }
        }

        func completePing(_ result: PingResult) {
            if let pingWaiter {
                self.pingWaiter = nil
                switch result {
                case .success: pingWaiter.resume()
                case .failure(let error): pingWaiter.resume(throwing: error)
                }
            } else {
                pingResult = result
            }
        }

        func next() async throws -> URLSessionWebSocketTask.Message {
            if let receiveFailure {
                self.receiveFailure = nil
                throw receiveFailure
            }
            if !messages.isEmpty { return messages.removeFirst() }
            return try await withCheckedThrowingContinuation { continuation in
                waiters.append(continuation)
            }
        }

        func yield(_ message: URLSessionWebSocketTask.Message) {
            if let waiter = waiters.first {
                waiters.removeFirst()
                waiter.resume(returning: message)
            } else {
                messages.append(message)
            }
        }

        func failNext(_ error: ZiggyWebSocketClientError) {
            if let waiter = waiters.first {
                waiters.removeFirst()
                waiter.resume(throwing: error)
            } else {
                receiveFailure = error
            }
        }

        func finish() {
            for waiter in waiters { waiter.resume(throwing: CancellationError()) }
            waiters.removeAll()
            pingWaiter?.resume(throwing: CancellationError())
            pingWaiter = nil
        }

        func currentPingCount() -> Int { pingCount }
    }

    private let state: State
    let response: URLResponse?

    init(statusCode: Int? = nil) {
        self.state = State()
        if let statusCode {
            response = HTTPURLResponse(
                url: URL(string: "https://ziggy.example.test")!,
                statusCode: statusCode,
                httpVersion: nil,
                headerFields: nil
            )
        } else {
            response = nil
        }
    }

    func resume() {}

    func ping() async throws {
        try await state.waitForPing()
    }

    func send(_ message: URLSessionWebSocketTask.Message) async throws {}

    func receive() async throws -> URLSessionWebSocketTask.Message {
        return try await state.next()
    }

    func cancel(with closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        Task { await state.finish() }
    }

    func receive(_ message: URLSessionWebSocketTask.Message) async {
        await state.yield(message)
    }

    func completePing() async {
        await state.completePing(.success)
    }

    func pingCount() async -> Int {
        await state.currentPingCount()
    }

    func failPing(with error: ZiggyWebSocketClientError) async {
        await state.completePing(.failure(error))
    }

    func failReceive(with error: ZiggyWebSocketClientError) async {
        await state.failNext(error)
    }
}
