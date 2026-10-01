# videotab — よく使う操作の入り口。詳しくは README.md を参照。

.PHONY: setup web run test

# 前提チェック(uv / ffmpeg / ffprobe)+依存の導入。何度実行しても安全
setup:
	./setup.sh

# ブラウザ画面を起動(http://127.0.0.1:8765/ が自動で開く。停止は Ctrl+C)
web:
	uv run videotab serve

# 画面なしで、動画ファイルの取り込みからタブ譜まで通しで実行: make run VIDEO=動画.mp4
run:
ifndef VIDEO
	$(error VIDEO に動画ファイルのパスを指定してください: make run VIDEO=動画.mp4)
endif
	uv run videotab run "$(VIDEO)"

# 開発用: 全テスト実行
test:
	uv run pytest -q
