// The rules every surface now shares, pinned where they live: the one
// staleness question, when a blockage began, and how a timeline entry
// dresses. Then the app itself, read the way a
// launch reviewer would: the credits at the foot of the sheet, after the
// Siri hint. (What VoiceOver hears is the UI test target's to check.)
import SwiftUI
import Vision
import XCTest

@testable import PDXTrain

final class StalenessRuleTests: XCTestCase {
    private func status(
        state: String = "CLEAR", stale: Bool = false, generatedAgo: TimeInterval = 0,
        since: TimeInterval? = nil, openSession: TimeInterval? = nil, feed: String = "null"
    ) throws -> BoardStatus {
        let iso = ISO8601DateFormatter()
        let stamp = { (ago: TimeInterval?) in
            ago.map { "\"\(iso.string(from: .now - $0))\"" } ?? "null"
        }
        let session = openSession.map { "{\"started_at\": \(stamp($0))}" } ?? "null"
        let json = """
            {"generated_at": \(stamp(generatedAgo)), "feed": \(feed), "crossings": [
                {"crossing_id": "SE_12TH_CLINTON", "state": "\(state)", "stale": \(stale),
                 "since": \(stamp(since)), "open_session": \(session), "latest_observation": null}
            ]}
            """
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(BoardStatus.self, from: Data(json.utf8))
    }

    func testAFreshVerdictIsCurrent() throws {
        let board = try status()
        XCTAssertFalse(board.isStale(board.clinton!, at: .now))
    }

    func testTheBoardsOwnStaleFlagWinsEvenWhenFresh() throws {
        let board = try status(stale: true)
        XCTAssertTrue(board.isStale(board.clinton!, at: .now))
    }

    func testAVerdictAgesOutAtTheHorizonAndNotBefore() throws {
        let board = try status(generatedAgo: 14 * 60)
        XCTAssertFalse(board.isStale(board.clinton!, at: .now))
        XCTAssertTrue(board.isStale(board.clinton!, at: .now + 2 * 60))
    }

    func testBlockedSincePrefersTheOpenSession() throws {
        let board = try status(state: "BLOCKED", since: 20 * 60, openSession: 12 * 60)
        let since = try XCTUnwrap(board.clinton?.blockedSince)
        XCTAssertEqual(Date.now.timeIntervalSince(since), 12 * 60, accuracy: 2)
    }

    func testBlockedSinceFallsBackToTheStateTimestamp() throws {
        let board = try status(state: "BLOCKED", since: 20 * 60)
        let since = try XCTUnwrap(board.clinton?.blockedSince)
        XCTAssertEqual(Date.now.timeIntervalSince(since), 20 * 60, accuracy: 2)
    }

    func testAClearCrossingNeverHasABlockedSince() throws {
        let board = try status(state: "CLEAR", since: 20 * 60, openSession: 12 * 60)
        XCTAssertNil(board.clinton?.blockedSince)
    }

    func testFeedHealthStillDecodesWithTheSinceTheServerSends() throws {
        let board = try status(feed: #"{"status": "upstream_stale", "since": "2026-08-31T05:00:00Z"}"#)
        XCTAssertEqual(board.feed?.status, "upstream_stale")
    }

    func testAnEntryDressesItselfLikeTheTheme() {
        let live = AspectEntry(date: .now, asOf: .now, aspect: .blocked, stale: false, blockedSince: .now)
        XCTAssertEqual(live.word, "BLOCKED")
        XCTAssertEqual(live.color, Theme.red)
        let silent = AspectEntry(date: .now, asOf: .now, aspect: .blocked, stale: true, blockedSince: nil)
        XCTAssertEqual(silent.word, "NO SIGNAL")
        XCTAssertEqual(silent.color, Theme.amber)
    }
}

@MainActor
final class LaunchBoardTests: XCTestCase {
    private var window: UIWindow!
    private var host: UIHostingController<BoardView>!

    override func setUp() {
        super.setUp()
        URLCache.shared.removeAllCachedResponses()
        BoardStubProtocol.frameFate = .succeed
        URLProtocol.registerClass(BoardStubProtocol.self)
    }

    override func tearDown() {
        URLProtocol.unregisterClass(BoardStubProtocol.self)
        window?.isHidden = true
        window = nil
        super.tearDown()
    }

    /// The site's footer, carried into the app: the sheet ends with whose
    /// cameras these are, after the Siri hint.
    func testTheSheetEndsWithTheCredits() throws {
        showBoard()
        scrollSheetToBottom()
        let shot = saveScreenshot(named: "board-sheet-credits")
        let lines = recognizedLines(in: shot)
        let text = lines.map(\.text).joined(separator: "\n")
        XCTAssertTrue(text.contains("ODOT and PBOT"), "the credits never reach the screen: \(text)")
        XCTAssertTrue(text.contains("automated estimates"), "the honesty line is missing: \(text)")
        let hint = try XCTUnwrap(lines.first { $0.text.contains("Shortcuts") }, "Siri hint missing: \(text)")
        let credits = try XCTUnwrap(lines.first { $0.text.contains("ODOT") })
        // Vision's boxes have their origin at the bottom: lower on screen is smaller.
        XCTAssertLessThan(credits.box.midY, hint.box.midY, "the credits should follow the Siri hint")
    }

    // MARK: - Hosting

    private func showBoard() {
        host = UIHostingController(rootView: BoardView())
        window = UIWindow(frame: UIScreen.main.bounds)
        if let scene = UIApplication.shared.connectedScenes.first(where: { $0 is UIWindowScene })
            as? UIWindowScene
        {
            window.windowScene = scene
        }
        window.rootViewController = host
        window.makeKeyAndVisible()
        pump(timeout: 10) { self.host.presentedViewController != nil }
        pump(seconds: 2)
        guard let sheet = host.presentedViewController?.sheetPresentationController else {
            return XCTFail("the board's sheet never presented")
        }
        for _ in 0..<4 {
            sheet.animateChanges {
                sheet.detents = [.large()]
                sheet.selectedDetentIdentifier = .large
            }
            pump(seconds: 0.5)
            if sheet.selectedDetentIdentifier == .large { break }
        }
        pump(seconds: 1)
    }

    private func scrollSheetToBottom() {
        guard let presented = host.presentedViewController?.view,
            let scroll = firstScrollView(in: presented)
        else { return XCTFail("the sheet has no scroll view") }
        let bottom = scroll.contentSize.height + scroll.adjustedContentInset.bottom - scroll.bounds.height
        scroll.setContentOffset(CGPoint(x: 0, y: max(0, bottom)), animated: false)
        pump(seconds: 1)
    }

    private func firstScrollView(in view: UIView) -> UIScrollView? {
        if let scroll = view as? UIScrollView { return scroll }
        for sub in view.subviews {
            if let found = firstScrollView(in: sub) { return found }
        }
        return nil
    }

    private func pump(seconds: TimeInterval) {
        pump(timeout: seconds) { false }
    }

    private func pump(timeout: TimeInterval, until condition: @escaping () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
    }

    // MARK: - Reading the screen

    private func recognizedLines(in image: UIImage) -> [(text: String, box: CGRect)] {
        guard let cg = image.cgImage else { return [] }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        try? VNImageRequestHandler(cgImage: cg).perform([request])
        return (request.results ?? []).compactMap { observation in
            observation.topCandidates(1).first.map { ($0.string, observation.boundingBox) }
        }
    }

    @discardableResult
    private func saveScreenshot(named name: String) -> UIImage {
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let dir = ProcessInfo.processInfo.environment["EVIDENCE_DIR"], let png = image.pngData() {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
        return image
    }
}
