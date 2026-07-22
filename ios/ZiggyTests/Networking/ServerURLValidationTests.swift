import Foundation
import XCTest
@testable import Ziggy

final class ServerURLValidationTests: XCTestCase {
    func testHTTPSIsRequiredForNonLocalHosts() {
        XCTAssertTrue(ZiggyServerURLValidation.isValid(URL(string: "https://chat.mihaichiorean.com")!))
        XCTAssertFalse(ZiggyServerURLValidation.isValid(URL(string: "https://ziggy.example.test")!))
        XCTAssertFalse(ZiggyServerURLValidation.isValid(URL(string: "http://chat.mihaichiorean.com")!))
    }

    func testOnlyProductionAndLoopbackHostsCanReceiveIdentityTokens() {
        XCTAssertTrue(ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "https://chat.mihaichiorean.com")!))
        XCTAssertTrue(ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "http://127.0.0.1:8080")!))
        XCTAssertFalse(ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "https://attacker.example")!))
        XCTAssertFalse(ZiggyServerURLValidation.isTrustedForIdentityToken(URL(string: "https://chat.mihaichiorean.com:8443")!))
    }

    func testHTTPIsAllowedOnlyForLocalHosts() {
        for value in ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"] {
            XCTAssertTrue(ZiggyServerURLValidation.isValid(URL(string: value)!), value)
        }
        XCTAssertTrue(ZiggyServerURLValidation.isValid(URL(string: "https://localhost:8080")!))
        XCTAssertFalse(ZiggyServerURLValidation.isValid(URL(string: "http://localhost.example.test")!))
    }

    func testUserInfoQueryAndFragmentAreRejected() {
        for value in [
            "https://user:password@ziggy.example.test",
            "https://ziggy.example.test?tenant=private",
            "https://ziggy.example.test#fragment"
        ] {
            XCTAssertNil(ZiggyServerURLValidation.url(from: value), value)
        }
    }

    func testMissingHostOrUnsupportedSchemeIsRejected() {
        XCTAssertNil(ZiggyServerURLValidation.url(from: "localhost:8080"))
        XCTAssertNil(ZiggyServerURLValidation.url(from: "ftp://ziggy.example.test"))
        XCTAssertNil(ZiggyServerURLValidation.url(from: "https:///path"))
    }
}
