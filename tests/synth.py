"""テスト用の合成フレーム。動画の上にタブ譜の帯が載った画面を作る。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from videotab.workdir import frame_name

W, H = 640, 360
BAND = (240, 350)
TAB_LINES = [276, 283, 290, 297, 304, 311]  # 間隔 7px
STAFF_LINES = [246, 250, 254, 258, 262]  # 五線（間隔 4px）


def video_noise(rng: np.random.Generator) -> np.ndarray:
    """帯の外の「演奏している映像」: なめらかでない色のむら。"""
    base = rng.integers(20, 200, size=(H // 8, W // 8, 3)).astype(np.uint8)
    return np.asarray(Image.fromarray(base).resize((W, H), Image.BILINEAR)).astype(np.float32)


def glyphs(seed: int, width: int = W) -> list[tuple[int, int]]:
    """ページの中身の代わりに置く「数字」の位置（x, 線の番号）。"""
    rng = np.random.default_rng(seed)
    xs = np.sort(rng.choice(np.arange(20, width - 20, 6), size=110, replace=False))
    return [(int(x), int(rng.integers(0, 6))) for x in xs]


def frame(
    page_seed: int,
    *,
    dark: bool = False,
    line_contrast: float = 14.0,
    cursor_x: int | None = None,
    shift: int = 0,
    rng_seed: int = 0,
    dy: int = 0,
    staff_dy: int = 0,
    second: int | None = None,
    staff: bool = True,
    tab_lines: list[float] | None = None,
    extra_lines: tuple[float, ...] = (),
) -> np.ndarray:
    """dy でタブの線と数字を、staff_dy で五線と音符の棒を上下にずらす（ページごとに段が動く画面）。
    second を渡すと、タブの線から second だけ上下にずれた所に 2 段目のタブを置く（地の色の範囲も広げる）。
    staff=False なら五線と音符の棒を描かない（タブだけの画面）。
    tab_lines を渡すと TAB_LINES の代わりにその位置へタブの線を引く（端数の位置は上下 2 行に濃さを分ける）。
    extra_lines の位置（dy でずれる）には、タブの線と同じ濃さの線を足す。
    """
    rng = np.random.default_rng(rng_seed)
    img = video_noise(rng)
    bg = 60.0 if dark else 252.0
    ink = 235.0 if dark else 20.0
    line = bg + line_contrast if dark else bg - line_contrast
    y0, y1 = BAND
    lo, hi = min(0, dy, staff_dy), max(0, dy, staff_dy)
    if second is not None:
        lo, hi = min(lo, second + dy), max(hi, second + dy)
    img[max(0, y0 + lo) : min(H, y1 + hi)] = bg
    for y in STAFF_LINES if staff else []:
        img[y + staff_dy, 10:W - 10] = line
    tabs = [[y + dy for y in tab_lines or TAB_LINES]]
    if second is not None:
        tabs.append([y + second + dy for y in tab_lines or TAB_LINES])
    for y in [y for ys in tabs for y in ys] + [y + dy for y in extra_lines]:
        top, f = int(np.floor(y)), y - np.floor(y)
        img[top, 10:W - 10] = bg + (line - bg) * (1 - f)
        if f:
            img[top + 1, 10:W - 10] = bg + (line - bg) * f
    # 数字（線の上に 4x5 の塊）。shift で横にずらす（横スクロール）
    for x, s in glyphs(page_seed, W + 400):
        x = x - shift
        if 12 <= x < W - 16:
            for ys in tabs:
                y = round(ys[s])
                img[y - 3 : y + 3, x : x + 5] = ink
            if staff:
                img[248 + staff_dy : 262 + staff_dy, x + 1 : x + 2] = ink  # 五線の上の音符の棒
    if cursor_x is not None:  # 演奏位置の枠（細い線の四角）
        img[y0 + 5, cursor_x : cursor_x + 60] = ink
        img[y1 - 5, cursor_x : cursor_x + 60] = ink
        img[y0 + 5 : y1 - 5, cursor_x] = ink
        img[y0 + 5 : y1 - 5, cursor_x + 60] = ink
    img += rng.normal(0, 1.5, img.shape)  # 圧縮ノイズの代わり
    return np.clip(img, 0, 255).astype(np.uint8)


def write_frames(workdir: Path, frames: list[np.ndarray], step: float = 1.0) -> None:
    out = workdir / "frames"
    out.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(frames, start=1):
        Image.fromarray(a).save(out / frame_name(i, (i - 1) * step))
