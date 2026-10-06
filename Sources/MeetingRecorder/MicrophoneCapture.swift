import AVFoundation
import CoreMedia
import Foundation

final class MicrophoneCapture: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate, @unchecked Sendable {
    private let captureSession = AVCaptureSession()
    private let queue = DispatchQueue(label: "MeetingRecorder.microphone")
    private var writer: ChunkedAudioWriter?

    static func devices() -> [MicrophoneDevice] {
        discoveredDevices()
            .map { MicrophoneDevice(id: $0.uniqueID, name: $0.localizedName) }
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    private static func discoveredDevices() -> [AVCaptureDevice] {
        let types: [AVCaptureDevice.DeviceType]
        if #available(macOS 14.0, *) {
            types = [.microphone, .external]
        } else {
            types = [.builtInMicrophone, .externalUnknown]
        }
        return AVCaptureDevice.DiscoverySession(
            deviceTypes: types,
            mediaType: .audio,
            position: .unspecified
        ).devices
    }

    func start(deviceID: String, writer: ChunkedAudioWriter) throws {
        guard let device = Self.discoveredDevices().first(where: { $0.uniqueID == deviceID }) else {
            throw NSError(domain: "MeetingRecorder", code: 20, userInfo: [NSLocalizedDescriptionKey: "選択したマイクが見つかりません"])
        }
        self.writer = writer
        captureSession.beginConfiguration()
        defer { captureSession.commitConfiguration() }
        for oldInput in captureSession.inputs { captureSession.removeInput(oldInput) }
        for oldOutput in captureSession.outputs { captureSession.removeOutput(oldOutput) }

        let input = try AVCaptureDeviceInput(device: device)
        guard captureSession.canAddInput(input) else {
            throw NSError(domain: "MeetingRecorder", code: 21, userInfo: [NSLocalizedDescriptionKey: "このマイクを使用できません"])
        }
        captureSession.addInput(input)

        let output = AVCaptureAudioDataOutput()
        output.setSampleBufferDelegate(self, queue: queue)
        guard captureSession.canAddOutput(output) else {
            throw NSError(domain: "MeetingRecorder", code: 22, userInfo: [NSLocalizedDescriptionKey: "マイク出力を作成できません"])
        }
        captureSession.addOutput(output)
        queue.async { [captureSession] in captureSession.startRunning() }
    }

    func stop() async {
        await withCheckedContinuation { continuation in
            queue.async { [self] in
                captureSession.stopRunning()
                guard let writer else {
                    continuation.resume()
                    return
                }
                writer.finish { continuation.resume() }
                self.writer = nil
            }
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        writer?.append(sampleBuffer)
    }
}
