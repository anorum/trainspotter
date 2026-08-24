// The wrist version: aspect, ticker, nothing else. Complications are the real
// product on the watch; this app is where a tap on one lands.
import SwiftUI

@main
struct PDXTrainWatchApp: App {
    var body: some Scene {
        WindowGroup { WatchBoardView() }
    }
}

struct WatchBoardView: View {
    @State private var status: BoardStatus?
    @State private var failed = false

    private let refresh = Timer.publish(every: 30, on: .main, in: .common).autoconnect()

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            content
        }
        .task { await load() }
        .onReceive(refresh) { _ in Task { await load() } }
    }

    /// Silent fetch failures keep the last status on screen; past the
    /// horizon that status is presented as stale rather than as current.
    private var agedOut: Bool {
        guard let generated = status?.generatedAt else { return false }
        return Date.now.timeIntervalSince(generated) > AspectProvider.stalenessHorizon
    }

    @ViewBuilder
    private var content: some View {
        if let crossing = status?.clinton {
            let stale = crossing.stale || agedOut
            let color = Theme.aspectColor(crossing.state, stale: stale)
            let blocked = crossing.state == .blocked && !stale
            VStack(spacing: 6) {
                Flasher(color: color, lit: (true, !blocked), lampSize: 12)
                Text(Theme.aspectWord(crossing.state, stale: stale))
                    .font(.system(.title3, weight: .bold))
                    .foregroundStyle(color)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                Text("12th & Clinton")
                    .font(.caption2)
                    .foregroundStyle(Theme.muted)
                if blocked, let started = crossing.openSession?.startedAt ?? crossing.since {
                    Text(started, style: .relative)
                        .font(.caption.monospaced())
                        .foregroundStyle(Theme.red)
                }
                if let generated = status?.generatedAt {
                    Text("updated \(generated.formatted(date: .omitted, time: .shortened))")
                        .font(.caption2.monospaced())
                        .foregroundStyle(Theme.muted)
                }
            }
        } else if failed {
            Button("Retry") { Task { await load() } }.tint(Theme.amber)
        } else {
            ProgressView().tint(Theme.muted)
        }
    }

    private func load() async {
        do {
            status = try await BoardAPI.fetch()
            failed = false
        } catch {
            failed = status == nil
        }
    }
}
