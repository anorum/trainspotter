// The board's wire contract, reduced to what a glance needs. Decoding is
// tolerant of extra fields so the API can grow without breaking the app.
import Foundation

// Encodable too, so a surface can write an aspect down and read it back
// (the widget's last-known-good memory) without a raw-value round trip.
enum Aspect: String, Codable {
    case blocked = "BLOCKED"
    case clear = "CLEAR"
    case unknown = "UNKNOWN"
}

struct CrossingNow: Decodable {
    /// Every surface asks about this one crossing; the wire id is the same
    /// whether we are reading the board or pulling the train sheet.
    static let featuredId = "SE_12TH_CLINTON"

    let crossingId: String
    let state: Aspect
    let stale: Bool
    let since: Date?
    let openSession: OpenSession?
    let latestObservation: Observation?

    struct OpenSession: Decodable {
        let startedAt: Date
        enum CodingKeys: String, CodingKey { case startedAt = "started_at" }
    }

    /// The frame the verdict came from - the picture behind the aspect.
    /// Identifiable by its content-addressed key, so a sheet can pin to one
    /// frame while newer ones arrive.
    struct Observation: Decodable, Identifiable {
        var id: String { objectKey }
        let cameraId: String
        let capturedAt: Date
        let objectKey: String

        enum CodingKeys: String, CodingKey {
            case cameraId = "camera_id"
            case capturedAt = "captured_at"
            case objectKey = "object_key"
        }
    }

    enum CodingKeys: String, CodingKey {
        case crossingId = "crossing_id"
        case state, stale, since
        case openSession = "open_session"
        case latestObservation = "latest_observation"
    }
}

/// One line of the train sheet: a blockage from first sighting to all-clear.
struct TrainSession: Decodable, Identifiable {
    let sessionId: String
    let startedAt: Date
    let durationSeconds: Int?
    let isOpen: Bool
    let certified: Bool

    var id: String { sessionId }

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case startedAt = "started_at"
        case durationSeconds = "duration_seconds"
        case isOpen = "is_open"
        case certified
    }
}

struct FeedHealth: Decodable {
    let status: String
    let since: Date?
}

struct BoardStatus: Decodable {
    let generatedAt: Date
    let crossings: [CrossingNow]
    let feed: FeedHealth?

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case crossings, feed
    }

    /// The featured crossing - the one the product is dialed in on.
    var clinton: CrossingNow? {
        crossings.first { $0.crossingId == CrossingNow.featuredId }
    }

    /// Past this age the board itself calls the feed stale; no glance
    /// surface may outlive it on its own.
    static let stalenessHorizon: TimeInterval = 15 * 60

    /// Whether this status has outlived the board's own staleness horizon
    /// as of `now`. Callers pass a clock they refresh, so a status held
    /// through silent fetch failures degrades on screen instead of
    /// counting on as if current.
    func agedOut(at now: Date) -> Bool {
        now.timeIntervalSince(generatedAt) > Self.stalenessHorizon
    }
}

enum BoardAPI {
    // Installed copies pin this host forever, so it has to be the product's
    // own domain rather than wherever the API happens to be hosted today.
    static let base = URL(string: "https://pdxtrain.com/api/v1")!

    static func fetch() async throws -> BoardStatus {
        try await get(BoardStatus.self, from: base.appending(path: "status"))
    }

    /// The last blockages, newest first, for the featured crossing.
    static func trainSheet(limit: Int = 20) async throws -> [TrainSession] {
        struct Page: Decodable { let sessions: [TrainSession] }
        var url = base.appending(path: "sessions")
        url.append(queryItems: [
            .init(name: "crossing_id", value: CrossingNow.featuredId),
            .init(name: "limit", value: String(limit)),
        ])
        return try await get(Page.self, from: url).sessions
    }

    /// Frames are content-addressed and served immutable, so the shared
    /// URL cache holds each picture forever.
    static func frameURL(_ objectKey: String) -> URL {
        base.appending(path: "frames/\(objectKey)")
    }

    /// Live answers bypass the local HTTP cache entirely.
    ///
    /// The app's own cadence - thirty seconds in the foreground, five minutes
    /// for a widget - is the freshness policy; a Cache-Control header is not.
    /// This exists because of a night when an edge rule briefly stamped
    /// /status with max-age=14400 and every widget that fetched in that
    /// window held a four-hour-old answer, honestly reporting it as NO
    /// SIGNAL once it aged out. Frames keep the default policy: they are
    /// content-addressed and immutable, and caching them is the point.
    private static func get<T: Decodable>(_ type: T.Type, from url: URL) async throws -> T {
        let request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
        let (data, _) = try await URLSession.shared.data(for: request)
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { d in
            let s = try d.singleValueContainer().decode(String.self)
            let iso = ISO8601DateFormatter()
            iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = iso.date(from: s) { return date }
            iso.formatOptions = [.withInternetDateTime]
            if let date = iso.date(from: s) { return date }
            throw DecodingError.dataCorrupted(
                .init(codingPath: d.codingPath, debugDescription: "unparseable date \(s)"))
        }
        return try decoder.decode(type, from: data)
    }
}
