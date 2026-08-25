// One line of the train sheet, phone and watch alike: when the gates
// dropped, how long they stayed down. An open line ticks; an uncertified
// line (a single glimpse the sessionizer could not corroborate) is dimmed
// rather than hidden - the sheet records everything, at honest weight.
import SwiftUI

struct TrainSheetRow: View {
    let session: TrainSession

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(started)
                    .font(.subheadline.monospaced())
                    .foregroundStyle(session.certified ? .white : Theme.muted)
                if session.isOpen {
                    Text("gates still down")
                        .font(.caption2)
                        .foregroundStyle(Theme.red)
                }
            }
            Spacer()
            Text(duration)
                .font(.subheadline.monospaced())
                .foregroundStyle(durationColor)
        }
        .padding(.vertical, 8)
    }

    private var durationColor: Color {
        if session.isOpen { return Theme.red }
        return session.certified ? Theme.muted : Theme.muted.opacity(0.6)
    }

    private var started: String {
        if Calendar.current.isDateInToday(session.startedAt) {
            return session.startedAt.formatted(date: .omitted, time: .shortened)
        }
        return session.startedAt.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    private var duration: String {
        if session.isOpen {
            let minutes = max(1, Int(Date.now.timeIntervalSince(session.startedAt) / 60))
            return "\(minutes) min +"
        }
        guard let seconds = session.durationSeconds, seconds >= 60 else { return "<1 min" }
        return "\(seconds / 60) min"
    }
}

/// The sheet's lines with a hairline between them, phone and watch alike.
struct TrainSheetList: View {
    let sessions: [TrainSession]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(sessions) { session in
                TrainSheetRow(session: session)
                if session.id != sessions.last?.id {
                    Divider().overlay(Theme.hairline)
                }
            }
        }
    }
}
