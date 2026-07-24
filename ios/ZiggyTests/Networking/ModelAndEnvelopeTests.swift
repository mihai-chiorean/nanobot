import Foundation
import Testing
@testable import Ziggy

@Suite
struct ModelAndEnvelopeTests {
    @Test
    func `production bootstrap shape decodes`() throws {
        let data = Data(#"{"token":"nbwt_short-lived","ws_path":"/","expires_in":299,"model_name":"qwen3.6-35b","access":"tenant"}"#.utf8)
        let response = try JSONDecoder().decode(BootstrapResponse.self, from: data)
        #expect(response.restToken == "nbwt_short-lived")
        #expect(response.webSocketToken == nil)
        #expect(response.webSocketPath == "/")
        #expect(response.expiresIn == 299)
        #expect(response.model == "qwen3.6-35b")
        #expect(response.access == "tenant")
    }

    @Test
    func `capabilities remain independent`() {
        let capabilities = RichContentCapabilities(advertised: [
            "rich_content_v1": .boolean(true),
            "mermaid_v1": .boolean(true)
        ])
        #expect(capabilities.richContentV1)
        #expect(!capabilities.mediaV1)
        #expect(capabilities.mermaidV1)
    }

    @Test
    func `bootstrap accepts capability name array`() throws {
        let data = Data(#"{"token":"t","capabilities":["rich_content_v1","media_v1"]}"#.utf8)
        let response = try JSONDecoder().decode(BootstrapResponse.self, from: data)
        let capabilities = RichContentCapabilities(advertised: response.capabilities)
        #expect(capabilities.richContentV1)
        #expect(capabilities.mediaV1)
        #expect(!capabilities.mermaidV1)
    }

    @Test
    func `unknown status and fields do not break message decoding`() throws {
        let data = Data(#"{"id":"m1","role":"future_role","content":"hello","status":"future_state","ignored":{"x":1}}"#.utf8)
        let message = try JSONDecoder().decode(ZiggyMessage.self, from: data)
        #expect(message.id == "m1")
        #expect(message.content.text == "hello")
        if case .unknown("future_role") = message.role {} else {
            Issue.record("role should preserve unknown value")
        }
        if case .unknown("future_state") = message.status {} else {
            Issue.record("status should preserve unknown value")
        }
    }

    @Test
    func `work statuses match server contract and preserve legacy aliases`() {
        let serverStatuses: [ZiggyStatus] = [
            .scheduled, .queued, .running, .waiting,
            .succeeded, .failed, .cancelled, .interrupted
        ]
        #expect(serverStatuses.map(\.rawValue) == [
            "scheduled", "queued", "running", "waiting",
            "succeeded", "failed", "cancelled", "interrupted"
        ])
        #expect(ZiggyStatus("pending") == .queued)
        #expect(ZiggyStatus("completed") == .succeeded)
        #expect(ZiggyStatus("canceled") == .cancelled)
        #expect(ZiggyStatus("in-progress") == .running)
    }

    @Test
    func `work status changed payload decodes terminal status`() throws {
        let data = Data(#"{"event":"work.event","task_id":"task-1","seq":8,"type":"status.changed","payload":{"status":"succeeded","result_summary":"Done"}}"#.utf8)
        let event = try JSONDecoder().decode(InboundWebSocketEvent.self, from: data)
        guard case .workEvent(let workEvent) = event else {
            Issue.record("expected work event")
            return
        }

        #expect(workEvent.status == .succeeded)
        #expect(AppModel.shouldRefreshWork(for: workEvent))
    }

    @Test
    func `durable work task uses prompt preview`() throws {
        let data = Data(#"{"task_id":"work_123","title":"Backend health","prompt_preview":"Check every service","status":"succeeded","created_at":"2026-07-22T18:00:00Z","updated_at":"2026-07-22T18:01:00Z"}"#.utf8)
        let task = try JSONDecoder().decode(WorkTask.self, from: data)

        #expect(task.id == "work_123")
        #expect(task.title == "Backend health")
        #expect(task.description == "Check every service")
        #expect(task.status == .succeeded)
    }

    @Test
    func `durable work event surfaces result and tool detail`() throws {
        let completed = try JSONDecoder().decode(
            WorkEvent.self,
            from: Data(#"{"task_id":"work_123","seq":9,"type":"status.changed","payload":{"status":"succeeded","result_summary":"All services healthy"}}"#.utf8)
        )
        let tool = try JSONDecoder().decode(
            WorkEvent.self,
            from: Data(#"{"task_id":"work_123","seq":4,"type":"tool.finished","payload":{"name":"shell","detail":"Checked four services"}}"#.utf8)
        )

        #expect(completed.status == .succeeded)
        #expect(completed.message == "All services healthy")
        #expect(tool.message == "Checked four services")
        #expect(tool.data?.objectString(for: ["name"]) == "shell")
    }

    @Test
    func `only terminal status changed events refresh work`() {
        for status in [ZiggyStatus.succeeded, .failed, .cancelled, .interrupted] {
            #expect(status.isTerminal)
            #expect(AppModel.shouldRefreshWork(for: WorkEvent(
                taskID: "task-1", type: "status.changed", status: status
            )))
        }
        for status in [ZiggyStatus.scheduled, .queued, .running, .waiting] {
            #expect(!status.isTerminal)
        }
        #expect(!AppModel.shouldRefreshWork(for: WorkEvent(
            taskID: "task-1", type: "status.changed", status: .running
        )))
        #expect(!AppModel.shouldRefreshWork(for: WorkEvent(
            taskID: "task-1", type: "progress", status: .succeeded
        )))
        #expect(AppModel.shouldRefreshWork(for: WorkEvent(
            taskID: "task-1", type: "status.changed", status: .interrupted
        )))
    }

    @Test
    func `every server status has work presentation`() {
        let statuses: [ZiggyStatus] = [
            .scheduled, .queued, .running, .waiting,
            .succeeded, .failed, .cancelled, .interrupted
        ]
        #expect(statuses.map(\.presentation.rawValue) == statuses.map(\.rawValue))
    }

    @Test
    func `production history message allows no ID and UTC without suffix`() throws {
        let data = Data(#"{"role":"assistant","content":"IOS_NATIVE_OK","timestamp":"2026-07-17T16:14:57.836606"}"#.utf8)
        let message = try JSONDecoder().decode(ZiggyMessage.self, from: data)
        #expect(!message.id.isEmpty)
        #expect(message.content.text == "IOS_NATIVE_OK")
        #expect(message.createdAt != nil)
    }

    @Test
    func `outbound envelope uses wire names`() throws {
        let envelope = OutboundWebSocketEnvelope.workSubscribe(taskID: "task/1", afterSequence: 7)
        let object = try #require(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(envelope)) as? [String: Any]
        )
        #expect(object["type"] as? String == "work.subscribe")
        #expect(object["task_id"] as? String == "task/1")
        #expect(object["after_seq"] as? Int == 7)
    }

    @Test
    func `outbound message uses production chat and media fields`() throws {
        let envelope = OutboundWebSocketEnvelope.message(
            chatID: "chat-1",
            content: "hello",
            media: [OutboundMedia(dataURL: "data:image/jpeg;base64,AA==", name: "photo.jpg")]
        )
        let object = try #require(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(envelope)) as? [String: Any]
        )
        #expect(object["type"] as? String == "message")
        #expect(object["chat_id"] as? String == "chat-1")
        #expect(object["session_key"] == nil)
        #expect((object["media"] as? [[String: Any]])?.first?["name"] as? String == "photo.jpg")
    }

    @Test
    func `inbound typed event and unknown event decode`() throws {
        let message = try JSONDecoder().decode(InboundWebSocketEvent.self, from: Data(#"{"event":"message","chat_id":"chat-1","text":"hello","kind":"assistant","future":true}"#.utf8))
        if case .message(let value) = message {
            #expect(value.chatID == "chat-1")
            #expect(value.text == "hello")
        } else {
            Issue.record("expected typed message")
        }

        let unknown = try JSONDecoder().decode(InboundWebSocketEvent.self, from: Data(#"{"event":"future.event","value":3}"#.utf8))
        if case .unknown(let type, let payload) = unknown {
            #expect(type == "future.event")
            guard case .object(let object) = payload else {
                Issue.record("unknown payload must be an object")
                return
            }
            #expect(object["value"] == .number(3))
        } else {
            Issue.record("unknown event must be preserved")
        }
    }

    @Test
    func `production work event uses flat envelope and payload`() throws {
        let data = Data(#"{"event":"work.event","task_id":"task-1","seq":4,"type":"progress","payload":{"message":"Checking deploy"},"actor":"ziggy","step_id":"step-2","created_at":"2026-07-17T12:00:00Z"}"#.utf8)
        let event = try JSONDecoder().decode(InboundWebSocketEvent.self, from: data)
        guard case .workEvent(let workEvent) = event else {
            Issue.record("expected work event")
            return
        }
        #expect(workEvent.taskID == "task-1")
        #expect(workEvent.sequence == 4)
        #expect(workEvent.message == "Checking deploy")
        #expect(workEvent.actor == "ziggy")
    }

    @Test
    func `REST list wrapper decodes items`() throws {
        let data = Data(#"{"data":[{"key":"chat-1","title":"One"}],"next_cursor":"next","has_more":true}"#.utf8)
        let response = try JSONDecoder().decode(RESTEnvelope<[SessionSummary]>.self, from: data)
        #expect(response.value?.first?.key == "chat-1")
    }
}
