// The app as VoiceOver hears it. Color is the whole message on the map,
// so the pin has to say where and what in words, and the picture has to
// say it is a picture. Read through XCUITest, which walks the same
// accessibility tree VoiceOver does, against the live board.
import XCTest

final class BoardAccessibilityUITests: XCTestCase {
    func testTheScreenReaderHearsWhereWhatAndThePicture() throws {
        let app = XCUIApplication()
        app.launch()

        // The pin names the crossing and its state, whatever the board says.
        let pin = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label BEGINSWITH '12th and Clinton, '")).firstMatch
        XCTAssertTrue(pin.waitForExistence(timeout: 20), "the map pin has no spoken name")
        let settled = NSPredicate(format: "NOT (label CONTAINS 'looking')")
        wait(for: [expectation(for: settled, evaluatedWith: pin)], timeout: 30)
        let state = pin.label.replacingOccurrences(of: "12th and Clinton, ", with: "")
        XCTAssertTrue(
            ["blocked", "clear", "no signal", "no answer"].contains(state),
            "the pin says something other than a state: \(pin.label)")
        XCTAssertTrue(app.buttons["Return to the crossing"].exists)
        save(app.screenshot(), named: "app-launch")

        // Pull the sheet all the way up, then to its foot: the credits.
        app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.78))
            .press(forDuration: 0.1, thenDragTo: app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.05)))
        let sheet = app.scrollViews.firstMatch
        let credits = app.staticTexts
            .matching(NSPredicate(format: "label CONTAINS 'ODOT and PBOT'")).firstMatch
        for _ in 0..<6 where !credits.isHittable {
            sheet.swipeUp()
        }
        XCTAssertTrue(credits.isHittable, "the credits never surface at the foot of the sheet")
        save(app.screenshot(), named: "app-sheet-foot")

        guard state != "no answer" else {
            throw XCTSkip("the board is unreachable, so there is no picture to label")
        }
        let caption = app.staticTexts["FROM THE CAMERA"]
        XCTAssertTrue(caption.waitForExistence(timeout: 10), "no camera section on the sheet")
        let picture = app.images["Camera picture of the crossing"]
        XCTAssertTrue(
            picture.waitForExistence(timeout: 30),
            "the camera frame has no spoken name; the sheet exposes: "
                + app.scrollViews.firstMatch.descendants(matching: .any).allElementsBoundByIndex
                    .map { "\($0.elementType.rawValue):\($0.label)" }.joined(separator: " | "))
    }

    private func save(_ shot: XCUIScreenshot, named name: String) {
        let attachment = XCTAttachment(screenshot: shot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
        if let dir = ProcessInfo.processInfo.environment["EVIDENCE_DIR"] {
            try? shot.pngRepresentation.write(
                to: URL(fileURLWithPath: dir).appendingPathComponent("\(name).png"))
        }
    }
}
