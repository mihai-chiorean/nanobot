import Foundation
import Testing
@testable import Ziggy

@Suite("Durable Work REST transport", .serialized)
struct WorkRESTTests {
    @Test
    func `commands use authenticated REST and plural follow up falls back to singular REST`() async throws {
        let recorder = WorkRESTRecorder()
        WorkRESTURLProtocol.handler = { request in
            recorder.record(request)
            let path = request.url?.path ?? ""
            switch (request.httpMethod, path) {
            case ("POST", "/api/work"):
                return WorkRESTURLProtocol.response(for: request, statusCode: 201,
                                                    body: #"{"task":{"task_id":"task-1","status":"queued"}}"#)
            case ("POST", "/api/work/task-1/cancel"):
                return WorkRESTURLProtocol.response(for: request, statusCode: 200,
                                                    body: #"{"task":{"task_id":"task-1","status":"cancelled"}}"#)
            case ("POST", "/api/work/task-1/messages"):
                return WorkRESTURLProtocol.response(for: request, statusCode: 404, body: "missing")
            case ("POST", "/api/work/task-1/message"):
                return WorkRESTURLProtocol.response(for: request, statusCode: 202, body: #"{"accepted":true}"#)
            default:
                return WorkRESTURLProtocol.response(for: request, statusCode: 500, body: "unexpected")
            }
        }
        defer { WorkRESTURLProtocol.handler = nil }

        let client = ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            bearerToken: "rest-secret",
            session: WorkRESTURLProtocol.makeSession()
        )
        let created = try await client.createWork(chatID: "chat-1", content: "run it", idempotencyKey: "create-request-0001")
        let cancelled = try await client.cancelWork(taskID: created.id, idempotencyKey: "cancel-request-0001")
        try await client.sendWorkFollowUp(taskID: created.id, content: "continue", idempotencyKey: "followup-request-0001")

        let requests = recorder.requests
        #expect(created.id == "task-1")
        #expect(cancelled.status == .cancelled)
        #expect(requests.count == 4)
        #expect(requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer rest-secret" })
        #expect(requests.allSatisfy { !($0.url?.absoluteString.contains("rest-secret") ?? true) })
        #expect(requests.map(\.url!.path).suffix(2) == ["/api/work/task-1/messages", "/api/work/task-1/message"])
        #expect(requests.map { $0.value(forHTTPHeaderField: "Idempotency-Key") } == [
            "create-request-0001",
            "cancel-request-0001",
            "followup-request-0001",
            "followup-request-0001"
        ])
    }

    @Test
    func `SSE sends last event id and deduplicates durable sequences`() async throws {
        let recorder = WorkRESTRecorder()
        WorkRESTURLProtocol.handler = { request in
            recorder.record(request)
            let body = """
            id: 8
            event: work.event
            data: {"type":"progress","payload":{"message":"one"}}

            id: 8
            event: work.event
            data: {"type":"progress","payload":{"message":"duplicate"}}

            id: 9
            event: work.event
            data: {"type":"status.changed","payload":{"status":"succeeded"}}

            """
            return WorkRESTURLProtocol.response(for: request, statusCode: 200, body: body,
                                                contentType: "text/event-stream")
        }
        defer { WorkRESTURLProtocol.handler = nil }

        let client = ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            bearerToken: "stream-secret",
            session: WorkRESTURLProtocol.makeSession()
        )
        let stream = client.streamWorkEvents(taskID: "task/1", lastEventID: "7", maxReconnectAttempts: 0)
        var iterator = stream.makeAsyncIterator()
        let first = try #require(try await iterator.next())
        let second = try #require(try await iterator.next())

        #expect(first.sequence == 8)
        #expect(first.message == "one")
        #expect(second.sequence == 9)
        #expect(second.status == .succeeded)
        let requests = recorder.requests
        #expect(requests.count == 1)
        #expect(requests.first?.value(forHTTPHeaderField: "Last-Event-ID") == "7")
        #expect(URLComponents(url: try #require(requests.first?.url), resolvingAgainstBaseURL: false)?.percentEncodedPath
            == "/api/work/task%2F1/events/stream")
        #expect(requests.first?.value(forHTTPHeaderField: "Authorization") == "Bearer stream-secret")
    }

    @Test
    func `SSE exposes unauthorized response for AppModel refresh handling without reconnecting`() async throws {
        let recorder = WorkRESTRecorder()
        WorkRESTURLProtocol.handler = { request in
            recorder.record(request)
            return WorkRESTURLProtocol.response(for: request, statusCode: 401, body: "unauthorized")
        }
        defer { WorkRESTURLProtocol.handler = nil }

        let client = ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            bearerToken: "expired-secret",
            session: WorkRESTURLProtocol.makeSession()
        )
        var iterator = client.streamWorkEvents(taskID: "task-1", maxReconnectAttempts: 3).makeAsyncIterator()
        do {
            _ = try await iterator.next()
            Issue.record("expected HTTP 401")
        } catch {
            #expect((error as? ZiggyRESTError) == .http(statusCode: 401, body: nil))
        }
        #expect(recorder.requests.first?.value(forHTTPHeaderField: "Authorization") == "Bearer expired-secret")
    }
}

private final class WorkRESTRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var storedRequests: [URLRequest] = []

    var requests: [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return storedRequests
    }

    func record(_ request: URLRequest) {
        lock.lock()
        storedRequests.append(request)
        lock.unlock()
    }
}

private final class WorkRESTURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) -> (HTTPURLResponse, Data))?

    static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Self.self]
        return URLSession(configuration: configuration)
    }

    static func response(for request: URLRequest, statusCode: Int, body: String,
                         contentType: String = "application/json") -> (HTTPURLResponse, Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: nil,
            headerFields: ["Content-Type": contentType]
        )!
        return (response, Data(body.utf8))
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (response, data) = handler(request)
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
