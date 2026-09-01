// The Siri hint exists because nobody discovers the Shortcut trick alone, so
// the words that carry it have to actually reach the screen. Assertions read
// the rendered pixels rather than the source strings - a view that lays out
// to nothing still holds its text.
import SwiftUI
import Vision
import XCTest

@testable import PDXTrain

@MainActor
final class AskSiriSectionTests: XCTestCase {
    private var window: UIWindow!

    override func tearDown() {
        window?.isHidden = true
        window = nil
        super.tearDown()
    }

    func testTheHintNamesBothThePhraseAndTheWayToChangeIt() {
        window = UIWindow(frame: UIScreen.main.bounds)
        if let scene = UIApplication.shared.connectedScenes
            .first(where: { $0 is UIWindowScene }) as? UIWindowScene
        {
            window.windowScene = scene
        }
        window.rootViewController = UIHostingController(
            rootView: AskSiriSection().padding(24).background(Theme.ink))
        window.makeKeyAndVisible()

        let deadline = Date().addingTimeInterval(1)
        while Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }

        let renderer = UIGraphicsImageRenderer(bounds: window.bounds)
        let shot = renderer.image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: shot)
        attachment.name = "ask-siri-section"
        attachment.lifetime = .keepAlways
        add(attachment)

        let text = recognizedText(in: shot)
        // The built-in phrase, which Apple requires to carry the app's name.
        XCTAssertTrue(
            text.localizedCaseInsensitiveContains("PDX Train status"),
            "the spoken phrase never reaches the screen: \(text)")
        // And the escape hatch from it, which is the only reason this exists.
        XCTAssertTrue(
            text.localizedCaseInsensitiveContains("Shortcuts"),
            "the hint omits how to choose your own phrase: \(text)")
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
}
