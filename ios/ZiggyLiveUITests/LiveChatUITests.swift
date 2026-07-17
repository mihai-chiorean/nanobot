import XCTest

final class LiveChatUITests: XCTestCase {
    @MainActor
    func testLiveChatRoundTrip() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let accessCode = environment["ZIGGY_GUEST_CODE"], !accessCode.isEmpty else {
            throw XCTSkip("Set ZIGGY_GUEST_CODE to run the live gateway test.")
        }

        let app = XCUIApplication()
        app.launchEnvironment["ZIGGY_GUEST_CODE"] = accessCode
        app.launchEnvironment["ZIGGY_SERVER_URL"] = environment["ZIGGY_SERVER_URL"]
            ?? "https://chat.mihaichiorean.com"
        app.launch()

        let conversation = app.staticTexts["New conversation"]
        XCTAssertTrue(conversation.waitForExistence(timeout: 15), "Ziggy did not create a chat")
        conversation.tap()

        let composer = app.textFields["Message"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5), "The native composer did not open")
        composer.tap()
        composer.typeText("Reply with exactly IOS_NATIVE_OK and nothing else.")
        app.buttons["Send message"].tap()

        let userMessage = app.descendants(matching: .any).matching(identifier: "user-message").firstMatch
        let assistantMessage = app.descendants(matching: .any).matching(identifier: "assistant-message").firstMatch
        XCTAssertTrue(userMessage.waitForExistence(timeout: 5))
        XCTAssertTrue(
            assistantMessage.waitForExistence(timeout: 90),
            "Ziggy did not stream a response back to the native app"
        )

        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Live Ziggy chat response"
        screenshot.lifetime = .keepAlways
        add(screenshot)
    }
}
