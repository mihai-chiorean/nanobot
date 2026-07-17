import Foundation

public enum AssistantStreamEvent: Sendable, Hashable {
    case connected(ConnectionInfo?)
    case delta(AssistantDelta)
    case completed(AssistantCompletion)
    case failed(AssistantStreamFailure)
}

public protocol AssistantStream: Sendable {
    var events: AsyncStream<AssistantStreamEvent> { get }
    func cancel()
}

public final class BufferedAssistantStream: AssistantStream, @unchecked Sendable {
    public let events: AsyncStream<AssistantStreamEvent>
    private let cancellation: @Sendable () -> Void

    public init(events: AsyncStream<AssistantStreamEvent>, cancellation: @escaping @Sendable () -> Void = {}) {
        self.events = events
        self.cancellation = cancellation
    }

    public func cancel() { cancellation() }
}
