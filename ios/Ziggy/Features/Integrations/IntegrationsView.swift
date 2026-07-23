import SwiftUI

struct IntegrationsView: View {
    @Environment(\.openURL) private var openURL
    @Environment(AppModel.self) private var appModel

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    googleSection
                }
                .padding(.horizontal, 16)
                .padding(.top, 18)
                .padding(.bottom, 30)
            }
            .background(ZiggyPalette.background)
            .toolbar(.hidden, for: .navigationBar)
        }
        .task {
            if appModel.connectorAccounts.isEmpty {
                await appModel.loadConnectors()
            }
        }
    }

    private var header: some View {
        HStack {
            Text("Integrations")
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(ZiggyPalette.foreground)
            Spacer()
            Button {
                Task { await appModel.loadConnectors() }
            } label: {
                if appModel.isLoadingConnectors {
                    ProgressView()
                        .controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .buttonStyle(PWAIconButton(size: 44))
            .disabled(appModel.isLoadingConnectors)
            .accessibilityLabel("Refresh integrations")
        }
    }

    private var googleSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            ZiggySectionLabel(title: "Google").padding(.leading, 8)
            PWAGroup {
                googleHeader
                if !googleAccounts.isEmpty {
                    IntegrationDivider()
                    ForEach(Array(googleAccounts.enumerated()), id: \.element.id) { index, account in
                        ConnectorAccountRow(account: account)
                        if index < googleAccounts.count - 1 {
                            IntegrationDivider()
                        }
                    }
                }
                if case .failed(let message) = appModel.connectorLoadState {
                    IntegrationDivider()
                    connectorFailure(message)
                }
            }
        }
    }

    private var googleAccounts: [ConnectorAccount] {
        appModel.connectorAccounts.filter { $0.provider.lowercased() == "google" }
    }

    private var googleHeader: some View {
        HStack(spacing: 12) {
            Image(systemName: "envelope")
                .font(.system(size: 16, weight: .medium))
                .foregroundStyle(ZiggyPalette.foreground)
                .frame(width: 34, height: 34)
                .background(ZiggyPalette.accent)
                .clipShape(RoundedRectangle(cornerRadius: 7))

            VStack(alignment: .leading, spacing: 2) {
                Text("Gmail")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(ZiggyPalette.foreground)
                Text(googleStatusLabel)
                    .font(.caption)
                    .foregroundStyle(ZiggyPalette.mutedForeground)
            }

            Spacer(minLength: 8)

            Button {
                Task {
                    guard let url = await appModel.googleConnectorAuthorizationURL() else { return }
                    openURL(url) { accepted in
                        if !accepted {
                            appModel.bannerMessage = "Google sign-in could not be opened."
                        }
                    }
                }
            } label: {
                if appModel.isStartingGoogleConnector {
                    ProgressView()
                        .controlSize(.small)
                        .frame(width: 72)
                } else {
                    Text(googleAccounts.isEmpty ? "Connect" : "Reconnect")
                        .font(.caption.weight(.semibold))
                        .frame(minWidth: 72)
                }
            }
            .buttonStyle(.bordered)
            .frame(minHeight: 44)
            .disabled(
                appModel.isStartingGoogleConnector
                    || appModel.connectorLoadState == .idle
                    || appModel.connectorLoadState == .loading
            )
            .accessibilityLabel(googleAccounts.isEmpty ? "Connect Gmail" : "Reconnect Gmail")
        }
        .padding(.horizontal, 13)
        .frame(minHeight: 62)
    }

    private var googleStatusLabel: String {
        if !googleAccounts.isEmpty {
            return "Email access"
        }
        switch appModel.connectorLoadState {
        case .idle, .loading:
            return "Checking connection"
        case .loaded:
            return "Not connected"
        case .failed:
            return "Unavailable"
        }
    }

    private func connectorFailure(_ message: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(ZiggyPalette.destructive)
            Text(message)
                .font(.caption)
                .foregroundStyle(ZiggyPalette.mutedForeground)
                .lineLimit(2)
            Spacer(minLength: 8)
            Button("Retry") {
                Task { await appModel.loadConnectors() }
            }
            .buttonStyle(.bordered)
            .frame(minHeight: 44)
        }
        .padding(.horizontal, 13)
        .frame(minHeight: 58)
    }
}

private struct ConnectorAccountRow: View {
    let account: ConnectorAccount

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(account.email)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(ZiggyPalette.foreground)
                    .lineLimit(2)
                if let lastError = account.lastError, !lastError.isEmpty {
                    Text(lastError)
                        .font(.caption)
                        .foregroundStyle(ZiggyPalette.destructive)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: 8)
            ConnectorStatusBadge(status: account.status)
        }
        .padding(.horizontal, 13)
        .frame(minHeight: 58)
    }
}

private struct ConnectorStatusBadge: View {
    let status: String

    private var isConnected: Bool {
        ["active", "connected"].contains(status.lowercased())
    }

    var body: some View {
        Label(
            isConnected ? "Connected" : status.capitalized,
            systemImage: isConnected ? "checkmark.circle.fill" : "exclamationmark.circle.fill"
        )
        .font(.caption2.weight(.semibold))
        .foregroundStyle(isConnected ? ZiggyPalette.emerald : ZiggyPalette.amber)
        .padding(.horizontal, 8)
        .frame(height: 26)
        .background(
            (isConnected ? ZiggyPalette.emerald : ZiggyPalette.amber).opacity(0.08),
            in: RoundedRectangle(cornerRadius: 6)
        )
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(ZiggyPalette.border.opacity(0.7)))
    }
}

private struct IntegrationDivider: View {
    var body: some View {
        Rectangle()
            .fill(ZiggyPalette.border.opacity(0.6))
            .frame(height: 1)
    }
}
