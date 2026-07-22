import ClerkKit
import ClerkKitUI
import SwiftUI

@main
struct ZiggyApp: App {
    private let clerk: Clerk?
    @State private var appModel: AppModel

    init() {
        let key = (Bundle.main.object(forInfoDictionaryKey: "ZiggyClerkPublishableKey") as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if key.hasPrefix("pk_") {
            let clerk = Clerk.configure(publishableKey: key)
            self.clerk = clerk
            _appModel = State(initialValue: AppModel(authSession: ClerkAuthSession(clerk: clerk)))
        } else {
            clerk = nil
            _appModel = State(initialValue: AppModel())
        }
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if let clerk {
                    RootView()
                        .prefetchClerkImages()
                        .environment(clerk)
                        .onOpenURL { url in
                            Task { try? await clerk.handle(url) }
                        }
                        .onChange(of: clerk.session?.id) { _, _ in
                            Task { await appModel.authenticationDidChange() }
                        }
                } else {
                    AuthenticationConfigurationView()
                }
            }
            .environment(appModel)
            .preferredColorScheme(appModel.theme.colorScheme)
        }
    }
}

private struct AuthenticationConfigurationView: View {
    var body: some View {
        ContentUnavailableView(
            "Authentication unavailable",
            systemImage: "person.crop.circle.badge.exclamationmark",
            description: Text("This build is missing its authentication configuration.")
        )
    }
}
