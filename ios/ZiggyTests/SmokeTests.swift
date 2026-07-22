import XCTest
@testable import Ziggy

final class SmokeTests: XCTestCase {
    @MainActor
    func testAppModelStartsOnChats() {
        XCTAssertEqual(AppModel().selectedTab, .chats)
    }

    @MainActor
    func testAppModelStartsSignedOutWithoutLegacyAccess() {
        let model = AppModel()
        XCTAssertEqual(model.identity, .signedOut)
        XCTAssertEqual(model.phase, .launching)
    }

    @MainActor
    func testSignOutClearsTenantDerivedStateBeforeProviderCompletes() async {
        let authSession = SmokeAuthSession()
        let model = AppModel(
            credentialStore: InMemoryCredentialStore(),
            authSession: authSession
        )
        model.identity = ZiggyIdentity(name: "Previous user", email: "previous@example.test")
        model.sessions = [SessionSummary(key: "websocket:previous")]
        model.messagesByChatID = [
            "previous": [ChatItem(chatID: "previous", role: .user, text: "private")]
        ]
        model.workEventsByTaskID = ["previous-task": []]
        model.selectedSessionKey = "websocket:previous"
        model.chatNavigationPath = ["previous"]

        await model.signOut()

        XCTAssertTrue(authSession.didSignOut)
        XCTAssertEqual(model.identity, .signedOut)
        XCTAssertTrue(model.sessions.isEmpty)
        XCTAssertTrue(model.messagesByChatID.isEmpty)
        XCTAssertTrue(model.workEventsByTaskID.isEmpty)
        XCTAssertNil(model.selectedSessionKey)
        XCTAssertTrue(model.chatNavigationPath.isEmpty)
        XCTAssertEqual(model.phase, .needsEnrollment)
    }
}

@MainActor
private final class SmokeAuthSession: AuthSessionProviding {
    var isSignedIn = true
    var identity: ZiggyIdentity? = ZiggyIdentity(name: "Test user", email: "test@example.test")
    var didSignOut = false

    func sessionToken() async throws -> String { "test-token" }

    func signOut() async throws {
        didSignOut = true
        isSignedIn = false
        identity = nil
    }
}
