import Foundation
import Testing
@testable import Ziggy

@Suite
struct WebSocketLifecycleTests {
    @Test
    func `connected waits for the first server frame`() async throws {
        let connection = TestWebSocketConnection()
        let client = makeClient(connection: connection)
        let collector = EventCollector()
        let eventTask = collect(client.events, into: collector)

        await client.start()
        try await Task.sleep(for: .milliseconds(50))
        let beforeFrame = await collector.events
        #expect(beforeFrame == [.state(.connecting)])

        await connection.receive(.string(#"{"event":"ready","chat_id":"chat-1"}"#))
        try await Task.sleep(for: .milliseconds(50))
        let afterFrame = await collector.events
        #expect(afterFrame.first == .state(.connecting))
        #expect(afterFrame.contains(.state(.connected)))

        await client.stop()
        eventTask.cancel()
    }

    @Test
    func `unauthorized upgrade is surfaced without reporting connected`() async throws {
        let connection = TestWebSocketConnection(statusCode: 401)
        let client = makeClient(connection: connection)
        let collector = EventCollector()
        let eventTask = collect(client.events, into: collector)

        await client.start()
        try await Task.sleep(for: .milliseconds(50))
        let beforeFrame = await collector.events
        #expect(beforeFrame == [.state(.connecting)])

        await connection.failReceive(with: .upgradeFailed(statusCode: 401))
        try await Task.sleep(for: .milliseconds(50))
        let events = await collector.events

        #expect(events.first == .state(.connecting))
        #expect(events.contains(.state(.failed("WebSocket upgrade failed (HTTP 401)."))))
        #expect(!events.contains(.state(.connected)))

        await client.stop()
        eventTask.cancel()
    }

    @Test
    func `connected socket sends periodic heartbeat pings`() async throws {
        let connection = TestWebSocketConnection()
        let client = makeClient(connection: connection, heartbeatInterval: .milliseconds(10))
        let collector = EventCollector()
        let eventTask = collect(client.events, into: collector)

        await client.start()
        await connection.completePing()
        await connection.receive(.string(#"{"event":"ready","chat_id":"chat-1"}"#))
        try await Task.sleep(for: .milliseconds(45))

        let pingCount = await connection.pingCount()
        #expect(pingCount >= 3)

        await client.stop()
        eventTask.cancel()
    }

    @Test
    func `outbound frames are written in FIFO order`() async throws {
        let connection = TestWebSocketConnection()
        let client = makeClient(connection: connection)
        let collector = EventCollector()
        let eventTask = collect(client.events, into: collector)

        await client.start()
        await connection.receive(.string(#"{"event":"ready","chat_id":"chat-1"}"#))
        #expect(try await eventually {
            await collector.events.contains(.state(.connected))
        })

        await client.send(.message(chatID: "chat-1", content: "first", media: []))
        await client.send(.message(chatID: "chat-1", content: "second", media: []))

        #expect(try await eventually { await connection.sentPayloads().count == 1 })
        await connection.permitSend()
        #expect(try await eventually { await connection.sentPayloads().count == 2 })
        await connection.permitSend()

        let payloads = await connection.sentPayloads()
        #expect(payloads[0].contains(#""content":"first""#))
        #expect(payloads[1].contains(#""content":"second""#))

        await client.stop()
        eventTask.cancel()
    }

    private func makeClient(
        connection: TestWebSocketConnection,
        heartbeatInterval: Duration = .seconds(10)
    ) -> ZiggyWebSocketClient {
        ZiggyWebSocketClient(
            baseURL: URL(string: "https://chat.mihaichiorean.com")!,
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

    private func eventually(
        timeout: Duration = .seconds(1),
        _ condition: @escaping @Sendable () async -> Bool
    ) async throws -> Bool {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        while clock.now < deadline {
            if await condition() { return true }
            try await Task.sleep(for: .milliseconds(5))
        }
        return await condition()
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
        var sendPermits = 0
        var sendWaiters: [CheckedContinuation<Void, Error>] = []
        var sentPayloads: [String] = []

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

        func send(_ message: URLSessionWebSocketTask.Message) async throws {
            switch message {
            case .string(let payload):
                sentPayloads.append(payload)
            case .data(let data):
                sentPayloads.append(String(decoding: data, as: UTF8.self))
            @unknown default:
                sentPayloads.append("")
            }
            if sendPermits > 0 {
                sendPermits -= 1
                return
            }
            try await withCheckedThrowingContinuation { continuation in
                sendWaiters.append(continuation)
            }
        }

        func permitSend() {
            if let waiter = sendWaiters.first {
                sendWaiters.removeFirst()
                waiter.resume()
            } else {
                sendPermits += 1
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
            for waiter in sendWaiters { waiter.resume(throwing: CancellationError()) }
            sendWaiters.removeAll()
        }

        func currentPingCount() -> Int { pingCount }
        func currentSentPayloads() -> [String] { sentPayloads }
    }

    private let state: State
    let response: URLResponse?

    init(statusCode: Int? = nil) {
        self.state = State()
        if let statusCode {
            response = HTTPURLResponse(
                url: URL(string: "https://chat.mihaichiorean.com")!,
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

    func send(_ message: URLSessionWebSocketTask.Message) async throws {
        try await state.send(message)
    }

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

    func permitSend() async {
        await state.permitSend()
    }

    func sentPayloads() async -> [String] {
        await state.currentSentPayloads()
    }
}
