// The app is deliberately one screen: the crossing where it lives, on a map
// quiet enough that only the streets and the rail line read, with the
// crossing's own flasher as the pin. A sheet that never dismisses rides over
// it: collapsed, a plaque that says the aspect big enough to read from across
// a room; pulled up, the camera's own frame and the train sheet. The widget is
// the product; this is its home - and the place a "you are here" can land later.
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
    @State private var sessions: [TrainSession] = []
    @State private var detent: PresentationDetent = .height(200)
    @State private var photoObservation: CrossingNow.Observation?

    private let refresh = Timer.publish(every: 30, on: .main, in: .common).autoconnect()
    private let flash = Timer.publish(every: 0.5, on: .main, in: .common).autoconnect()

    private let tickerFont = Font.system(.body, design: .monospaced)
    private let frameShape = RoundedRectangle(cornerRadius: 12)

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
        map
            .background(Theme.ink)
            .preferredColorScheme(.dark)
            .sheet(isPresented: .constant(true)) { boardSheet }
            // A physical tick when the aspect actually changes - the pocket
            // version of the lamps switching.
            .sensoryFeedback(.impact(weight: .medium), trigger: crossing?.state)
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

    private var boardSheet: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                plaque
                snapshot
                trainSheet
            }
            .padding(.horizontal, 24)
            .padding(.top, 18)
            .padding(.bottom, 32)
        }
        .scrollIndicators(.hidden)
        .presentationDetents([.height(200), .medium, .large], selection: $detent)
        .presentationBackgroundInteraction(.enabled(upThrough: .medium))
        .presentationBackground(Theme.ink)
        .presentationDragIndicator(.visible)
        .interactiveDismissDisabled()
        .preferredColorScheme(.dark)
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
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The picture behind the verdict: the exact frame the classifier
    /// judged, straight from the crossing's camera.
    @ViewBuilder
    private var snapshot: some View {
        if let observation = crossing?.latestObservation {
            VStack(alignment: .leading, spacing: 8) {
                sectionLabel("FROM THE CAMERA")
                AsyncImage(url: BoardAPI.frameURL(observation.objectKey)) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().aspectRatio(contentMode: .fit)
                    case .failure:
                        // Honest failure beats an eternal spinner; the next
                        // observation brings a new URL and a fresh attempt.
                        frameShape
                            .fill(Theme.panel)
                            .aspectRatio(4 / 3, contentMode: .fit)
                            .overlay {
                                Text("The picture did not arrive.")
                                    .font(.footnote)
                                    .foregroundStyle(Theme.muted)
                            }
                    default:
                        frameShape
                            .fill(Theme.panel)
                            .aspectRatio(4 / 3, contentMode: .fit)
                            .overlay(ProgressView().tint(Theme.muted))
                    }
                }
                .clipShape(frameShape)
                .contentShape(frameShape)
                .onTapGesture { photoObservation = observation }
                // Item-bound, so the open photo is pinned to the frame that
                // was tapped: a refresh must not swap or dismiss it mid-look.
                .fullScreenCover(item: $photoObservation) { tapped in
                    FramePhotoView(observation: tapped)
                }
                .overlay(frameShape.stroke(Theme.hairline, lineWidth: 1))
                FrameCaption(observation: observation)
            }
        }
    }

    /// The dispatcher's record: every blockage, newest first.
    @ViewBuilder
    private var trainSheet: some View {
        if !sessions.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                sectionLabel("TRAIN SHEET")
                    .padding(.bottom, 4)
                TrainSheetList(sessions: sessions)
            }
        }
    }

    private func sectionLabel(_ title: String) -> some View {
        Text(title)
            .font(.system(.footnote).width(.condensed))
            .kerning(2)
            .foregroundStyle(Theme.muted)
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
        async let sheet = BoardAPI.trainSheet()
        do {
            status = try await BoardAPI.fetch()
            failed = false
        } catch {
            failed = status == nil
        }
        // A sheet that fails leaves the last one standing rather than blanking.
        if let lines = try? await sheet { sessions = lines }
    }
}

/// A frame's provenance - which camera, and when - shown wherever the
/// picture is.
struct FrameCaption: View {
    let observation: CrossingNow.Observation

    var body: some View {
        Text("\(observation.cameraId) - \(observation.capturedAt.formatted(date: .omitted, time: .shortened))")
            .font(.caption.monospaced())
            .foregroundStyle(Theme.muted)
    }
}

/// The camera frame, full screen: what the squint at the small version was
/// asking for. Tap anywhere to come back.
struct FramePhotoView: View {
    let observation: CrossingNow.Observation
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            VStack(spacing: 12) {
                AsyncImage(url: BoardAPI.frameURL(observation.objectKey)) { phase in
                    switch phase {
                    case .success(let image):
                        image.resizable().aspectRatio(contentMode: .fit)
                    case .failure:
                        Text("The picture did not arrive.")
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                    default:
                        ProgressView().tint(Theme.muted)
                    }
                }
                FrameCaption(observation: observation)
            }
        }
        .onTapGesture { dismiss() }
    }
}
