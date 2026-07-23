import Testing
@testable import Ziggy

@Suite
struct SpeechDictationTests {
    @Test
    func `dictation events are equatable and sendable values`() {
        #expect(SpeechDictationEvent.partial("hello") == .partial("hello"))
        #expect(SpeechDictationEvent.final("hello world") == .final("hello world"))
        #expect(SpeechDictationEvent.partial("hello") != .final("hello"))
    }

    @Test
    func `permission and lifecycle errors have actionable messages`() {
        let error = SpeechDictationError.microphoneDenied

        #expect(error.errorDescription == "Microphone permission is denied.")
        #expect(error.recoverySuggestion == "Enable the permission in Settings and try again.")
    }
}
