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
                EnrollmentView()
            case .failed(let message):
                EnrollmentView(errorMessage: message)
            case .ready:
                ZiggyTabView()
            }
        }
        .task { await appModel.start() }
        .tint(ZiggyPalette.teal)
    }
}

private struct ZiggyLaunchView: View {
    let label: String

    var body: some View {
        VStack(spacing: 20) {
            Image("ZiggyAvatar")
                .resizable()
                .scaledToFit()
                .frame(width: 112, height: 112)
                .clipShape(RoundedRectangle(cornerRadius: 8))
            Text("Ziggy")
                .font(.largeTitle.bold())
                .foregroundStyle(ZiggyPalette.ink)
            ProgressView(label)
                .foregroundStyle(ZiggyPalette.mutedInk)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(ZiggyPalette.canvas)
    }
}

private struct ZiggyTabView: View {
    @Environment(AppModel.self) private var appModel

    var body: some View {
        @Bindable var appModel = appModel

        TabView(selection: $appModel.selectedTab) {
            ChatListView()
                .tag(AppModel.Tab.chats)
                .tabItem { Label("Chats", systemImage: "bubble.left.and.bubble.right") }

            WorkListView()
                .tag(AppModel.Tab.work)
                .tabItem { Label("Work", systemImage: "bolt.horizontal.circle") }

            SettingsView()
                .tag(AppModel.Tab.settings)
                .tabItem { Label("Settings", systemImage: "gearshape") }
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
}

private struct ZiggyBanner: View {
    let message: String
    let onDismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(ZiggyPalette.amber)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(ZiggyPalette.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: onDismiss) { Image(systemName: "xmark") }
                .buttonStyle(.plain)
                .foregroundStyle(ZiggyPalette.mutedInk)
                .accessibilityLabel("Dismiss")
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(ZiggyPalette.line))
    }
}

#Preview {
    RootView()
        .environment(AppModel(credentialStore: InMemoryCredentialStore()))
}
