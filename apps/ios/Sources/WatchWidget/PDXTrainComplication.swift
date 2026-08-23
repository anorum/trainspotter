// Watch-face complications: the aspect living on the face itself, in the
// fewest pixels the family allows. Same timeline discipline as the phone
// widget - WidgetKit's refresh budget roughly matches the cameras' cadence,
// and the entry carries its own timestamp so nothing claims false freshness.
import SwiftUI
import WidgetKit

struct ComplicationView: View {
    let entry: AspectEntry
    @Environment(\.widgetFamily) private var family

    private var color: Color { Theme.aspectColor(entry.aspect, stale: entry.stale) }
    private var word: String { Theme.aspectWord(entry.aspect, stale: entry.stale) }


    var body: some View {
        switch family {
        case .accessoryCorner:
            Circle()
                .fill(color)
                .widgetLabel {
                    // Self-describing on tinted faces, where the dot's color
                    // flattens away: a bare duration would not say what it is.
                    if let since = entry.blockedSince {
                        Text("Blocked \(since, style: .relative)")
                    } else {
                        Text(word)
                    }
                }
                .containerBackground(.clear, for: .widget)
        case .accessoryInline:
            if let since = entry.blockedSince {
                Text("Blocked \(since, style: .relative)")
                    .containerBackground(.clear, for: .widget)
            } else {
                Text("12th & Clinton: \(word.lowercased())")
                    .containerBackground(.clear, for: .widget)
            }
        case .accessoryRectangular:
            VStack(alignment: .leading, spacing: 2) {
                Text("12TH & CLINTON").font(.caption2.weight(.semibold))
                Text(word).font(.headline).foregroundStyle(color)
                if let since = entry.blockedSince {
                    Text(since, style: .relative).font(.caption2)
                }
            }
            .containerBackground(.clear, for: .widget)
        default:
            // Shape carries the state, not hue alone: watchOS renders
            // complications in accented mode on tinted faces, which flattens
            // every color to one tint - a red-vs-green dot would say nothing
            // there. The glyph differs per aspect, so it reads on any face.
            ZStack {
                Circle().fill(color.opacity(0.25))
                Image(systemName: entry.symbol)
                    .font(.system(size: 20, weight: .bold))
                    .foregroundStyle(color)
            }
            .containerBackground(.clear, for: .widget)
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
