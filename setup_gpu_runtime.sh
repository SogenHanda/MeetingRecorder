#!/bin/zsh
set -euo pipefail

project_root="${0:A:h}"
runtime_root="$HOME/Library/Application Support/MeetingRecorder/runtime"
python_command="$runtime_root/.venv/bin/python3"
whisper_version="v1.9.4"
source_root="$runtime_root/whisper.cpp-$whisper_version"

if [[ ! -x "$python_command" ]]; then
  print -u2 "先にプロジェクト内の ./setup_runtime.sh を実行してください。"
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  print -u2 "GPU版はApple SiliconのMacを対象にしています。Intel Macでは設定でCPUを選択してください。"
  exit 1
fi

# Do not install or upgrade global Homebrew packages used by other applications.
if [[ ! -x "$runtime_root/.venv/bin/cmake" ]]; then
  "$python_command" -m pip install 'cmake>=3.28,<4'
fi
if [[ ! -d "$source_root/.git" ]]; then
  git clone --depth 1 --branch "$whisper_version" https://github.com/ggml-org/whisper.cpp.git "$source_root"
fi
if [[ "$(git -C "$source_root" rev-parse HEAD)" != "927cfce34f31707e17f2bff35c349632fb9e2c3a" ]]; then
  print -u2 "GPUエンジンのソースが指定バージョンと異なります: $source_root"
  exit 1
fi
"$runtime_root/.venv/bin/cmake" -S "$source_root" -B "$source_root/build" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
  -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON \
  -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF -DWHISPER_BUILD_IS_DEV=OFF
"$runtime_root/.venv/bin/cmake" --build "$source_root/build" --target whisper-cli --parallel 4
mkdir -p "$runtime_root/bin"
cp "$source_root/build/bin/whisper-cli" "$runtime_root/bin/whisper-cli"
HF_HUB_DISABLE_XET=1 "$python_command" "$project_root/Sources/MeetingRecorder/Resources/process_session.py" --prepare-gpu
print "Metal GPU音声処理ランタイムを作成しました: $runtime_root/bin/whisper-cli"
