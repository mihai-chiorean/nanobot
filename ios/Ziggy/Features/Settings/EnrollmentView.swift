import ClerkKit
import ClerkKitUI
import SwiftUI

struct EnrollmentView: View {
    @Environment(AppModel.self) private var appModel
    @Environment(Clerk.self) private var clerk
    @Environment(\.colorScheme) private var colorScheme
    @State private var showsAuthentication = false
    var errorMessage: String?

    var body: some View {
        @Bindable var appModel = appModel

        ScrollView {
            VStack(spacing: 24) {
                HStack {
                    Spacer()
                    Button {
                        appModel.toggleTheme(currentScheme: colorScheme)
                    } label: {
                        Image(systemName: colorScheme == .dark ? "sun.max" : "moon")
                    }
                    .buttonStyle(PWAIconButton())
                    .accessibilityLabel("Toggle theme")
                }

                VStack(spacing: 11) {
                    Image("ZiggyAvatar")
                        .resizable()
                        .scaledToFit()
                        .frame(width: 72, height: 72)
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                        .accessibilityLabel("Ziggy")
                    Text("Ziggy")
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(ZiggyPalette.foreground)
                    Text(clerk.user?.primaryEmailAddress?.emailAddress ?? "Private AI workspace")
                        .font(.caption)
                        .foregroundStyle(ZiggyPalette.mutedForeground)
                }

                VStack(alignment: .leading, spacing: 9) {
                    ZiggySectionLabel(title: "Account")
                        .padding(.leading, 3)

                    if let errorMessage {
                        Label(errorMessage, systemImage: "exclamationmark.circle")
                            .font(.caption)
                            .foregroundStyle(ZiggyPalette.destructive)
                            .padding(.horizontal, 3)
                    }

                    Button {
                        guard appModel.isServerURLAllowed else {
                            appModel.bannerMessage = "Use the production Ziggy server or a localhost URL for development."
                            return
                        }
                        if clerk.user == nil {
                            showsAuthentication = true
                        } else {
                            Task { await appModel.connectAuthenticated() }
                        }
                    } label: {
                        Label(
                            clerk.user == nil ? "Sign in" : "Retry connection",
                            systemImage: clerk.user == nil ? "person.badge.key" : "arrow.clockwise"
                        )
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(ZiggyPalette.primaryForeground)
                            .frame(maxWidth: .infinity)
                            .frame(height: 44)
                            .background(ZiggyPalette.primary)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .disabled(!appModel.isServerURLAllowed)
                    .opacity(appModel.isServerURLAllowed ? 1 : 0.55)

                    if clerk.user != nil {
                        Button {
                            Task { await appModel.signOut() }
                        } label: {
                            Label("Sign out", systemImage: "rectangle.portrait.and.arrow.right")
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(ZiggyPalette.foreground)
                                .frame(maxWidth: .infinity)
                                .frame(height: 42)
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint("Use a different Ziggy account")
                    }

                    VStack(spacing: 0) {
                        DisclosureGroup("Server") {
                            TextField("Server URL", text: $appModel.serverURLText)
                                .keyboardType(.URL)
                                .textContentType(.URL)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .padding(.top, 10)
                        }
                        .font(.subheadline)
                        .foregroundStyle(ZiggyPalette.mutedForeground)
                        .padding(.horizontal, 13)
                        .frame(minHeight: 48)
                    }
                    .background(ZiggyPalette.card)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(ZiggyPalette.border.opacity(0.75)))

                    if !appModel.serverURLText.isEmpty && !appModel.isServerURLAllowed {
                        Label("Use the production host or localhost for development.", systemImage: "exclamationmark.circle")
                            .font(.caption)
                            .foregroundStyle(ZiggyPalette.destructive)
                            .padding(.horizontal, 3)
                    }
                }
                .frame(maxWidth: 440)

                Spacer(minLength: 24)
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 30)
        }
        .background(ZiggyPalette.background)
        .sheet(isPresented: $showsAuthentication) {
            AuthView(mode: .signInOrUp)
        }
    }
}
