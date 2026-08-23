// Watch-face complications: the aspect living on the face itself, in the
// fewest pixels the family allows. Same timeline discipline as the phone
// widget - WidgetKit's refresh budget roughly matches the cameras' cadence,
// and the entry carries its own timestamp so nothing claims false freshness.
import SwiftUI
import WidgetKit

struct ComplicationEntry: TimelineEntry {
    let date: Date
    let aspect: Aspect
    let stale: Bool
    let blockedSince: Date?
}

struct ComplicationProvider: TimelineProvider {
    func placeholder(in _: Context) -> ComplicationEntry {
        ComplicationEntry(date: .now, aspect: .clear, stale: false, blockedSince: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (ComplicationEntry) -> Void) {
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(await current()) }
    }

    func getTimeline(in _: Context, completion: @escaping (Timeline<ComplicationEntry>) -> Void) {
        Task {
            completion(Timeline(entries: [await current()], policy: .after(.now + 5 * 60)))
        }
    }

    private func current() async -> ComplicationEntry {
        guard let status = try? await BoardAPI.fetch(), let crossing = status.clinton else {
            return ComplicationEntry(date: .now, aspect: .unknown, stale: true, blockedSince: nil)
        }
        return ComplicationEntry(
            date: status.generatedAt,
            aspect: crossing.state,
            stale: crossing.stale,
            blockedSince: crossing.state == .blocked
                ? (crossing.openSession?.startedAt ?? crossing.since)
                : nil
        )
    }
}

struct ComplicationView: View {
    let entry: ComplicationEntry
    @Environment(\.widgetFamily) private var family

    private var color: Color { Theme.aspectColor(entry.aspect, stale: entry.stale) }
    private var word: String { Theme.aspectWord(entry.aspect, stale: entry.stale) }

    var body: some View {
        switch family {
        case .accessoryCorner:
            Circle()
                .fill(color)
                .widgetLabel {
                    if let since = entry.blockedSince {
                        Text(since, style: .relative)
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
            ZStack {
                Circle().fill(color.opacity(0.25))
                Circle().fill(color).frame(width: 22, height: 22)
            }
            .containerBackground(.clear, for: .widget)
        }
    }
}

@main
struct PDXTrainComplication: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "PDXTrainAspectWatch", provider: ComplicationProvider()) { entry in
            ComplicationView(entry: entry)
        }
        .configurationDisplayName("Crossing aspect")
        .description("Red or green at 12th & Clinton, on the face.")
        .supportedFamilies([
            .accessoryCircular, .accessoryCorner, .accessoryRectangular, .accessoryInline,
        ])
    }
}
