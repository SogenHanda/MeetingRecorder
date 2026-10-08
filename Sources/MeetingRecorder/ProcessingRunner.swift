import Foundation

struct ProcessingResult: Sendable {
    let transcriptURL: URL
    let minutesURL: URL
}

enum ProcessingRunner {
    static func run(
        sessionDirectory: URL, whisperModel: String, ollamaModel: String, huggingFaceToken: String,
        language: String, vocabulary: String, audioSource: String, forceTranscription: Bool,
        onProgress: @escaping @MainActor @Sendable (String) -> Void
    ) async throws -> ProcessingResult {
        let sourceTreeScript = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("Resources/process_session.py")
        guard let script = Bundle.main.url(forResource: "process_session", withExtension: "py")
                ?? (FileManager.default.fileExists(atPath: sourceTreeScript.path) ? sourceTreeScript : nil) else {
            throw NSError(domain: "MeetingRecorder", code: 40, userInfo: [NSLocalizedDescriptionKey: "処理スクリプトが見つかりません"])
        }

        return try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            let output = Pipe()
            let logBuffer = ProcessingLogBuffer()
            let runtimePython = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/MeetingRecorder/runtime/.venv/bin/python3")
            process.executableURL = FileManager.default.fileExists(atPath: runtimePython.path)
                ? runtimePython
                : URL(fileURLWithPath: "/usr/bin/env")
            var arguments = (process.executableURL == runtimePython ? [] : ["python3"]) + [
                script.path,
                "--session", sessionDirectory.path,
                "--model", whisperModel,
                "--ollama-model", ollamaModel,
                "--language", language,
                "--vocabulary", String(vocabulary.prefix(1000)),
                "--audio-source", audioSource
            ]
            if forceTranscription {
                arguments.append("--force-transcription")
            } else if FileManager.default.fileExists(atPath: sessionDirectory.appendingPathComponent("transcript.json").path) {
                arguments.append("--summary-only")
            }
            process.arguments = arguments
            var environment = ProcessInfo.processInfo.environment
            let inheritedPath = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(inheritedPath)"
            environment["HF_HUB_DISABLE_XET"] = environment["HF_HUB_DISABLE_XET"] ?? "1"
            if !huggingFaceToken.isEmpty { environment["PYANNOTE_TOKEN"] = huggingFaceToken }
            process.environment = environment
            process.standardOutput = output
            process.standardError = output
            output.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty { return }
                for message in logBuffer.append(data) {
                    Task { @MainActor in onProgress(message) }
                }
            }
            process.terminationHandler = { process in
                output.fileHandleForReading.readabilityHandler = nil
                let data = output.fileHandleForReading.readDataToEndOfFile()
                _ = logBuffer.append(data)
                let log = logBuffer.errorLog()
                if process.terminationStatus == 0 {
                    continuation.resume(returning: ProcessingResult(
                        transcriptURL: sessionDirectory.appendingPathComponent("transcript.md"),
                        minutesURL: sessionDirectory.appendingPathComponent("minutes.md")
                    ))
                } else {
                    continuation.resume(throwing: NSError(
                        domain: "MeetingRecorder",
                        code: Int(process.terminationStatus),
                        userInfo: [NSLocalizedDescriptionKey: log.isEmpty ? "文字起こし処理に失敗しました" : log]
                    ))
                }
            }
            do {
                try process.run()
            } catch {
                output.fileHandleForReading.readabilityHandler = nil
                continuation.resume(throwing: error)
            }
        }
    }
}

private final class ProcessingLogBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var pending = Data()
    private var log = Data()

    func append(_ data: Data) -> [String] {
        lock.lock()
        defer { lock.unlock() }
        log.append(data)
        if log.count > 65_536 { log.removeFirst(log.count - 65_536) }
        pending.append(data)
        var messages: [String] = []
        while let newline = pending.firstIndex(of: 10) {
            let line = String(decoding: pending[..<newline], as: UTF8.self)
            pending.removeSubrange(...newline)
            if line.hasPrefix("PROGRESS: ") {
                messages.append(String(line.dropFirst(10)))
            }
        }
        // Also bound subprocess output without newlines (e.g. download progress).
        if pending.count > 65_536 { pending.removeFirst(pending.count - 65_536) }
        return messages
    }

    func errorLog() -> String {
        lock.lock()
        defer { lock.unlock() }
        return String(decoding: log, as: UTF8.self).split(separator: "\n")
            .filter { !$0.hasPrefix("PROGRESS: ") }.joined(separator: "\n")
    }
}
