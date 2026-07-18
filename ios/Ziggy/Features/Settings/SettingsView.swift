import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        @Bindable var appModel = appModel

        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Text("General")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(ZiggyPalette.foreground)

                    SettingsSection(title: "Account") {
                        SettingsRow(title: "Name", value: appModel.identity.name)
                        SettingsDivider()
                        SettingsRow(title: "Email", value: appModel.identity.email)
                    }

                    SettingsSection(title: "Interface") {
                        HStack {
                            Text("Appearance")
                                .font(.subheadline.weight(.medium))
                            Spacer(minLength: 12)
                            Picker("Appearance", selection: $appModel.theme) {
                                ForEach(ZiggyTheme.allCases) { theme in
                                    Text(theme.label).tag(theme)
                                }
                            }
                            .labelsHidden()
                            .pickerStyle(.segmented)
                            .frame(maxWidth: 230)
                            .onChange(of: appModel.theme) { _, theme in appModel.setTheme(theme) }
                        }
                        .padding(.horizontal, 13)
                        .frame(minHeight: 54)
                    }

                    SettingsSection(title: "Connection") {
                        HStack {
                            Text("Status").font(.subheadline.weight(.medium))
                            Spacer()
                            ZiggyConnectionStatus(state: connectionStatus, showsLabel: true)
                        }
                        .padding(.horizontal, 13)
                        .frame(minHeight: 54)
                        SettingsDivider()
                        SettingsRow(title: "Server", value: appModel.serverURLText)
                        SettingsDivider()
                        SettingsRow(title: "Model", value: appModel.modelName)
                        SettingsDivider()
                        SettingsRow(title: "Chat", value: "WebSocket")
                        SettingsDivider()
                        SettingsRow(title: "Streaming API", value: "SSE")
                    }

                    SettingsSection(title: "Access") {
                        Button {
                            Task { await appModel.retryConnection() }
                        } label: {
                            SettingsAction(title: "Reconnect", symbol: "arrow.clockwise")
                        }
                        .buttonStyle(.plain)
                        SettingsDivider()
                        Button(role: .destructive) {
                            Task { await appModel.disconnectAndForget() }
                        } label: {
                            SettingsAction(title: "Remove access", symbol: "trash", destructive: true)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 18)
                .padding(.bottom, 30)
            }
            .background(ZiggyPalette.background)
            .toolbar(.hidden, for: .navigationBar)
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

private struct SettingsSection<Content: View>: View {
    let title: String
    let content: Content

    init(title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ZiggySectionLabel(title: title).padding(.leading, 8)
            PWAGroup { content }
        }
    }
}

private struct SettingsRow: View {
    let title: String
    let value: String

    var body: some View {
        HStack(spacing: 18) {
            Text(title)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(ZiggyPalette.foreground)
            Spacer(minLength: 8)
            Text(value)
                .font(.subheadline)
                .foregroundStyle(ZiggyPalette.mutedForeground)
                .lineLimit(2)
                .multilineTextAlignment(.trailing)
        }
        .padding(.horizontal, 13)
        .frame(minHeight: 54)
    }
}

private struct SettingsAction: View {
    let title: String
    let symbol: String
    var destructive = false

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).frame(width: 18)
            Text(title).font(.subheadline.weight(.medium))
            Spacer()
        }
        .foregroundStyle(destructive ? ZiggyPalette.destructive : ZiggyPalette.foreground)
        .padding(.horizontal, 13)
        .frame(minHeight: 52)
        .contentShape(Rectangle())
    }
}

private struct SettingsDivider: View {
    var body: some View {
        Rectangle().fill(ZiggyPalette.border.opacity(0.6)).frame(height: 1)
    }
}
