import Foundation

final class ActiveSessionLedger: @unchecked Sendable {
    private let lock = NSLock()
    private let store: SessionStore
    let directory: URL
    private var manifest: MeetingManifest

    init(store: SessionStore, directory: URL, manifest: MeetingManifest) {
        self.store = store
        self.directory = directory
        self.manifest = manifest
    }

    func add(_ chunk: AudioChunk) {
        lock.lock()
        defer { lock.unlock() }
        manifest.chunks.append(chunk)
        do {
            try store.save(manifest, in: directory)
        } catch {
            NSLog("Could not update session ledger: %@", error.localizedDescription)
        }
    }

    func finalize() -> MeetingManifest {
        lock.lock()
        defer { lock.unlock() }
        manifest.endedAt = Date()
        do {
            try store.save(manifest, in: directory)
        } catch {
            NSLog("Could not finalize session ledger: %@", error.localizedDescription)
        }
        return manifest
    }
}
