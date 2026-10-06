# コマンド

段ごとにコマンドを 1 つずつ動かす方法と、コマンドの一覧、作業フォルダの中身の説明です。[README に戻る](../README.md)

## コマンドを 1 つずつ使う

```sh
uv run videotab add 動画.mp4                  # 新しい作業フォルダ work/XXXX/ に取り込み、ID（XXXX）を表示
uv run videotab frames XXXX                   # 1 秒 1 枚の画像に
uv run videotab strip XXXX                    # タブの帯と弦の線の位置を検出
uv run videotab pages XXXX                    # ページに分けて拡大画像を作る
# ここで AI が拡大画像を読み、work/XXXX/parts/*.json に小節ごとの alphaTex を書く
uv run videotab check XXXX                    # 拍数・タイ・記法の検査
uv run videotab build XXXX                    # つないで work/XXXX/XXXX.html と .alphatex を出力
uv run videotab verify XXXX --mark 10=57      # 繰り返し・テンポを展開した時刻を動画と比べる
```

- `XXXX` は `add` が表示する ID です。`add` は続けるコマンドを `videotab frames <作業フォルダのパス>` の形で表示します。ID の代わりに作業フォルダのパスを渡すと、`--root` で置き場を変えたときも同じ作業フォルダを指せます。
- `-` で始まる名前のファイルは、`uv run videotab add ./-x.mp4` のようにパスで渡します。

画像の読み方の決まりは [AGENTS.md](../AGENTS.md) にあります。

## コマンド一覧

| コマンド | 内容 |
|---|---|
| `serve [--port N] [--root DIR]` | 動画ファイルを送るとタブ譜まで作る画面を開く |
| `run FILE\|ID [--title T] [--creator C] [--source-url U] [--engine claude\|codex] [--model M] [--effort E] [--step 段] [--root DIR]` | 取り込みからタブ譜の出力までを通しで実行（画面なし）。動画ファイルなら新しく取り込み、既存の作業フォルダの ID ならその続きから進める。`--title` などは新しく取り込むときだけ、`--step` は既存の ID のときだけ使える。`--model` / `--effort` は読み取りのモデルと推論の強さ（[読み取りのモデルと推論の強さ](model-settings.md)） |
| `add FILE [--title T] [--creator C] [--source-url U] [--root DIR]` | 動画ファイル（.mp4 .m4v .mov .webm .mkv .avi、4 GB まで）を ffprobe で確かめ、新しい作業フォルダに `video.<拡張子>` として取り込む。題名（既定はファイル名）・作成者・元動画のページ（https:// で始まるもの）・元のファイル名・幅と高さ・長さを `meta.json` に記録 |
| `frames ID [--from DIR\|ZIP] [--fps N]` | ffmpeg で一定間隔の画像に。既存の画像フォルダや [komadori](https://komadori.orukubami.sh) の書き出し ZIP も取り込める |
| `strip ID [--band Y0 Y1]` | タブの帯の位置と 1〜6 弦の線の y 座標を検出し、確認用の画像を出す。ページによって段の上下の位置が動く動画では、全フレームの線の位置を記録し、帯は線が動く範囲の全体を含む（確認用の画像は、段がいちばん上と下にあるフレームを含む）。線が見つからなければ終了コード 3 で止まる（通しの実行ではこのとき動画と画像を消す。`--band` で決めた帯は、通しの実行でやり直しても使われる） |
| `pages ID [--threshold T]` | 帯の中身の切り替わり（ページ送り・横スクロール）でページに分け、弦の番号付きの拡大画像と一覧（`index.md`）を作る。段の位置が動く動画では、拡大画像の印と一覧の線の位置はページごとの位置で、位置が変わる所でもページを区切る |
| `zoom ID FRAME` | 1 フレームをページと同じ倍率で拡大（演奏位置の枠に隠れた数字の確認など）。印はそのフレームの線の位置に付く |
| `check ID\|FILE` | 小節ごとの長さ、つながり先のないタイ、記法のくせを検査 |
| `build ID` | 読み取り結果をつなぐ。重ねて読んだ小節の食い違い・抜け・検査の誤りがあれば一覧にして止める |
| `verify ID [--mark 小節=秒]` | 繰り返し・テンポ変化を展開した各小節の開始時刻を出し、動画で見た時刻と比べる |
| `--version` | videotab の版を表示（画面では見出しの横に出る） |

`pages` の自動判定は、帯全体の差に加えて、数字の周りの小さな変化も調べます。演奏位置の枠のような長い直線は局所比較から除き、同じ配置でフレットの数字だけが変わるページを拾います。`--threshold T` を指定した場合は、この局所比較を使わず、従来どおり帯全体の差と横スクロールで区切ります。

## 作業フォルダ

`work/<ID>/` にまとまります。ID は、動画のファイル名の英数字と 6 桁の 16 進の乱数からなる名前です（例 `practice-1a2b3c`。ファイル名に英数字が無ければ `video-1a2b3c`）。`work/` は git の管理外です。

| パス | 中身 |
|---|---|
| `video.<拡張子>` | 取り込んだ動画 |
| `meta.json` | 動画の題名・作成者・元動画のページ・元のファイル名・幅と高さ・長さ、検出した帯と線の基準の位置（段の位置が動く動画では、フレームの区間ごとの基準からのずれも） |
| `frames/` | `NNNN_MMmSSsmmm.png`（フレーム番号・時刻） |
| `strip/` | 帯と線の確認用の画像 |
| `pages/` | ページごとの拡大画像、`pages.json`、`index.md` |
| `parts/*.json` | 読み取り結果 `{"小節番号": "alphaTex"}`（分けて読んだら、まとまりごとに 1 ファイル） |
| `score.json` | 曲名・副題・動画の作成者・テンポ・拍子・チューニング・カポ |
| `resolve.json` | 食い違いを解いた小節 |
| `marks.json` | 動画で見た小節の時刻 `[[小節, 秒], ...]` |
| `readers/` | 読み取りの AI ごとの報告と「ページ → 最初の小節」 |
| `job.json` / `job.log` | 通しの実行の段ごとの状態とログ |
| `<ID>.html` / `<ID>.alphatex` | できあがり |

画面の「この曲を消す」は、`work/<ID>/` をフォルダごと消します（動画・画像・タブ譜・読み取りの結果、前回までの読み取り結果の `history/` も含みます）。元に戻せません。実行中・順番待ちの曲と、`videotab run` が動いている曲は消せません。
