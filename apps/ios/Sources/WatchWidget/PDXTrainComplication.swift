// Watch-face complications: the aspect living on the face itself, in the
// fewest pixels the family allows. Same timeline discipline as the phone
// widget - WidgetKit's refresh budget roughly matches the cameras' cadence,
// and the entry carries its own timestamp so nothing claims false freshness.
import SwiftUI
import WidgetKit

struct ComplicationView: View {
    let entry: AspectEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        // Every family draws on the face's own background.
        content.containerBackground(.clear, for: .widget)
    }

    @ViewBuilder
    private var content: some View {
        switch family {
        case .accessoryCorner:
            Circle()
                .fill(entry.color)
                .widgetLabel {
                    // Self-describing on tinted faces, where the dot's color
                    // flattens away: a bare duration would not say what it is.
                    if let since = entry.blockedSince {
                        Text("Blocked \(since, style: .relative)")
                    } else {
                        Text(entry.word)
                    }
                }
        case .accessoryInline:
            if let since = entry.blockedSince {
                Text("Blocked \(since, style: .relative)")
            } else {
                Text("12th & Clinton: \(entry.word.lowercased())")
            }
        case .accessoryRectangular:
            VStack(alignment: .leading, spacing: 2) {
                Text("12TH & CLINTON").font(.caption2.weight(.semibold))
                Text(entry.word).font(.headline).foregroundStyle(entry.color)
                if let since = entry.blockedSince {
                    Text(since, style: .relative).font(.caption2)
                }
            }
        default:
            // Shape carries the state, not hue alone: watchOS renders
            // complications in accented mode on tinted faces, which flattens
            // every color to one tint - a red-vs-green dot would say nothing
            // there. The glyph differs per aspect, so it reads on any face.
            ZStack {
                Circle().fill(entry.color.opacity(0.25))
                Image(systemName: entry.symbol)
                    .font(.system(size: 20, weight: .bold))
                    .foregroundStyle(entry.color)
            }
        }
    }
}

@main
struct PDXTrainComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "PDXTrainAspectWatch", provider: AspectProvider()) { entry in
            ComplicationView(entry: entry)
        }
        .configurationDisplayName("Crossing aspect")
        .description("Red or green at 12th & Clinton, on the face.")
        .supportedFamilies([
            .accessoryCircular, .accessoryCorner, .accessoryRectangular, .accessoryInline,
        ])
    }
}
