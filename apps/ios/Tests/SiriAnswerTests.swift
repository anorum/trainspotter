// Exercises CheckCrossingIntent the way Siri does: the board API is stubbed
// at the URL-loading layer, perform() runs for real, and assertions read
// what the user gets - the spoken dialog's actual words and the snippet
// card's rendered pixels.
import AppIntents
import SwiftUI
import Vision
import XCTest

@testable import PDXTrain

/// Serves a configurable board status to anything on the shared session;
/// frame requests fail immediately so failure copy is observable.
final class SiriBoardStub: URLProtocol {
    static var statusJSON = ""

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host() == BoardAPI.base.host()
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let url = request.url else { return }
        if url.path.hasSuffix("/status") {
            let data = Data(Self.statusJSON.utf8)
            let response = URLResponse(
                url: url, mimeType: "application/json", expectedContentLength: data.count,
                textEncodingName: nil)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
        }
    }

    override func stopLoading() {}

    static func status(state: String, sinceSecondsAgo: TimeInterval, openSession: Bool) -> String {
        let iso = ISO8601DateFormatter()
        let now = iso.string(from: .now)
        let since = iso.string(from: .now - sinceSecondsAgo)
        let session = openSession ? #"{"started_at": "\#(since)"}"# : "null"
        return """
            {
              "generated_at": "\(now)",
              "crossings": [
                {
                  "crossing_id": "SE_12TH_CLINTON",
                  "state": "\(state)",
                  "stale": false,
                  "since": "\(since)",
                  "open_session": \(session),
                  "latest_observation": null
                }
              ]
            }
            """
    }
}

final class SiriAnswerTests: XCTestCase {
    override func setUp() {
        super.setUp()
        URLProtocol.registerClass(SiriBoardStub.self)
    }

    override func tearDown() {
        URLProtocol.unregisterClass(SiriBoardStub.self)
        super.tearDown()
    }

    func testHourScaleBlockageIsSpokenInHoursAndMinutes() async throws {
        SiriBoardStub.statusJSON = SiriBoardStub.status(
            state: "BLOCKED", sinceSecondsAgo: 80 * 60 + 30, openSession: true)
        let answer = try await performedAnswer()
        XCTAssertTrue(
            answer.contains("1 hour and 20 minutes"),
            "hour-scale duration not spoken naturally: \(answer.prefix(500))")
    }

    func testFreshlyClearedCrossingSaysJustCleared() async throws {
        SiriBoardStub.statusJSON = SiriBoardStub.status(
            state: "CLEAR", sinceSecondsAgo: 60, openSession: false)
        let answer = try await performedAnswer()
        XCTAssertTrue(
            answer.contains("just cleared"),
            "a minute-old all-clear should say 'just cleared': \(answer.prefix(500))")
    }

    func testLongClearSpellIsSpokenInHours() async throws {
        SiriBoardStub.statusJSON = SiriBoardStub.status(
            state: "CLEAR", sinceSecondsAgo: 8 * 3600 + 30, openSession: false)
        let answer = try await performedAnswer()
        XCTAssertTrue(
            answer.contains("8 hours"),
            "an overnight clear spell should be spoken in hours: \(answer.prefix(500))")
    }

    /// Runs the real intent and reads the answer out of the opaque result's
    /// reflection - the dialog's words (and interpolated durations) appear
    /// there verbatim, and AppIntents offers no public accessor.
    private func performedAnswer() async throws -> String {
        String(reflecting: try await CheckCrossingIntent().perform())
    }
}

@MainActor
final class SiriSnippetTests: XCTestCase {
    /// The card Siri shows must carry the verdict on its own: the aspect
    /// word in signal color next to the flasher, with the crossing named.
    func testSnippetCardRendersTheVerdict() throws {
        let renderer = ImageRenderer(
            content: AspectSnippet(aspect: .blocked, stale: false, line: "gates down 80 min")
                .frame(width: 360))
        renderer.scale = 3
        let image = try XCTUnwrap(renderer.uiImage, "the snippet card did not render")
        save(image, named: "siri-snippet-blocked")

        let text = recognizedText(in: image)
        XCTAssertTrue(text.localizedCaseInsensitiveContains("BLOCKED"), "aspect word missing: \(text)")
        XCTAssertTrue(text.localizedCaseInsensitiveContains("12th & Clinton"), "crossing missing: \(text)")
    }

    private func recognizedText(in image: UIImage) -> String {
        guard let cg = image.cgImage else { return "" }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        try? VNImageRequestHandler(cgImage: cg).perform([request])
        return (request.results ?? [])
            .compactMap { $0.topCandidates(1).first?.string }
            .joined(separator: "\n")
    }

    private func save(_ image: UIImage, named name: String) {
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let dir = ProcessInfo.processInfo.environment["EVIDENCE_DIR"],
            let png = image.pngData()
        {
            try? png.write(to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }
}
