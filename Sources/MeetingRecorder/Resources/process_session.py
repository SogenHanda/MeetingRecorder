#!/usr/bin/env python3
"""Create a multilingual transcript, voice-based speaker labels and meeting minutes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SpeechSegment:
    start: float
    end: float
    text: str
    speaker: str = "SPEAKER_00"


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


def concat_source(session: Path, chunks: list[dict], source: str, destination: Path, ffmpeg: str) -> bool:
    selected = sorted(
        (chunk for chunk in chunks if chunk["source"] == source),
        key=lambda chunk: chunk["startedAt"],
    )
    files = [session / chunk["relativePath"] for chunk in selected]
    files = [path for path in files if path.exists()]
    if not files:
        return False
    concat_file = destination.with_suffix(".concat.txt")
    concat_file.write_text("".join(f"file '{str(path).replace(chr(39), chr(39) * 2)}'\n" for path in files), encoding="utf-8")
    run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-ar", "16000", "-ac", "1", str(destination)])
    concat_file.unlink(missing_ok=True)
    return True


def build_mix(session: Path, manifest: dict) -> Path:
    ffmpeg = find_ffmpeg()
    work = session / ".processing"
    work.mkdir(exist_ok=True)
    mic = work / "microphone.wav"
    system = work / "system.wav"
    has_mic = concat_source(session, manifest["chunks"], "microphone", mic, ffmpeg)
    has_system = concat_source(session, manifest["chunks"], "system", system, ffmpeg)
    if not has_mic and not has_system:
        raise RuntimeError("処理できる音声チャンクがありません。")
    mixed = work / "mixed.wav"
    if has_mic and has_system:
        run([
            ffmpeg, "-y", "-i", str(mic), "-i", str(system),
            "-filter_complex", "amix=inputs=2:duration=longest:normalize=0",
            "-ar", "16000", "-ac", "1", str(mixed),
        ])
    else:
        shutil.copy2(mic if has_mic else system, mixed)
    return mixed


def transcribe(audio: Path, model_name: str) -> tuple[list[SpeechSegment], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError("音声処理ランタイムが未設定です。プロジェクト内の setup_runtime.sh を実行してください。") from error
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    segments, info = model.transcribe(str(audio), beam_size=5, vad_filter=True, word_timestamps=False)
    result = [SpeechSegment(float(item.start), float(item.end), item.text.strip()) for item in segments if item.text.strip()]
    return result, info.language or "unknown"


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
    for preferred in ("qwen3:4b-instruct", "qwen3:1.7b", "gemma3:1b"):
        if preferred in installed:
            print(f"Requested Ollama model '{requested}' is unavailable; using '{preferred}'.", file=sys.stderr)
            return preferred
    usable = [name for name in installed if "embed" not in name.lower()]
    if usable:
        print(f"Requested Ollama model '{requested}' is unavailable; using '{usable[0]}'.", file=sys.stderr)
        return usable[0]
    raise RuntimeError(
        "要約用のOllamaモデルがありません。`ollama pull qwen3:4b-instruct` を実行してください。"
    )


def ollama_summary(title: str, transcript: str, requested_model: str) -> tuple[str, str]:
    model = resolve_ollama_model(requested_model.strip() or "qwen3:4b-instruct")
    prompt = f"""次の会議ログから、事実だけを使って日本語の議事録をMarkdownで作成してください。
見出しは「概要」「主な論点」「決定事項」「アクション項目」「未解決事項」とし、該当なしは「なし」と書いてください。
発言者はA、B、Cなどログの表記を維持してください。

会議名: {title}

ログ:
{transcript[:60000]}
"""
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "keep_alive": "10m",
            "options": {"temperature": 0.2},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response).get("response", "").strip()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollamaの要約処理に失敗しました: {detail}") from error
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Ollamaの要約処理に失敗しました: {error}") from error
    if not result:
        raise RuntimeError("Ollamaから空の要約が返されました。")
    return result, model


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--ollama-model", default="")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

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
        mixed = build_mix(session, manifest)
        segments, language = transcribe(mixed, args.model)
        apply_speakers(segments, diarize(mixed))
        transcript = write_transcript(session, manifest, segments, language)
    if not transcript.strip():
        write_no_speech_minutes(session, manifest["title"])
        print(session / "minutes.md")
        return 0
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
