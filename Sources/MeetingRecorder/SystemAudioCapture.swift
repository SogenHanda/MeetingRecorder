import CoreMedia
import Foundation
import ScreenCaptureKit

@available(macOS 13.0, *)
final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let queue = DispatchQueue(label: "MeetingRecorder.system-audio")
    private var stream: SCStream?
    private var writer: ChunkedAudioWriter?

    func start(writer: ChunkedAudioWriter) async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw NSError(domain: "MeetingRecorder", code: 30, userInfo: [NSLocalizedDescriptionKey: "録音可能なディスプレイが見つかりません"])
        }
        let ownBundleID = Bundle.main.bundleIdentifier
        let excludedApps = content.applications.filter { $0.bundleIdentifier == ownBundleID }
        let filter = SCContentFilter(display: display, excludingApplications: excludedApps, exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.showsCursor = false
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2
        configuration.queueDepth = 3

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        self.writer = writer
        self.stream = stream
        try await stream.startCapture()
    }

    func stop() async {
        if let stream {
            try? await stream.stopCapture()
        }
        await withCheckedContinuation { continuation in
            queue.async { [self] in
                guard let writer else {
                    continuation.resume()
                    return
                }
                writer.finish { continuation.resume() }
                self.writer = nil
            }
        }
        stream = nil
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        NSLog("System audio capture stopped: %@", error.localizedDescription)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of outputType: SCStreamOutputType) {
        guard outputType == .audio else { return }
        writer?.append(sampleBuffer)
    }
}
