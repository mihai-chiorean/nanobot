import Security
import XCTest
@testable import Ziggy

final class CredentialStoreTests: XCTestCase {
    func testInMemoryStoreSavesUpdatesAndDeletesCredentials() async throws {
        let store = InMemoryCredentialStore()
        let url = URL(string: "https://ziggy.example.test")!

        try await store.save(serverURL: url)
        try await store.save(guestEnrollmentCode: "guest-code-1")
        let savedURL = try await store.serverURL()
        let savedGuestCode = try await store.guestEnrollmentCode()
        XCTAssertEqual(savedURL, url)
        XCTAssertEqual(savedGuestCode, "guest-code-1")

        try await store.save(guestEnrollmentCode: "guest-code-2")
        let updatedGuestCode = try await store.guestEnrollmentCode()
        XCTAssertEqual(updatedGuestCode, "guest-code-2")

        try await store.removeServerURL()
        try await store.removeGuestEnrollmentCode()
        let removedURL = try await store.serverURL()
        let removedGuestCode = try await store.guestEnrollmentCode()
        XCTAssertNil(removedURL)
        XCTAssertNil(removedGuestCode)
    }

    func testInMemoryStoreRejectsEphemeralToken() async throws {
        let store = InMemoryCredentialStore()

        do {
            try await store.saveEnrollmentCredential("nbwt_short_lived", for: .guestCode)
            XCTFail("Expected short-lived token rejection")
        } catch let error as CredentialStoreError {
            XCTAssertEqual(
                error,
                .invalidValue(key: .enrollment(.guestCode), reason: .ephemeralToken)
            )
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testInMemoryStoreSupportsBothLongLivedEnrollmentModes() async throws {
        let store = InMemoryCredentialStore()

        try await store.saveEnrollmentCredential("owner-code", for: .ownerAccessCode)
        try await store.saveEnrollmentCredential("guest-code", for: .guestCode)

        let ownerCode = try await store.ownerAccessCode()
        let guestCode = try await store.enrollmentCredential(for: .guestCode)
        XCTAssertEqual(ownerCode, "owner-code")
        XCTAssertEqual(guestCode, "guest-code")

        try await store.removeEnrollmentCredential(for: .ownerAccessCode)
        let removedOwnerCode = try await store.ownerAccessCode()
        let retainedGuestCode = try await store.enrollmentCredential(for: .guestCode)
        XCTAssertNil(removedOwnerCode)
        XCTAssertEqual(retainedGuestCode, "guest-code")
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
        try await store.save(guestEnrollmentCode: "guest-code")
        let savedURL = try await store.serverURL()
        let savedGuestCode = try await store.guestEnrollmentCode()
        XCTAssertEqual(savedURL, url)
        XCTAssertEqual(savedGuestCode, "guest-code")

        try await store.save(guestEnrollmentCode: "updated-code")
        let updatedGuestCode = try await store.guestEnrollmentCode()
        XCTAssertEqual(updatedGuestCode, "updated-code")

        try await store.removeGuestEnrollmentCode()
        try await store.removeServerURL()
        let removedGuestCode = try await store.guestEnrollmentCode()
        let removedURL = try await store.serverURL()
        XCTAssertNil(removedGuestCode)
        XCTAssertNil(removedURL)
    }
}
