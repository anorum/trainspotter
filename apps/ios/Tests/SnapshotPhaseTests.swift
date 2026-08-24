// Exercises the camera-frame section of the real BoardView end to end:
// the board API is stubbed at the URL-loading layer, so AsyncImage makes a
// real request through the real code path, and assertions read the rendered
// pixels (Vision text recognition, color sampling) - exactly what the user
// would see: a spinner while loading, the honest "did not arrive" message on
// failure, and the picture itself on success.
import SwiftUI
import Vision
import XCTest

@testable import PDXTrain

/// Intercepts every request to the board's host on the shared session
/// (both `BoardAPI` and `AsyncImage` use it). Status and sessions succeed
/// with fixed JSON; the frame request's fate is set per test.
final class BoardStubProtocol: URLProtocol {
    enum FrameFate {
        case fail
        case succeed
    }

    static var frameFate: FrameFate = .fail
    /// Frames are delayed so the loading state is observable first.
    static let frameDelay: TimeInterval = 5.0
    static var sawFrameRequest = false

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host == "pdxtrain.alexnorum.com"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let url = request.url else { return }
        if url.path.hasSuffix("/status") {
            respond(json: Self.statusJSON)
        } else if url.path.contains("/sessions") {
            respond(json: #"{"sessions": []}"#)
        } else if url.path.contains("/frames/") {
            Self.sawFrameRequest = true
            DispatchQueue.global().asyncAfter(deadline: .now() + Self.frameDelay) {
                switch Self.frameFate {
                case .fail:
                    self.client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
                case .succeed:
                    self.respond(data: Self.framePNG, mimeType: "image/png")
                }
            }
        } else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
        }
    }

    override func stopLoading() {}

    private func respond(json: String) {
        respond(data: Data(json.utf8), mimeType: "application/json")
    }

    private func respond(data: Data, mimeType: String) {
        guard let url = request.url else { return }
        let response = URLResponse(
            url: url, mimeType: mimeType, expectedContentLength: data.count,
            textEncodingName: nil)
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    private static var statusJSON: String {
        let iso = ISO8601DateFormatter()
        let now = iso.string(from: Date())
        // A unique key per fate keeps the "cached forever" frame URL cache
        // from short-circuiting the request under test.
        let key = "test-frame-\(frameFate).jpg"
        return """
            {
              "generated_at": "\(now)",
              "crossings": [
                {
                  "crossing_id": "SE_12TH_CLINTON",
                  "state": "CLEAR",
                  "stale": false,
                  "since": "\(now)",
                  "open_session": null,
                  "latest_observation": {
                    "camera_id": "odot-clinton",
                    "captured_at": "\(now)",
                    "object_key": "\(key)"
                  }
                }
              ],
              "feed": { "status": "ok", "since": null }
            }
            """
    }

    /// A recognizable orange block, so success is visible in the screenshot
    /// and detectable by the color sampler.
    private static let framePNG: Data = {
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: 400, height: 300))
        return renderer.image { ctx in
            UIColor.orange.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: 400, height: 300))
        }.pngData()!
    }()
}

@MainActor
final class SnapshotPhaseTests: XCTestCase {
    private var window: UIWindow!
    private var sheet: UISheetPresentationController?
    private let failureCopy = "did not arrive"

    override func setUp() {
        super.setUp()
        URLCache.shared.removeAllCachedResponses()
        BoardStubProtocol.sawFrameRequest = false
        URLProtocol.registerClass(BoardStubProtocol.self)
    }

    override func tearDown() {
        URLProtocol.unregisterClass(BoardStubProtocol.self)
        window?.isHidden = true
        window = nil
        sheet = nil
        super.tearDown()
    }

    func testFrameFailureShowsMessageInsteadOfEternalSpinner() throws {
        BoardStubProtocol.frameFate = .fail
        showBoardWithExpandedSheet()

        // The frame request is still in flight: the placeholder spinner
        // shows and the failure copy must not.
        let loading = saveScreenshot(named: "camera-frame-loading")
        XCTAssertFalse(
            recognizedText(in: loading).localizedCaseInsensitiveContains(failureCopy),
            "failure message visible while the frame is still loading")

        // Let the stubbed request fail, then re-expand (SwiftUI reasserts
        // its own detent selection on update) and read the screen.
        pump(seconds: BoardStubProtocol.frameDelay)
        expandSheet()
        let failed = saveScreenshot(named: "camera-frame-failure")

        XCTAssertTrue(BoardStubProtocol.sawFrameRequest, "AsyncImage never requested the frame")
        XCTAssertTrue(
            recognizedText(in: failed).localizedCaseInsensitiveContains(failureCopy),
            "frame load failed but the failure message is not on screen")
    }

    func testFrameSuccessStillShowsThePicture() throws {
        BoardStubProtocol.frameFate = .succeed
        showBoardWithExpandedSheet()

        pump(seconds: BoardStubProtocol.frameDelay + 1.0)
        expandSheet()
        let shot = saveScreenshot(named: "camera-frame-success")

        XCTAssertTrue(BoardStubProtocol.sawFrameRequest, "AsyncImage never requested the frame")
        XCTAssertFalse(
            recognizedText(in: shot).localizedCaseInsensitiveContains(failureCopy),
            "a successful frame load must not show the failure message")
        XCTAssertTrue(
            containsOrange(shot),
            "the fetched (orange) frame is not visible on screen")
    }

    // MARK: - Hosting

    /// Hosts the real BoardView in its own window, waits for the stubbed
    /// status to land, and pulls the sheet to its largest detent so the
    /// camera section is on screen.
    private func showBoardWithExpandedSheet() {
        let host = UIHostingController(rootView: BoardView())
        let scene = UIApplication.shared.connectedScenes.first { $0 is UIWindowScene }
        window = UIWindow(frame: UIScreen.main.bounds)
        if let windowScene = scene as? UIWindowScene {
            window.windowScene = windowScene
        }
        window.rootViewController = host
        window.makeKeyAndVisible()

        pump(timeout: 10) { host.presentedViewController != nil }
        sheet = host.presentedViewController?.sheetPresentationController
        XCTAssertNotNil(sheet, "the board's sheet never presented")

        // Let the (instant) stubbed status fetch apply before expanding,
        // so the resulting view update cannot snap the detent back.
        pump(seconds: 2.0)
        expandSheet()
    }

    /// SwiftUI reasserts the collapsed detent from its selection binding on
    /// view updates, so expansion re-applies until the sheet reports large.
    private func expandSheet() {
        guard let sheet else { return }
        for _ in 0..<4 {
            sheet.animateChanges {
                sheet.detents = [.large()]
                sheet.selectedDetentIdentifier = .large
            }
            pump(seconds: 0.5)
            if sheet.selectedDetentIdentifier == .large { break }
        }
        pump(seconds: 1.0)
    }

    // MARK: - Run-loop pumping

    private func pump(seconds: TimeInterval) {
        pump(timeout: seconds) { false }
    }

    private func pump(timeout: TimeInterval, until condition: @escaping () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
    }

    // MARK: - Reading the rendered screen

    /// OCR over the actual rendered pixels - asserts what a user sees, not
    /// what the view tree claims.
    private func recognizedText(in image: UIImage) -> String {
        guard let cg = image.cgImage else { return "" }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        try? VNImageRequestHandler(cgImage: cg).perform([request])
        return (request.results ?? [])
            .compactMap { $0.topCandidates(1).first?.string }
            .joined(separator: "\n")
    }

    /// Whether a meaningful patch of the stub frame's orange made it on
    /// screen. The image is redrawn into an RGBA context so the channel
    /// layout is known, then sampled at reduced size.
    private func containsOrange(_ image: UIImage) -> Bool {
        guard let cg = image.cgImage else { return false }
        let width = cg.width / 8
        let height = cg.height / 8
        guard
            let context = CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return false }
        context.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let bytes = context.data?.assumingMemoryBound(to: UInt8.self) else { return false }
        var hits = 0
        for y in 0..<height {
            for x in 0..<width {
                let offset = y * context.bytesPerRow + x * 4
                let r = Int(bytes[offset])
                let g = Int(bytes[offset + 1])
                let b = Int(bytes[offset + 2])
                if r > 190, (90...190).contains(g), b < 110 { hits += 1 }
            }
        }
        return hits > 50
    }

    // MARK: - Evidence

    /// Renders the window (sheet included) to a PNG: attached to the result
    /// bundle always, and written to $EVIDENCE_DIR when the runner sets it.
    @discardableResult
    private func saveScreenshot(named name: String) -> UIImage {
        let renderer = UIGraphicsImageRenderer(bounds: window.bounds)
        let image = renderer.image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)

        if let dir = ProcessInfo.processInfo.environment["EVIDENCE_DIR"],
            let png = image.pngData()
        {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
        return image
    }
}
