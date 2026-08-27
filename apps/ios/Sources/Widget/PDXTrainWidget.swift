// The glance: the crossing's aspect on the home screen and lock screen,
// no app-opening required. WidgetKit refreshes on its own budget (roughly
// every 5-15 minutes in practice), which matches the cameras' own 3-10
// minute cadence - the widget shows when its picture is from, so it never
// pretends to more freshness than it has.
import AppIntents
import SwiftUI
import WidgetKit

/// The button behind a tap on the widget's clock: performing any intent
/// makes WidgetKit reload the timeline, which is the whole point - the
/// fetch happens in the provider, as always.
struct RefreshCrossingIntent: AppIntent {
    static let title: LocalizedStringResource = "Refresh the crossing"
    static let isDiscoverable = false
    func perform() async throws -> some IntentResult { .result() }
}

/// The "as of" footer, tappable to ask again right now.
struct AsOfRefresh: View {
    let asOf: Date

    var body: some View {
        Button(intent: RefreshCrossingIntent()) {
            HStack(spacing: 3) {
                Text(asOf, style: .time)
                Image(systemName: "arrow.clockwise")
            }
            .font(.system(size: 9).monospaced())
            .foregroundStyle(Theme.muted)
        }
        .buttonStyle(.plain)
    }
}

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
        case .systemMedium:
            MediumWidgetView(entry: entry)
        default:
            VStack(spacing: 10) {
                Flasher(aspect: entry.aspect, stale: entry.stale, lampSize: 16)
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
                AsOfRefresh(asOf: entry.asOf)
            }
            .containerBackground(Theme.ink, for: .widget)
        }
    }
}

/// The roomier glance: the same truth as the small widget with the labels
/// spelled out instead of stacked.
struct MediumWidgetView: View {
    let entry: AspectEntry

    private var color: Color { Theme.aspectColor(entry.aspect, stale: entry.stale) }
    private var word: String { Theme.aspectWord(entry.aspect, stale: entry.stale) }

    var body: some View {
        HStack(spacing: 18) {
            VStack(spacing: 8) {
                Flasher(aspect: entry.aspect, stale: entry.stale, lampSize: 18)
                Text(word)
                    .font(.system(.title2, weight: .bold).width(.condensed))
                    .foregroundStyle(color)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity)
            VStack(alignment: .leading, spacing: 5) {
                Text("12TH & CLINTON")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Theme.muted)
                if let since = entry.blockedSince {
                    Text("gates down")
                        .font(.caption)
                        .foregroundStyle(Theme.red)
                    Text(since, style: .relative)
                        .font(.headline.monospaced())
                        .foregroundStyle(Theme.red)
                } else {
                    Text(entry.stale ? "cameras quiet" : "as of")
                        .font(.caption)
                        .foregroundStyle(Theme.muted)
                }
                Spacer(minLength: 0)
                AsOfRefresh(asOf: entry.asOf)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .containerBackground(Theme.ink, for: .widget)
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
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular, .accessoryRectangular])
    }
}
