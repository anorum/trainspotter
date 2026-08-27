// The full-screen photo must be as honest as the small frame it enlarges:
// a failed fetch shows the same plain message, never an eternal spinner.
// The frame request is stubbed to fail; assertions read the rendered pixels.
import SwiftUI
import Vision
import XCTest

@testable import PDXTrain

@MainActor
final class FramePhotoTests: XCTestCase {
    private var window: UIWindow!

    override func setUp() {
        super.setUp()
        URLCache.shared.removeAllCachedResponses()
        SiriBoardStub.statusJSON = ""
        URLProtocol.registerClass(SiriBoardStub.self)
    }

    override func tearDown() {
        URLProtocol.unregisterClass(SiriBoardStub.self)
        window?.isHidden = true
        window = nil
        super.tearDown()
    }

    func testFailedFrameShowsTheHonestMessageFullScreen() {
        let observation = CrossingNow.Observation(
            cameraId: "odot-clinton", capturedAt: .now,
            objectKey: "fullscreen-test-frame.jpg")
        window = UIWindow(frame: UIScreen.main.bounds)
        if let scene = UIApplication.shared.connectedScenes
            .first(where: { $0 is UIWindowScene }) as? UIWindowScene
        {
            window.windowScene = scene
        }
        window.rootViewController = UIHostingController(
            rootView: FramePhotoView(observation: observation))
        window.makeKeyAndVisible()

        // Let the stubbed frame request fail and the view settle.
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }

        let renderer = UIGraphicsImageRenderer(bounds: window.bounds)
        let shot = renderer.image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        save(shot, named: "fullscreen-photo-failure")

        let text = recognizedText(in: shot)
        XCTAssertTrue(
            text.localizedCaseInsensitiveContains("did not arrive"),
            "the full-screen photo hides its failure: \(text)")
        XCTAssertTrue(
            text.localizedCaseInsensitiveContains("odot-clinton"),
            "the frame's provenance caption is missing: \(text)")
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
