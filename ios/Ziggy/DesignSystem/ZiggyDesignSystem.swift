import SwiftUI

enum ZiggyPalette {
    static let canvas = Color(light: Color(red: 0.965, green: 0.970, blue: 0.972), dark: Color(red: 0.095, green: 0.105, blue: 0.115))
    static let panel = Color(light: .white, dark: Color(red: 0.135, green: 0.145, blue: 0.155))
    static let ink = Color(light: Color(red: 0.105, green: 0.120, blue: 0.135), dark: Color(red: 0.925, green: 0.925, blue: 0.900))
    static let mutedInk = Color(light: Color(red: 0.360, green: 0.380, blue: 0.390), dark: Color(red: 0.690, green: 0.700, blue: 0.680))
    static let teal = Color(light: Color(red: 0.035, green: 0.420, blue: 0.430), dark: Color(red: 0.250, green: 0.720, blue: 0.700))
    static let amber = Color(light: Color(red: 0.700, green: 0.400, blue: 0.060), dark: Color(red: 0.950, green: 0.680, blue: 0.260))
    static let coral = Color(light: Color(red: 0.700, green: 0.180, blue: 0.150), dark: Color(red: 0.950, green: 0.430, blue: 0.360))
    static let moss = Color(light: Color(red: 0.270, green: 0.470, blue: 0.180), dark: Color(red: 0.520, green: 0.720, blue: 0.360))
    static let line = Color(light: Color.black.opacity(0.10), dark: Color.white.opacity(0.13))
}

private extension Color {
    init(light: Color, dark: Color) {
        #if os(iOS)
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? UIColor(dark) : UIColor(light)
        })
        #else
        self = light
        #endif
    }
}

struct ZiggyMarkdownText: View {
    let markdown: String
    var font: Font = .body

    var body: some View {
        Group {
            if let attributed = try? AttributedString(markdown: markdown, options: .init(interpretedSyntax: .full)) {
                Text(attributed)
            } else {
                Text(markdown)
            }
        }
        .font(font)
        .foregroundStyle(ZiggyPalette.ink)
        .textSelection(.enabled)
        .tint(ZiggyPalette.teal)
    }
}

struct ZiggySectionLabel: View {
    let title: String

    var body: some View {
        Text(title.uppercased())
            .font(.caption.weight(.semibold))
            .foregroundStyle(ZiggyPalette.mutedInk)
    }
}
