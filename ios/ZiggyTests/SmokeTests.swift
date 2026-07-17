import XCTest
@testable import Ziggy

final class SmokeTests: XCTestCase {
    @MainActor
    func testAppModelStartsOnChats() {
        XCTAssertEqual(AppModel().selectedTab, .chats)
    }
}
