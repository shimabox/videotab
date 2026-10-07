"""カメラ撮影の薄い斜線を探し、フレームごとの傾きと上下の揺れを補正する。"""

from __future__ import annotations

from collections import Counter

import numpy as np
from PIL import Image

from videotab.strip import (
    Staff, StripResult, _background, _grow_band, _largest_group,
    find_peaks, find_staves, line_offset, load_rgb, to_gray,
)

MAX_SLOPE = 0.035  # 横 1000px に対して上下 35px まで（約 2 度）
LINE_FLOOR = 1.5


def straighten(rgb: np.ndarray, slope: float, dy: float = 0, background=0, scale: float = 1) -> np.ndarray:
    """元画像の y = scale * 基準 y + dy + slope * (x - 中心 x) を水平な基準に戻す。"""
    if not slope and not dy and scale == 1:
        return rgb
    gray = rgb.ndim == 2
    im = Image.fromarray(rgb.astype(np.float32) if gray else rgb.astype(np.uint8))
    fill = float(background) if gray else tuple(background) if np.ndim(background) else (int(background),) * 3
    out = im.transform(
        im.size, Image.Transform.AFFINE,
        (1, 0, 0, slope, scale, dy - slope * (im.width - 1) / 2),
        Image.Resampling.BILINEAR, fillcolor=fill,
    )
    return np.asarray(out).astype(np.float32)


def strength(gray: np.ndarray, polarity: int) -> np.ndarray:
    """上下それぞれの地との局所コントラスト。緩やかな明るさのむらの影響を抑える。"""
    k = line_offset(gray.shape[0])
    response = np.zeros_like(gray)
    mid = polarity * gray[k:-k]
    response[k:-k] = np.minimum(mid - polarity * gray[:-2*k], mid - polarity * gray[2*k:])
    # 画面の縁と補正でできた余白を除いて、横幅の過半数で続く線だけを見る。
    margin = max(1, gray.shape[1] // 10)
    return np.median(response[:, margin:-margin], axis=1)


def find_frame(rgb: np.ndarray, band=None, polarity=None) -> tuple[int, list[Staff], float] | None:
    gray = to_gray(rgb)
    h, w = gray.shape
    # 横だけ間引く。縦の薄い 1px 線を潰さず、傾きの探索にかかる時間を抑える。
    columns = np.linspace(0, w - 1, min(w, 240)).round().astype(int)
    small = gray[:, columns]
    candidates = []
    # 全幅での高低差を 1px 刻みで探す。水平を優先し、同点で不必要に傾けない。
    slopes = sorted(np.linspace(-MAX_SLOPE, MAX_SLOPE, 2 * int(np.ceil(w * MAX_SLOPE)) + 1), key=abs)
    for slope in slopes:
        corrected = straighten(small, slope * (w - 1) / max(1, len(columns) - 1), background=float(np.median(small)))
        for pol in (polarity,) if polarity else (-1, 1):
            profile = strength(corrected, pol)
            if band:
                profile[:band[0]] = 0
                profile[band[1]:] = 0
            staves = find_staves(find_peaks(profile, LINE_FLOOR), h)
            score = sum(st.score for st in staves)
            if staves:
                candidates.append((score, pol, staves, float(slope)))
    if not candidates:
        return None
    # 遠近の歪みで五線とタブの傾きが少し異なることがある。傾きごとではなく、探索全体で
    # find_staves と同じ間隔の比較を行い、五線と連桁からできた狭い 6 本の組を除く。
    widest = max(np.mean([st.spacing for st in item[2]]) for item in candidates)
    candidates = [item for item in candidates if np.mean([st.spacing for st in item[2]]) >= widest * 0.85]
    best = max(candidates, key=lambda item: item[0])
    return best[1], best[2], best[3]


def detect(frames, picks, rgbs, band=None) -> StripResult | None:
    sampled = [find_frame(rgb, band) for rgb in rgbs]
    singles = [item[1] if item else [] for item in sampled]
    group = _largest_group(singles)
    if group:
        counts = Counter((sampled[i][0], len(singles[i])) for i in group)
        kind = max(counts, key=counts.get)
        group = [i for i in group if (sampled[i][0], len(singles[i])) == kind]
    # 少なくとも 2 枚で同じ段を観測する。映像中の偶然の直線を採用しない。
    if len(group) < min(2, len(frames)):
        return None
    ref = max(group, key=lambda i: sum(st.score for st in singles[i]))
    polarity, base, _ = sampled[ref]
    known = {f.index: item for f, item in zip(picks, sampled)}
    observations = []
    for f in frames:
        item = known.get(f.index) if f.index in known else find_frame(load_rgb(f.path), band, polarity)
        fit = geometry(item[1], base) if item and item[0] == polarity else None
        observations.append((fit[0], item[2], fit[1]) if fit else (None, None, None))
    found = [i for i, (dy, _, _) in enumerate(observations) if dy is not None]
    if not found:
        return None
    # 見えないフレームにも直前の補正を持たせるが、タブが見えたという判定には使わない。
    last = observations[found[0]]
    corrections = []
    for f, (dy, slope, scale) in zip(frames, observations):
        visible = dy is not None
        if visible:
            last = (dy, slope, scale)
        corrections.append((f.index, round(last[0], 2), last[1], visible, last[2]))
    by_index = {row[0]: row for row in corrections}
    aligned = [
        straighten(rgb, by_index[f.index][2], by_index[f.index][1], (128,)*3, by_index[f.index][4])
        for f, rgb in zip(picks, rgbs) if by_index[f.index][3]
    ]
    background = _background(aligned, base)
    if band is None:
        y0, y1 = _grow_band(aligned, base, background)
        band = (max(0, y0 - 2), min(rgbs[0].shape[0], y1 + 2))
    return StripResult(
        polarity, base, band, background, len(picks),
        sum(by_index[f.index][3] for f in picks),
        located={frames[i].index for i in found}, corrections=corrections,
    )


def geometry(staves: list[Staff], base: list[Staff]) -> tuple[float, float] | None:
    """距離の変化による線の間隔の伸縮を含め、全段に共通の y = scale * 基準 y + dy を当てる。"""
    if len(staves) != len(base):
        return None
    scale = float(np.median([st.spacing / ref.spacing for st, ref in zip(staves, base)]))
    if not 0.75 <= scale <= 1.25:
        return None
    dy = float(np.median(np.concatenate([np.array(st.lines) - scale * np.array(ref.lines)
                                        for st, ref in zip(staves, base)])))
    for st, ref in zip(staves, base):
        if np.max(np.abs(np.array(st.lines) - (scale * np.array(ref.lines) + dy))) > max(1, st.spacing * 0.15):
            return None
    return dy, scale
