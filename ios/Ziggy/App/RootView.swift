import ClerkKit
import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        Group {
            switch appModel.phase {
            case .launching:
                ZiggyLaunchView(label: "Opening Ziggy")
            case .connecting:
                ZiggyLaunchView(label: "Connecting")
            case .needsEnrollment:
                EnrollmentView(errorMessage: appModel.bannerMessage)
            case .failed(let message):
                EnrollmentView(errorMessage: message)
            case .ready:
                ZiggyTabView()
            }
        }
        .task { await appModel.start() }
        .tint(ZiggyPalette.foreground)
        .background(ZiggyPalette.background)
    }
}

private struct ZiggyLaunchView: View {
    let label: String

    var body: some View {
        VStack(spacing: 20) {
            Image("ZiggyAvatar")
                .resizable()
                .scaledToFit()
                .frame(width: 72, height: 72)
                .clipShape(RoundedRectangle(cornerRadius: 14))
            Text("Ziggy")
                .font(.title3.weight(.semibold))
                .foregroundStyle(ZiggyPalette.foreground)
            ProgressView(label)
                .font(.caption)
                .foregroundStyle(ZiggyPalette.mutedForeground)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(ZiggyPalette.canvas)
    }
}

private struct ZiggyTabView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        @Bindable var appModel = appModel

        VStack(spacing: 0) {
            Group {
                switch appModel.selectedTab {
                case .chats: ChatListView()
                case .work: WorkListView()
                case .settings: SettingsView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            if appModel.chatNavigationPath.isEmpty {
                Rectangle().fill(ZiggyPalette.border.opacity(0.7)).frame(height: 1)
                HStack(spacing: 6) {
                    TabButton(tab: .chats, title: "Chats", symbol: "bubble.left.and.bubble.right")
                    TabButton(tab: .work, title: "Work", symbol: "bolt.horizontal")
                    TabButton(tab: .settings, title: "Settings", symbol: "gearshape")
                }
                .padding(.horizontal, 10)
                .padding(.top, 7)
                .padding(.bottom, 4)
                .background(ZiggyPalette.sidebar)
            }
        }
        .overlay(alignment: .top) {
            if let message = appModel.bannerMessage {
                ZiggyBanner(message: message) { appModel.bannerMessage = nil }
                    .padding(.horizontal, 12)
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: appModel.bannerMessage)
    }

    @ViewBuilder
    private func TabButton(tab: AppModel.Tab, title: String, symbol: String) -> some View {
        let selected = appModel.selectedTab == tab
        Button {
            appModel.selectedTab = tab
        } label: {
            VStack(spacing: 3) {
                Image(systemName: symbol)
                    .font(.system(size: 16, weight: selected ? .semibold : .regular))
                Text(title).font(.caption2.weight(selected ? .semibold : .medium))
            }
            .foregroundStyle(selected ? ZiggyPalette.foreground : ZiggyPalette.mutedForeground)
            .frame(maxWidth: .infinity)
            .frame(height: 42)
            .background(selected ? ZiggyPalette.accent : .clear)
            .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
    }
}

private struct ZiggyBanner: View {
    let message: String
    let onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle")
                .foregroundStyle(ZiggyPalette.destructive)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(ZiggyPalette.foreground)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: onDismiss) { Image(systemName: "xmark") }
                .buttonStyle(.plain)
                .foregroundStyle(ZiggyPalette.mutedForeground)
                .accessibilityLabel("Dismiss")
        }
        .padding(12)
        .background(ZiggyPalette.card, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(ZiggyPalette.border))
    }
}

#Preview {
    RootView()
        .environment(AppModel(credentialStore: InMemoryCredentialStore()))
        .environment(Clerk.preview { $0.isSignedIn = false })
}
