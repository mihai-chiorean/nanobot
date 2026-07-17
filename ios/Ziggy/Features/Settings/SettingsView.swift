import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        NavigationStack {
            Form {
                Section("Account") {
                    LabeledContent("Name", value: appModel.identity.name)
                    LabeledContent("Email", value: appModel.identity.email)
                }

                Section("Connection") {
                    LabeledContent("Status") {
                        ZiggyConnectionStatus(state: connectionStatus, showsLabel: true)
                    }
                    LabeledContent("Server", value: appModel.serverURLText)
                    LabeledContent("Model", value: appModel.modelName)
                    LabeledContent("Chat", value: "WebSocket")
                    LabeledContent("Streaming API", value: "SSE")
                }

                Section {
                    Button {
                        Task { await appModel.retryConnection() }
                    } label: {
                        Label("Reconnect", systemImage: "arrow.clockwise")
                    }
                    Button(role: .destructive) {
                        Task { await appModel.disconnectAndForget() }
                    } label: {
                        Label("Remove Access", systemImage: "trash")
                    }
                }
            }
            .navigationTitle("Settings")
        }
    }

    private var connectionStatus: ZiggyConnectionState {
        switch appModel.connectionState {
        case .connected: .connected
        case .connecting, .reconnecting: .connecting
        case .failed(let message): .failed(message)
        case .idle, .stopped: .offline
        }
    }
}
