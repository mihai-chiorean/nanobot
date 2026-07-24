import Foundation
import Security
import Testing
@testable import Ziggy

@Suite
struct CredentialStoreTests {
    @Test
    @MainActor
    func `startup removes previously persisted untrusted server URL`() async {
        let store = LegacyCredentialStore(value: "https://attacker.example")
        let model = AppModel(credentialStore: store)

        await model.start()

        #expect(model.serverURLText == ZiggyServerURLValidation.productionURL.absoluteString)
        #expect(model.phase == .needsEnrollment)
        let didRemoveServerURL = await store.didRemoveServerURL
        #expect(didRemoveServerURL)
    }

    @Test
    func `in-memory store saves updates and deletes server URL`() async throws {
        let store = InMemoryCredentialStore()
        let url = try #require(URL(string: "https://chat.mihaichiorean.com"))

        try await store.save(serverURL: url)
        let savedURL = try await store.serverURL()
        #expect(savedURL == url)

        let updatedURL = try #require(URL(string: "http://127.0.0.1:8080/api"))
        try await store.save(serverURL: updatedURL)
        let savedUpdatedURL = try await store.serverURL()
        #expect(savedUpdatedURL == updatedURL)

        try await store.removeServerURL()
        let removedURL = try await store.serverURL()
        #expect(removedURL == nil)
    }

    @Test
    func `credential validation rejects invalid server URL`() async {
        let store = InMemoryCredentialStore()

        await #expect(throws: CredentialStoreError.invalidValue(
            key: .serverURL,
            reason: .invalidServerURL
        )) {
            try await store.setValue("localhost:8080", for: .serverURL)
        }
    }

    @Test
    func `keychain store round trips and deletes values`() async throws {
        let service = "com.mihaichiorean.ziggy.tests.\(UUID().uuidString)"
        let store = KeychainCredentialStore(service: service)

        let url = try #require(URL(string: "https://chat.mihaichiorean.com/api"))
        do {
            try await store.save(serverURL: url)
        } catch CredentialStoreError.keychainFailure(_, _, let status) where status == errSecMissingEntitlement {
            try Test.cancel("Unsigned simulator test hosts do not have Keychain entitlements.")
        }
        let savedURL = try await store.serverURL()
        #expect(savedURL == url)

        let updatedURL = try #require(URL(string: "http://localhost:8080/v1"))
        try await store.save(serverURL: updatedURL)
        let savedUpdatedURL = try await store.serverURL()
        #expect(savedUpdatedURL == updatedURL)

        try await store.removeServerURL()
        let removedURL = try await store.serverURL()
        #expect(removedURL == nil)
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
