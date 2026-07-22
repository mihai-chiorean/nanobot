import Security
import XCTest
@testable import Ziggy

final class CredentialStoreTests: XCTestCase {
    @MainActor
    func testStartupRemovesPreviouslyPersistedUntrustedServerURL() async {
        let store = LegacyCredentialStore(value: "https://attacker.example")
        let model = AppModel(credentialStore: store)

        await model.start()

        XCTAssertEqual(model.serverURLText, ZiggyServerURLValidation.productionURL.absoluteString)
        XCTAssertEqual(model.phase, .needsEnrollment)
        let didRemoveServerURL = await store.didRemoveServerURL
        XCTAssertTrue(didRemoveServerURL)
    }

    func testInMemoryStoreSavesUpdatesAndDeletesServerURL() async throws {
        let store = InMemoryCredentialStore()
        let url = URL(string: "https://chat.mihaichiorean.com")!

        try await store.save(serverURL: url)
        let savedURL = try await store.serverURL()
        XCTAssertEqual(savedURL, url)

        let updatedURL = URL(string: "http://127.0.0.1:8080/api")!
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

        let url = URL(string: "https://chat.mihaichiorean.com/api")!
        do {
            try await store.save(serverURL: url)
        } catch CredentialStoreError.keychainFailure(_, _, let status) where status == errSecMissingEntitlement {
            throw XCTSkip("Unsigned simulator test hosts do not have Keychain entitlements.")
        }
        let savedURL = try await store.serverURL()
        XCTAssertEqual(savedURL, url)

        let updatedURL = URL(string: "http://localhost:8080/v1")!
        try await store.save(serverURL: updatedURL)
        let savedUpdatedURL = try await store.serverURL()
        XCTAssertEqual(savedUpdatedURL, updatedURL)

        try await store.removeServerURL()
        let removedURL = try await store.serverURL()
        XCTAssertNil(removedURL)
    }
}

private actor LegacyCredentialStore: CredentialStoring {
    let value: String
    private(set) var didRemoveServerURL = false

    init(value: String) {
        self.value = value
    }

    func value(for key: CredentialKey) async throws -> String? {
        value
    }

    func setValue(_ value: String, for key: CredentialKey) async throws {}

    func removeValue(for key: CredentialKey) async throws {
        didRemoveServerURL = true
    }
}
