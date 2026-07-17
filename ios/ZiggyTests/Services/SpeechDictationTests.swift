import XCTest
@testable import Ziggy

final class SpeechDictationTests: XCTestCase {
    func testDictationEventsAreEquatableAndSendableValues() {
        XCTAssertEqual(SpeechDictationEvent.partial("hello"), .partial("hello"))
        XCTAssertEqual(SpeechDictationEvent.final("hello world"), .final("hello world"))
        XCTAssertNotEqual(SpeechDictationEvent.partial("hello"), .final("hello"))
    }

    func testPermissionAndLifecycleErrorsHaveActionableMessages() {
        let error = SpeechDictationError.microphoneDenied

        XCTAssertEqual(error.errorDescription, "Microphone permission is denied.")
        XCTAssertEqual(error.recoverySuggestion, "Enable the permission in Settings and try again.")
    }
}
