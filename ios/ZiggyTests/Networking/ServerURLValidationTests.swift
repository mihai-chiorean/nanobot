import Foundation
import Testing
@testable import Ziggy

@Suite
struct ServerURLValidationTests {
    @Test
    func `HTTPS is required for non-local hosts`() {
        #expect(ZiggyServerURLValidation.isValid(URL(string: "https://chat.mihaichiorean.com")!))
        #expect(!ZiggyServerURLValidation.isValid(URL(string: "https://ziggy.example.test")!))
        #expect(!ZiggyServerURLValidation.isValid(URL(string: "http://chat.mihaichiorean.com")!))
    }

    @Test
    func `only production and loopback hosts can receive identity tokens`() {
        #expect(ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "https://chat.mihaichiorean.com")!))
        #expect(ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "http://127.0.0.1:8080")!))
        #expect(!ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "https://attacker.example")!))
        #expect(!ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "https://chat.mihaichiorean.com:8443")!))
    }

    @Test
    func `HTTP is allowed only for local hosts`() {
        for value in ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"] {
            #expect(ZiggyServerURLValidation.isValid(URL(string: value)!), Comment(rawValue: value))
        }
        #expect(ZiggyServerURLValidation.isValid(URL(string: "https://localhost:8080")!))
        #expect(!ZiggyServerURLValidation.isValid(URL(string: "http://localhost.example.test")!))
    }

    @Test
    func `user info query and fragment are rejected`() {
        for value in [
            "https://user:password@ziggy.example.test",
            "https://ziggy.example.test?tenant=private",
            "https://ziggy.example.test#fragment"
        ] {
            #expect(ZiggyServerURLValidation.url(from: value) == nil, Comment(rawValue: value))
        }
    }

    @Test
    func `missing host or unsupported scheme is rejected`() {
        #expect(ZiggyServerURLValidation.url(from: "localhost:8080") == nil)
        #expect(ZiggyServerURLValidation.url(from: "ftp://ziggy.example.test") == nil)
        #expect(ZiggyServerURLValidation.url(from: "https:///path") == nil)
    }
}
