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

    @ViewBuilder
    private var content: some View {
        if let crossing = status?.clinton {
            let color = Theme.aspectColor(crossing.state, stale: crossing.stale)
            let blocked = crossing.state == .blocked && !crossing.stale
            VStack(spacing: 6) {
                Flasher(color: color, lit: (true, !blocked), lampSize: 12)
                Text(Theme.aspectWord(crossing.state, stale: crossing.stale))
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
