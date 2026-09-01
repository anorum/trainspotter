// "Hey Siri, is the train blocking?" - an App Intent that fetches the board
// and answers out loud. Apple requires the app name inside built-in phrases;
// a personal Shortcut can wrap this intent under any phrase at all (the
// README shows how), which is where the natural wording lives.
import AppIntents
import SwiftUI

struct CheckCrossingIntent: AppIntent {
    static let title: LocalizedStringResource = "Check the crossing"
    static let description = IntentDescription(
        "Answers whether a train is blocking 12th & Clinton right now.")

    /// What Siri says and what it shows, decided together - each branch below
    /// is then just the words and the aspect, and the card is built once.
    private struct Answer {
        let dialog: IntentDialog
        let aspect: Aspect
        var stale = false
        let line: String
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ShowsSnippetView {
        let answer = await answer()
        return .result(
            dialog: answer.dialog,
            view: AspectSnippet(aspect: answer.aspect, stale: answer.stale, line: answer.line))
    }

    private func answer() async -> Answer {
        guard let status = try? await BoardAPI.fetch(), let crossing = status.clinton else {
            return Answer(
                dialog: "The board is not answering right now.",
                aspect: .unknown, stale: true, line: "board unreachable")
        }
        // One clock reading for the whole answer, taken once the board has
        // spoken, so the age it reports and the age it speaks agree.
        let now = Date.now
        if status.isStale(crossing, at: now) {
            return Answer(
                dialog:
                    "The cameras have gone quiet, so I can't say - the last word was more than fifteen minutes ago.",
                aspect: .unknown, stale: true, line: "cameras quiet")
        }
        switch crossing.state {
        case .blocked:
            guard let started = crossing.blockedSince else {
                return Answer(
                    dialog: "Yes - a train is blocking 12th and Clinton.",
                    aspect: .blocked, line: "gates down")
            }
            let minutes = max(1, Int(now.timeIntervalSince(started) / 60))
            return Answer(
                dialog:
                    "Yes - a train has been blocking 12th and Clinton for \(spoken(minutes: minutes)).",
                aspect: .blocked, line: "gates down \(minutes) min")
        case .clear:
            guard let since = crossing.since else {
                return Answer(
                    dialog: "No - the crossing is clear.", aspect: .clear, line: "clear")
            }
            let minutes = Int(now.timeIntervalSince(since) / 60)
            if minutes < 2 {
                return Answer(
                    dialog: "No - the crossing just cleared.",
                    aspect: .clear, line: "just cleared")
            }
            return Answer(
                dialog: "No - the crossing is clear, and has been for \(spoken(minutes: minutes)).",
                aspect: .clear, line: "clear \(minutes) min")
        case .unknown:
            return Answer(
                dialog: "The camera looked but couldn't decide - treat it as unknown.",
                aspect: .unknown, line: "undecided")
        }
    }

    /// A duration the way a person would say it: minutes under an hour,
    /// hours (with any leftover minutes) past it.
    private func spoken(minutes: Int) -> String {
        guard minutes >= 60 else { return "\(minutes) minute\(minutes == 1 ? "" : "s")" }
        let hours = minutes / 60
        let rest = minutes % 60
        let hoursPart = "\(hours) hour\(hours == 1 ? "" : "s")"
        guard rest > 0 else { return hoursPart }
        return "\(hoursPart) and \(rest) minute\(rest == 1 ? "" : "s")"
    }
}

/// The card Siri shows while speaking: the plaque, reduced to one line.
struct AspectSnippet: View {
    let aspect: Aspect
    let stale: Bool
    let line: String

    private var color: Color { Theme.aspectColor(aspect, stale: stale) }

    var body: some View {
        HStack(spacing: 14) {
            Flasher(aspect: aspect, stale: stale, lampSize: 15)
            VStack(alignment: .leading, spacing: 2) {
                Text(Theme.aspectWord(aspect, stale: stale))
                    .font(.system(.title3, weight: .bold).width(.condensed))
                    .foregroundStyle(color)
                Text("12th & Clinton - \(line)")
                    .font(.caption)
                    .foregroundStyle(Theme.muted)
            }
            Spacer()
        }
        .padding(16)
        .background(Theme.ink, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct PDXTrainShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: CheckCrossingIntent(),
            phrases: [
                "Is the train blocking in \(.applicationName)",
                "Is a train blocking in \(.applicationName)",
                "Is the crossing blocked in \(.applicationName)",
                "Can I get across in \(.applicationName)",
                "\(.applicationName) status",
                "Check \(.applicationName)",
            ],
            shortTitle: "Check the crossing",
            systemImageName: "train.side.front.car"
        )
    }
}
