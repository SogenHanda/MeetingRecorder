import AVFoundation
import CoreMedia
import Foundation

final class ChunkedAudioWriter {
    typealias Completion = @Sendable (AudioChunk) -> Void

    private let source: AudioSource
    private let directory: URL
    private let sessionStartedAt: Date
    private let chunkDuration: TimeInterval
    private let onChunkCompleted: Completion

    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var chunkStartedPTS: CMTime?
    private var chunkStartedDate: Date?
    private var chunkURL: URL?
    private var sequence = 0

    init(source: AudioSource, directory: URL, sessionStartedAt: Date, chunkDuration: TimeInterval = 300, onChunkCompleted: @escaping Completion) {
        self.source = source
        self.directory = directory
        self.sessionStartedAt = sessionStartedAt
        self.chunkDuration = chunkDuration
        self.onChunkCompleted = onChunkCompleted
    }

    func append(_ sampleBuffer: CMSampleBuffer) {
        guard CMSampleBufferDataIsReady(sampleBuffer), CMSampleBufferGetNumSamples(sampleBuffer) > 0 else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)

        if let start = chunkStartedPTS,
           CMTimeGetSeconds(CMTimeSubtract(pts, start)) >= chunkDuration {
            finishCurrent(at: Date())
        }

        if writer == nil {
            do {
                try beginChunk(at: pts)
            } catch {
                NSLog("Could not start %@ audio chunk: %@", source.rawValue, error.localizedDescription)
                return
            }
        }

        guard let writer, let input, writer.status == .writing, input.isReadyForMoreMediaData else { return }
        if !input.append(sampleBuffer) {
            NSLog("Could not append %@ audio: %@", source.rawValue, writer.error?.localizedDescription ?? "unknown error")
        }
    }

    func finish(completion: @escaping @Sendable () -> Void = {}) {
        finishCurrent(at: Date(), completion: completion)
    }

    private func beginChunk(at pts: CMTime) throws {
        sequence += 1
        let filename = String(format: "%@-%05d.m4a", source.rawValue, sequence)
        let url = directory.appendingPathComponent(filename)
        try? FileManager.default.removeItem(at: url)

        let writer = try AVAssetWriter(outputURL: url, fileType: .m4a)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: 48_000,
            AVNumberOfChannelsKey: 2,
            AVEncoderBitRateKey: 128_000
        ]
        let input = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else {
            throw NSError(domain: "MeetingRecorder", code: 10, userInfo: [NSLocalizedDescriptionKey: "Audio writer input is unsupported"])
        }
        writer.add(input)
        guard writer.startWriting() else {
            throw writer.error ?? NSError(domain: "MeetingRecorder", code: 11)
        }
        writer.startSession(atSourceTime: pts)

        self.writer = writer
        self.input = input
        self.chunkStartedPTS = pts
        self.chunkStartedDate = Date()
        self.chunkURL = url
    }

    private func finishCurrent(at endedAt: Date, completion: @escaping @Sendable () -> Void = {}) {
        guard let writer, let input, let url = chunkURL, let startedAt = chunkStartedDate else {
            completion()
            return
        }
        self.writer = nil
        self.input = nil
        self.chunkURL = nil
        self.chunkStartedPTS = nil
        self.chunkStartedDate = nil

        input.markAsFinished()
        writer.finishWriting { [source, directory, sessionStartedAt, onChunkCompleted] in
            guard writer.status == .completed else {
                NSLog("Could not finish %@ audio chunk: %@", source.rawValue, writer.error?.localizedDescription ?? "unknown error")
                completion()
                return
            }
            let relative = url.path.replacingOccurrences(of: directory.deletingLastPathComponent().path + "/", with: "")
            let safeStart = max(startedAt, sessionStartedAt)
            onChunkCompleted(AudioChunk(source: source, relativePath: relative, startedAt: safeStart, endedAt: endedAt))
            completion()
        }
    }
}
