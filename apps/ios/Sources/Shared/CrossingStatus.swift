// The board's wire contract, reduced to what a glance needs. Decoding is
// tolerant of extra fields so the API can grow without breaking the app.
import Foundation

enum Aspect: String, Decodable {
    case blocked = "BLOCKED"
    case clear = "CLEAR"
    case unknown = "UNKNOWN"
}

struct CrossingNow: Decodable {
    let crossingId: String
    let state: Aspect
    let stale: Bool
    let since: Date?
    let openSession: OpenSession?

    struct OpenSession: Decodable {
        let startedAt: Date
        enum CodingKeys: String, CodingKey { case startedAt = "started_at" }
    }

    enum CodingKeys: String, CodingKey {
        case crossingId = "crossing_id"
        case state, stale, since
        case openSession = "open_session"
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
        crossings.first { $0.crossingId == "SE_12TH_CLINTON" }
    }
}

enum BoardAPI {
    static let statusURL = URL(string: "https://pdxtrain.alexnorum.com/api/v1/status")!

    static func fetch() async throws -> BoardStatus {
        let (data, _) = try await URLSession.shared.data(from: statusURL)
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
        return try decoder.decode(BoardStatus.self, from: data)
    }
}
