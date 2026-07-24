import XCTest

final class LiveChatUITests: XCTestCase {
    @MainActor
    func testLiveChatRoundTrip() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["ZIGGY_LIVE_SESSION_READY"] == "1" else {
            throw XCTSkip("Sign in once on this simulator, then set ZIGGY_LIVE_SESSION_READY=1.")
        }

        let app = XCUIApplication()
        app.launchEnvironment["ZIGGY_SERVER_URL"] = environment["ZIGGY_SERVER_URL"]
            ?? "https://chat.mihaichiorean.com"
        if let theme = environment["ZIGGY_THEME"], !theme.isEmpty {
            app.launchEnvironment["ZIGGY_THEME"] = theme
        }
        app.launch()

        let conversation = app.buttons.matching(identifier: "chat-session").firstMatch
        XCTAssertTrue(conversation.waitForExistence(timeout: 15), "Ziggy did not create a chat")
        conversation.tap()

        let composer = app.textFields["Message"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5), "The native composer did not open")
        let assistantMessages = app.descendants(matching: .any).matching(identifier: "assistant-message")
        let assistantCountBeforeSend = assistantMessages.count
        composer.tap()
        composer.typeText("Reply with exactly IOS_NATIVE_OK and nothing else.")
        app.buttons["Send message"].tap()

        let userMessage = app.descendants(matching: .any).matching(identifier: "user-message").firstMatch
        XCTAssertTrue(userMessage.waitForExistence(timeout: 5))
        let expectedResponse = app.staticTexts["IOS_NATIVE_OK"]
        XCTAssertTrue(
            expectedResponse.waitForExistence(timeout: 90),
            "Ziggy did not stream the expected response back to the native app"
        )
        XCTAssertGreaterThan(assistantMessages.count, assistantCountBeforeSend)

        app.buttons["Back to chats"].tap()
        XCTAssertTrue(conversation.waitForExistence(timeout: 5))
        conversation.tap()
        XCTAssertTrue(app.buttons["Back to chats"].waitForExistence(timeout: 5))
        XCTAssertTrue(expectedResponse.waitForExistence(timeout: 5))

        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Live Ziggy chat response"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
