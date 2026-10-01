#!/usr/bin/env bash
# videotab セットアップスクリプト(macOS / Linux)
#
# やること:
#   1. uv(Python パッケージマネージャ)があるか確認。無ければ公式インストーラの
#      実行を提案(同意したときだけ実行)
#   2. ffmpeg と ffprobe があるか確認。無ければ Homebrew での導入を提案するか、導入方法を案内
#      (ffprobe は ffmpeg と一緒に入ります)
#   3. uv sync --locked でロック済み依存一式(Python 3.11 含む)を導入
#   4. 読み取りに使う Claude Code / Codex があるか確認(ログインが要るので導入はしない)
#
# 何度実行しても安全です(導入済みの項目はスキップされます)。

set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }

say "videotab セットアップを開始します"

# --- 1. uv -----------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv: $(uv --version)"
else
  warn "uv が見つかりません。uv は Python 本体と依存の導入を全部やってくれるツールです。"
  printf '    公式インストーラ (https://astral.sh/uv) を実行しますか? [y/N] '
  read -r answer || answer=""
  if [ "${answer:-}" = "y" ] || [ "${answer:-}" = "Y" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # インストーラが入れる標準の場所を PATH に足す(このスクリプト実行中だけ)
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || {
      warn "uv が PATH に見つかりません。ターミナルを開き直してから再実行してください。"
      exit 1
    }
    ok "uv: $(uv --version)"
  else
    warn "中断しました。uv を入れてから再実行してください: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
fi

# --- 2. ffmpeg ---------------------------------------------------------------
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
  warn "ffmpeg が見つかりません(動画を画像に切り出すのに必須です)。"
  if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    printf '    Homebrew でインストールしますか? (brew install ffmpeg) [y/N] '
    read -r answer || answer=""
    if [ "${answer:-}" = "y" ] || [ "${answer:-}" = "Y" ]; then
      brew install ffmpeg
      command -v ffmpeg >/dev/null 2>&1 || {
        warn "Homebrew の処理は完了しましたが、ffmpeg が PATH に見つかりません。"
        warn "ターミナルを開き直してから再実行してください。"
        exit 1
      }
      ok "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
    else
      warn "中断しました。ffmpeg を入れてから再実行してください。"
      exit 1
    fi
  else
    warn "お使いの環境に合わせて導入してください:"
    warn "  macOS:  brew install ffmpeg"
    warn "  Ubuntu: sudo apt install ffmpeg"
    exit 1
  fi
fi

# ffprobe は取り込む動画の中身(映像が入っているか)を確かめるのに使う。ffmpeg と一緒に入る
if command -v ffprobe >/dev/null 2>&1; then
  ok "ffprobe: $(ffprobe -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
  warn "ffprobe が見つかりません(取り込む動画を確かめるのに必須です)。"
  warn "ffmpeg と一緒に入るものです。ffmpeg を入れ直してください:"
  warn "  macOS:  brew reinstall ffmpeg"
  warn "  Ubuntu: sudo apt install ffmpeg"
  exit 1
fi

# --- 3. 依存の導入 -----------------------------------------------------------
say "Python 3.11 と依存パッケージを導入します"
# 公開済みの uv.lock をセットアップ中に暗黙更新しない。pyproject.toml と
# 食い違っている場合は失敗させ、開発側で lock を更新してから配布する。
uv sync --locked

# --- 4. 読み取りに使うエージェント ------------------------------------------
found=""
for name in claude codex; do
  if command -v "$name" >/dev/null 2>&1; then
    found="$found $name"
  fi
done
if [ -n "$found" ]; then
  ok "読み取りに使えるエージェント:$found"
else
  warn "読み取りに使う Claude Code(claude)か Codex(codex)が見つかりません。"
  warn "どちらかを入れてログインしてから使ってください:"
  warn "  Claude Code: https://claude.com/claude-code"
  warn "  Codex:       https://github.com/openai/codex"
fi

say "セットアップ完了!"
cat <<'DONE'

  使い方:
    make web                   # ブラウザ画面を起動(おすすめ)
    make run VIDEO=動画.mp4    # 画面なしで、動画ファイルの取り込みからタブ譜まで通しで実行

  make が無い環境では uv run videotab serve / uv run videotab run 動画.mp4 でも同じです。

DONE
