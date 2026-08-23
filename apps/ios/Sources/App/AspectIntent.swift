// "Hey Siri, is the train blocking?" - an App Intent that fetches the board
// and answers out loud. Apple requires the app name inside built-in phrases;
// a personal Shortcut can wrap this intent under any phrase at all (the
// README shows how), which is where the natural wording lives.
import AppIntents

struct CheckCrossingIntent: AppIntent {
    static let title: LocalizedStringResource = "Check the crossing"
    static let description = IntentDescription(
        "Answers whether a train is blocking 12th & Clinton right now.")

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let status = try? await BoardAPI.fetch(), let crossing = status.clinton else {
            return .result(dialog: "The board is not answering right now.")
        }
        if crossing.stale {
            return .result(dialog:
                "The cameras have gone quiet, so I can't say - the last word was more than fifteen minutes ago.")
        }
        switch crossing.state {
        case .blocked:
            if let started = crossing.openSession?.startedAt ?? crossing.since {
                let minutes = max(1, Int(Date.now.timeIntervalSince(started) / 60))
                return .result(dialog:
                    "Yes - a train has been blocking 12th and Clinton for \(minutes) minute\(minutes == 1 ? "" : "s").")
            }
            return .result(dialog: "Yes - a train is blocking 12th and Clinton.")
        case .clear:
            return .result(dialog: "No - the crossing is clear.")
        case .unknown:
            return .result(dialog:
                "The camera looked but couldn't decide - treat it as unknown.")
        }
    }
}

struct PDXTrainShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: CheckCrossingIntent(),
            phrases: [
                "Is the train blocking in \(.applicationName)",
                "\(.applicationName) status",
                "Check \(.applicationName)",
            ],
            shortTitle: "Check the crossing",
            systemImageName: "train.side.front.car"
        )
    }
}
