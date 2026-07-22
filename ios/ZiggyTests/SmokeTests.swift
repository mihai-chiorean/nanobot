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
}
