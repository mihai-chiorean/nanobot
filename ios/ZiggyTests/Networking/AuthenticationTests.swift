import Foundation
import XCTest
@testable import Ziggy

final class AuthenticationTests: XCTestCase {
    override func tearDown() {
        URLProtocolStub.handler = nil
        super.tearDown()
    }

    func testAuthenticatedBootstrapUsesBearerTokenAndNoURLCredential() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertNil(URLComponents(url: try XCTUnwrap(request.url), resolvingAgainstBaseURL: false)?.query)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer clerk-session-token")
            XCTAssertFalse(try XCTUnwrap(request.url).absoluteString.contains("clerk-session-token"))
            return Self.response(for: request, body: #"{"token":"short","expires_in":300}"#)
        }

        let result = try await ZiggyRESTClient(
            baseURL: try XCTUnwrap(URL(string: "https://chat.mihaichiorean.com")),
            session: session
        ).bootstrapAuthenticated(identityToken: "clerk-session-token")
        XCTAssertEqual(result.restToken, "short")
    }

    func testAuthenticatedBootstrapRejectsAnUntrustedConfiguredHostBeforeNetworking() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { _ in
            XCTFail("an identity token request must not reach an untrusted host")
            throw URLError(.badServerResponse)
        }

        do {
            _ = try await ZiggyRESTClient(
                baseURL: try XCTUnwrap(URL(string: "https://attacker.example")),
                session: session
            ).bootstrapAuthenticated(identityToken: "clerk-session-token")
            XCTFail("expected untrusted host rejection")
        } catch {
            XCTAssertEqual(error as? ZiggyRESTError, .invalidURL)
        }
    }

    func testClerkCallbackValidationAcceptsOnlyTheRegisteredCallback() throws {
        XCTAssertTrue(ClerkCallbackValidation.accepts(try XCTUnwrap(
            URL(string: "com.mihaichiorean.ziggy://callback?code=example&state=opaque")
        )))
        XCTAssertFalse(ClerkCallbackValidation.accepts(try XCTUnwrap(
            URL(string: "com.mihaichiorean.ziggy://attacker/callback?code=example")
        )))
        XCTAssertFalse(ClerkCallbackValidation.accepts(try XCTUnwrap(
            URL(string: "https://chat.mihaichiorean.com/callback?code=example")
        )))
        XCTAssertFalse(ClerkCallbackValidation.accepts(try XCTUnwrap(
            URL(string: "com.mihaichiorean.ziggy://callback/extra?code=example")
        )))
    }

    func testRESTResponseLimitUsesContentLengthPrecheck() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            Self.response(for: request, body: String(repeating: "x", count: 65))
        }
        let client = ZiggyRESTClient(
            baseURL: try XCTUnwrap(URL(string: "https://chat.mihaichiorean.com")),
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

    func testRESTResponseLimitAppliesWhileReadingChunkedBody() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            Self.response(for: request, body: String(repeating: "x", count: 65), includeContentLength: false)
        }
        let client = ZiggyRESTClient(
            baseURL: try XCTUnwrap(URL(string: "https://chat.mihaichiorean.com")),
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

    private static func response(for request: URLRequest, body: String,
                                 includeContentLength: Bool = true) -> (HTTPURLResponse, Data) {
        var headers = ["Content-Type": "application/json"]
        if includeContentLength {
            headers["Content-Length"] = String(Data(body.utf8).count)
        }
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: nil,
            headerFields: headers
        )!
        return (response, Data(body.utf8))
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
