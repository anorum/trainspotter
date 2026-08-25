// The glance: the crossing's aspect on the home screen and lock screen,
// no app-opening required. WidgetKit refreshes on its own budget (roughly
// every 5-15 minutes in practice), which matches the cameras' own 3-10
// minute cadence - the widget shows when its picture is from, so it never
// pretends to more freshness than it has.
import SwiftUI
import WidgetKit

struct WidgetView: View {
    let entry: AspectEntry
    @Environment(\.widgetFamily) private var family

    private var color: Color { Theme.aspectColor(entry.aspect, stale: entry.stale) }
    private var word: String { Theme.aspectWord(entry.aspect, stale: entry.stale) }

    var body: some View {
        switch family {
        case .accessoryCircular:
            // Lock-screen accessory widgets always render vibrant, which
            // flattens every color to one tint - so the glyph, not the hue,
            // has to carry the state here.
            ZStack {
                AccessoryWidgetBackground()
                Image(systemName: entry.symbol)
                    .font(.system(size: 20, weight: .bold))
            }
            .containerBackground(.clear, for: .widget)
        case .accessoryRectangular:
            VStack(alignment: .leading, spacing: 2) {
                Text("12TH & CLINTON").font(.caption2.weight(.semibold))
                Text(word).font(.headline)
                if let since = entry.blockedSince {
                    Text(since, style: .relative).font(.caption2)
                }
            }
            .containerBackground(.clear, for: .widget)
        default:
            VStack(spacing: 10) {
                Flasher(
                    color: color,
                    lit: (true, entry.aspect != .blocked || entry.stale),
                    lampSize: 16
                )
                Text(word)
                    .font(.system(.title2, weight: .bold).width(.condensed))
                    .foregroundStyle(color)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                if let since = entry.blockedSince {
                    Text(since, style: .relative)
                        .font(.caption2.monospaced())
                        .foregroundStyle(Theme.red)
                } else {
                    Text("12th & Clinton")
                        .font(.caption2)
                        .foregroundStyle(Theme.muted)
                }
                Text(entry.asOf, style: .time)
                    .font(.system(size: 9).monospaced())
                    .foregroundStyle(Theme.muted)
            }
            .containerBackground(Theme.ink, for: .widget)
        }
    }
}

@main
struct PDXTrainWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "PDXTrainAspect", provider: AspectProvider()) { entry in
            WidgetView(entry: entry)
        }
        .configurationDisplayName("Crossing aspect")
        .description("Red or green at 12th & Clinton, at a glance.")
        .supportedFamilies([.systemSmall, .accessoryCircular, .accessoryRectangular])
    }
}
