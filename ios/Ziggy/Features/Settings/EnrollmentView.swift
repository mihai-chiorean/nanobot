import SwiftUI

struct EnrollmentView: View {
    @Environment(AppModel.self) private var appModel
    @Environment(\.colorScheme) private var colorScheme
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
                    Text(appModel.identity.email)
                        .font(.caption)
                        .foregroundStyle(ZiggyPalette.mutedForeground)
                }

                VStack(alignment: .leading, spacing: 9) {
                    ZiggySectionLabel(title: "Private access")
                        .padding(.leading, 3)
                    VStack(spacing: 0) {
                        SecureField("Access code", text: $appModel.accessCode)
                            .textContentType(.password)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .padding(.horizontal, 13)
                            .frame(height: 50)

                        Rectangle().fill(ZiggyPalette.border.opacity(0.65)).frame(height: 1)

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

                    if let errorMessage {
                        Label(errorMessage, systemImage: "exclamationmark.circle")
                            .font(.caption)
                            .foregroundStyle(ZiggyPalette.destructive)
                            .padding(.horizontal, 3)
                    }

                    Button {
                        Task { await appModel.connect() }
                    } label: {
                        Text("Connect")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(ZiggyPalette.primaryForeground)
                            .frame(maxWidth: .infinity)
                            .frame(height: 44)
                            .background(ZiggyPalette.primary)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .disabled(appModel.accessCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .opacity(appModel.accessCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.45 : 1)
                }
                .frame(maxWidth: 440)

                Spacer(minLength: 24)
            }
            .padding(.horizontal, 22)
            .padding(.top, 8)
            .padding(.bottom, 30)
        }
        .background(ZiggyPalette.background)
    }
}
