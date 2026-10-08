import SwiftUI

struct RecordingPlaybackView: View {
    let session: MeetingSession
    let isRecording: Bool
    @StateObject private var playback = RecordingPlayback()
    @State private var source: PlaybackSource = .both
    @State private var scrubPosition: TimeInterval = 0
    @State private var isScrubbing = false

    private var loadID: String {
        "\(session.id)-\(session.manifest.chunks.count)-\(source.rawValue)-\(isRecording)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("元の音声", systemImage: "waveform").font(.headline)
                Spacer()
                Picker("再生する音声", selection: $source) {
                    ForEach(PlaybackSource.allCases.filter { option in
                        option == .both || session.manifest.chunks.contains { option.includes($0.source) }
                    }) { option in Text(option.title).tag(option) }
                }
                .frame(width: 220)
                .disabled(isRecording)
            }
            if isRecording {
                Text("録音を停止すると再生できます。")
                    .font(.callout).foregroundStyle(.secondary)
            } else if playback.isLoading {
                HStack { ProgressView().controlSize(.small); Text("音声を読み込み中…") }
            } else {
                HStack(spacing: 12) {
                    Button(action: playback.toggle) {
                        Label(playback.isPlaying ? "一時停止" : "再生", systemImage: playback.isPlaying ? "pause.fill" : "play.fill")
                    }
                    .disabled(playback.duration <= 0)
                    Text(timestamp(isScrubbing ? scrubPosition : playback.position))
                        .monospacedDigit().frame(width: 75)
                    Slider(value: Binding(
                        get: { isScrubbing ? scrubPosition : playback.position },
                        set: { scrubPosition = $0 }
                    ), in: 0...max(1, playback.duration), onEditingChanged: { editing in
                        if editing {
                            scrubPosition = playback.position
                            isScrubbing = true
                        } else {
                            isScrubbing = false
                            playback.seek(to: scrubPosition)
                        }
                    })
                    .accessibilityLabel("再生位置")
                    .disabled(playback.duration <= 0)
                    Text(timestamp(playback.duration)).monospacedDigit()
                }
                if let message = playback.message {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .padding(16)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
        .task(id: loadID) {
            isScrubbing = false
            guard !isRecording else { playback.reset(); return }
            if !PlaybackSource.allCases.filter({ option in
                option == .both || session.manifest.chunks.contains { option.includes($0.source) }
            }).contains(source) {
                source = .both
                return
            }
            await playback.load(session: session, source: source)
        }
        .onDisappear { playback.reset() }
    }

    private func timestamp(_ seconds: TimeInterval) -> String {
        let value = max(0, Int(seconds.isFinite ? seconds : 0))
        return String(format: "%02d:%02d:%02d", value / 3600, (value % 3600) / 60, value % 60)
    }
}
