// One timeline for every glanceable surface. The phone widget and the watch
// complications ask the same question of the same endpoint on the same
// schedule, so the entry and provider live here and each platform contributes
// only its views.
import WidgetKit

struct AspectEntry: TimelineEntry {
    /// When WidgetKit should start showing this entry - a display
    /// transition time, never a claim about the data's age.
    let date: Date
    /// When the board last spoke - or, if it never answered, when we
    /// last asked. The only timestamp a surface may show, so an aged-out
    /// entry cannot look fresher than its data.
    let asOf: Date
    let aspect: Aspect
    let stale: Bool
    let blockedSince: Date?

    /// Distinct per aspect so a glance survives monochrome rendering: iOS
    /// lock-screen accessory widgets always render vibrant, and watchOS
    /// renders accented on tinted faces - hue alone would say nothing there.
    var symbol: String {
        if stale { return "questionmark" }
        switch aspect {
        case .blocked: return "train.side.front.car"
        case .clear: return "checkmark"
        case .unknown: return "questionmark"
        }
    }
}

struct AspectProvider: TimelineProvider {
    func placeholder(in _: Context) -> AspectEntry {
        AspectEntry(date: .now, asOf: .now, aspect: .clear, stale: false, blockedSince: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (AspectEntry) -> Void) {
        // The widget gallery must not wait on the network.
        if context.isPreview {
            completion(placeholder(in: context))
            return
        }
        Task { completion(await current()) }
    }

    func getTimeline(in _: Context, completion: @escaping (Timeline<AspectEntry>) -> Void) {
        Task {
            // Ask again in five minutes; WidgetKit throttles as it sees fit,
            // and the cameras only refresh every three to ten anyway. If it
            // never comes back - budget exhausted, phone offline - a second,
            // degraded entry takes over at the horizon so a blocked duration
            // cannot keep counting on data nobody has checked.
            let now = await current()
            // An entry already stale needs no successor - and dating one in
            // the past would hand WidgetKit an out-of-order timeline.
            var entries = [now]
            if !now.stale {
                entries.append(AspectEntry(
                    date: now.asOf + BoardStatus.stalenessHorizon, asOf: now.asOf,
                    aspect: now.aspect, stale: true, blockedSince: nil))
            }
            completion(Timeline(entries: entries, policy: .after(.now + 5 * 60)))
        }
    }

    private func current() async -> AspectEntry {
        guard let status = try? await BoardAPI.fetch(), let crossing = status.clinton else {
            return AspectEntry(date: .now, asOf: .now, aspect: .unknown, stale: true, blockedSince: nil)
        }
        // The server can hand over a status that is already past the
        // horizon (a wedged generator behind a live server, a cached
        // response); age it here like every other surface does.
        let stale = crossing.stale || status.agedOut(at: .now)
        return AspectEntry(
            date: .now,
            asOf: status.generatedAt,
            aspect: crossing.state,
            stale: stale,
            // A stale feed's last word may have been "blocked"; a running
            // duration would claim a freshness the cameras cannot back.
            blockedSince: crossing.state == .blocked && !stale
                ? (crossing.openSession?.startedAt ?? crossing.since)
                : nil
        )
    }
}
