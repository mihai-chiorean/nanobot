import SwiftUI

struct EnrollmentView: View {
    @Environment(AppModel.self) private var appModel
    var errorMessage: String?

    var body: some View {
        @Bindable var appModel = appModel

        ScrollView {
            VStack(spacing: 24) {
                Image("ZiggyAvatar")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 132, height: 132)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .accessibilityLabel("Ziggy")

                VStack(spacing: 8) {
                    Text("Ziggy")
                        .font(.largeTitle.bold())
                        .foregroundStyle(ZiggyPalette.ink)
                    Text(appModel.identity.email)
                        .font(.subheadline)
                        .foregroundStyle(ZiggyPalette.mutedInk)
                }

                VStack(alignment: .leading, spacing: 14) {
                    ZiggySectionLabel(title: "Private access")
                    SecureField("Access code", text: $appModel.accessCode)
                        .textContentType(.password)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .padding(12)
                        .background(ZiggyPalette.panel, in: RoundedRectangle(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(ZiggyPalette.line))

                    DisclosureGroup("Server") {
                        TextField("Server URL", text: $appModel.serverURLText)
                            .keyboardType(.URL)
                            .textContentType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .padding(.top, 10)
                    }
                    .font(.subheadline)
                    .foregroundStyle(ZiggyPalette.mutedInk)

                    if let errorMessage {
                        Label(errorMessage, systemImage: "exclamationmark.circle.fill")
                            .font(.subheadline)
                            .foregroundStyle(ZiggyPalette.coral)
                    }

                    Button {
                        Task { await appModel.connect() }
                    } label: {
                        Label("Connect", systemImage: "lock.open.fill")
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 5)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .disabled(appModel.accessCode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                .frame(maxWidth: 440)
            }
            .padding(.horizontal, 24)
            .padding(.top, 64)
            .padding(.bottom, 32)
        }
        .background(ZiggyPalette.canvas)
    }
}
