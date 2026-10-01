"""strip: タブ譜の帯の位置と、弦の線の y 座標を自動で見つける。

考え方:
- タブ譜は「ほぼ横幅いっぱいの細い横線が 6 本、等間隔に並んだもの」。行ごとに、
  上下の画素より濃い（または明るい）画素が横に何割あるかを数え、その山を線とみなす。
  等間隔の山 6 本の組をタブ 1 段とする（五線は 5 本なので混ざらない）。
- 帯は、タブの線の間の地の色が上下に続く範囲。五線や小節番号、タブ下の連桁もこの中に入る。
- 演奏中の色枠や字幕に引っ張られないよう、動画全体から間引いた何枚かの中央値で判断する。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from videotab.workdir import Frame


def load_rgb(path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB")).astype(np.float32)


def to_gray(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def line_offset(height: int) -> int:
    """線の上下を比べる距離。360p で 2px、1080p で 6px。"""
    return max(2, round(height / 180))


def line_strength(gray: np.ndarray, polarity: int, q: float = 0.5) -> np.ndarray:
    """行ごとに、上下の画素と比べて polarity 方向にどれだけ濃い（明るい）かの横方向の中央値を返す。

    polarity=-1 は白地に濃い線、+1 は暗い地に明るい線。上と下の両方より濃い（明るい）
    ときだけ値が出るので、線の縁の少し外側に偽の線が出ない。中央値なので、数字や色枠が
    行の半分未満を占めるだけなら線の強さは保たれ、数字だけの行は 0 に近くなる。
    薄い灰色の線（地との差が 10 程度）でも拾える。q を上げると、横幅の一部にしかない線も拾う。
    """
    k = line_offset(gray.shape[0])
    resp = np.zeros_like(gray)
    mid = gray[k:-k] * polarity
    resp[k:-k] = np.minimum(mid - gray[: -2 * k] * polarity, mid - gray[2 * k :] * polarity)
    return np.quantile(resp, q, axis=1)


def find_peaks(frac: np.ndarray, floor: float = 4.0) -> list[tuple[float, float]]:
    """線の強さが floor を超える連続区間ごとに（重心の y, 最大値）を返す。"""
    peaks = []
    y, n = 0, len(frac)
    while y < n:
        if frac[y] < floor:
            y += 1
            continue
        y1 = y
        while y1 < n and frac[y1] >= floor:
            y1 += 1
        seg = frac[y:y1]
        center = float((np.arange(y, y1) * seg).sum() / seg.sum())
        peaks.append((center, float(seg.max())))
        y = y1
    return peaks


@dataclass
class Staff:
    lines: list[float]  # 6 本の y（上が 1 弦）
    score: float
    deviation: float = 0.0

    @property
    def spacing(self) -> float:
        return (self.lines[-1] - self.lines[0]) / 5


def find_staves(peaks: list[tuple[float, float]], height: int) -> list[Staff]:
    """等間隔に並んだ山 6 本の組を探す。重ならない組を強い順に返す。"""
    ys = [p[0] for p in peaks]
    candidates = []
    for i in range(len(peaks)):
        for j in range(i + 5, len(peaks)):
            s = (ys[j] - ys[i]) / 5
            if s < 3 or s > height / 12:
                continue
            tol = max(0.75, 0.1 * s)
            picked = [i]
            for m in range(1, 5):
                want = ys[i] + m * s
                best = min(range(i + 1, j), key=lambda q: abs(ys[q] - want))
                if abs(ys[best] - want) > tol or best in picked:
                    break
                picked.append(best)
            else:
                picked.append(j)
                # 組の外側、1 間隔の所にも同じような線があれば 6 本の組とは言えない
                outside = [
                    q
                    for q in range(len(peaks))
                    if q not in picked
                    and (abs(ys[q] - (ys[i] - s)) <= tol or abs(ys[q] - (ys[j] + s)) <= tol)
                ]
                score = sum(peaks[q][1] for q in picked) - sum(peaks[q][1] for q in outside)
                # 等間隔からのずれ（間隔に対する割合）。点数が同じならずれの小さい組を採る
                dev = sum(abs(ys[q] - (ys[i] + m * s)) for m, q in enumerate(picked)) / s
                candidates.append(Staff([ys[q] for q in picked], score, dev))
    candidates.sort(key=lambda st: (-round(st.score, 1), st.deviation))
    # タブの線の間隔は五線より広い。五線＋連桁が 6 本の組に見えることがあるので、
    # いちばん広い間隔の組に近いものだけ残す（2 段組のタブは同じ間隔で両方残る）。
    widest = max((st.spacing for st in candidates if st.score > 0), default=0)
    candidates = [st for st in candidates if st.spacing >= 0.85 * widest]
    chosen: list[Staff] = []
    for st in candidates:
        if st.score <= 0:
            continue
        if all(st.lines[-1] < c.lines[0] - c.spacing or st.lines[0] > c.lines[-1] + c.spacing for c in chosen):
            chosen.append(st)
    return sorted(chosen, key=lambda st: st.lines[0])


NO_TAB_EXIT = 3  # videotab strip が「タブ譜が写っていない」で終わるときの終了コード


class NoTabFound(SystemExit):
    """帯を指定せずに探して、タブ譜の 6 本線がどこにも見つからなかった。"""


@dataclass
class StripResult:
    polarity: int  # -1: 白地に濃い線 / +1: 暗い地に明るい線
    staves: list[Staff]
    band: tuple[int, int]  # 帯の y 範囲 [y0, y1)
    background: tuple[int, int, int]
    frames_used: int
    frames_with_tab: int


def sample_frames(frames: list[Frame], n: int = 24) -> list[Frame]:
    if len(frames) <= n:
        return frames
    idx = np.linspace(0, len(frames) - 1, n).round().astype(int)
    return [frames[i] for i in sorted(set(idx))]


def detect(frames: list[Frame], n_samples: int = 24, band: tuple[int, int] | None = None) -> StripResult:
    """帯と弦の線を探す。band を渡すと、その y 範囲の中だけで線を探し、帯もその範囲にする。"""
    picks = sample_frames(frames, n_samples)
    rgbs = [load_rgb(f.path) for f in picks]
    height = rgbs[0].shape[0]
    grays = [to_gray(a) for a in rgbs]

    best = None
    for polarity in (-1, 1):
        strengths = np.stack([line_strength(g, polarity) for g in grays])
        if band:
            strengths[:, : band[0]] = 0
            strengths[:, band[1] :] = 0
        # 演奏中の色枠などで一部のフレームだけ線が隠れても残るよう、上位 1/3 の値で見る
        strength = np.quantile(strengths, 0.67, axis=0)
        staves = find_staves(find_peaks(strength), height)
        if staves and (best is None or sum(s.score for s in staves) > sum(s.score for s in best[1])):
            best = (polarity, staves, strengths)
    if best is None:
        if band:
            raise SystemExit("タブ譜の 6 本線が見つかりませんでした（帯の指定を見直してください）")
        raise NoTabFound(
            "タブ譜の 6 本線が見つかりませんでした。この動画にはタブ譜が写っていないかもしれません"
            "（写っているなら --band Y0 Y1 で帯のおおよその範囲を指定してください）"
        )
    polarity, staves, strengths = best

    with_tab = sum(1 for f in strengths if _has_staff(f, staves[0]))
    background = _background(rgbs, staves)
    if band is None:
        y0, y1 = _grow_band(rgbs, staves, background)
        # 縁の小節番号や記号が切れないよう少し広げる
        band = (max(0, y0 - 2), min(height, y1 + 2))
    return StripResult(polarity, staves, band, background, len(picks), with_tab)


def _has_staff(frac: np.ndarray, staff: Staff, floor: float = 4.0) -> bool:
    hits = 0
    for y in staff.lines:
        lo, hi = int(y) - 1, int(y) + 3
        if frac[max(lo, 0) : hi].max() >= floor:
            hits += 1
    return hits >= 5


def _background(rgbs: list[np.ndarray], staves: list[Staff]) -> tuple[int, int, int]:
    """タブの線と線の間の行（線そのものを避けた真ん中）の色の中央値を地の色とする。"""
    st = staves[0]
    rows = [int(round((a + b) / 2)) for a, b in zip(st.lines, st.lines[1:])]
    px = np.concatenate([a[rows].reshape(-1, 3) for a in rgbs])
    return tuple(int(v) for v in np.median(px, axis=0))


def _grow_band(
    rgbs: list[np.ndarray], staves: list[Staff], bg: tuple[int, int, int], dist: float = 40.0
) -> tuple[int, int]:
    """地の色の行が続く範囲まで、タブの線から上下に広げる。"""
    height = rgbs[0].shape[0]
    cover = np.median(
        np.stack([(np.abs(a - np.array(bg, dtype=np.float32)).max(axis=2) < dist).mean(axis=1) for a in rgbs]),
        axis=0,
    )
    gap = int(max(3, 2 * staves[0].spacing))  # 五線・連桁・数字が並ぶ行は地が少なくても続けて見る
    top, bottom = int(staves[0].lines[0]), int(staves[-1].lines[-1]) + 1

    def grow(start: int, step: int) -> int:
        edge, miss, y = start, 0, start
        while 0 <= y + step < height:
            y += step
            if cover[y] >= 0.5:
                edge, miss = y, 0
            else:
                miss += 1
                if miss > gap:
                    break
        return edge

    return grow(top, -1), grow(bottom, 1) + 1


def debug_image(rgb: np.ndarray, result: StripResult) -> Image.Image:
    """帯を枠で、検出した弦の線を色つきの短い印で示した確認用画像。"""
    im = Image.fromarray(rgb.astype(np.uint8))
    d = ImageDraw.Draw(im)
    y0, y1 = result.band
    d.rectangle([0, y0, im.width - 1, y1 - 1], outline=(255, 0, 255), width=max(1, im.height // 360))
    tick = max(8, im.width // 40)
    for st in result.staves:
        for n, y in enumerate(st.lines, start=1):
            color = (0, 200, 0) if n % 2 else (0, 120, 255)
            d.line([0, y, tick, y], fill=color, width=max(1, im.height // 360))
            d.line([im.width - tick, y, im.width, y], fill=color, width=max(1, im.height // 360))
    return im
