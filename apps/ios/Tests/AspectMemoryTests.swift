// The widget's memory must be exactly as honest as a live fetch: within the
// horizon it repeats the last real answer at its true age; past the horizon
// it is silence, same as never having heard.
import XCTest

@testable import PDXTrain

final class AspectMemoryTests: XCTestCase {
    override func setUp() {
        super.setUp()
        UserDefaults.standard.removeObject(forKey: AspectMemory.key)
    }

    func testRecallWithinHorizonRepeatsTheAnswerAtItsTrueAge() {
        let asOf = Date.now - 5 * 60
        let since = Date.now - 12 * 60
        AspectMemory.remember(asOf: asOf, aspect: .blocked, blockedSince: since)
        let entry = AspectMemory.recall(at: .now)
        XCTAssertEqual(entry?.aspect, .blocked)
        XCTAssertEqual(entry?.stale, false)
        XCTAssertEqual(entry?.asOf, asOf)
        XCTAssertEqual(entry?.blockedSince, since)
    }

    func testRecallPastHorizonDegradesToUnknownAndDropsTheDuration() {
        let asOf = Date.now - BoardStatus.stalenessHorizon - 60
        AspectMemory.remember(asOf: asOf, aspect: .blocked, blockedSince: asOf)
        let entry = AspectMemory.recall(at: .now)
        XCTAssertEqual(entry?.aspect, .unknown)
        XCTAssertEqual(entry?.stale, true)
        XCTAssertNil(entry?.blockedSince)
        // The timestamp stays the truth: when the board last spoke.
        XCTAssertEqual(entry?.asOf, asOf)
    }

    func testNoMemoryMeansNoEntry() {
        XCTAssertNil(AspectMemory.recall(at: .now))
    }
}
