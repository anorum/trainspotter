// The board's identity, in native clothes: ink ground, signal aspects.
import SwiftUI

enum Theme {
    static let ink = Color(red: 0x0E / 255, green: 0x14 / 255, blue: 0x1C / 255)
    static let panel = Color(red: 0x17 / 255, green: 0x1F / 255, blue: 0x2A / 255)
    static let hairline = Color(red: 0x2A / 255, green: 0x35 / 255, blue: 0x42 / 255)
    static let muted = Color(red: 0x93 / 255, green: 0xA0 / 255, blue: 0xAF / 255)
    static let red = Color(red: 0xE5 / 255, green: 0x48 / 255, blue: 0x4D / 255)
    static let green = Color(red: 0x46 / 255, green: 0xA7 / 255, blue: 0x58 / 255)
    static let amber = Color(red: 0xFF / 255, green: 0xB2 / 255, blue: 0x24 / 255)

    static func aspectColor(_ aspect: Aspect, stale: Bool) -> Color {
        if stale { return amber }
        switch aspect {
        case .blocked: return red
        case .clear: return green
        case .unknown: return amber
        }
    }

    static func aspectWord(_ aspect: Aspect, stale: Bool) -> String {
        stale ? "NO SIGNAL" : aspect.rawValue
    }
}

/// The twin-lamp signal housing, the form of the flashers at the real
/// crossing. Static in widgets (WidgetKit does not animate); the app version
/// alternates the lamps when blocked.
struct Flasher: View {
    let color: Color
    var lit: (Bool, Bool) = (true, true)
    var lampSize: CGFloat = 18

    var body: some View {
        HStack(spacing: lampSize * 0.4) {
            lamp(on: lit.0)
            lamp(on: lit.1)
        }
        .padding(.horizontal, lampSize * 0.45)
        .padding(.vertical, lampSize * 0.35)
        .background(Theme.ink, in: Capsule())
        .overlay(Capsule().stroke(Theme.hairline, lineWidth: 1))
    }

    private func lamp(on: Bool) -> some View {
        Circle()
            .fill(on ? color : color.opacity(0.18))
            .frame(width: lampSize, height: lampSize)
            .shadow(color: on ? color.opacity(0.8) : .clear, radius: lampSize * 0.35)
    }
}
