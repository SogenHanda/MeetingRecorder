#!/usr/bin/env python3
"""Create a multilingual transcript, voice-based speaker labels and meeting minutes."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path


@dataclass
class SpeechSegment:
    start: float
    end: float
    text: str
    speaker: str = "SPEAKER_00"
    audio_source: str = "mixed"
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


def progress(message: str) -> None:
    print(f"PROGRESS: {message}", file=sys.stderr, flush=True)


def source_name(source: str) -> str:
    return {"microphone": "マイク", "system": "システム音声", "mixed": "混合音声"}.get(source, source)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def read_manifest(session: Path) -> dict:
    with (session / "session.json").open(encoding="utf-8") as handle:
        return json.load(handle)


def find_ffmpeg() -> str:
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    for candidate in (Path("/opt/homebrew/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")):
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("ffmpegが見つかりません。Homebrewで `brew install ffmpeg` を実行してください。")


def concat_source(session: Path, manifest: dict, source: str, destination: Path, ffmpeg: str) -> bool:
    selected = sorted(
        (chunk for chunk in manifest["chunks"] if chunk["source"] == source),
        key=lambda chunk: chunk["startedAt"],
    )
    if not selected:
        return False
    started_at = datetime.fromisoformat(manifest["startedAt"].replace("Z", "+00:00"))
    frames_written = 0
    # Decode one five-minute file at a time. Memory use stays bounded for long meetings.
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        for index, chunk in enumerate(selected, start=1):
            path = session / chunk["relativePath"]
            if not path.is_file():
                raise RuntimeError(f"元音声が見つかりません: {path.name}")
            chunk_start = datetime.fromisoformat(chunk["startedAt"].replace("Z", "+00:00"))
            target_frame = max(0, round((chunk_start - started_at).total_seconds() * 16000))
            silence_frames = max(0, target_frame - frames_written)
            while silence_frames:
                count = min(silence_frames, 16000)
                output.writeframesraw(bytes(count * 2))
                frames_written += count
                silence_frames -= count
            progress(f"{source_name(source)}を準備中 {index}/{len(selected)}")
            decoded = subprocess.run([
                ffmpeg, "-v", "error", "-i", str(path), "-ar", "16000", "-ac", "1",
                "-f", "s16le", "pipe:1",
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            output.writeframesraw(decoded)
            frames_written += len(decoded) // 2
    return True


def build_audio_sources(session: Path, manifest: dict, audio_source: str) -> dict[str, Path]:
    ffmpeg = find_ffmpeg()
    work = session / ".processing"
    work.mkdir(exist_ok=True)
    sources: dict[str, Path] = {}
    for source in ("microphone", "system"):
        if audio_source not in ("separate", "mixed", source):
            continue
        path = work / f"{source}.wav"
        if concat_source(session, manifest, source, path, ffmpeg):
            sources[source] = path
    if not sources:
        raise RuntimeError("処理できる音声チャンクがありません。")
    return sources


def build_mix(session: Path, sources: dict[str, Path]) -> Path:
    if len(sources) == 1:
        return next(iter(sources.values()))
    ffmpeg = find_ffmpeg()
    work = session / ".processing"
    mixed = work / "mixed.wav"
    run([
        ffmpeg, "-y", "-i", str(sources["microphone"]), "-i", str(sources["system"]),
        "-filter_complex", "amix=inputs=2:duration=longest:normalize=1",
        "-ar", "16000", "-ac", "1", str(mixed),
    ])
    return mixed


@dataclass
class MetalWhisperModel:
    executable: Path
    model_path: Path
    vad_path: Path
    verified: bool = False


def find_whisper_cli() -> Path:
    runtime = Path.home() / "Library/Application Support/MeetingRecorder/runtime/bin/whisper-cli"
    candidates = [runtime, Path("/opt/homebrew/bin/whisper-cli"), Path("/usr/local/bin/whisper-cli")]
    discovered = shutil.which("whisper-cli")
    if discovered:
        candidates.append(Path(discovered))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise RuntimeError("Metal GPU用ランタイムがありません。プロジェクト内の ./setup_gpu_runtime.sh を実行してください。")


def cached_gpu_file(repository: str, filename: str) -> Path:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    try:
        return Path(hf_hub_download(repository, filename, local_files_only=True))
    except LocalEntryNotFoundError:
        progress(f"GPU用モデルを初回ダウンロード中: {filename}")
        return Path(hf_hub_download(repository, filename))


def load_metal_model(model_name: str) -> MetalWhisperModel:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Metal GPU版はApple SiliconのMacが必要です。設定でCPUを選択すると従来方式を使用できます。")
    executable = find_whisper_cli()
    local_path = Path(model_name).expanduser()
    if local_path.is_file():
        model_path = local_path.resolve()
    else:
        supported = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
                     "large-v1", "large-v2", "large-v3", "large-v3-turbo"}
        if model_name not in supported:
            raise RuntimeError("GPU版では large-v3 / large-v3-turbo などのモデル名、またはGGML .binファイルのパスを指定してください。")
        # Full F16 model: do not silently trade recognition quality for quantization.
        model_path = cached_gpu_file("ggerganov/whisper.cpp", f"ggml-{model_name}.bin")
    vad_path = cached_gpu_file("ggml-org/whisper-vad", "ggml-silero-v6.2.0.bin")
    return MetalWhisperModel(executable, model_path, vad_path)


def load_whisper_model(model_name: str, engine: str = "metal"):
    if engine == "metal":
        return load_metal_model(model_name)
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        from huggingface_hub.errors import LocalEntryNotFoundError
    except ImportError as error:
        raise RuntimeError("音声処理ランタイムが未設定です。プロジェクト内の setup_runtime.sh を実行してください。") from error
    progress(f"文字起こしモデル {model_name} を読み込み中（初回はダウンロード）")
    if Path(model_name).is_dir():
        model_path = model_name
    else:
        try:
            model_path = download_model(model_name, local_files_only=True)
        except (LocalEntryNotFoundError, FileNotFoundError):
            model_path = ""
        if not model_path or not (Path(model_path) / "model.bin").is_file():
            progress(f"モデル {model_name} を初回ダウンロード中")
            model_path = download_model(model_name)
    return WhisperModel(model_path, device="auto", compute_type="int8", cpu_threads=min(8, os.cpu_count() or 4))


def transcribe(audio: Path, model, language: str, vocabulary: str, source: str) -> tuple[list[SpeechSegment], str]:
    if isinstance(model, MetalWhisperModel):
        return transcribe_metal(audio, model, language, vocabulary, source)
    segments, info = model.transcribe(
        str(audio), language=None if language == "auto" else language,
        multilingual=language == "auto",
        task="transcribe", beam_size=5, patience=1.2,
        temperature=(0.0, 0.2, 0.4), vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 600, "speech_pad_ms": 400},
        word_timestamps=True, hallucination_silence_threshold=2.0,
        initial_prompt=vocabulary.strip() or None, hotwords=vocabulary.strip() or None,
    )
    result = []
    last_reported = -30.0
    for item in segments:
        if item.text.strip():
            result.append(SpeechSegment(
                float(item.start), float(item.end), item.text.strip(),
                audio_source=source, avg_logprob=float(item.avg_logprob),
                no_speech_prob=float(item.no_speech_prob),
            ))
        if item.end - last_reported >= 30:
            progress(f"{source_name(source)}を文字起こし中 {timestamp(item.end)} / {timestamp(info.duration)}")
            last_reported = item.end
    return result, info.language or "unknown"


def parse_metal_segments(payload: dict, source: str, offset: float = 0) -> list[SpeechSegment]:
    result = []
    for item in payload["transcription"]:
        text = item["text"].strip()
        if not text:
            continue
        result.append(SpeechSegment(
            offset + item["offsets"]["from"] / 1000,
            offset + item["offsets"]["to"] / 1000,
            text, audio_source=source,
        ))
    return result


def run_metal_window(command: list[str], output: Path, model: MetalWhisperModel,
                     source: str, completed: float, window_duration: float, total: float) -> dict:
    # Drain output continuously, but do not expose recognized private text in UI logs.
    tail = ""
    gpu_selected = False
    gpu_weights = False
    gpu_failed = False
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace") as process:
        def stop_child(signum, frame):
            process.terminate()
            raise SystemExit(128 + signum)

        previous_handlers = {signum: signal.signal(signum, stop_child) for signum in (signal.SIGTERM, signal.SIGINT)}
        try:
            for line in process.stderr:
                tail = (tail + line)[-16000:]
                if re.search(r"whisper_backend_init_gpu: using (?:Metal|MTL)\d* backend", line):
                    gpu_selected = True
                if re.search(r"whisper_model_load:.*(?:Metal|MTL)\d*.*total size\s*=\s*[1-9][\d.]* MB", line):
                    gpu_weights = True
                if re.search(r"failed to initialize (?:Metal|MTL)\d* backend", line):
                    gpu_failed = True
                match = re.search(r"progress\s*=\s*(\d+)%", line)
                if match:
                    position = min(total, completed + window_duration * int(match[1]) / 100)
                    progress(f"{source_name(source)}をGPUで文字起こし中 {timestamp(position)} / {timestamp(total)}")
            code = process.wait()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    if code != 0:
        raise RuntimeError(f"Metal GPU文字起こしに失敗しました（終了コード {code}）:\n{tail}")
    if not (gpu_selected and gpu_weights) or gpu_failed:
        raise RuntimeError(f"Metal GPUが使用されませんでした。CPUへ自動的には切り替えません。./setup_gpu_runtime.sh を再実行してください。\n{tail}")
    model.verified = True
    if not output.is_file():
        raise RuntimeError(f"GPU認識結果が作成されませんでした。言語・モデルの設定を確認してください。\n{tail}")
    with output.open(encoding="utf-8") as handle:
        return json.load(handle)


def transcribe_metal(audio: Path, model: MetalWhisperModel, language: str,
                     vocabulary: str, source: str) -> tuple[list[SpeechSegment], str]:
    result: list[SpeechSegment] = []
    languages: list[str] = []
    # Bound decoding memory for long meetings. Overlap provides context at boundaries;
    # each segment belongs to exactly one window according to its midpoint.
    window_seconds = 600
    context_seconds = 10
    with wave.open(str(audio), "rb") as original, tempfile.TemporaryDirectory(prefix="metal-", dir=audio.parent) as folder:
        rate = original.getframerate()
        total_frames = original.getnframes()
        total = total_frames / rate
        for core_start_frame in range(0, total_frames, window_seconds * rate):
            core_end_frame = min(total_frames, core_start_frame + window_seconds * rate)
            start_frame = max(0, core_start_frame - context_seconds * rate)
            end_frame = min(total_frames, core_end_frame + context_seconds * rate)
            window = Path(folder) / "window.wav"
            output_base = Path(folder) / "result"
            output = output_base.with_suffix(".json")
            if output.exists():
                output.unlink()
            original.setpos(start_frame)
            with wave.open(str(window), "wb") as chunk:
                chunk.setparams(original.getparams())
                chunk.writeframes(original.readframes(end_frame - start_frame))
            progress(f"{source_name(source)}をMetal GPUで文字起こし中 {timestamp(core_start_frame / rate)} / {timestamp(total)}")
            command = [
                str(model.executable), "--model", str(model.model_path), "--file", str(window),
                "--language", language, "--beam-size", "5", "--best-of", "5", "--threads", "4",
                "--flash-attn", "--vad", "--vad-model", str(model.vad_path),
                "--vad-min-silence-duration-ms", "600", "--vad-speech-pad-ms", "400",
                "--output-json-full", "--output-file", str(output_base), "--print-progress", "--suppress-nst",
            ]
            if vocabulary.strip():
                command.extend(["--prompt", vocabulary.strip(), "--carry-initial-prompt"])
            payload = run_metal_window(command, output, model, source,
                                       start_frame / rate, (end_frame - start_frame) / rate, total)
            languages.append(payload.get("result", {}).get("language", "unknown"))
            for item in parse_metal_segments(payload, source, start_frame / rate):
                midpoint = (item.start + item.end) / 2
                if core_start_frame / rate <= midpoint < core_end_frame / rate:
                    result.append(item)
    return result, ", ".join(dict.fromkeys(languages)) or language


def merge_sources(segments: list[SpeechSegment]) -> tuple[list[SpeechSegment], int]:
    """Suppress only highly similar, overlapping cross-source echoes; keep raw source results."""
    result: list[SpeechSegment] = []
    active: list[SpeechSegment] = []
    duplicates = 0
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        normalized = re.sub(r"[\W_]+", "", segment.text).lower()
        duplicate = False
        active = [previous for previous in active if previous.end >= segment.start]
        for previous in reversed(active):
            if previous.audio_source == segment.audio_source:
                continue
            overlap = min(previous.end, segment.end) - max(previous.start, segment.start)
            minimum_duration = max(0.1, min(previous.end - previous.start, segment.end - segment.start))
            if overlap / minimum_duration < 0.7:
                continue
            previous_text = re.sub(r"[\W_]+", "", previous.text).lower()
            if len(normalized) >= 12 and SequenceMatcher(None, normalized, previous_text).ratio() >= 0.95:
                duplicate = True
                break
        if duplicate:
            duplicates += 1
        else:
            result.append(segment)
            active.append(segment)
    return result, duplicates


def backup_outputs(session: Path) -> Path | None:
    files = [session / name for name in ("transcript.md", "transcript.json", "transcript.sources.json", "transcription.json", "minutes.md")]
    existing = [path for path in files if path.is_file()]
    if not existing:
        return None
    history = session / "history" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    history.mkdir(parents=True)
    for path in existing:
        shutil.copy2(path, history / path.name)
    return history


def diarize(audio: Path) -> list[tuple[float, float, str]]:
    token = os.environ.get("PYANNOTE_TOKEN", "").strip()
    if not token:
        print("Warning: Hugging Face token is not configured; all speech will use speaker A.", file=sys.stderr)
        return []
    try:
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=token)
        output = pipeline(str(audio))
        annotation = getattr(output, "speaker_diarization", output)
        return [(float(turn.start), float(turn.end), str(speaker)) for turn, _, speaker in annotation.itertracks(yield_label=True)]
    except Exception as error:
        print(f"Warning: speaker classification failed: {error}", file=sys.stderr)
        return []


def apply_speakers(segments: list[SpeechSegment], turns: list[tuple[float, float, str]]) -> None:
    first_seen: dict[str, float] = {}
    for start, _, speaker in turns:
        first_seen[speaker] = min(start, first_seen.get(speaker, start))
    ordered = sorted(first_seen, key=first_seen.get)
    labels = {speaker: chr(ord("A") + index) if index < 26 else f"{index + 1}" for index, speaker in enumerate(ordered)}
    for segment in segments:
        overlaps = []
        for start, end, speaker in turns:
            overlap = max(0.0, min(segment.end, end) - max(segment.start, start))
            if overlap > 0:
                overlaps.append((overlap, speaker))
        if overlaps:
            segment.speaker = max(overlaps)[1]
        else:
            segment.speaker = "SPEAKER_00"
        segment.speaker = labels.get(segment.speaker, "A")


def timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    return f"{value // 3600:02d}:{(value % 3600) // 60:02d}:{value % 60:02d}"


def write_transcript(session: Path, manifest: dict, segments: list[SpeechSegment], language: str) -> str:
    lines = [f"# {manifest['title']} — 全文ログ", "", f"- 検出言語: {language}", f"- 開始: {manifest['startedAt']}", ""]
    current = None
    plain = []
    for segment in segments:
        if segment.speaker != current:
            lines.extend([f"## 話者 {segment.speaker}", ""])
            current = segment.speaker
        line = f"[{timestamp(segment.start)}] {segment.text}"
        lines.append(line)
        plain.append(f"[{timestamp(segment.start)}] 話者{segment.speaker}: {segment.text}")
    (session / "transcript.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (session / "transcript.json").write_text(json.dumps([vars(item) for item in segments], ensure_ascii=False, indent=2), encoding="utf-8")
    return "\n".join(plain)


def plain_transcript(segments: list[SpeechSegment]) -> str:
    return "\n".join(
        f"[{timestamp(item.start)}] 話者{item.speaker}: {item.text}"
        for item in segments
    )


def load_existing_segments(session: Path) -> list[SpeechSegment]:
    path = session / "transcript.json"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [SpeechSegment(**item) for item in json.load(handle)]


def write_no_speech_minutes(session: Path, title: str) -> None:
    content = f"""# {title} — 議事録

## 概要

録音から要約対象となる発言を検出できませんでした。

## 主な論点

- なし

## 決定事項

- なし

## アクション項目

- なし

## 未解決事項

- なし

---

要約モデル: 未使用（発言なし）
"""
    (session / "minutes.md").write_text(content, encoding="utf-8")


def available_ollama_models() -> list[str]:
    request = urllib.request.Request("http://127.0.0.1:11434/api/tags")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Ollamaへ接続できません。`brew services start ollama` を実行してから再試行してください。"
        ) from error
    return [
        item.get("name") or item.get("model")
        for item in payload.get("models", [])
        if item.get("name") or item.get("model")
    ]


def resolve_ollama_model(requested: str) -> str:
    installed = available_ollama_models()
    if requested in installed:
        return requested
    requested_latest = f"{requested}:latest" if ":" not in requested else requested
    if requested_latest in installed:
        return requested_latest
    for preferred in ("qwen3.5:9b", "qwen3:4b-instruct", "qwen3:1.7b", "gemma3:1b"):
        if preferred in installed:
            print(f"Requested Ollama model '{requested}' is unavailable; using '{preferred}'.", file=sys.stderr)
            return preferred
    usable = [name for name in installed if "embed" not in name.lower()]
    if usable:
        print(f"Requested Ollama model '{requested}' is unavailable; using '{usable[0]}'.", file=sys.stderr)
        return usable[0]
    raise RuntimeError(
        "要約用のOllamaモデルがありません。`ollama pull qwen3.5:9b` を実行してください。"
    )


def ollama_generate(model: str, prompt: str, num_predict: int) -> str:
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=json.dumps({
            "model": model,
            "prompt": prompt,
            "system": (
                "あなたは会議記録を扱う慎重な編集者です。入力にない事実を補わず、"
                "提案・検討・決定を明確に区別してください。回答は日本語で記述してください。"
            ),
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {
                "temperature": 0.1,
                "num_ctx": 32768,
                "num_predict": num_predict,
            },
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1200) as response:
            result = json.load(response).get("response", "").strip()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollamaの要約処理に失敗しました: {detail}") from error
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Ollamaの要約処理に失敗しました: {error}") from error
    if not result:
        raise RuntimeError("Ollamaから空の要約が返されました。")
    return result


def split_transcript(transcript: str, max_chars: int = 18000) -> list[str]:
    """Split on utterance boundaries while retaining every part of the transcript."""
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in transcript.splitlines():
        line_size = len(line) + 1
        if current and current_size + line_size > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_size = 0
        if line_size > max_chars:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_size = 0
            chunks.extend(line[index:index + max_chars] for index in range(0, len(line), max_chars))
            continue
        current.append(line)
        current_size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def explicit_action_items(transcript: str, limit: int = 80) -> list[str]:
    """Create evidence-preserving items from unambiguous future-work expressions."""
    pattern = re.compile(
        r"(進めます|共有します|確認します|対応します|連絡します|送ります|"
        r"作ります|まとめます|提出します|申請します|実施します|やります|検討します)"
    )
    line_pattern = re.compile(r"^\[([^]]+)] 話者([^:]+):\s*(.+)$")
    deadline_pattern = re.compile(r"(今週|来週|今月|来月|明日|本日|今日|\d{1,2}月\d{1,2}日)")
    lines = transcript.splitlines()
    actions: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        matched = line_pattern.match(line)
        if not matched or not pattern.search(matched.group(3)):
            continue
        target_text = matched.group(3).strip()
        if target_text.endswith(("か", "?", "？")):
            continue
        speaker = matched.group(2)
        context: list[tuple[str, str]] = [(matched.group(1), target_text)]
        for previous_index in range(index - 1, max(-1, index - 4), -1):
            previous = line_pattern.match(lines[previous_index])
            if not previous or previous.group(2) != speaker:
                break
            context.insert(0, (previous.group(1), previous.group(3).strip()))
        combined_text = " ".join(text for _, text in context)
        if "もし" in combined_text or "仮に" in combined_text:
            continue
        deadline_match = deadline_pattern.search(combined_text)
        if deadline_match:
            deadline = deadline_match.group(1)
            deadline_index = next(
                item_index for item_index, (_, text) in enumerate(context)
                if deadline_pattern.search(text)
            )
            context = context[deadline_index:]
            combined_text = " ".join(text for _, text in context)
        else:
            deadline = "不明"
        time_range = context[0][0] if len(context) == 1 else f"{context[0][0]}〜{context[-1][0]}"
        item = f"- 話者{speaker} / 「{combined_text}」 / {deadline}（根拠: {time_range}）"
        if item not in seen:
            actions.append(item)
            seen.add(item)
        if len(actions) >= limit:
            break
    return actions


def ensure_explicit_actions(summary: str, actions: list[str]) -> str:
    if not actions:
        return summary
    match = re.search(r"(^## アクション項目\s*$)(.*?)(?=^## |\Z)", summary, flags=re.MULTILINE | re.DOTALL)
    if not match:
        return summary
    existing = match.group(2).strip()
    existing_items = [] if not existing or existing == "なし" else existing.splitlines()

    def action_key(item: str) -> str:
        quoted = re.search(r"「([^」]+)」", item)
        return re.sub(r"\s+", "", quoted.group(1) if quoted else item)

    explicit_keys = {action_key(item) for item in actions}
    merged = [item for item in existing_items if action_key(item) not in explicit_keys] + actions
    unique: list[str] = []
    seen: set[str] = set()
    for item in merged:
        key = action_key(item)
        if key not in seen:
            unique.append(item)
            seen.add(key)
    content = "\n".join(unique)
    replacement = f"{match.group(1)}\n{content}\n\n"
    return summary[:match.start()] + replacement + summary[match.end():]


def direct_summary_prompt(title: str, transcript: str) -> str:
    return f"""以下の会議ログ全体から、読み手が会議の結論と次の行動を正確に把握できる議事録を作成してください。

必須ルール:
- ログに明記された事実だけを使い、推測や一般論を追加しない
- 提案や検討中の内容を「決定事項」に含めない
- 担当者と期限は、明言された場合だけ記載する
- 「〜します」「進めます」「共有します」など、話者が未来の作業を約束した発言はアクション項目として扱う
- 単なる論点をアクションや未解決事項へ言い換えない。依頼・宿題・保留・継続検討が明示されていなければ「なし」とする
- 重要な項目には、根拠となる時刻と話者を可能な限り付ける
- 重複発言は統合するが、反対意見や条件は落とさない
- 該当する内容がない見出しには「なし」と書く
- 話者はログにあるA、B、Cなどの表記を維持する

出力形式（この順番を厳守）:
# {title} — 議事録

## 概要
会議の目的、議論の流れ、結論を2〜4文で記載。

## 主な論点
箇条書き。

## 決定事項
箇条書き。

## アクション項目
「担当者 / 内容 / 期限」が分かる箇条書き。明言されていない項目は「不明」とする。

## 未解決事項
会議中に未解決・継続検討とされた項目だけを箇条書き。

会議ログ:
{transcript}
"""


def extract_chunk_notes(title: str, chunk: str, index: int, total: int, model: str) -> str:
    prompt = f"""会議「{title}」のログを全{total}分割したうち、第{index}部分です。
後段で全体の議事録を作るため、この部分の事実を漏れなく簡潔に抽出してください。

必須ルール:
- 発言にない解釈を加えない
- 提案、検討、合意済みの決定を区別する
- 時刻、話者、数値、固有名詞、条件、反対意見を保持する
- アクションの担当者・期限は明言された場合だけ記載する
- 「〜します」「進めます」「共有します」などの未来の作業を約束した発言を、時刻・話者・期限とともに必ず抽出する
- 単なる論点をアクションや未解決事項へ言い換えない
- 内容がない分類には「なし」と書く

出力見出し:
## 話題と要点
## 決定事項
## アクション項目
## 未解決事項
## 重要な背景・条件

対象ログ:
{chunk}
"""
    progress(f"要約用の事実抽出: {index}/{total}")
    return ollama_generate(model, prompt, num_predict=1600)


def verify_summary(title: str, evidence: str, draft: str, model: str) -> str:
    prompt = f"""会議「{title}」の議事録案を、根拠資料と照合して校正してください。
校正後の完成版だけを出力し、校正内容の説明や前置きは書かないでください。

校正ルール:
- 根拠資料から直接確認できない主張、因果関係、評価、未来予測は削除する
- 「決定事項」は参加者の合意または決定が明示されたものだけ残す
- 「アクション項目」は依頼、約束、担当、次の作業が明示されたものだけ残す
- 「〜します」「進めます」「共有します」などの未来の作業を約束した発言は、明示的なアクションとして残す
- 「未解決事項」は保留、未回答、継続検討が明示されたものだけ残す。単なる話題や問題提起を未解決事項にしない
- 担当者や期限が明記されていなければ「不明」とし、推測しない
- 根拠にある重要な反対意見、条件、後半で更新された結論を落とさない
- 項目がなくなった見出しには「なし」と書く
- レベル2見出しは「概要」「主な論点」「決定事項」「アクション項目」「未解決事項」の5つだけとし、この順番を維持する
- 「背景」「条件」など指定外の見出しは作らず、必要な内容は「主な論点」へ統合する

根拠資料:
{evidence}

校正対象の議事録案:
{draft}
"""
    progress("議事録を根拠ログと照合中")
    return ollama_generate(model, prompt, num_predict=3000)


def ollama_summary(title: str, transcript: str, requested_model: str) -> tuple[str, str]:
    model = resolve_ollama_model(requested_model.strip() or "qwen3.5:9b")
    chunks = split_transcript(transcript)
    explicit_actions = explicit_action_items(transcript)
    if len(chunks) == 1:
        draft = ollama_generate(model, direct_summary_prompt(title, transcript), num_predict=2400)
        verified = verify_summary(title, transcript, draft, model)
        return ensure_explicit_actions(verified, explicit_actions), model

    notes = [
        extract_chunk_notes(title, chunk, index, len(chunks), model)
        for index, chunk in enumerate(chunks, start=1)
    ]
    combined_notes = "\n\n".join(
        f"# ログ分割 {index}/{len(notes)} の抽出結果\n\n{note}"
        for index, note in enumerate(notes, start=1)
    )
    action_evidence = "\n".join(explicit_actions) if explicit_actions else "なし"
    evidence = (
        f"{combined_notes}\n\n# 原ログから機械抽出した明示的なアクション\n\n"
        "以下は原ログの話者・発言・期限・時刻をそのまま保持した項目です。"
        "最終議事録のアクション項目へ必ず反映してください。\n\n"
        f"{action_evidence}"
    )
    final_prompt = f"""以下は、会議「{title}」の全ログを時系列に分割し、各部分から事実を抽出した結果です。
全分割の情報を統合して最終議事録を作成してください。前半だけを重視せず、後半の決定や結論も必ず反映してください。

必須ルール:
- 抽出結果にない事実を追加しない
- 提案・候補・個人の意見を、合意済みの決定として扱わない
- 重複は統合する一方、変更された結論は最終状態が分かるようにする
- 担当者と期限は明言された場合だけ記載し、それ以外は「不明」とする
- 「〜します」「進めます」「共有します」など、話者が未来の作業を約束した発言はアクション項目として扱う
- 単なる論点をアクションや未解決事項へ言い換えない。依頼・宿題・保留・継続検討が明示されていなければ「なし」とする
- 重要な項目には、残っている時刻と話者を可能な限り付ける
- 該当する内容がない見出しには「なし」と書く

出力形式（この順番を厳守）:
# {title} — 議事録

## 概要
会議の目的、議論の流れ、結論を2〜4文で記載。

## 主な論点
箇条書き。

## 決定事項
箇条書き。

## アクション項目
「担当者 / 内容 / 期限」が分かる箇条書き。

## 未解決事項
会議中に未解決・継続検討とされた項目だけを箇条書き。

上記5つ以外のレベル2見出しは作らないこと。背景や条件は「主な論点」に統合すること。

全ログからの抽出結果と行動候補:
{evidence}
    """
    progress("全ログの抽出結果を統合中")
    draft = ollama_generate(model, final_prompt, num_predict=3000)
    verified = verify_summary(title, evidence, draft, model)
    return ensure_explicit_actions(verified, explicit_actions), model


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--engine", choices=("metal", "cpu"), default="metal")
    parser.add_argument("--prepare-gpu", action="store_true")
    parser.add_argument("--ollama-model", default="")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--summary-only", action="store_true")
    mode.add_argument("--force-transcription", action="store_true")
    parser.add_argument("--transcribe-only", action="store_true")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--vocabulary", default="")
    parser.add_argument("--audio-source", choices=("separate", "mixed", "microphone", "system"), default="separate")
    args = parser.parse_args()

    if args.prepare_gpu:
        load_metal_model(args.model)
        print("GPU用モデルの準備が完了しました。")
        return 0
    if not args.session:
        parser.error("--session が必要です")

    session = Path(args.session).resolve()
    manifest = read_manifest(session)
    existing_transcript = session / "transcript.json"
    segments = load_existing_segments(session) if args.summary_only else []
    if args.summary_only and existing_transcript.exists() and not segments:
        write_no_speech_minutes(session, manifest["title"])
        print(session / "minutes.md")
        return 0
    if segments:
        transcript = plain_transcript(segments)
    else:
        sources = build_audio_sources(session, manifest, args.audio_source)
        started_processing = time.monotonic()
        model = load_whisper_model(args.model, args.engine)
        raw_segments: list[SpeechSegment] = []
        languages: list[str] = []
        inputs = {"mixed": build_mix(session, sources)} if args.audio_source == "mixed" else sources
        for source, audio in inputs.items():
            source_segments, detected_language = transcribe(audio, model, args.language, args.vocabulary, source)
            raw_segments.extend(source_segments)
            languages.append(detected_language)
        gpu_verified = isinstance(model, MetalWhisperModel) and model.verified
        del model
        segments, duplicates = merge_sources(raw_segments)
        progress("声ごとの話者を分類中")
        turns = diarize(build_mix(session, sources)) if os.environ.get("PYANNOTE_TOKEN", "").strip() else []
        apply_speakers(segments, turns)
        language = ", ".join(dict.fromkeys(languages))
        backup = backup_outputs(session)
        if backup:
            progress(f"以前のログと議事録を保存しました: {backup.name}")
        transcript = write_transcript(session, manifest, segments, language)
        (session / "transcript.sources.json").write_text(
            json.dumps([vars(item) for item in raw_segments], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        metadata = {
            "model": args.model, "requested_language": args.language,
            "engine": "whisper.cpp" if args.engine == "metal" else "faster-whisper",
            "device": "metal" if gpu_verified else "cpu",
            "gpu_verified": gpu_verified,
            "recognition_seconds": round(time.monotonic() - started_processing, 2),
            "detected_languages": languages, "vocabulary": args.vocabulary,
            "audio_source": args.audio_source, "duplicate_echoes_removed": duplicates,
            "speaker_classification": "voice" if turns else "unavailable",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "low_confidence_segments": None if args.engine == "metal" else sum(
                item.avg_logprob is not None and item.avg_logprob < -1.0 for item in segments
            ),
            "backup_directory": str(backup.relative_to(session)) if backup else None,
        }
        (session / "transcription.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.transcribe_only:
        print(session / "transcript.md")
        return 0
    if not transcript.strip():
        write_no_speech_minutes(session, manifest["title"])
        print(session / "minutes.md")
        return 0
    progress("全文ログから議事録を作成中")
    summary, summary_model = ollama_summary(manifest["title"], transcript, args.ollama_model)
    if not summary.startswith("#"):
        summary = f"# {manifest['title']} — 議事録\n\n{summary}"
    summary = summary.rstrip() + f"\n\n---\n\n要約モデル: `{summary_model}`\n"
    (session / "minutes.md").write_text(summary, encoding="utf-8")
    print(session / "minutes.md")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
