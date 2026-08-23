// The app is deliberately one screen: the crossing's aspect, big enough to
// read from across a room, honest about its own freshness. The widget is the
// product; this is its home.
import SwiftUI

@main
struct PDXTrainApp: App {
    var body: some Scene {
        WindowGroup {
            BoardView()
        }
    }
}

struct BoardView: View {
    @State private var status: BoardStatus?
    @State private var failed = false
    @State private var flashPhase = false

    private let refresh = Timer.publish(every: 30, on: .main, in: .common).autoconnect()
    private let flash = Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            content
        }
        .task { await load() }
        .onReceive(refresh) { _ in Task { await load() } }
        .onReceive(flash) { _ in
            if blocked { flashPhase.toggle() }
        }
    }

    private var blocked: Bool {
        guard let crossing = status?.clinton else { return false }
        return crossing.state == .blocked && !crossing.stale
    }

    @ViewBuilder
    private var content: some View {
        if let crossing = status?.clinton {
            let color = Theme.aspectColor(crossing.state, stale: crossing.stale)
            VStack(spacing: 24) {
                Spacer()
                Flasher(
                    color: color,
                    lit: blocked ? (flashPhase, !flashPhase) : (true, true),
                    lampSize: 44
                )
                Text(Theme.aspectWord(crossing.state, stale: crossing.stale))
                    .font(.system(size: 52, weight: .bold, design: .default).width(.condensed))
                    .foregroundStyle(color)
                Text("12TH & CLINTON")
                    .font(.system(.title3, design: .default).width(.condensed))
                    .kerning(2)
                    .foregroundStyle(.white)
                if blocked, let started = crossing.openSession?.startedAt ?? crossing.since {
                    Text("Blocked for \(started, style: .relative)")
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(Theme.red)
                } else if let since = crossing.since {
                    Text("\(crossing.state == .clear ? "Clear" : "Unknown") since \(since.formatted(date: .omitted, time: .shortened))")
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(Theme.muted)
                }
                if let feed = status?.feed, feed.status != "ok" {
                    Text(feedLine(feed))
                        .font(.footnote)
                        .foregroundStyle(Theme.amber)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
                Spacer()
                if let generated = status?.generatedAt {
                    Text("updated \(generated.formatted(date: .omitted, time: .standard))")
                        .font(.caption.monospaced())
                        .foregroundStyle(Theme.muted)
                        .padding(.bottom, 12)
                }
            }
        } else if failed {
            VStack(spacing: 12) {
                Text("The board is not answering.")
                    .foregroundStyle(Theme.muted)
                Button("Try again") { Task { await load() } }
                    .tint(Theme.amber)
            }
        } else {
            ProgressView().tint(Theme.muted)
        }
    }

    private func feedLine(_ feed: FeedHealth) -> String {
        switch feed.status {
        case "upstream_down": return "ODOT's camera server is not answering - the pipeline is healthy and waiting."
        case "upstream_stale": return "ODOT is serving no new pictures - the pipeline is healthy and waiting."
        case "capture_stale": return "Our capture service has been quiet - this one is on us."
        default: return ""
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
