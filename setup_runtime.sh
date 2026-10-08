#!/bin/zsh
set -euo pipefail

runtime_root="$HOME/Library/Application Support/MeetingRecorder/runtime"
python_command=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [[ "$version" == "3.10" || "$version" == "3.11" || "$version" == "3.12" || "$version" == "3.13" ]]; then
      python_command="$candidate"
      break
    fi
  fi
done

if [[ -z "$python_command" ]]; then
  print -u2 "Python 3.10〜3.13が必要です。例: brew install python@3.12"
  exit 1
fi

mkdir -p "$runtime_root"
"$python_command" -m venv "$runtime_root/.venv"
"$runtime_root/.venv/bin/python3" -m pip install --upgrade pip
"$runtime_root/.venv/bin/python3" -m pip install -r "${0:A:h}/requirements.txt"
if [[ "$(uname -m)" == "arm64" ]]; then
  "${0:A:h}/setup_gpu_runtime.sh"
fi
print "ローカル音声処理ランタイムを作成しました: $runtime_root/.venv"
