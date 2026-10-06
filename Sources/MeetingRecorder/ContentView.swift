import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationSplitView {
            List(model.sessions, selection: $model.selectedSessionID) { session in
                VStack(alignment: .leading, spacing: 4) {
                    Text(session.manifest.title).font(.headline)
                    Text(session.manifest.startedAt.formatted(date: .abbreviated, time: .shortened))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .tag(session.id)
                .padding(.vertical, 3)
            }
            .navigationTitle("ミーティング")
            .toolbar {
                Button(action: model.refreshSessions) { Image(systemName: "arrow.clockwise") }
            }
        } detail: {
            VStack(spacing: 0) {
                recordingPanel
                Divider()
                sessionDetail
            }
            .navigationTitle("Meeting Recorder")
        }
        .alert("エラー", isPresented: Binding(
            get: { model.errorMessage != nil },
            set: {
                if !$0 {
                    model.errorMessage = nil
                    model.needsScreenRecordingPermission = false
                }
            }
        )) {
            if model.needsScreenRecordingPermission {
                Button("システム設定を開く") { model.openScreenRecordingSettings() }
            }
            Button("OK", role: .cancel) { model.errorMessage = nil }
        } message: {
            Text(model.errorMessage ?? "")
        }
    }

    private var recordingPanel: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("新しい録音").font(.title2.bold())
                    Text("Zoomなどの音声を、ほかのアプリの音響設定を変えずに記録します。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                if model.isRecording {
                    Label(duration(model.elapsed), systemImage: "record.circle.fill")
                        .font(.title3.monospacedDigit())
                        .foregroundStyle(.red)
                }
            }

            HStack(spacing: 12) {
                TextField("会議名（任意）", text: $model.meetingTitle)
                    .textFieldStyle(.roundedBorder)
                Picker("マイク", selection: $model.selectedMicrophoneID) {
                    ForEach(model.microphones) { device in Text(device.name).tag(device.id) }
                }
                .frame(width: 260)
                Button(action: model.refreshDevices) { Image(systemName: "arrow.clockwise") }
                    .help("マイク一覧を更新")
            }

            HStack {
                Toggle("システム音声を録音（Zoom対応）", isOn: $model.captureSystemAudio)
                    .disabled(model.isRecording)
                Spacer()
                Text(model.status).font(.callout).foregroundStyle(.secondary)
                if model.isRecording {
                    Button("停止", role: .destructive) { Task { await model.stopRecording() } }
                        .keyboardShortcut(".", modifiers: .command)
                } else {
                    Button("録音開始") { Task { await model.startRecording() } }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.selectedMicrophoneID.isEmpty)
                }
            }
        }
        .padding(24)
        .background(.regularMaterial)
    }

    @ViewBuilder
    private var sessionDetail: some View {
        if let session = model.selectedSession {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(session.manifest.title).font(.title.bold())
                        Text("\(session.manifest.startedAt.formatted(date: .long, time: .shortened)) · \(duration(session.manifest.duration))")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Finderで表示", action: model.revealSelectedSession)
                }

                Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 8) {
                    GridRow { Text("マイク").foregroundStyle(.secondary); Text(session.manifest.microphoneName) }
                    GridRow { Text("システム音声").foregroundStyle(.secondary); Text(session.manifest.capturesSystemAudio ? "あり" : "なし") }
                    GridRow { Text("音声チャンク").foregroundStyle(.secondary); Text("\(session.manifest.chunks.count)個") }
                }

                HStack {
                    Button {
                        Task { await model.processSelectedSession() }
                    } label: {
                        if model.isProcessing { ProgressView().controlSize(.small) } else { Label("議事録を作成", systemImage: "text.bubble") }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isProcessing || session.manifest.chunks.isEmpty)

                    if FileManager.default.fileExists(atPath: session.transcriptURL.path) {
                        Button("全文ログを開く") { model.open(session.transcriptURL) }
                    }
                    if FileManager.default.fileExists(atPath: session.summaryURL.path) {
                        Button("要約を開く") { model.open(session.summaryURL) }
                    }
                }

                Spacer()
                Text("音声は5分ごとに分割され、アプリケーションサポート領域へ保存されます。話者名は声ごとにA、B、C…として出力されます。")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            .padding(24)
        } else {
            VStack(spacing: 12) {
                Image(systemName: "waveform")
                    .font(.system(size: 42))
                    .foregroundStyle(.secondary)
                Text("録音を選択").font(.title2.bold())
                Text("左側から過去の録音を選択してください。")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func duration(_ seconds: TimeInterval) -> String {
        let value = max(0, Int(seconds))
        return String(format: "%02d:%02d:%02d", value / 3600, (value % 3600) / 60, value % 60)
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        Form {
            TextField("Whisperモデル", text: $model.whisperModel)
            Text("例: small / medium / large-v3。初回のみモデルがダウンロードされます。")
                .font(.caption).foregroundStyle(.secondary)
            TextField("Ollamaモデル", text: $model.ollamaModel)
            Text("推奨: qwen3:4b-instruct。Ollama未起動やモデル未導入の場合は、簡易要約へ落とさずエラーを表示します。")
                .font(.caption).foregroundStyle(.secondary)
            SecureField("Hugging Faceトークン（話者分類用）", text: $model.huggingFaceToken)
            Text("pyannoteの話者分類に使用します。値はmacOSキーチェーンへ保存されます。")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(24)
    }
}
