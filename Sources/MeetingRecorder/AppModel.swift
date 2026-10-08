import AVFoundation
import AppKit
import CoreGraphics
import Foundation

@MainActor
final class AppModel: ObservableObject {
    @Published var sessions: [MeetingSession] = []
    @Published var microphones: [MicrophoneDevice] = []
    @Published var selectedMicrophoneID = ""
    @Published var selectedSessionID: UUID?
    @Published var meetingTitle = ""
    @Published var captureSystemAudio = true
    @Published var isRecording = false
    @Published var isProcessing = false
    @Published var status = "待機中"
    @Published var errorMessage: String?
    @Published var needsScreenRecordingPermission = false
    @Published var elapsed: TimeInterval = 0
    @Published var isRenamePresented = false
    @Published var isDeleteConfirmationPresented = false
    @Published var renameTitle = ""

    private var pendingRenameSessionID: UUID?
    private var pendingDeleteSessionID: UUID?

    @Published var whisperModel: String {
        didSet { UserDefaults.standard.set(whisperModel, forKey: "whisperModel") }
    }
    @Published var transcriptionEngine: String {
        didSet { UserDefaults.standard.set(transcriptionEngine, forKey: "transcriptionEngine") }
    }
    @Published var transcriptionLanguage: String {
        didSet { UserDefaults.standard.set(transcriptionLanguage, forKey: "transcriptionLanguage") }
    }
    @Published var transcriptionVocabulary: String {
        didSet { UserDefaults.standard.set(transcriptionVocabulary, forKey: "transcriptionVocabulary") }
    }
    @Published var transcriptionAudioSource: String {
        didSet { UserDefaults.standard.set(transcriptionAudioSource, forKey: "transcriptionAudioSource") }
    }
    @Published var ollamaModel: String {
        didSet { UserDefaults.standard.set(ollamaModel, forKey: "ollamaModel") }
    }
    @Published var huggingFaceToken: String {
        didSet { KeychainStore.save(huggingFaceToken, account: "pyannote-token") }
    }

    private let store = SessionStore()
    private let microphoneCapture = MicrophoneCapture()
    private let systemCapture = SystemAudioCapture()
    private var activeDirectory: URL?
    private var activeManifest: MeetingManifest?
    private var activeLedger: ActiveSessionLedger?
    private var timer: Timer?
    private var processingWasCancelled = false

    init() {
        whisperModel = UserDefaults.standard.string(forKey: "whisperModel") ?? "large-v3"
        #if arch(arm64)
        transcriptionEngine = UserDefaults.standard.string(forKey: "transcriptionEngine") ?? "metal"
        #else
        transcriptionEngine = UserDefaults.standard.string(forKey: "transcriptionEngine") ?? "cpu"
        #endif
        transcriptionLanguage = UserDefaults.standard.string(forKey: "transcriptionLanguage") ?? "ja"
        transcriptionVocabulary = UserDefaults.standard.string(forKey: "transcriptionVocabulary") ?? ""
        transcriptionAudioSource = UserDefaults.standard.string(forKey: "transcriptionAudioSource") ?? "separate"
        ollamaModel = UserDefaults.standard.string(forKey: "ollamaModel") ?? "qwen3.5:9b"
        huggingFaceToken = KeychainStore.load(account: "pyannote-token") ?? ""
        refreshDevices()
        refreshSessions()
    }

    var selectedSession: MeetingSession? {
        sessions.first { $0.id == selectedSessionID }
    }

    var pendingDeleteSession: MeetingSession? {
        sessions.first { $0.id == pendingDeleteSessionID }
    }

    func refreshDevices() {
        microphones = MicrophoneCapture.devices()
        if !microphones.contains(where: { $0.id == selectedMicrophoneID }) {
            selectedMicrophoneID = microphones.first?.id ?? ""
        }
    }

    func refreshSessions() {
        sessions = store.loadAll()
        if selectedSessionID == nil { selectedSessionID = sessions.first?.id }
    }

    func startRecording() async {
        guard !isRecording, let microphone = microphones.first(where: { $0.id == selectedMicrophoneID }) else {
            errorMessage = "使用するマイクを選択してください"
            return
        }

        let permission = await AVCaptureDevice.requestAccess(for: .audio)
        guard permission else {
            errorMessage = "システム設定でマイクへのアクセスを許可してください"
            return
        }

        if captureSystemAudio && !CGPreflightScreenCaptureAccess() {
            guard CGRequestScreenCaptureAccess() else {
                needsScreenRecordingPermission = true
                errorMessage = "システム設定の「プライバシーとセキュリティ」→「画面とシステムオーディオの録音」でMeeting Recorderを許可し、アプリを開き直してください。"
                return
            }
        }

        do {
            let (directory, manifest) = try store.createSession(
                title: meetingTitle,
                microphoneName: microphone.name,
                capturesSystemAudio: captureSystemAudio
            )
            activeDirectory = directory
            activeManifest = manifest
            let ledger = ActiveSessionLedger(store: store, directory: directory, manifest: manifest)
            activeLedger = ledger

            let callback: ChunkedAudioWriter.Completion = { [ledger] chunk in
                ledger.add(chunk)
            }
            let micWriter = ChunkedAudioWriter(
                source: .microphone,
                directory: directory.appendingPathComponent("audio"),
                sessionStartedAt: manifest.startedAt,
                onChunkCompleted: callback
            )
            try microphoneCapture.start(deviceID: microphone.id, writer: micWriter)

            if captureSystemAudio {
                let systemWriter = ChunkedAudioWriter(
                    source: .system,
                    directory: directory.appendingPathComponent("audio"),
                    sessionStartedAt: manifest.startedAt,
                    onChunkCompleted: callback
                )
                do {
                    try await systemCapture.start(writer: systemWriter)
                } catch {
                    await microphoneCapture.stop()
                    throw error
                }
            }

            isRecording = true
            status = "録音中"
            elapsed = 0
            timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                Task { @MainActor in
                    guard let self, let started = self.activeManifest?.startedAt else { return }
                    self.elapsed = Date().timeIntervalSince(started)
                }
            }
        } catch {
            if captureSystemAudio && !CGPreflightScreenCaptureAccess() {
                needsScreenRecordingPermission = true
                errorMessage = "システム設定の「プライバシーとセキュリティ」→「画面とシステムオーディオの録音」でMeeting Recorderを許可し、アプリを開き直してください。"
            } else {
                errorMessage = error.localizedDescription
            }
            activeDirectory = nil
            activeManifest = nil
            activeLedger = nil
            status = "録音を開始できませんでした"
        }
    }

    func stopRecording() async {
        guard isRecording else { return }
        status = "録音を保存中"
        timer?.invalidate()
        timer = nil
        await microphoneCapture.stop()
        if captureSystemAudio { await systemCapture.stop() }

        if let ledger = activeLedger { activeManifest = ledger.finalize() }
        isRecording = false
        activeDirectory = nil
        activeManifest = nil
        activeLedger = nil
        status = "保存しました"
        meetingTitle = ""
        refreshSessions()
    }

    func processSelectedSession(forceTranscription: Bool = false) async {
        guard let session = selectedSession, !isProcessing, !isRecording else { return }
        isProcessing = true
        processingWasCancelled = false
        status = "文字起こし・話者分類中"
        do {
            _ = try await ProcessingRunner.run(
                sessionDirectory: session.directory,
                whisperModel: whisperModel,
                engine: transcriptionEngine,
                ollamaModel: ollamaModel,
                huggingFaceToken: huggingFaceToken,
                language: transcriptionLanguage,
                vocabulary: transcriptionVocabulary,
                audioSource: transcriptionAudioSource,
                forceTranscription: forceTranscription,
                onProgress: { [weak self] message in
                    if self?.isProcessing == true, self?.processingWasCancelled == false { self?.status = message }
                }
            )
            refreshSessions()
            status = "議事録を作成しました"
        } catch {
            if processingWasCancelled {
                status = "文字起こし処理を中止しました"
            } else {
                errorMessage = error.localizedDescription
                status = "処理に失敗しました"
            }
        }
        isProcessing = false
    }

    func cancelProcessing() {
        guard isProcessing else { return }
        processingWasCancelled = true
        status = "文字起こし処理を中止しています"
        ProcessingRunner.cancel()
    }

    func revealSelectedSession() {
        guard let session = selectedSession else { return }
        NSWorkspace.shared.activateFileViewerSelecting([session.directory])
    }

    func open(_ url: URL) { NSWorkspace.shared.open(url) }

    func prepareRename(_ session: MeetingSession) {
        pendingRenameSessionID = session.id
        renameTitle = session.manifest.title
        isRenamePresented = true
    }

    @discardableResult
    func saveRename() -> Bool {
        guard !isProcessing, !isRecording else { return false }
        let title = renameTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else {
            errorMessage = "名前を入力してください"
            return false
        }
        guard let id = pendingRenameSessionID,
              let session = sessions.first(where: { $0.id == id }) else {
            errorMessage = "編集する録音が見つかりません"
            return false
        }
        do {
            try store.rename(session, to: title)
            refreshSessions()
            selectedSessionID = id
            pendingRenameSessionID = nil
            isRenamePresented = false
            status = "名前を変更しました"
            return true
        } catch {
            errorMessage = "名前を変更できませんでした: \(error.localizedDescription)"
            return false
        }
    }

    func prepareDelete(_ session: MeetingSession) {
        pendingDeleteSessionID = session.id
        isDeleteConfirmationPresented = true
    }

    func deletePreparedSession() {
        guard !isProcessing, !isRecording else { return }
        guard let session = pendingDeleteSession else {
            isDeleteConfirmationPresented = false
            return
        }
        do {
            try store.moveToTrash(session)
            if selectedSessionID == session.id { selectedSessionID = nil }
            pendingDeleteSessionID = nil
            isDeleteConfirmationPresented = false
            refreshSessions()
            status = "録音をゴミ箱へ移動しました"
        } catch {
            errorMessage = "録音を削除できませんでした: \(error.localizedDescription)"
            isDeleteConfirmationPresented = false
        }
    }

    func openScreenRecordingSettings() {
        needsScreenRecordingPermission = false
        errorMessage = nil
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }

}
