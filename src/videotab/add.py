"""add: 手元の動画ファイルを、新しい作業フォルダ（work/<ID>/video.<拡張子>）に取り込む。

取り込みの手順:
  1. 置き場（作業フォルダの親）に、本体専用の一時フォルダ（inside.stage）を作る
  2. 一時フォルダの video.part へ、ちょうど渡された長さだけ書き写す
  3. ffprobe で映像が入っていることを確かめる（幅・高さは回転を適用したあとの向き）
  4. 新しい ID で作業フォルダを作る（名前がぶつかったら ID を作り直す）
  5. 付け替えで video.<拡張子> として取り込み、meta.json を書く
4 以降で失敗したら、作った作業フォルダを消す。一時フォルダは成功でも失敗でも残さない。
画面のアップロードも、受け取った本文を同じ手順で取り込む（receive）。

ffprobe / ffmpeg には、入力の前に INPUT_LIMITS を付ける。中身が再生リストなどの「動画」でも、
ネットにも、指定した動画ファイル以外のローカルファイルにも触れずに断る。

題名・作成者・元動画のページは、利用者が入れたものだけを meta.json に残す。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import BinaryIO

from videotab import inside
from videotab.workdir import WORK_ROOT, save_meta

VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi")
MAX_BYTES = 4 * 1024**3  # 取り込める動画の大きさの上限（4 GB）
MAX_TEXT = 200  # 題名・作成者の長さの上限（文字）
MAX_URL = 2000  # 元動画のページの長さの上限（文字）
MAX_NAME = 255  # ファイル名の長さの上限（文字）
CHUNK = 1024 * 1024
STAGED = "video.part"  # 一時フォルダの中で書き写している途中の名前
ID_STEM = 40  # ID のうち、ファイル名から作る部分の長さの上限

# 受け付ける入れ物の demuxer の名前（ffmpeg -demuxers の mov/mp4 系・matroska/webm・avi）
DEMUXERS = ("mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi")
# ffprobe / ffmpeg の入力の前に付ける制限。読むのはローカルのファイルだけ（http などのプロトコルを
# 使わない）、入れ物は上の demuxer だけ（concat・hls などの再生リストとして解釈しない）。
# どちらも入力の解析より前に効く
INPUT_LIMITS = ("-protocol_whitelist", "file", "-format_whitelist", ",".join(DEMUXERS))


class NotVideo(ValueError):
    """取り込めない動画（拡張子・大きさ・入力の項目・中身が合わない）。文言は利用者に見せる。"""


def video_suffix(name: str) -> str:
    """動画ファイルの拡張子（小文字）。動画の拡張子でなければ NotVideo。"""
    suffix = Path(name).suffix.lower()
    if suffix not in VIDEO_EXTS:
        raise NotVideo(f"動画ファイル（{' '.join(VIDEO_EXTS)}）を選んでください")
    return suffix


def check_size(length: int) -> None:
    if length <= 0:
        raise NotVideo("動画が空です")
    if length > MAX_BYTES:
        raise NotVideo(f"動画が大きすぎます（{MAX_BYTES // 1024**3} GB まで）")


def _has_control(text: str) -> bool:
    return any(unicodedata.category(c) == "Cc" for c in text)


def check_text(label: str, value: str | None) -> str | None:
    """題名・作成者。前後の空白を除き、空なら None。制御文字を含む・長すぎるなら NotVideo。"""
    value = (value or "").strip()
    if not value:
        return None
    if _has_control(value):
        raise NotVideo(f"{label}に改行などの制御文字は使えません")
    if len(value) > MAX_TEXT:
        raise NotVideo(f"{label}は {MAX_TEXT} 文字までにしてください")
    return value


def check_link(url: str | None) -> str | None:
    """元動画のページ。空なら None。https:// で始まり空白を含まない、長すぎないリンクでなければ NotVideo。"""
    url = (url or "").strip()
    if not url:
        return None
    if not url.startswith("https://") or any(c.isspace() for c in url) or _has_control(url):
        raise NotVideo("元動画のページは https:// で始まる URL にしてください")
    if len(url) > MAX_URL:
        raise NotVideo(f"元動画のページは {MAX_URL} 文字までにしてください")
    return url


def check_name(name: str) -> str:
    """元のファイル名（パスは除く）。動画の拡張子でない・名前として使えなければ NotVideo。"""
    name = Path(str(name or "")).name
    if not name or _has_control(name) or len(name) > MAX_NAME:
        raise NotVideo("ファイル名が読めません")
    video_suffix(name)
    return name


def check_request(
    name: str, length: int, *, title: str | None, creator: str | None, source_url: str | None
) -> dict:
    """中身を読む前に確かめられることを、すべて確かめる。整えた値の表を返す（合わなければ NotVideo）。"""
    name = check_name(name)
    check_size(length)
    return {
        "name": name,
        "title": check_text("題名", title),
        "creator": check_text("作成者", creator),
        "source_url": check_link(source_url),
    }


def new_id(name: str) -> str:
    """ファイル名から作る作業フォルダの ID。必ず英数字で始まり、workdir.ID_PATTERN に合う。

    名前（拡張子を除く）の英数字・_・- 以外を - にし、先頭と末尾の - と _ を除いて 40 文字までにする。
    空か英数字で始まらなければ video を使う。そのあとに - と 6 桁の 16 進の乱数を付ける。
    """
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-_")[:ID_STEM]
    if not re.match(r"[A-Za-z0-9]", stem):
        stem = "video"
    return f"{stem}-{secrets.token_hex(3)}"


def find_video(workdir: Path) -> Path | None:
    """作業フォルダに取り込んだ動画（video.* のうち、拡張子が VIDEO_EXTS のもの）。"""
    for p in sorted(workdir.glob("video.*")):
        if p.suffix.lower() in VIDEO_EXTS:
            return p
    return None


def ffprobe_bin() -> str:
    found = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    if not Path(found).exists():
        raise SystemExit("ffprobe が見つかりません（ffmpeg を入れると一緒に入ります）")
    return found


def probe_command(path: Path) -> list[str]:
    return [
        ffprobe_bin(),
        "-v",
        "error",
        *INPUT_LIMITS,
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:stream_side_data=rotation:stream_tags=rotate:format=duration,format_name",
        "-of",
        "json",
        str(path),
    ]


def _rotation(stream: dict) -> int:
    """映像の回転（度）。新しい ffprobe は side_data_list の rotation、古い版は tags の rotate。
    どちらも無い・読めないなら 0。向きの符号は版で逆だが、幅と高さの入れ替えの判定には影響しない。"""
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(round(float(side["rotation"])))
            except (TypeError, ValueError, OverflowError):
                break
    try:
        return int(round(float((stream.get("tags") or {}).get("rotate"))))
    except (TypeError, ValueError, OverflowError):
        return 0


def probe(path: Path) -> dict:
    """動画の映像の幅・高さと長さ（秒）。幅・高さは回転を適用したあとの向き。
    映像が無い・受け付ける入れ物として読めなければ NotVideo。"""
    proc = subprocess.run(probe_command(path), capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    # 入力の前の制限で足りるが、読めた入れ物の名前も確かめる
    names = set(str(fmt.get("format_name") or "").split(","))
    if proc.returncode != 0 or not streams or not names & set(DEMUXERS):
        raise NotVideo("映像の入った動画として読めません")
    try:
        duration = round(float(fmt.get("duration")), 3)
    except (TypeError, ValueError):
        duration = None
    width, height = streams[0].get("width"), streams[0].get("height")
    if _rotation(streams[0]) % 180 == 90:  # 90 の奇数倍（-90、90、270 など）なら縦横を入れ替える
        width, height = height, width
    return {"width": width, "height": height, "duration": duration}


def add(
    src: Path,
    root: Path = WORK_ROOT,
    *,
    title: str | None = None,
    creator: str | None = None,
    source_url: str | None = None,
) -> Path:
    """動画ファイル src を新しい作業フォルダに取り込み、その作業フォルダを返す。"""
    src = Path(src)
    check_name(src.name)
    if not src.is_file():
        raise NotVideo(f"{src} がありません")
    with src.open("rb") as f:
        return receive(f, os.fstat(f.fileno()).st_size, src.name, root,
                       title=title, creator=creator, source_url=source_url)  # fmt: skip


def receive(
    stream: BinaryIO,
    length: int,
    name: str,
    root: Path = WORK_ROOT,
    *,
    title: str | None = None,
    creator: str | None = None,
    source_url: str | None = None,
) -> Path:
    """stream から length バイトの動画（元のファイル名 name）を読んで新しい作業フォルダに取り込み、
    その作業フォルダを返す。"""
    req = check_request(name, length, title=title, creator=creator, source_url=source_url)
    name = req["name"]
    suffix = video_suffix(name)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    place = root.resolve()
    with inside.stage(place) as stage:
        _copy(stream, length, stage)
        info = probe(stage.path / STAGED)
        workdir = _make_workdir(place, name)
        try:
            with inside.open_dir(workdir) as top:
                inside.move(stage, STAGED, top, "video" + suffix, notify=print)
            meta = {
                "id": workdir.name,
                "title": req["title"] or Path(name).stem,
                "creator": req["creator"],
                "source_url": req["source_url"],
                "source_file": name,
                **info,
            }
            save_meta(workdir, meta)
        except BaseException:
            _remove_workdir(place, workdir.name)
            raise
    return workdir


def _copy(stream: BinaryIO, length: int, stage: inside.Folder) -> None:
    """stream の length バイトを一時フォルダの STAGED に書き写す。足りなければ NotVideo。"""
    fd = os.open(STAGED, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage.fd)
    with os.fdopen(fd, "wb") as out:
        left = length
        while left > 0:
            chunk = stream.read(min(CHUNK, left))
            if not chunk:
                raise NotVideo("動画を最後まで受け取れませんでした")
            out.write(chunk)
            left -= len(chunk)


def _make_workdir(place: Path, name: str) -> Path:
    """置き場の直下に、新しい ID の作業フォルダを作る（名前がぶつかったら ID を作り直す）。"""
    for _ in range(100):
        workdir = place / new_id(name)
        try:
            os.mkdir(workdir)
        except FileExistsError:
            continue
        return workdir
    raise FileExistsError("作業フォルダの名前を決められません")


def _remove_workdir(place: Path, job_id: str) -> None:
    """取り込みの途中で失敗したとき、作った作業フォルダを消す（リンクはたどらない）。"""
    try:
        with inside.open_dir(place) as top:
            inside.remove(top, job_id, ignore_errors=True)
    except (OSError, SystemExit) as e:
        print(f"videotab: 作業フォルダ {job_id} を消せませんでした（{inside.reason(e)}）", flush=True)
