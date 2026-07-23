import Foundation
import Testing
@testable import Ziggy

@Suite(.serialized)
struct AuthenticationTests {
    @Test
    func `authenticated bootstrap uses bearer token and no URL credential`() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            let url = try #require(request.url)
            #expect(request.httpMethod == "GET")
            #expect(URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
            )?.query == nil)
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer clerk-session-token")
            #expect(!url.absoluteString.contains("clerk-session-token"))
            return Self.response(for: request, body: #"{"token":"short","expires_in":300}"#)
        }
        defer { URLProtocolStub.handler = nil }

        let result = try await ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            session: session
        ).bootstrapAuthenticated(identityToken: "clerk-session-token")
        #expect(result.restToken == "short")
    }

    @Test
    func `authenticated bootstrap rejects an untrusted configured host before networking`() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { _ in
            Issue.record("an identity token request must not reach an untrusted host")
            throw URLError(.badServerResponse)
        }
        defer { URLProtocolStub.handler = nil }

        await #expect(throws: ZiggyRESTError.invalidURL) {
            try await ZiggyRESTClient(
                baseURL: try #require(URL(string: "https://attacker.example")),
                session: session
            ).bootstrapAuthenticated(identityToken: "clerk-session-token")
        }
    }

    @Test
    func `connector accounts use fresh identity token and decode tenant envelope`() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            let url = try #require(request.url)
            #expect(request.httpMethod == "GET")
            #expect(request.url?.path == "/connectors/accounts")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer clerk-session-token")
            #expect(!url.absoluteString.contains("clerk-session-token"))
            return Self.response(
                for: request,
                body: """
                {"accounts":[{
                  "account_id":"acct-google-1",
                  "provider":"google",
                  "email":"owner@example.com",
                  "scopes":["openid","https://www.googleapis.com/auth/gmail.readonly"],
                  "status":"active",
                  "created_at":"2026-07-22T18:00:00Z",
                  "updated_at":"2026-07-22T18:30:00Z"
                }]}
                """
            )
        }
        defer { URLProtocolStub.handler = nil }

        let response = try await ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            session: session
        ).fetchConnectorAccounts(identityToken: "clerk-session-token")

        #expect(response.items.count == 1)
        #expect(response.items.first?.id == "acct-google-1")
        #expect(response.items.first?.email == "owner@example.com")
        #expect(response.items.first?.status == "active")
    }

    @Test
    func `Google connector start uses identity token and accepts only Google authorization host`() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            #expect(request.url?.path == "/connectors/oauth/google/start")
            #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer clerk-session-token")
            return Self.response(
                for: request,
                body: #"{"authorization_url":"https://accounts.google.com/o/oauth2/v2/auth?state=opaque"}"#
            )
        }
        defer { URLProtocolStub.handler = nil }

        let authorization = try await ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            session: session
        ).startGoogleConnector(identityToken: "clerk-session-token")

        #expect(authorization.googleURL?.host == "accounts.google.com")
        #expect(ConnectorAuthorization(
            authorizationURL: "https://accounts.google.com.attacker.example/oauth"
        ).googleURL == nil)
        #expect(ConnectorAuthorization(
            authorizationURL: "https://accounts.google.com/not-oauth?state=opaque"
        ).googleURL == nil)
        #expect(ConnectorAuthorization(
            authorizationURL: "https://accounts.google.com/o/oauth2/v2/auth?state=opaque#fragment"
        ).googleURL == nil)
        #expect(ConnectorAuthorization(
            authorizationURL: "https://accounts.google.com/o/oauth2/v2/auth?state=one&state=two"
        ).googleURL == nil)
    }

    @Test
    func `connector identity token rejects an untrusted configured host before networking`() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { _ in
            Issue.record("a connector identity token request must not reach an untrusted host")
            throw URLError(.badServerResponse)
        }
        defer { URLProtocolStub.handler = nil }

        await #expect(throws: ZiggyRESTError.invalidURL) {
            try await ZiggyRESTClient(
                baseURL: try #require(URL(string: "https://attacker.example")),
                session: session
            ).fetchConnectorAccounts(identityToken: "clerk-session-token")
        }
    }

    @Test
    func `Clerk callback validation accepts only the registered callback`() throws {
        #expect(ClerkCallbackValidation.accepts(try #require(
            URL(string: "com.mihaichiorean.ziggy://callback?code=example&state=opaque")
        )))
        #expect(!ClerkCallbackValidation.accepts(try #require(
            URL(string: "com.mihaichiorean.ziggy://attacker/callback?code=example")
        )))
        #expect(!ClerkCallbackValidation.accepts(try #require(
            URL(string: "https://chat.mihaichiorean.com/callback?code=example")
        )))
        #expect(!ClerkCallbackValidation.accepts(try #require(
            URL(string: "com.mihaichiorean.ziggy://callback/extra?code=example")
        )))
    }

    @Test
    func `REST response limit uses content length precheck`() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            Self.response(for: request, body: String(repeating: "x", count: 65))
        }
        defer { URLProtocolStub.handler = nil }
        let client = ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            session: session,
            maxResponseBytes: 64
        )
        await #expect(throws: ZiggyRESTError.responseTooLarge(limit: 64)) {
            try await client.fetchSessions()
        }
    }

    @Test
    func `REST response limit applies while reading chunked body`() async throws {
        let session = makeSession()
        URLProtocolStub.handler = { request in
            Self.response(for: request, body: String(repeating: "x", count: 65), includeContentLength: false)
        }
        defer { URLProtocolStub.handler = nil }
        let client = ZiggyRESTClient(
            baseURL: try #require(URL(string: "https://chat.mihaichiorean.com")),
            session: session,
            maxResponseBytes: 64
        )

        await #expect(throws: ZiggyRESTError.responseTooLarge(limit: 64)) {
            try await client.fetchSessions()
        }
    }

    @Test
    @MainActor
    func `absolute and relative expiry use earliest deadline`() throws {
        let now = Date(timeIntervalSince1970: 1_000)
        let absolute = ZiggyTimestamp(now.addingTimeInterval(30))
        let response = BootstrapResponse(restToken: "t", expiresIn: 300, expiresAt: absolute)
        #expect(response.expirationDate(relativeTo: now) == absolute.date)
        #expect(AppModel.credentialRefreshDate(for: response, now: now) == now.addingTimeInterval(27))

        let expired = BootstrapResponse(
            restToken: "t",
            expiresIn: 300,
            expiresAt: ZiggyTimestamp(now.addingTimeInterval(-1))
        )
        #expect(try #require(AppModel.credentialRefreshDate(for: expired, now: now)) < now)
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
