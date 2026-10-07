#!/usr/bin/env python3
"""Create a multilingual transcript, voice-based speaker labels and meeting minutes."""

from __future__ import annotations

import argparse
import json
import os
import re
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
    print(f"要約用の事実抽出: {index}/{total}", file=sys.stderr, flush=True)
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
    print("議事録を根拠ログと照合中", file=sys.stderr, flush=True)
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
    print("全ログの抽出結果を統合中", file=sys.stderr, flush=True)
    draft = ollama_generate(model, final_prompt, num_predict=3000)
    verified = verify_summary(title, evidence, draft, model)
    return ensure_explicit_actions(verified, explicit_actions), model


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
