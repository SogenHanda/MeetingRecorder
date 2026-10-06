import Foundation

struct ProcessingResult: Sendable {
    let transcriptURL: URL
    let minutesURL: URL
}

enum ProcessingRunner {
    static func run(sessionDirectory: URL, whisperModel: String, ollamaModel: String, huggingFaceToken: String) async throws -> ProcessingResult {
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
            let runtimePython = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/MeetingRecorder/runtime/.venv/bin/python3")
            process.executableURL = FileManager.default.fileExists(atPath: runtimePython.path)
                ? runtimePython
                : URL(fileURLWithPath: "/usr/bin/env")
            var arguments = (process.executableURL == runtimePython ? [] : ["python3"]) + [
                script.path,
                "--session", sessionDirectory.path,
                "--model", whisperModel,
                "--ollama-model", ollamaModel
            ]
            if FileManager.default.fileExists(atPath: sessionDirectory.appendingPathComponent("transcript.json").path) {
                arguments.append("--summary-only")
            }
            process.arguments = arguments
            var environment = ProcessInfo.processInfo.environment
            let inheritedPath = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(inheritedPath)"
            if !huggingFaceToken.isEmpty { environment["PYANNOTE_TOKEN"] = huggingFaceToken }
            process.environment = environment
            process.standardOutput = output
            process.standardError = output
            process.terminationHandler = { process in
                let data = output.fileHandleForReading.readDataToEndOfFile()
                let log = String(decoding: data, as: UTF8.self)
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
                continuation.resume(throwing: error)
            }
        }
    }
}
