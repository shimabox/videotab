"""strip: タブ譜の帯の位置と、弦の線の y 座標を自動で見つける。

考え方:
- タブ譜は「ほぼ横幅いっぱいの細い横線が 6 本、等間隔に並んだもの」。行ごとに、
  上下の画素より濃い（または明るい）画素が横に何割あるかを数え、その山を線とみなす。
  等間隔の山 6 本の組をタブ 1 段とする（五線は 5 本なので混ざらない）。
- 帯は、タブの線の間の地の色が上下に続く範囲。五線や小節番号、タブ下の連桁もこの中に入る。
- 演奏中の色枠や字幕に引っ張られないよう、動画全体から間引いた何枚かの中央値で判断する。
- 楽譜ソフトの画面などで、ページによって段が上下に動くことがある。間引いた 1 枚ずつでも線を探し、
  位置が動いていれば全フレームで位置を求め、位置の同じフレームの区間ごとのずれ（shifts）を返す。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw

from videotab.workdir import Frame


IMAGE_FORMATS = ("PNG", "JPEG")  # フレームの画像として開く形式（切り出しは PNG、取り込みは PNG か JPEG）


def load_rgb(path) -> np.ndarray:
    # Pillow は名前ではなく中身で形式を決める。形式を限らないと、.png の名前で置かれた別の形式
    # （EPS は外部のプログラムで描画する）まで開くので、フレームの形式だけにする
    return np.asarray(Image.open(path, formats=IMAGE_FORMATS).convert("RGB")).astype(np.float32)


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
    staves: list[Staff]  # 基準の位置（位置が動く動画では、観測された位置のひとつ）
    band: tuple[int, int]  # 帯の y 範囲 [y0, y1)。位置が動く動画では動く範囲の全体を含む
    background: tuple[int, int, int]
    frames_used: int
    frames_with_tab: int
    # 位置が動く動画だけ: (最初のフレーム番号, 最後のフレーム番号, dy) の並び。全フレームを隙間なく覆い、
    # そのフレームの線は staves の各 y + dy（全段共通）。動かない動画では空
    shifts: list[tuple[int, int, float]] = field(default_factory=list)
    located: set[int] = field(default_factory=set)  # 位置が動く動画だけ: 線の位置が見つかったフレームの番号
    # カメラ撮影の補正: (フレーム番号, y の移動量, 線の傾き dy/dx, 検出できたか, 縦の倍率)。全フレーム分。
    corrections: list[tuple[int, float, float, bool, float]] = field(default_factory=list)

    def dy_at(self, index: int) -> float:
        if self.corrections:
            return next((row[1] for row in self.corrections if row[0] == index), 0.0)
        return next((d for a, b, d in self.shifts if a <= index <= b), 0.0)

    def slope_at(self, index: int) -> float:
        return next((row[2] for row in self.corrections if row[0] == index), 0.0)

    def scale_at(self, index: int) -> float:
        return next((row[4] for row in self.corrections if row[0] == index), 1.0)


def sample_frames(frames: list[Frame], n: int = 24) -> list[Frame]:
    if len(frames) <= n:
        return frames
    idx = np.linspace(0, len(frames) - 1, n).round().astype(int)
    return [frames[i] for i in sorted(set(idx))]


SAME_SPACING = 0.1  # 線の間隔がこの割合以内なら同じ型の段とみなす
SEARCH_LINES = 3.0  # 全フレームの位置を探すとき、間引いた画像で見た範囲の外へ広げる幅（線の間隔の何本分か）


def still_tolerance(spacing: float) -> float:
    """この幅以内のずれは、位置が動いていないとみなす。"""
    return max(1.0, 0.15 * spacing)


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
        fit = _fit(strengths, height)
        if fit and (best is None or sum(s.score for s in fit[0]) > sum(s.score for s in best[1])):
            best = (polarity, *fit, strengths)
    if (
        best is None
        or sum(s.score for s in best[1]) / (6 * len(best[1])) < 8
    ):
        from videotab import camera

        corrected = camera.detect(frames, picks, rgbs, band)
        if corrected is not None:
            return corrected
    if best is None:
        if band:
            raise SystemExit("タブ譜の 6 本線が見つかりませんでした（帯の指定を見直してください）")
        raise NoTabFound(
            "タブ譜の 6 本線が見つかりませんでした。この動画にはタブ譜が写っていないかもしれません"
            "（写っているなら --band Y0 Y1 で帯のおおよその範囲を指定してください）"
        )
    polarity, staves, dys, strengths = best

    tol = still_tolerance(staves[0].spacing)
    if any(d is not None and abs(d) > tol for d in dys):
        # 間引いた画像で位置が動いて見えたときだけ、全フレームの位置を求める
        seen = [d for d in dys if d is not None]
        if band:  # 線が帯に収まる範囲
            window = (band[0] - staves[0].lines[0], band[1] - 1 - staves[-1].lines[-1])
        else:
            reach = SEARCH_LINES * staves[0].spacing
            window = (min(seen) - reach, max(seen) + reach)
        known = {f.index: st for f, st in zip(picks, strengths)}
        located, peaks = locate(frames, polarity, staves, window, band=band, known=known)
        shifts = _segments(
            [f.index for f in frames],
            located,
            tol,
            lambda pos, d: _fits_own(peaks[pos], staves, located[pos], d),
        )
        if len(shifts) > 1:
            found = {f.index for f, d in zip(frames, located) if d is not None}
            with_tab = sum(1 for f in picks if f.index in found)
            pick_dys = [next(d for a, b, d in shifts if a <= f.index <= b) for f in picks]
            background = _background(rgbs, staves, pick_dys)
            if band is None:
                ds = [d for _, _, d in shifts]
                y0, y1 = _grow_band(rgbs, staves, background, reach=(min(ds), max(ds)))
                band = (max(0, y0 - 2), min(height, y1 + 2))
            return StripResult(polarity, staves, band, background, len(picks), with_tab, shifts, found)
        if shifts and abs(shifts[0][2]) > tol:  # 1 通りの位置にまとまったら、動かない動画とみなす
            staves = [Staff([y + shifts[0][2] for y in st.lines], st.score, st.deviation) for st in staves]

    with_tab = sum(1 for f in strengths if _has_staff(f, staves[0]))
    background = _background(rgbs, staves)
    if band is None:
        y0, y1 = _grow_band(rgbs, staves, background)
        # 縁の小節番号や記号が切れないよう少し広げる
        band = (max(0, y0 - 2), min(height, y1 + 2))
    return StripResult(polarity, staves, band, background, len(picks), with_tab)


def _fit(strengths: np.ndarray, height: int) -> tuple[list[Staff], list[float | None]] | None:
    """間引いた画像の線の強さ（1 行が 1 枚）から、基準の段と、1 枚ごとの基準からのずれを求める。

    まず全部を重ねて探す（位置が動かない動画はこれで決まる）。1 枚ずつ探した結果のうち、線の間隔が
    同じものがいちばん多い集まりと、重ねた結果の段の数と間隔が合わなければ（位置が動いて、重ねた山が
    ぼやけたり偽の組ができたりしたとき）、1 枚ずつのずれを打ち消すようにずらして重ね直す。
    """
    # 演奏中の色枠などで一部のフレームだけ線が隠れても残るよう、上位 1/3 の値で見る
    stacked = find_staves(find_peaks(np.quantile(strengths, 0.67, axis=0)), height)
    singles = [find_staves(find_peaks(f), height) for f in strengths]
    group = _largest_group(singles)
    if not group:  # 1 枚ずつでは線が薄くて見つからない。比べる相手がないので、重ねた結果を使う
        return (stacked, [None] * len(singles)) if stacked else None
    # 段の数は集まりの中でいちばん多い数（最頻値）。1 枚だけ余分な組が見つかっても引きずられない。
    # 同数なら多いほう（段の一部が演奏位置の枠などで隠れて、少なく見えるフレームがあるため）
    counts = Counter(len(singles[i]) for i in group)
    count = max(counts, key=lambda n: (counts[n], n))
    spacing = float(np.median([_spacing(singles[i]) for i in group]))
    if stacked and len(stacked) == count and _same_spacing(_spacing(stacked), spacing):
        base = stacked
    else:
        ref = max((i for i in group if len(singles[i]) == count), key=lambda i: sum(s.score for s in singles[i]))
        offsets = [_offset(st, singles[ref]) for st in singles]
        aligned = np.stack([_shift_rows(f, d) for f, d in zip(strengths, offsets)])
        base = find_staves(find_peaks(np.quantile(aligned, 0.67, axis=0)), height)
        if not base:
            return None
    return base, [_offset(st, base) for st in singles]


def _spacing(staves: list[Staff]) -> float:
    return float(np.mean([s.spacing for s in staves]))


def _same_spacing(a: float, b: float) -> bool:
    return abs(a - b) <= SAME_SPACING * max(a, b)


def _largest_group(singles: list[list[Staff]]) -> list[int]:
    """1 枚ずつの結果を線の間隔でまとめ、いちばん枚数の多い集まり（同数なら間隔の広いほう）の番号を返す。"""
    spacing = {i: _spacing(st) for i, st in enumerate(singles) if st}
    if not spacing:
        return []

    def members(i: int) -> list[int]:
        return [j for j in spacing if abs(spacing[j] - spacing[i]) <= SAME_SPACING * spacing[i]]

    return members(max(spacing, key=lambda i: (len(members(i)), spacing[i])))


def _offset(staves: list[Staff], base: list[Staff]) -> float | None:
    """1 枚の段の、基準からの上下のずれ。段の数と線の間隔が基準と合わなければ None。"""
    if not staves or len(staves) != len(base):
        return None
    if not all(_same_spacing(s.spacing, b.spacing) for s, b in zip(staves, base)):
        return None
    return float(np.median(np.concatenate([np.subtract(s.lines, b.lines) for s, b in zip(staves, base)])))


def _shift_rows(strength: np.ndarray, dy: float | None) -> np.ndarray:
    """線の強さを -dy 行（整数に丸める）ずらし、基準の位置にそろえる。dy が分からなければそのまま。"""
    k = 0 if dy is None else int(round(dy))
    if k == 0:
        return strength
    out = np.zeros_like(strength)
    if k > 0:
        out[:-k] = strength[k:]
    else:
        out[-k:] = strength[:k]
    return out


def locate(
    frames: list[Frame],
    polarity: int,
    staves: list[Staff],
    window: tuple[float, float],
    band: tuple[int, int] | None = None,
    known: dict[int, np.ndarray] | None = None,
) -> tuple[list[float | None], list[list[tuple[float, float]]]]:
    """フレームごとに、基準の段の型を当てて上下のずれ dy を求める。見つからないフレームは None。

    dy は window の範囲で探す。known にフレーム番号ごとの線の強さがあれば、画像を読み直さずに使う。
    フレームごとの山の並びも返す（区間にまとめるとき、別の dy で採点し直すのに使う）。
    """
    out, peaks = [], []
    for f in frames:
        strength = (known or {}).get(f.index)
        if strength is None:
            strength = line_strength(to_gray(load_rgb(f.path)), polarity)
            if band:
                strength[: band[0]] = 0
                strength[band[1] :] = 0
        peaks.append(find_peaks(strength))
        out.append(_match(peaks[-1], staves, window))
    return out, peaks


OUTSIDE_TOLERANCE = 0.2  # 組の外側 1 間隔の所の線を見る許容差（線の間隔に対する割合）


def _match(peaks: list[tuple[float, float]], staves: list[Staff], window: tuple[float, float]) -> float | None:
    """山の並びに基準の段を上下にずらして当て、いちばん合うずれを返す。点数は _score。"""
    if not peaks:
        return None
    lo, hi = window
    lines = [y for st in staves for y in st.lines]
    candidates = sorted({round(p - y, 1) for p, _ in peaks for y in lines if lo <= p - y <= hi})
    best, best_score = None, 0.0
    for d in candidates:
        fit = _score(peaks, staves, d)
        if fit and fit[0] > best_score:
            best, best_score = float(np.median(fit[1])), fit[0]
    return best


def _score(
    peaks: list[tuple[float, float]], staves: list[Staff], d: float
) -> tuple[float, list[float]] | None:
    """基準の段を d だけずらして山の並びに当てたときの点数と、当たった線のずれ。

    点数は find_staves と同じ考え方で、当たった線の強さの和から、組の外側 1 間隔の所にある線の強さを
    引く（ちょうど 1 間隔ずれた位置では 5 本しか当たらず、外側の線で減点される）。ページによって線の
    間隔が基準とわずかに違うと、外側の線は予測の位置から離れて見えるので、外側を見る許容差は線の
    間隔に比例させて広く取る。全段でそれぞれ 6 本中 5 本以上が当たらなければ None。
    """
    if not peaks:
        return None
    ys = np.array([p[0] for p in peaks])
    ws = np.array([p[1] for p in peaks])
    score, residuals = 0.0, []
    for st in staves:
        tol = max(0.75, 0.1 * st.spacing)
        hits: dict[int, float] = {}
        for y in st.lines:
            k = int(np.argmin(np.abs(ys - (y + d))))
            if abs(ys[k] - (y + d)) <= tol and k not in hits:
                hits[k] = float(ys[k] - y)
        if len(hits) < 5:
            return None
        score += float(ws[list(hits)].sum())
        outside = max(tol, OUTSIDE_TOLERANCE * st.spacing)
        for edge in (st.lines[0] - st.spacing, st.lines[-1] + st.spacing):
            k = int(np.argmin(np.abs(ys - (edge + d))))
            if abs(ys[k] - (edge + d)) <= outside and k not in hits:
                score -= float(ws[k])
        residuals += hits.values()
    return score, residuals


def _fits_own(peaks: list[tuple[float, float]], staves: list[Staff], own: float, other: float) -> bool:
    """そのフレームの位置が、別の dy（other）より自分の dy（own）のほうがよく当てはまるか。"""
    theirs = _score(peaks, staves, other)
    if theirs is None:
        return True
    mine = _score(peaks, staves, own)
    return mine is not None and mine[0] > theirs[0]


def _segments(
    indices: list[int],
    located: list[float | None],
    tol: float,
    fits_own: Callable[[int, float], bool] | None = None,
) -> list[tuple[int, int, float]]:
    """フレームごとの dy を、位置の同じ区間（最初のフレーム番号, 最後のフレーム番号, dy）にまとめる。

    続くフレームの dy の差が tol 以内なら同じ区間とし、区間の dy は中央値（0.1 で丸める）。位置の
    分からないフレームは直前の区間に入れる（先頭は直後の区間）。両隣が同じ位置で挟まれた 1 フレーム
    だけの区間は、外れとして隣に吸収する。fits_own（区間のフレームの位置, 両隣の dy）を渡すと、それが
    真のとき（そのフレームが両隣の dy より自分の dy によく当てはまるとき）は吸収せずに残す。
    """
    runs: list[list] = []  # [最初の位置, 最後の位置, [dy]]
    last = 0.0
    for pos, d in enumerate(located):
        if d is None:
            if runs:
                runs[-1][1] = pos
            continue
        if runs and abs(d - last) <= tol:
            runs[-1][1] = pos
            runs[-1][2].append(d)
        else:
            runs.append([pos if runs else 0, pos, [d]])
        last = d
    k = 1
    while k < len(runs) - 1:
        before, one, after = runs[k - 1], runs[k], runs[k + 1]
        around = before[2] + after[2]
        if (
            one[0] == one[1]
            and abs(np.median(before[2]) - np.median(after[2])) <= tol
            and not (fits_own and fits_own(one[0], float(np.median(around))))
        ):
            runs[k - 1 : k + 2] = [[before[0], after[1], around]]
            k = max(1, k - 1)
        else:
            k += 1
    return [(indices[a], indices[b], round(float(np.median(ds)), 1)) for a, b, ds in runs]


def _has_staff(frac: np.ndarray, staff: Staff, floor: float = 4.0) -> bool:
    hits = 0
    for y in staff.lines:
        lo, hi = int(y) - 1, int(y) + 3
        if frac[max(lo, 0) : hi].max() >= floor:
            hits += 1
    return hits >= 5


def _background(
    rgbs: list[np.ndarray], staves: list[Staff], dys: list[float] | None = None
) -> tuple[int, int, int]:
    """タブの線と線の間の行（線そのものを避けた真ん中）の色の中央値を地の色とする。

    dys（画像ごとの上下のずれ）を渡すと、画像ごとにその位置の行で見る。
    """
    st = staves[0]
    mids = [(a + b) / 2 for a, b in zip(st.lines, st.lines[1:])]
    dys = dys or [0.0] * len(rgbs)
    px = np.concatenate([a[[int(round(y + d)) for y in mids]].reshape(-1, 3) for a, d in zip(rgbs, dys)])
    return tuple(int(v) for v in np.median(px, axis=0))


def _grow_band(
    rgbs: list[np.ndarray],
    staves: list[Staff],
    bg: tuple[int, int, int],
    dist: float = 40.0,
    reach: tuple[float, float] = (0.0, 0.0),
) -> tuple[int, int]:
    """地の色の行が続く範囲まで、タブの線から上下に広げる。

    reach（いちばん上と下の位置の dy）を渡すと、線が動く範囲の上端と下端から広げる。
    """
    height = rgbs[0].shape[0]
    cover = np.median(
        np.stack([(np.abs(a - np.array(bg, dtype=np.float32)).max(axis=2) < dist).mean(axis=1) for a in rgbs]),
        axis=0,
    )
    gap = int(max(3, 2 * staves[0].spacing))  # 五線・連桁・数字が並ぶ行は地が少なくても続けて見る
    top, bottom = int(staves[0].lines[0] + reach[0]), int(staves[-1].lines[-1] + reach[1]) + 1

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


def debug_image(rgb: np.ndarray, result: StripResult, dy: float = 0.0, slope: float = 0.0, scale: float = 1.0) -> Image.Image:
    """帯を枠で、検出した弦の線を色つきの短い印で示した確認用画像。印は基準の位置から dy だけずらす。"""
    im = Image.fromarray(rgb.astype(np.uint8))
    d = ImageDraw.Draw(im)
    y0, y1 = result.band
    width = max(1, im.height // 360)
    def at(x, y):
        return y * scale + dy + slope * (x - (im.width - 1) / 2)
    if result.corrections:
        right = im.width - 1
        d.line([(0, at(0, y0)), (right, at(right, y0)), (right, at(right, y1 - 1)),
                (0, at(0, y1 - 1)), (0, at(0, y0))], fill=(255, 0, 255), width=width)
    else:
        d.rectangle([0, y0, im.width - 1, y1 - 1], outline=(255, 0, 255), width=width)
    tick = max(8, im.width // 40)
    for st in result.staves:
        for n, y in enumerate(st.lines, start=1):
            color = (0, 200, 0) if n % 2 else (0, 120, 255)
            d.line([0, at(0, y), tick, at(tick, y)], fill=color, width=width)
            d.line([im.width - tick, at(im.width - tick, y), im.width - 1, at(im.width - 1, y)],
                   fill=color, width=width)
    return im
