"""紙の楽譜の写真・画像一覧・PDF を、時刻を持たない元ページとして取り込む。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps, UnidentifiedImageError

from videotab import add, frames, inside, pdfpages
from videotab.workdir import frame_name, list_frames, load_meta, save_meta

EXTS = (*frames.IMAGE_EXTS, ".pdf", ".zip")
MAX_PAGES = 200
LONG_EDGE = 2400  # PDF のページを画像にするときの長辺（px）
PDF_TIMEOUT = 120  # PDF の画像化を待つ秒数
MAX_BYTES = 256 * 1024**2
MAX_EXPANDED_BYTES = 1024**3


def check_request(name: str, length: int, **fields) -> dict:
    name = Path(str(name or "")).name
    if not name or len(name) > add.MAX_NAME or add._has_control(name) or Path(name).suffix.lower() not in EXTS:
        raise ValueError("写真（PNG・JPEG）、PDF、画像の ZIP を選んでください")
    if not 0 < length <= MAX_BYTES:
        raise ValueError(f"紙の楽譜のファイルは空でない {MAX_BYTES // 1024**2} MB 以下にしてください")
    return {"name": name, "title": add.check_text("題名", fields.get("title")),
            "creator": add.check_text("作成者", fields.get("creator")),
            "source_url": add.check_link(fields.get("source_url"))}


def _new_folder(root: Path, name: str) -> Path:
    while True:
        wd = root / add.new_id(name)
        try:
            wd.mkdir()
            return wd
        except FileExistsError:
            pass


def _image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ValueError(f"画像として読めません: {path.name}") from e


def _pdf_command(path: Path, out: Path, max_pages: int, long_edge: int) -> list[str]:
    return [sys.executable, "-m", "videotab.pdfpages", str(path), str(out), str(max_pages), str(long_edge)]


def _pdf(path: Path, stage: inside.Folder) -> list[Path]:
    # PDF は別のプロセスで画像にする。壊れた PDF で描画が落ちても固まっても、ここへは終了コードだけが返る。
    try:
        result = subprocess.run(_pdf_command(path, stage.path, MAX_PAGES, LONG_EDGE),
                                capture_output=True, stdin=subprocess.DEVNULL, timeout=PDF_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError("PDF の画像化が時間内に終わりませんでした。ページを分けて取り込んでください") from None
    if result.returncode == pdfpages.ENCRYPTED:
        raise ValueError("この PDF は暗号化されています。パスワードを外してから取り込んでください")
    if result.returncode == pdfpages.TOO_MANY:
        raise ValueError(f"PDF は {MAX_PAGES} ページまでにしてください")
    files = sorted(stage.path.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1]))
    if result.returncode != pdfpages.OK or not files:
        raise ValueError("PDF を画像化できませんでした")
    return files


def receive(stream: BinaryIO, length: int, name: str, root: Path, **fields) -> Path:
    req = check_request(name, length, **fields)
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    wd = None
    try:
        with inside.stage(root) as stage:
            source = stage.path / ("source" + Path(req["name"]).suffix.lower())
            # 大きさを制限して書き写す。受け取りが途中で終わった場合は取り込まない。
            with source.open("wb") as out:
                remaining = length
                while remaining:
                    chunk = stream.read(min(add.CHUNK, remaining))
                    if not chunk:
                        raise ValueError("ファイルの受け取りが途中で終わりました")
                    out.write(chunk)
                    remaining -= len(chunk)
            suffix = source.suffix
            if suffix == ".pdf":
                pages = _pdf(source, stage)
            elif suffix == ".zip":
                # 既存の ZIP 取り込みは名前を展開先のパスに使わず、大きさも検査する。
                try:
                    with zipfile.ZipFile(source) as archive:
                        images = [m for m in archive.infolist() if not m.is_dir()
                                  and Path(m.filename).suffix.lower() in frames.IMAGE_EXTS
                                  and not Path(m.filename).name.startswith(".") and "__MACOSX" not in m.filename]
                        if not 0 < len(images) <= MAX_PAGES:
                            raise ValueError(f"画像の ZIP は 1〜{MAX_PAGES} ページにしてください")
                        if sum(m.file_size for m in images) > MAX_EXPANDED_BYTES:
                            raise ValueError("ZIP の画像の合計が大きすぎます（展開後 1 GB まで）")
                    frames.import_folder(stage.path, source)
                except (zipfile.BadZipFile, RuntimeError, SystemExit) as e:
                    raise ValueError(f"画像の ZIP を読めません: {e}") from None
                pages = [f.path for f in list_frames(stage.path)]
            else:
                pages = [source]
            if not 0 < len(pages) <= MAX_PAGES:
                raise ValueError(f"紙の楽譜は 1〜{MAX_PAGES} ページにしてください")
            wd = _new_folder(root, req["name"])
            with inside.open_dir(wd, "frames") as folder:
                for i, page in enumerate(pages, start=1):
                    inside.write_image(folder, frame_name(i, 0), _image(page))
            save_meta(wd, {"id": wd.name, "title": req["title"] or Path(req["name"]).stem,
                           "creator": req["creator"], "source_url": req["source_url"],
                           "source_file": req["name"], "source_kind": "document", "frames_from": req["name"]})
        return wd
    except BaseException:
        if wd:
            shutil.rmtree(wd)
        raise


def import_source(source: Path, root: Path, **fields) -> Path:
    if source.is_dir():
        # フォルダは既存の読み込みを使うが、動画の擬似時刻は引き継がない。
        title = add.check_text("題名", fields.get("title")) or source.name
        creator = add.check_text("作成者", fields.get("creator"))
        url = add.check_link(fields.get("source_url"))
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        wd = _new_folder(root, source.name)
        try:
            frames.import_folder(wd, source)
            imported = list_frames(wd)
            if len(imported) > MAX_PAGES:
                raise ValueError(f"紙の楽譜は {MAX_PAGES} ページまでにしてください")
            # リネームで番号の衝突を避けるため、専用の一時フォルダを経由する。
            with inside.stage(root) as stage:
                for i, f in enumerate(imported, start=1):
                    inside.write_image(stage, frame_name(i, 0), _image(f.path))
                with inside.fresh_dir(wd, "frames") as folder:
                    for name in sorted(os.listdir(stage.fd)):
                        if name.endswith(".png"):
                            inside.move(stage, name, folder)
            meta = load_meta(wd)
            meta.update(title=title, creator=creator, source_url=url, source_file=source.name,
                        source_kind="document", frames_from=source.name)
            save_meta(wd, meta)
            return wd
        except BaseException:
            shutil.rmtree(wd)
            raise
    with source.open("rb") as stream:
        return receive(stream, os.fstat(stream.fileno()).st_size, source.name, root, **fields)
