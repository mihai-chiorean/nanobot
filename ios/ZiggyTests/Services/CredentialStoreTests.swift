import Security
import XCTest
@testable import Ziggy

final class CredentialStoreTests: XCTestCase {
    func testInMemoryStoreSavesUpdatesAndDeletesServerURL() async throws {
        let store = InMemoryCredentialStore()
        let url = URL(string: "https://ziggy.example.test")!

        try await store.save(serverURL: url)
        let savedURL = try await store.serverURL()
        XCTAssertEqual(savedURL, url)

        let updatedURL = URL(string: "https://chat.example.test/api")!
        try await store.save(serverURL: updatedURL)
        let savedUpdatedURL = try await store.serverURL()
        XCTAssertEqual(savedUpdatedURL, updatedURL)

        try await store.removeServerURL()
        let removedURL = try await store.serverURL()
        XCTAssertNil(removedURL)
    }

    func testCredentialValidationRejectsInvalidServerURL() async throws {
        let store = InMemoryCredentialStore()

        do {
            try await store.setValue("localhost:8080", for: .serverURL)
            XCTFail("Expected invalid URL rejection")
        } catch let error as CredentialStoreError {
            XCTAssertEqual(
                error,
                .invalidValue(key: .serverURL, reason: .invalidServerURL)
            )
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testKeychainStoreRoundTripsAndDeletesValues() async throws {
        let service = "com.mihaichiorean.ziggy.tests.\(UUID().uuidString)"
        let store = KeychainCredentialStore(service: service)

        let url = URL(string: "https://ziggy.example.test/api")!
        do {
            try await store.save(serverURL: url)
        } catch CredentialStoreError.keychainFailure(_, _, let status) where status == errSecMissingEntitlement {
            throw XCTSkip("Unsigned simulator test hosts do not have Keychain entitlements.")
        }
        let savedURL = try await store.serverURL()
        XCTAssertEqual(savedURL, url)

        let updatedURL = URL(string: "https://chat.example.test/v1")!
        try await store.save(serverURL: updatedURL)
        let savedUpdatedURL = try await store.serverURL()
        XCTAssertEqual(savedUpdatedURL, updatedURL)

        try await store.removeServerURL()
        let removedURL = try await store.serverURL()
        XCTAssertNil(removedURL)
    }
}
