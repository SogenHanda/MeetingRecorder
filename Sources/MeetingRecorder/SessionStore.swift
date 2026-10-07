import Foundation

final class SessionStore {
    private let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()

    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()

    let root: URL

    init() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        root = support.appendingPathComponent("MeetingRecorder/Sessions", isDirectory: true)
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }

    func createSession(title: String, microphoneName: String, capturesSystemAudio: Bool) throws -> (URL, MeetingManifest) {
        let id = UUID()
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let directory = root.appendingPathComponent("\(formatter.string(from: Date()))-\(id.uuidString.prefix(8))", isDirectory: true)
        try FileManager.default.createDirectory(at: directory.appendingPathComponent("audio"), withIntermediateDirectories: true)
        let manifest = MeetingManifest(
            id: id,
            title: title.isEmpty ? "無題のミーティング" : title,
            startedAt: Date(),
            endedAt: nil,
            microphoneName: microphoneName,
            capturesSystemAudio: capturesSystemAudio,
            chunks: []
        )
        try save(manifest, in: directory)
        return (directory, manifest)
    }

    func save(_ manifest: MeetingManifest, in directory: URL) throws {
        let data = try encoder.encode(manifest)
        let target = directory.appendingPathComponent("session.json")
        let temporary = directory.appendingPathComponent("session.json.tmp")
        try data.write(to: temporary, options: .atomic)
        if FileManager.default.fileExists(atPath: target.path) {
            _ = try FileManager.default.replaceItemAt(target, withItemAt: temporary)
        } else {
            try FileManager.default.moveItem(at: temporary, to: target)
        }
    }

    func rename(_ session: MeetingSession, to title: String) throws {
        var manifest = session.manifest
        manifest.title = title
        try save(manifest, in: session.directory)
    }

    func moveToTrash(_ session: MeetingSession) throws {
        var resultingURL: NSURL?
        try FileManager.default.trashItem(at: session.directory, resultingItemURL: &resultingURL)
    }

    func loadAll() -> [MeetingSession] {
        let directories = (try? FileManager.default.contentsOfDirectory(
            at: root,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        )) ?? []

        return directories.compactMap { directory in
            let file = directory.appendingPathComponent("session.json")
            guard let data = try? Data(contentsOf: file),
                  var manifest = try? decoder.decode(MeetingManifest.self, from: data) else { return nil }
            // A forced quit can leave an open manifest. Keep every completed chunk
            // and close the session at the latest safely written timestamp.
            if manifest.endedAt == nil, let lastChunk = manifest.chunks.max(by: { $0.endedAt < $1.endedAt }) {
                manifest.endedAt = lastChunk.endedAt
                try? save(manifest, in: directory)
            }
            return MeetingSession(id: manifest.id, directory: directory, manifest: manifest)
        }.sorted { $0.manifest.startedAt > $1.manifest.startedAt }
    }
}
