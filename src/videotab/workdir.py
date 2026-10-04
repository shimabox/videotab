"""作業フォルダ（work/<ID>/）の場所と、その中の meta.json・フレームの扱い。

作業フォルダの中身:
  meta.json     動画の情報と、strip が決めた帯・弦の線の位置
  video.*       add で取り込んだ動画
  frames/       NNNN_MMmSSsmmm.png（フレーム番号・時刻）
  strip/        帯と線の確認用の画像
  pages/        ページごとの拡大画像と pages.json・index.md
  parts/        読み取り結果 part_*.json（{"小節番号": "alphaTex"}）
  score.json    曲名・テンポ・チューニングなどの見出し
  resolve.json  食い違いを解いた小節（{"小節番号": "alphaTex"}）
  marks.json    動画で見た小節の時刻（[[小節番号, 秒], ...]）

読み手は作業フォルダの中にリンクを作れるので、本体が作業フォルダに書くときは inside の部品を通し、
リンクをたどって作業フォルダの外を変えない（write_json・create_json・save_meta もそれを使う）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from videotab import confine, inside

WORK_ROOT = Path("work")
# 作業フォルダの ID（work/ の直下の名前）。英数字で始まるので、コマンドの引数でオプションと紛れない。
# 置き場の一時フォルダや消している途中のフォルダ（. で始まる）は合わないので、一覧に出ない
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_JSON_BYTES = 32 * 1024 * 1024  # 読む JSON の大きさの上限（32 MB）。作業フォルダの JSON は読み手も書ける
FRAME_NAME = re.compile(r"^(\d+)_(\d+)m(\d+)s(\d{3})\.(png|jpg)$")


def resolve_target(target: str) -> Path:
    """ID（work/ 以下の名前）かパスのどちらでも作業フォルダを指せるようにする。"""
    p = Path(target)
    if p.exists() or "/" in target or target.startswith("."):
        return p
    return WORK_ROOT / target


# 読むときの confine.guard は、読み取りのエージェントから実行されたとき（VIDEOTAB_CONFINE あり）
# だけ、作業フォルダの外を指すパス（リンク）を断る。無いときは何もしない。
# 書くときは閉じ込めの有無によらず inside の部品を通し、リンクをたどって作業フォルダの外に書かない
# （途中のフォルダが外へのリンクなら断り、書く名前がリンクなどなら通常のファイルに置き換える）。
# notify は置き換えを知らせる先（コマンドは標準出力、通しの実行の中では job.log）。


def load_meta(workdir: Path) -> dict:
    path = confine.guard(workdir / "meta.json")
    if not path.exists():
        return {}
    return _load_json(path)


def _load_json(path: Path):
    """JSON のファイルを読む。MAX_JSON_BYTES を超えるものは、読み込まずに ValueError（壊れた JSON と同じ扱い）。"""
    with open(path, "rb") as f:
        raw = f.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(f"{path.name} が大きすぎます（{MAX_JSON_BYTES // 1024**2} MB まで）")
    return json.loads(raw.decode("utf-8"))


def save_meta(workdir: Path, meta: dict, *, notify=print) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    write_json(workdir / "meta.json", meta, root=confine.root_of(workdir), notify=notify)


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def write_json(path: Path, data, *, root: Path, notify=print) -> None:
    """作業フォルダ root（resolve 済み）の中の path に JSON を書く（途中のフォルダは無ければ作る）。"""
    rel = inside.rel_path(root, path)
    with inside.open_dir(root, rel.parent) as folder:
        inside.write_text(folder, rel.name, json_text(data), notify=notify)


def create_json(path: Path, data, *, root: Path) -> None:
    """path が無いときだけ JSON を新しく作る（利用者・読み手のデータの雛形）。

    中を指すリンクはたどって、その先に作る。外を指すリンクなら、先が無くても断る（Outside）。
    """
    target = confine.guard(root / inside.rel_path(root, path), root=root)
    rel = target.relative_to(root)
    with inside.open_dir(root, rel.parent) as folder:
        inside.create_text(folder, rel.name, json_text(data))


def read_json(path: Path):
    return _load_json(confine.guard(path))


@dataclass(frozen=True)
class Frame:
    index: int  # フレーム番号（1 始まり、ファイル名の NNNN）
    time: float  # 動画の中の秒
    path: Path


def frame_name(index: int, time: float, ext: str = "png") -> str:
    ms = round(time * 1000)
    m, rest = divmod(ms, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{index:04d}_{m:02d}m{s:02d}s{milli:03d}.{ext}"


def parse_frame_name(name: str) -> tuple[int, float] | None:
    m = FRAME_NAME.match(name)
    if not m:
        return None
    index, mm, ss, milli = (int(g) for g in m.groups()[:4])
    return index, mm * 60 + ss + milli / 1000


def list_frames(workdir: Path) -> list[Frame]:
    frames = []
    folder = confine.guard(workdir / "frames")
    for p in sorted(folder.iterdir()) if folder.exists() else []:
        parsed = parse_frame_name(p.name)
        if parsed:
            frames.append(Frame(parsed[0], parsed[1], p))
    return frames


def fmt_time(t: float) -> str:
    sign = "-" if t < 0 else ""
    m, s = divmod(abs(t), 60)
    return f"{sign}{int(m)}:{s:04.1f}"
