import AVFoundation
import Foundation

enum PlaybackSource: String, CaseIterable, Identifiable {
    case both, microphone, system

    var id: String { rawValue }
    var title: String {
        switch self {
        case .both: return "両方"
        case .microphone: return "マイクのみ"
        case .system: return "システム音声のみ"
        }
    }

    func includes(_ source: AudioSource) -> Bool {
        self == .both || rawValue == source.rawValue
    }
}

struct PlaybackComposition {
    let asset: AVMutableComposition
    let missingFiles: Int

    static func build(session: MeetingSession, source: PlaybackSource) async throws -> PlaybackComposition {
        let composition = AVMutableComposition()
        var inserted = 0
        var missing = 0
        for audioSource in [AudioSource.microphone, .system] where source.includes(audioSource) {
            let chunks = session.manifest.chunks.filter { $0.source == audioSource }
                .sorted { $0.startedAt < $1.startedAt }
            guard !chunks.isEmpty,
                  let destination = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) else { continue }
            var previousEnd = CMTime.zero
            for chunk in chunks {
                try Task.checkCancellation()
                let url = session.directory.appendingPathComponent(chunk.relativePath)
                guard FileManager.default.fileExists(atPath: url.path) else {
                    missing += 1
                    continue
                }
                let asset = AVURLAsset(url: url)
                guard let track = try await asset.loadTracks(withMediaType: .audio).first else {
                    throw NSError(domain: "MeetingRecorder", code: 50, userInfo: [NSLocalizedDescriptionKey: "音声トラックがありません: \(url.lastPathComponent)"])
                }
                let range = try await track.load(.timeRange)
                // Wall-clock dates preserve the relative start of microphone and system audio.
                // Rounded manifest dates can overlap slightly; never insert over an earlier chunk.
                let offset = CMTime(seconds: max(0, chunk.startedAt.timeIntervalSince(session.manifest.startedAt)), preferredTimescale: 48_000)
                let start = CMTimeMaximum(offset, previousEnd)
                try destination.insertTimeRange(range, of: track, at: start)
                previousEnd = CMTimeAdd(start, range.duration)
                inserted += 1
            }
        }
        guard inserted > 0 else {
            throw NSError(domain: "MeetingRecorder", code: 51, userInfo: [NSLocalizedDescriptionKey: "選択した音声ファイルが見つかりません。"])
        }
        return PlaybackComposition(asset: composition, missingFiles: missing)
    }
}

@MainActor
final class RecordingPlayback: ObservableObject {
    @Published private(set) var isLoading = false
    @Published private(set) var isPlaying = false
    @Published private(set) var duration: TimeInterval = 0
    @Published private(set) var position: TimeInterval = 0
    @Published private(set) var message: String?

    private var player: AVPlayer?
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var generation = UUID()

    func load(session: MeetingSession, source: PlaybackSource) async {
        reset()
        let currentGeneration = generation
        isLoading = true
        do {
            let result = try await PlaybackComposition.build(session: session, source: source)
            try Task.checkCancellation()
            guard currentGeneration == generation else { return }
            duration = CMTimeGetSeconds(result.asset.duration)
            let item = AVPlayerItem(asset: result.asset)
            let player = AVPlayer(playerItem: item)
            self.player = player
            if result.missingFiles > 0 { message = "音声ファイル\(result.missingFiles)個が見つからず、その部分を省いています。" }
            timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main) { [weak self] time in
                Task { @MainActor [weak self] in
                    guard let self, currentGeneration == self.generation else { return }
                    self.position = max(0, CMTimeGetSeconds(time))
                    if item.status == .failed {
                        self.message = item.error?.localizedDescription ?? "音声を再生できませんでした。"
                        self.isPlaying = false
                    }
                }
            }
            endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) { [weak self] _ in
                Task { @MainActor [weak self] in
                    guard let self, currentGeneration == self.generation else { return }
                    self.isPlaying = false
                    self.position = self.duration
                }
            }
            isLoading = false
        } catch {
            guard currentGeneration == generation else { return }
            isLoading = false
            if !Task.isCancelled { message = error.localizedDescription }
        }
    }

    func toggle() {
        guard let player else { return }
        if isPlaying {
            pause()
        } else {
            if position >= duration - 0.1 { seek(to: 0) }
            player.play()
            isPlaying = true
        }
    }

    func pause() {
        player?.pause()
        isPlaying = false
    }

    func seek(to seconds: TimeInterval) {
        position = min(duration, max(0, seconds))
        player?.seek(to: CMTime(seconds: position, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func reset() {
        generation = UUID()
        pause()
        if let timeObserver { player?.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
        timeObserver = nil
        endObserver = nil
        player = nil
        duration = 0
        position = 0
        message = nil
        isLoading = false
    }

    deinit {
        player?.pause()
        if let timeObserver { player?.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
    }
}
