import SwiftUI
import Textual

enum ZiggyPalette {
    // Mirrors webui/src/globals.css. Color is reserved for status and errors.
    static let background = Color(lightHex: 0xFFFFFF, darkHex: 0x1A1A1A)
    static let foreground = Color(lightHex: 0x1F1F20, darkHex: 0xF5F5F6)
    static let card = Color(lightHex: 0xFFFFFF, darkHex: 0x1F1F1F)
    static let secondary = Color(lightHex: 0xF5F5F5, darkHex: 0x1F1F1F)
    static let muted = Color(lightHex: 0xF5F5F5, darkHex: 0x212121)
    static let mutedForeground = Color(lightHex: 0x737373, darkHex: 0x999999)
    static let accent = Color(lightHex: 0xF5F5F5, darkHex: 0x262626)
    static let border = Color(lightHex: 0xE5E5E5, darkHex: 0x2E2E2E)
    static let sidebar = Color(lightHex: 0xFAFAFA, darkHex: 0x1F1F1F)
    static let primary = Color(lightHex: 0x29292B, darkHex: 0xFAFAFA)
    static let primaryForeground = Color(lightHex: 0xFAFAFA, darkHex: 0x171717)

    static let emerald = Color(lightHex: 0x15803D, darkHex: 0x4ADE80)
    static let amber = Color(lightHex: 0xB45309, darkHex: 0xFCD34D)
    static let destructive = Color(lightHex: 0xDC2626, darkHex: 0xF87171)

    // Compatibility names used by feature components.
    static let canvas = background
    static let panel = card
    static let ink = foreground
    static let mutedInk = mutedForeground
    static let line = border
    static let teal = foreground
    static let coral = destructive
    static let moss = emerald
}

private extension Color {
    init(lightHex: UInt, darkHex: UInt) {
        self.init(uiColor: UIColor { traits in
            UIColor(hex: traits.userInterfaceStyle == .dark ? darkHex : lightHex)
        })
    }
}

private extension UIColor {
    convenience init(hex: UInt) {
        self.init(
            red: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: 1
        )
    }
}

struct ZiggyMarkdownText: View {
    let markdown: String
    var font: Font = .body

    var body: some View {
        Group {
            if RichContentSafety.allowsStructuredMarkdown(markdown) {
                StructuredText(markdown: markdown)
                    .textual.structuredTextStyle(.gitHub)
                    .textual.textSelection(.enabled)
            } else {
                Text(markdown)
                    .lineSpacing(3)
                    .textSelection(.enabled)
            }
        }
        .font(font)
        .foregroundStyle(ZiggyPalette.foreground.opacity(0.94))
        .tint(ZiggyPalette.foreground)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ZiggySectionLabel: View {
    let title: String

    var body: some View {
        Text(title)
            .font(.caption.weight(.medium))
            .foregroundStyle(ZiggyPalette.mutedForeground)
    }
}

struct PWAIconButton: ButtonStyle {
    var size: CGFloat = 34
    var foreground: Color = ZiggyPalette.mutedForeground

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 15, weight: .medium))
            .foregroundStyle(foreground)
            .frame(width: size, height: size)
            .background(configuration.isPressed ? ZiggyPalette.accent : .clear)
            .clipShape(RoundedRectangle(cornerRadius: 7))
    }
}

struct PWAGroup<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        VStack(spacing: 0) { content }
            .background(ZiggyPalette.card.opacity(0.8))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(ZiggyPalette.border.opacity(0.7)))
    }
}
