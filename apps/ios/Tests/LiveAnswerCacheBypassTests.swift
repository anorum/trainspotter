// Reproduces the night an edge rule stamped /status with max-age=14400: a
// widget that fetched in that window cached the answer for four hours, and
// every refresh after - including tap-to-refresh - re-served the local copy
// until it aged into NO SIGNAL. A live answer must reach the origin every
// time, no matter what a cached response's headers claim.
import XCTest

@testable import PDXTrain

/// Stands in for the board behind that bad edge rule: answers every
/// BoardAPI request with a scripted body stamped max-age=14400, and serves
/// the locally cached copy whenever the request's cache policy would let
/// URLSession's own HTTP loader do the same. Registered globally, so
/// URLSession.shared - the session BoardAPI actually uses - goes through it.
final class PoisonedEdgeStub: URLProtocol {
    static var statusGeneratedAt = ""
    static var sessionId = ""

    private static let cacheRespectingPolicies: Set<URLRequest.CachePolicy> = [
        .useProtocolCachePolicy, .returnCacheDataElseLoad, .returnCacheDataDontLoad,
    ]

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host() == "pdxtrain.com"
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        if Self.cacheRespectingPolicies.contains(request.cachePolicy),
            let cached = cachedResponse ?? URLCache.shared.cachedResponse(for: request)
        {
            client?.urlProtocol(self, didReceive: cached.response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: cached.data)
            client?.urlProtocolDidFinishLoading(self)
            return
        }

        let body = Data(scriptedBody().utf8)
        let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1",
            headerFields: [
                "Content-Type": "application/json",
                "Cache-Control": "public, max-age=14400",
            ])!
        URLCache.shared.storeCachedResponse(
            CachedURLResponse(response: response, data: body), for: request)
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private func scriptedBody() -> String {
        if request.url?.path().hasSuffix("/status") == true {
            return """
                {"generated_at": "\(Self.statusGeneratedAt)", "crossings": [
                    {"crossing_id": "SE_12TH_CLINTON", "state": "CLEAR", "stale": false}
                ]}
                """
        }
        return """
            {"sessions": [{"session_id": "\(Self.sessionId)",
                "started_at": "2026-08-31T05:00:00Z", "is_open": false, "certified": true}]}
            """
    }
}

final class LiveAnswerCacheBypassTests: XCTestCase {
    override func setUp() {
        super.setUp()
        URLProtocol.registerClass(PoisonedEdgeStub.self)
        URLCache.shared.removeAllCachedResponses()
    }

    override func tearDown() {
        URLProtocol.unregisterClass(PoisonedEdgeStub.self)
        URLCache.shared.removeAllCachedResponses()
        super.tearDown()
    }

    func testStatusRefreshGetsTheLiveAnswerNotThePoisonedCachedOne() async throws {
        PoisonedEdgeStub.statusGeneratedAt = "2026-08-31T02:00:00Z"
        _ = try await BoardAPI.fetch()

        PoisonedEdgeStub.statusGeneratedAt = "2026-08-31T06:00:00Z"
        let refreshed = try await BoardAPI.fetch()

        XCTAssertEqual(
            refreshed.generatedAt,
            ISO8601DateFormatter().date(from: "2026-08-31T06:00:00Z"),
            "a refresh re-served the locally cached status instead of asking the board")
    }

    func testTrainSheetRefreshAlsoBypassesTheLocalCache() async throws {
        PoisonedEdgeStub.sessionId = "the-stale-one"
        _ = try await BoardAPI.trainSheet()

        PoisonedEdgeStub.sessionId = "the-live-one"
        let refreshed = try await BoardAPI.trainSheet()

        XCTAssertEqual(
            refreshed.map(\.sessionId), ["the-live-one"],
            "a refresh re-served the locally cached train sheet instead of asking the board")
    }
}
