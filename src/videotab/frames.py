"""frames: 動画を一定間隔の画像にする。既存の画像フォルダや ZIP を取り込むこともできる。"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from fnmatch import fnmatchcase
from pathlib import Path

from videotab import confine, inside
from videotab.add import INPUT_LIMITS, find_video
from videotab.workdir import frame_name, load_meta, parse_frame_name, save_meta

IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(found).exists():
        raise SystemExit("ffmpeg が見つかりません（brew install ffmpeg）")
    return found


def _forget_done(workdir: Path) -> None:
    """切り出しを最後まで終えた印（frames_from）を消す。途中で失敗したら印のないまま残る。"""
    meta = load_meta(workdir)
    if meta.pop("frames_from", None) is not None:
        save_meta(workdir, meta)


def _check_force(out: Path, force: bool) -> None:
    # frames/ がリンクなら、先の中身を見ずに作り直す（_fresh_frames がリンクだけを消す）
    if not force and inside.has_entries(out):
        raise SystemExit(f"{out} にはすでに画像があります（作り直すなら --force）")


def _fresh_frames(workdir: Path) -> inside.Folder:
    """frames/ を空にして作り直し、リンクをたどらずに開く（リンクならリンクだけを消す）。"""
    return inside.fresh_dir(confine.root_of(workdir), "frames", notify=print)


def extract_command(ffmpeg: str, video: Path, out_pattern: Path, fps: float) -> list[str]:
    """動画を画像にする ffmpeg のコマンド。入力の前に add.INPUT_LIMITS を付ける
    （作業フォルダの動画は読み手も置き換えられるので、取り込み時の確認に頼らない）。"""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        *INPUT_LIMITS,
        "-i",
        str(video),
        "-vf",
        f"fps={fps}",
        "-start_number",
        "1",
        str(out_pattern),
    ]


def extract(workdir: Path, fps: float = 1.0, force: bool = False) -> int:
    video = find_video(workdir)
    if video is None:
        raise SystemExit(f"{workdir} に動画がありません（先に videotab add で動画を取り込んでください）")
    # 動画が作業フォルダの外を指すリンクなら断る（外のファイルを ffmpeg に読ませない）
    root = confine.root_of(workdir)
    video = confine.guard(root / inside.rel_path(root, video), root=root)
    _check_force(workdir / "frames", force)
    _forget_done(workdir)
    ffmpeg = ffmpeg_bin()
    # ffmpeg は、読み手が書けない置き場（作業フォルダの親）の一時フォルダに書き出す。書き終えてから
    # frames/ を作り直し、付け替えで取り込む（書いている間に作業フォルダの中を差し替えられても外に書かない）
    with inside.stage(root.parent) as stage:
        subprocess.run(extract_command(ffmpeg, video, stage.path / "raw_%05d.png", fps), check=True)
        raw = sorted(name for name in os.listdir(stage.fd) if fnmatchcase(name, "raw_*.png"))
        with _fresh_frames(workdir) as out:
            for i, name in enumerate(raw, start=1):
                inside.move(stage, name, out, frame_name(i, (i - 1) / fps), notify=print)
    meta = load_meta(workdir)
    meta.update({"fps": fps, "frames_from": str(video.name)})
    save_meta(workdir, meta)
    return len(raw)


def import_folder(workdir: Path, src: Path, fps: float = 1.0, force: bool = False) -> int:
    """既存の画像フォルダか ZIP を frames/ に写す。

    ファイル名が NNNN_MMmSSsmmm.png（komadori の書き出しと同じ規則）ならその時刻を使い、
    違う名前なら並び順に fps 間隔の時刻を振る。
    """
    if not src.exists():
        raise SystemExit(f"{src} がありません")
    if src.is_file() and src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as zf:
            members = sorted(
                (m for m in zf.infolist() if not m.is_dir() and Path(m.filename).suffix.lower() in IMAGE_EXTS
                 and not Path(m.filename).name.startswith(".") and "__MACOSX" not in m.filename),
                key=lambda m: Path(m.filename).name,
            )  # fmt: skip
            sources = [(Path(m.filename).name, (lambda m=m: zf.read(m))) for m in members]
            return _import(workdir, src, sources, fps, force)
    files = sorted(p for p in src.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    return _import(workdir, src, [(p.name, p.read_bytes) for p in files], fps, force)


def _import(workdir: Path, src: Path, sources, fps: float, force: bool) -> int:
    if not sources:
        raise SystemExit(f"{src} に画像がありません")
    _check_force(workdir / "frames", force)
    _forget_done(workdir)
    named = all(parse_frame_name(name) for name, _ in sources)
    with _fresh_frames(workdir) as out:
        for i, (name, read) in enumerate(sources, start=1):
            if named:
                index, t = parse_frame_name(name)
            else:
                index, t = i, (i - 1) / fps
            ext = Path(name).suffix.lower().lstrip(".").replace("jpeg", "jpg")
            inside.write_bytes(out, frame_name(index, t, ext), read(), notify=print)
    meta = load_meta(workdir)
    meta.setdefault("id", workdir.name)
    meta.setdefault("title", src.stem.removesuffix("_frames") if src.is_file() else src.name.removesuffix("_frames"))
    meta.update({"frames_from": str(src)})
    save_meta(workdir, meta)
    return len(sources)
