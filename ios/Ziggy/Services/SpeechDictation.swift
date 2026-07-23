import AVFoundation
import Foundation
import Speech

enum SpeechDictationEvent: Equatable, Sendable {
    case partial(String)
    case final(String)
}

enum SpeechDictationError: Error, Equatable, LocalizedError, Sendable {
    case speechRecognitionDenied
    case speechRecognitionRestricted
    case microphoneDenied
    case recognizerUnavailable
    case alreadyRunning
    case audioSessionConfigurationFailed(String)
    case audioEngineStartFailed(String)
    case recognitionFailed(String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .speechRecognitionDenied:
            "Speech recognition permission is denied."
        case .speechRecognitionRestricted:
            "Speech recognition is restricted on this device."
        case .microphoneDenied:
            "Microphone permission is denied."
        case .recognizerUnavailable:
            "Speech recognition is temporarily unavailable."
        case .alreadyRunning:
            "Dictation is already running."
        case let .audioSessionConfigurationFailed(message):
            "The audio session could not be configured: \(message)"
        case let .audioEngineStartFailed(message):
            "The microphone could not be started: \(message)"
        case let .recognitionFailed(message):
            "Speech recognition failed: \(message)"
        case .cancelled:
            "Dictation was cancelled."
        }
    }

    var recoverySuggestion: String? {
        switch self {
        case .speechRecognitionDenied, .microphoneDenied:
            "Enable the permission in Settings and try again."
        case .speechRecognitionRestricted:
            "Check Screen Time or device management restrictions."
        case .recognizerUnavailable:
            "Check the network connection and try again."
        case .alreadyRunning:
            "Stop the current dictation before starting another one."
        case .audioSessionConfigurationFailed, .audioEngineStartFailed, .recognitionFailed:
            "Stop dictation and try again."
        case .cancelled:
            nil
        }
    }
}

@MainActor
protocol SpeechDictating: AnyObject {
    func requestPermissions() async throws
    func start() async throws -> AsyncThrowingStream<SpeechDictationEvent, Error>
    func stop()
    func cancel()
}

@MainActor
final class AppleSpeechDictationService: SpeechDictating {
    private let recognizer: SFSpeechRecognizer?
    private let audioEngine = AVAudioEngine()
    private let audioSession = AVAudioSession.sharedInstance()

    private var recognitionTask: SFSpeechRecognitionTask?
    private var requestBox: RecognitionRequestBox?
    private var streamContinuation: AsyncThrowingStream<SpeechDictationEvent, Error>.Continuation?
    private var sessionID: UUID?
    private var hasInstalledTap = false
    private var hasActivatedAudioSession = false

    init(locale: Locale = .current) {
        recognizer = SFSpeechRecognizer(locale: locale)
    }

    func requestPermissions() async throws {
        let speechStatus = await requestSpeechAuthorization()
        switch speechStatus {
        case .authorized:
            break
        case .denied:
            throw SpeechDictationError.speechRecognitionDenied
        case .restricted:
            throw SpeechDictationError.speechRecognitionRestricted
        case .notDetermined:
            throw SpeechDictationError.speechRecognitionDenied
        @unknown default:
            throw SpeechDictationError.speechRecognitionDenied
        }

        let microphoneGranted = await requestMicrophonePermission()
        guard microphoneGranted else {
            throw SpeechDictationError.microphoneDenied
        }
    }

    func start() async throws -> AsyncThrowingStream<SpeechDictationEvent, Error> {
        guard streamContinuation == nil else {
            throw SpeechDictationError.alreadyRunning
        }

        try await requestPermissions()
        guard let recognizer, recognizer.isAvailable else {
            throw SpeechDictationError.recognizerUnavailable
        }

        let newSessionID = UUID()
        let stream = AsyncThrowingStream<SpeechDictationEvent, Error> { [weak self] continuation in
            guard let self else {
                continuation.finish(throwing: SpeechDictationError.cancelled)
                return
            }
            self.streamContinuation = continuation
            continuation.onTermination = { [weak self] _ in
                Task { @MainActor [weak self] in
                    self?.finishCurrentSession(with: nil)
                }
            }
        }
        sessionID = newSessionID

        do {
            try configureAudioSession()

            let request = SFSpeechAudioBufferRecognitionRequest()
            request.shouldReportPartialResults = true
            let box = RecognitionRequestBox(request: request)
            requestBox = box

            let inputNode = audioEngine.inputNode
            inputNode.removeTap(onBus: 0)
            inputNode.installTap(onBus: 0, bufferSize: 1_024, format: inputNode.outputFormat(forBus: 0)) { [box] buffer, _ in
                box.append(buffer)
            }
            hasInstalledTap = true

            recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
                let text = result?.bestTranscription.formattedString
                let isFinal = result?.isFinal ?? false
                let errorMessage = error?.localizedDescription
                Task { @MainActor [weak self] in
                    self?.handleRecognition(
                        sessionID: newSessionID,
                        text: text,
                        isFinal: isFinal,
                        errorMessage: errorMessage
                    )
                }
            }

            audioEngine.prepare()
            do {
                try audioEngine.start()
            } catch {
                throw SpeechDictationError.audioEngineStartFailed(error.localizedDescription)
            }
        } catch {
            finishCurrentSession(with: error as? SpeechDictationError ?? .audioSessionConfigurationFailed(error.localizedDescription))
            throw error
        }

        return stream
    }

    func stop() {
        finishCurrentSession(with: nil)
    }

    func cancel() {
        finishCurrentSession(with: SpeechDictationError.cancelled)
    }

    private func configureAudioSession() throws {
        do {
            try audioSession.setCategory(.record, mode: .measurement, options: [.duckOthers])
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
            hasActivatedAudioSession = true
        } catch {
            throw SpeechDictationError.audioSessionConfigurationFailed(error.localizedDescription)
        }
    }

    private func handleRecognition(
        sessionID incomingSessionID: UUID,
        text: String?,
        isFinal: Bool,
        errorMessage: String?
    ) {
        guard sessionID == incomingSessionID, streamContinuation != nil else {
            return
        }

        if let errorMessage {
            finishCurrentSession(with: SpeechDictationError.recognitionFailed(errorMessage))
            return
        }

        guard let text else {
            return
        }

        streamContinuation?.yield(isFinal ? .final(text) : .partial(text))
        if isFinal {
            finishCurrentSession(with: nil)
        }
    }

    private func finishCurrentSession(with error: SpeechDictationError?) {
        let continuation = streamContinuation
        streamContinuation = nil
        sessionID = nil

        recognitionTask?.cancel()
        recognitionTask = nil
        requestBox?.endAudio()
        requestBox = nil

        if hasInstalledTap {
            audioEngine.inputNode.removeTap(onBus: 0)
            hasInstalledTap = false
        }
        audioEngine.stop()

        if hasActivatedAudioSession {
            try? audioSession.setActive(false, options: .notifyOthersOnDeactivation)
            hasActivatedAudioSession = false
        }

        if let error {
            continuation?.finish(throwing: error)
        } else {
            continuation?.finish()
        }
    }

    private func requestSpeechAuthorization() async -> SFSpeechRecognizerAuthorizationStatus {
        await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
    }

    private func requestMicrophonePermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }
}

private final class RecognitionRequestBox: @unchecked Sendable {
    let request: SFSpeechAudioBufferRecognitionRequest

    init(request: SFSpeechAudioBufferRecognitionRequest) {
        self.request = request
    }

    func append(_ buffer: AVAudioPCMBuffer) {
        request.append(buffer)
    }

    func endAudio() {
        request.endAudio()
    }
}
