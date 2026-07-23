import Testing
@testable import Ziggy

@Suite
struct SmokeTests {
    @Test
    @MainActor
    func `app model starts on chats`() {
        #expect(AppModel().selectedTab == .chats)
    }

    @Test
    @MainActor
    func `app model starts signed out without legacy access`() {
        let model = AppModel()
        #expect(model.identity == .signedOut)
        #expect(model.phase == .launching)
    }

    @Test
    @MainActor
    func `new chat immediately opens an ephemeral draft`() throws {
        let model = AppModel()

        model.newChat()

        let sessionKey = try #require(model.selectedSessionKey)
        #expect(model.chatNavigationPath == [sessionKey])
        #expect(sessionKey.hasPrefix("websocket:"))
        #expect(model.sessions.isEmpty)
        #expect(model.messagesByChatID.isEmpty)
    }

    @Test
    @MainActor
    func `background transition immediately marks socket idle`() async {
        let model = AppModel()
        model.connectionState = .connected

        model.applicationDidEnterBackground()

        #expect(model.connectionState == .idle)
        await model.applicationDidBecomeActive()
        #expect(model.phase == .launching)
    }

    @Test
    @MainActor
    func `sign out clears tenant derived state before provider completes`() async {
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

        #expect(authSession.didSignOut)
        #expect(model.identity == .signedOut)
        #expect(model.sessions.isEmpty)
        #expect(model.messagesByChatID.isEmpty)
        #expect(model.workEventsByTaskID.isEmpty)
        #expect(model.selectedSessionKey == nil)
        #expect(model.chatNavigationPath.isEmpty)
        #expect(model.phase == .needsEnrollment)
    }
}

@MainActor
private final class SmokeAuthSession: AuthSessionProviding {
    var isSignedIn = true
    var sessionIdentifier: String? = "smoke-session"
    var identity: ZiggyIdentity? = ZiggyIdentity(name: "Test user", email: "test@example.test")
    var didSignOut = false

    func sessionToken() async throws -> String { "test-token" }

    func signOut() async throws {
        didSignOut = true
        isSignedIn = false
        sessionIdentifier = nil
        identity = nil
    }
}
