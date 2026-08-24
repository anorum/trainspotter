// The app is deliberately one screen: the crossing where it lives, on a map
// quiet enough that only the streets and the rail line read, with the
// crossing's own flasher as the pin and a plaque that says the aspect big
// enough to read from across a room. The widget is the product; this is
// its home - and the place a "you are here" can land later.
import MapKit
import SwiftUI

@main
struct PDXTrainApp: App {
    var body: some Scene {
        WindowGroup {
            BoardView()
        }
    }
}

enum Crossing {
    static let clinton = CLLocationCoordinate2D(latitude: 45.5036, longitude: -122.65381)

    /// Close enough that the rail line's diagonal through the grid is legible.
    static var home: MapCameraPosition {
        .region(MKCoordinateRegion(
            center: clinton,
            latitudinalMeters: 900,
            longitudinalMeters: 900))
    }
}

struct BoardView: View {
    @State private var status: BoardStatus?
    @State private var failed = false
    @State private var flashPhase = false
    // Advanced on every refresh tick so age-based staleness re-evaluates
    // even when the fetch fails and nothing else changes.
    @State private var now = Date()
    // State rather than a constant so a later "fit me and the crossing" is a
    // reassignment, not a restructure.
    @State private var camera = Crossing.home

    private let refresh = Timer.publish(every: 30, on: .main, in: .common).autoconnect()
    private let flash = Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()

    private let plaqueShape = UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28)
    private let tickerFont = Font.system(.body, design: .monospaced)

    private var crossing: CrossingNow? { status?.clinton }

    private var stale: Bool {
        guard let status, let crossing else { return false }
        return crossing.stale || status.agedOut(at: now)
    }

    private var blocked: Bool {
        guard let crossing else { return false }
        return crossing.state == .blocked && !stale
    }

    private var color: Color {
        guard let crossing else { return Theme.muted }
        return Theme.aspectColor(crossing.state, stale: stale)
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            map
            plaque
        }
        .background(Theme.ink)
        .preferredColorScheme(.dark)
        .task { await load() }
        .onReceive(refresh) { _ in
            now = .now
            Task { await load() }
        }
        .onReceive(flash) { _ in
            if blocked { flashPhase.toggle() }
        }
    }

    private var map: some View {
        Map(position: $camera) {
            Annotation("12th & Clinton", coordinate: Crossing.clinton, anchor: .center) {
                crossingPin
            }
            .annotationTitles(.hidden)
        }
        .mapStyle(.standard(elevation: .flat, emphasis: .muted, pointsOfInterest: .excludingAll))
        .mapControlVisibility(.hidden)
        .ignoresSafeArea()
        .overlay(alignment: .topTrailing) { recenterButton }
    }

    private var crossingPin: some View {
        ZStack {
            // The aspect bleeds into the streets: the one loud thing.
            Circle()
                .fill(color.opacity(crossing == nil ? 0 : 0.38))
                .frame(width: 190, height: 190)
                .blur(radius: 34)
            Flasher(
                color: color,
                lit: blocked ? (flashPhase, !flashPhase) : (true, true),
                lampSize: 22
            )
        }
    }

    private var recenterButton: some View {
        Button {
            withAnimation(.snappy) { camera = Crossing.home }
        } label: {
            Image(systemName: "scope")
                .font(.body.weight(.semibold))
                .foregroundStyle(.white)
                .padding(10)
                .background(Theme.panel, in: Circle())
                .overlay(Circle().stroke(Theme.hairline, lineWidth: 1))
        }
        .accessibilityLabel("Return to the crossing")
        .padding(16)
    }

    private var plaque: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("12TH & CLINTON")
                .font(.system(.subheadline).width(.condensed))
                .kerning(2)
                .foregroundStyle(Theme.muted)
            HStack(alignment: .lastTextBaseline) {
                Text(aspectWord)
                    .font(.system(size: 56, weight: .bold).width(.condensed))
                    .foregroundStyle(color)
                    .minimumScaleFactor(0.7)
                    .lineLimit(1)
                Spacer()
                if let generated = status?.generatedAt {
                    Text("updated \(generated.formatted(date: .omitted, time: .standard))")
                        .font(.caption.monospaced())
                        .foregroundStyle(Theme.muted)
                }
            }
            ticker
        }
        .padding(.horizontal, 24)
        .padding(.top, 22)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.ink, in: plaqueShape)
        .overlay(plaqueShape.stroke(Theme.hairline, lineWidth: 1))
    }

    private var aspectWord: String {
        guard let crossing else { return failed ? "NO ANSWER" : "LOOKING" }
        return Theme.aspectWord(crossing.state, stale: stale)
    }

    @ViewBuilder
    private var ticker: some View {
        if let crossing {
            if blocked, let started = crossing.openSession?.startedAt ?? crossing.since {
                Text("Blocked for \(started, style: .relative)")
                    .font(tickerFont)
                    .foregroundStyle(Theme.red)
            } else if let since = crossing.since {
                Text("\(crossing.state == .clear ? "Clear" : "Unknown") since \(since.formatted(date: .omitted, time: .shortened))")
                    .font(tickerFont)
                    .foregroundStyle(Theme.muted)
            }
            if let feed = status?.feed, feed.status != "ok" {
                Text(feedLine(feed))
                    .font(.footnote)
                    .foregroundStyle(Theme.amber)
            }
        } else if failed {
            HStack {
                Text("The board is not answering.")
                    .font(tickerFont)
                    .foregroundStyle(Theme.muted)
                Spacer()
                Button("Try again") { Task { await load() } }
                    .tint(Theme.amber)
            }
        } else {
            Text("Asking the cameras")
                .font(tickerFont)
                .foregroundStyle(Theme.muted)
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
