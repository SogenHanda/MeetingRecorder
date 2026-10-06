import Foundation

enum AudioSource: String, Codable, Sendable {
    case microphone
    case system

    var displayName: String {
        switch self {
        case .microphone: return "マイク"
        case .system: return "システム音声"
        }
    }
}

struct AudioChunk: Codable, Identifiable, Sendable {
    var id = UUID()
    let source: AudioSource
    let relativePath: String
    let startedAt: Date
    let endedAt: Date
}

struct MeetingManifest: Codable, Identifiable, Sendable {
    let id: UUID
    var title: String
    let startedAt: Date
    var endedAt: Date?
    var microphoneName: String
    var capturesSystemAudio: Bool
    var chunks: [AudioChunk]

    var duration: TimeInterval {
        (endedAt ?? Date()).timeIntervalSince(startedAt)
    }
}

struct MeetingSession: Identifiable, Hashable {
    let id: UUID
    let directory: URL
    let manifest: MeetingManifest

    static func == (lhs: MeetingSession, rhs: MeetingSession) -> Bool {
        lhs.id == rhs.id && lhs.manifest.endedAt == rhs.manifest.endedAt && lhs.manifest.chunks.count == rhs.manifest.chunks.count
    }

    func hash(into hasher: inout Hasher) { hasher.combine(id) }

    var transcriptURL: URL { directory.appendingPathComponent("transcript.md") }
    var summaryURL: URL { directory.appendingPathComponent("minutes.md") }
}

struct MicrophoneDevice: Identifiable, Hashable {
    let id: String
    let name: String
}
