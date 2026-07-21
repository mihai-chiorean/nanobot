import Foundation
import XCTest
@testable import Ziggy

final class AuthenticationTests: XCTestCase {
    override func tearDown() {
        URLProtocolStub.handler = nil
        super.tearDown()
    }

    func testGuestBootstrapPostsSecretInBodyNotURL() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertNil(URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.query)
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Content-Type"),
                "application/x-www-form-urlencoded"
            )
            let body = try XCTUnwrap(String(data: try XCTUnwrap(Self.bodyData(for: request)), encoding: .utf8))
            XCTAssertEqual(URLComponents(string: "?" + body)?.queryItems?.first?.value, "private-code")
            XCTAssertFalse(try XCTUnwrap(request.url).absoluteString.contains("private-code"))
            return Self.response(for: request, body: #"{"token":"short","expires_in":300}"#)
        }

        let result = try await ZiggyRESTClient(
            baseURL: try XCTUnwrap(URL(string: "https://example.test")),
            session: session
        ).bootstrapGuest(code: "private-code")
        XCTAssertEqual(result.restToken, "short")
    }

    func testRESTResponseLimitAppliesBeforeDecode() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            Self.response(for: request, body: String(repeating: "x", count: 65))
        }
        let client = ZiggyRESTClient(
            baseURL: try XCTUnwrap(URL(string: "https://example.test")),
            session: session,
            maxResponseBytes: 64
        )
        do {
            _ = try await client.fetchSessions()
            XCTFail("expected response limit")
        } catch {
            XCTAssertEqual(error as? ZiggyRESTError, .responseTooLarge(limit: 64))
        }
    }

    @MainActor
    func testAbsoluteAndRelativeExpiryUseEarliestDeadline() throws {
        let now = Date(timeIntervalSince1970: 1_000)
        let absolute = ZiggyTimestamp(now.addingTimeInterval(30))
        let response = BootstrapResponse(restToken: "t", expiresIn: 300, expiresAt: absolute)
        XCTAssertEqual(response.expirationDate(relativeTo: now), absolute.date)
        XCTAssertEqual(AppModel.credentialRefreshDate(for: response, now: now), now.addingTimeInterval(27))

        let expired = BootstrapResponse(
            restToken: "t",
            expiresIn: 300,
            expiresAt: ZiggyTimestamp(now.addingTimeInterval(-1))
        )
        XCTAssertLessThan(try XCTUnwrap(AppModel.credentialRefreshDate(for: expired, now: now)), now)
    }

    private func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [URLProtocolStub.self]
        return URLSession(configuration: configuration)
    }

    private static func response(for request: URLRequest, body: String) -> (HTTPURLResponse, Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        return (response, Data(body.utf8))
    }

    private static func bodyData(for request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1_024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count >= 0 else { return nil }
            if count == 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

private final class URLProtocolStub: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        do {
            let handler = Self.handler ?? { _ in throw URLError(.badServerResponse) }
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
