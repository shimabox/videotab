"""pages: 帯の中身が切り替わる所でページに分け、ページごとに拡大画像を作る。

- 帯の「インク」（地の色と大きく違う画素）を、今のページの最初のフレームと比べる
  （直前のフレームではなく、採用済みのフレームと比べる。komadori と同じ考え方で、
  少しずつ進む変化もたまった差として拾える）。
- 差がしきい値を超えたらページ送り。しきい値は差の分布の切れ目（対数で見ていちばん
  大きな隙間）から自動で決める。今弾いている音の色替えはインクが変わらないので差にならない。
- しきい値より小さな差でも、帯を横にずらすと差が縮むなら横スクロールとみなして区切る。
  演奏位置の枠が動いただけなら、ずらさない位置がいちばん近いので区切らない。
- 1 枚だけの区間は、前後のページの重ね合わせ（切り替えのフェード）なら捨てる。
  横スクロールの動画では 1 枚だけの画面も新しい中身なので残す。
- 拡大画像は、弦の線の間隔が約 30px になる倍率で、帯を少し重ねて横に分割する。
  左の余白に弦の番号（線の位置）を書くので、数字がどの線に乗っているかを取り違えにくい。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from videotab import confine, inside
from videotab.strip import line_offset, load_rgb, to_gray
from videotab.workdir import Frame, fmt_time, json_text

INK_DIFF = 60.0  # 地の明るさからこれだけ離れた画素をインクとみなす
TARGET_SPACING = 30.0  # 拡大後の弦の線の間隔（px）
MAX_PIECE_WIDTH = 1400  # 拡大画像 1 枚の幅の上限（px、余白を除く）
MARGIN = 44  # 弦の番号を書く左の余白
LONG_PAGE = 15.0  # これより長く同じページが続いたら、切り替えの取りこぼしを疑う（秒）


@dataclass
class Setup:
    band: tuple[int, int]
    polarity: int
    background: tuple[int, int, int]
    staves: list[list[float]]  # 基準の線の位置
    # ページによって段が上下に動く動画だけ: [最初のフレーム番号, 最後のフレーム番号, dy] の並び
    shifts: list[list] = field(default_factory=list)

    @classmethod
    def from_meta(cls, meta: dict, frames: list[Frame] | None = None) -> "Setup":
        """meta.json の strip から作る。frames を渡すと、shifts が今のフレームと合うかを確かめる。"""
        s = meta.get("strip")
        if not s:
            raise SystemExit("帯の位置がまだ決まっていません（先に videotab strip）")
        shifts = s.get("shifts") or []
        if frames and shifts and shifts[-1][1] != frames[-1].index:
            raise SystemExit(
                f"線の位置はフレーム {shifts[-1][1]} までの分しかありません（画像は {frames[-1].index} 枚）。"
                "画像を作り直したときは strip をやり直してください"
            )
        return cls(tuple(s["band"]), s["polarity"], tuple(s["background"]), s["staves"], shifts)

    def dy(self, index: int) -> float:
        """フレーム番号 index の線の、基準からの上下のずれ（shifts がなければ 0）。"""
        return float(next((d for a, b, d in self.shifts if a <= index <= b), 0.0))

    @property
    def bg_gray(self) -> float:
        r, g, b = self.background
        return 0.299 * r + 0.587 * g + 0.114 * b

    @property
    def spacing(self) -> float:
        return float(np.mean([(st[-1] - st[0]) / 5 for st in self.staves]))


@dataclass
class Page:
    number: int
    frames: list[Frame]
    pick: Frame
    images: list[str] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.frames[0].time

    @property
    def duration(self) -> float:
        if len(self.frames) < 2:
            return 0.0
        step = self.frames[1].time - self.frames[0].time
        return self.frames[-1].time - self.frames[0].time + step


COMPARE_WIDTH = 640  # 比べるときは帯をこの幅に縮める（解像度によらず同じしきい値で見る）


def band_gray(frame: Frame, setup: Setup) -> np.ndarray:
    y0, y1 = setup.band
    return to_gray(load_rgb(frame.path))[y0:y1]


def ink_map(gray: np.ndarray, setup: Setup) -> np.ndarray:
    ink = np.abs(gray - setup.bg_gray) > INK_DIFF
    h, w = ink.shape
    if w <= COMPARE_WIDTH:
        return ink
    small = Image.fromarray(ink.astype(np.uint8) * 255).resize(
        (COMPARE_WIDTH, max(1, round(h * COMPARE_WIDTH / w))), Image.BOX
    )
    return np.asarray(small) > 64


def _longest_run(mask: np.ndarray) -> int:
    if not mask.any():
        return 0
    padded = np.concatenate([[0], mask.astype(np.int8), [0]])
    edges = np.flatnonzero(np.diff(padded))
    return int((edges[1::2] - edges[::2]).max())


def has_tab(gray: np.ndarray, setup: Setup, floor: float = 3.0, dy: float = 0.0) -> bool:
    """帯の中に、strip で見つけたタブの線が見えているか。dy はそのフレームの基準からのずれ。

    線の行に、上下より濃い（明るい）画素が横に長く続く所があるかで見る。和音で線が
    埋まった画面や、横幅の途中で終わる最後のページでも、数字の間に線は続いている。
    映像のざらつきは点がばらばらなので長く続かない。
    """
    h, w = gray.shape
    k = line_offset(setup.band[1])
    pol = setup.polarity
    need = max(8, round(w * 12 / 640))
    y0 = setup.band[0]
    hits = 0
    for y in setup.staves[0]:
        y += dy
        best = 0
        for row in range(int(round(y)) - y0 - 1, int(round(y)) - y0 + 2):
            if row - k < 0 or row + k >= h:
                continue
            mid = gray[row] * pol
            ridge = np.minimum(mid - gray[row - k] * pol, mid - gray[row + k] * pol) >= floor
            best = max(best, _longest_run(ridge))
        hits += best >= need
    return hits >= 5


def ink_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(a != b))


def shifted_diff(a: np.ndarray, b: np.ndarray) -> float:
    """b を横に 1px 以上ずらして a と重ねたときの差の最小値（重なった部分で比べる）。"""
    w = a.shape[1]
    best = 1.0
    for dx in range(1, w // 2 + 1):
        best = min(best, float(np.mean(a[:, dx:] != b[:, :-dx])), float(np.mean(a[:, :-dx] != b[:, dx:])))
    return best


def auto_threshold(diffs: np.ndarray) -> float:
    """差の分布で、ふだんの揺れと切り替えの間にある切れ目をしきい値にする。"""
    noise = float(np.median(diffs)) if len(diffs) else 0.0
    vals = np.sort(diffs[diffs > max(2 * noise, 0.004)])
    if len(vals) < 2:
        return 0.02
    logs = np.log(vals)
    gaps = np.diff(logs)
    i = int(np.argmax(gaps))
    thr = float(math.exp((logs[i] + logs[i + 1]) / 2))
    return min(max(thr, 0.012), 0.06)


def _grow(mask: np.ndarray) -> np.ndarray:
    """上下左右に 1px 広げる（圧縮による 1px のずれを許すため）。"""
    grown = mask.copy()
    grown[1:] |= mask[:-1]
    grown[:-1] |= mask[1:]
    grown[:, 1:] |= grown[:, :-1].copy()
    grown[:, :-1] |= grown[:, 1:].copy()
    return grown


def _is_blend(ink: np.ndarray, before: np.ndarray, after: np.ndarray) -> bool:
    """ink が前後のページの重ね合わせ（切り替え途中のフェード）でできているか。

    前後の和集合にほぼ収まるだけでなく、前のページにしかないインクと後のページにしか
    ないインクの両方をある程度含むときだけ重ね合わせとみなす。前のページから音が
    減っただけの本物のページ（前のページの部分集合）を消さないため。
    """
    total = ink.sum()
    if total == 0:
        return False
    if (ink & ~_grow(before | after)).sum() / total >= 0.05:
        return False
    only_before = before & ~_grow(after)
    only_after = after & ~_grow(before)
    if only_before.sum() == 0 or only_after.sum() == 0:
        return False
    grown_ink = _grow(ink)
    kept_before = (only_before & grown_ink).sum() / only_before.sum()
    kept_after = (only_after & grown_ink).sum() / only_after.sum()
    return kept_before >= 0.5 and kept_after >= 0.5


@dataclass
class Detection:
    pages: list[Page]
    threshold: float
    no_tab: list[Frame]
    dropped: list[Frame]
    diffs: list[float]


def detect_pages(frames: list[Frame], setup: Setup, threshold: float | None = None) -> Detection:
    grays = [band_gray(f, setup) for f in frames]
    dys = [setup.dy(f.index) for f in frames]
    tab = [has_tab(g, setup, dy=d) for g, d in zip(grays, dys)]
    inks = [ink_map(g, setup) for g in grays]

    diffs = [0.0] + [ink_diff(inks[i], inks[i - 1]) for i in range(1, len(frames))]
    tab_diffs = np.array([d for i, d in enumerate(diffs) if i and tab[i] and tab[i - 1]])
    thr = threshold if threshold is not None else auto_threshold(tab_diffs)
    # ずらして比べるのは、ふだんの揺れよりはっきり大きい差のときだけ
    floor = max(0.008, 2 * float(np.median(tab_diffs))) if len(tab_diffs) else 0.008

    # タブが見えているフレームを、今のページの最初のフレームとの差で区切る。
    # 線の位置が変わる所（strip の shifts の区間の境目）では、差が小さくても必ず区切る
    segments: list[list[int]] = []
    for i in range(len(frames)):
        if not tab[i]:
            continue
        if segments and segments[-1][-1] == i - 1 and dys[i] == dys[segments[-1][0]]:
            d = ink_diff(inks[i], inks[segments[-1][0]])
            scrolled = floor < d <= thr and shifted_diff(inks[segments[-1][-1]], inks[i]) < diffs[i]
            if d <= thr and not scrolled:
                segments[-1].append(i)
                continue
        segments.append([i])

    kept, dropped = [], []
    for k, seg in enumerate(segments):
        if len(seg) == 1 and 0 < k < len(segments) - 1:
            before, after = inks[segments[k - 1][-1]], inks[segments[k + 1][0]]
            if _is_blend(inks[seg[0]], before, after):
                dropped.append(frames[seg[0]])
                continue
        kept.append(seg)

    pages = [
        Page(n, [frames[i] for i in seg], frames[seg[len(seg) // 2]]) for n, seg in enumerate(kept, start=1)
    ]
    no_tab = [f for f, t in zip(frames, tab) if not t]
    return Detection(pages, thr, no_tab, dropped, diffs)


# --- 拡大画像


@dataclass
class Layout:
    scale: float
    pieces: list[tuple[int, int]]  # 横の切り出し範囲 [x0, x1)
    lines: list[list[float]]  # 拡大画像の上での弦の線の y（段ごと）


def layout(setup: Setup, width: int) -> Layout:
    scale = max(1.0, round(TARGET_SPACING / setup.spacing, 1))
    piece_w = int(MAX_PIECE_WIDTH / scale)
    if piece_w >= width:
        pieces = [(0, width)]
    else:
        overlap = int(piece_w * 0.12)
        n = math.ceil((width - overlap) / (piece_w - overlap))
        step = (width - piece_w) / (n - 1)
        pieces = [(round(i * step), round(i * step) + piece_w) for i in range(n)]
    return Layout(scale, pieces, zoomed_lines(setup, scale))


def zoomed_lines(setup: Setup, scale: float, dy: float = 0.0) -> list[list[float]]:
    """基準から dy ずれた位置の弦の線の、拡大画像の上での y（段ごと）。"""
    y0 = setup.band[0]
    return [[round((y + dy - y0 + 0.5) * scale - 0.5, 1) for y in st] for st in setup.staves]


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 古い Pillow
        return ImageFont.load_default()


def render_piece(rgb: np.ndarray, setup: Setup, lay: Layout, piece: int, dy: float = 0.0) -> Image.Image:
    """帯の横の 1 区切りを拡大し、左の余白に弦の番号と線の印を書く。

    切り出す範囲と倍率は全ページ共通。印だけを、その画像のフレームの基準からのずれ dy に合わせて動かす。
    """
    x0, x1 = lay.pieces[piece]
    y0, y1 = setup.band
    crop = Image.fromarray(rgb[y0:y1, x0:x1].astype(np.uint8))
    w, h = round(crop.width * lay.scale), round(crop.height * lay.scale)
    big = crop.resize((w, h), Image.LANCZOS)
    out = Image.new("RGB", (w + MARGIN, h), (40, 40, 40))
    out.paste(big, (MARGIN, 0))
    d = ImageDraw.Draw(out)
    font = _font(max(12, int(TARGET_SPACING * 0.6)))
    for st in lay.lines:
        for n, y in enumerate(st, start=1):
            y += dy * lay.scale
            color = (255, 210, 0) if n % 2 else (0, 220, 255)
            d.line([MARGIN - 10, y, MARGIN - 1, y], fill=color, width=2)
            d.text((4, y), str(n), fill=color, font=font, anchor="lm")
    return out


def piece_label(i: int) -> str:
    return "abcdefghij"[i]


def write_pages(workdir: Path, frames: list[Frame], setup: Setup, det: Detection) -> Path:
    """pages/ に拡大画像と pages.json・index.md を書く。

    pages/ が作業フォルダの外を指すリンクなら、リンクを消して作り直す。古い画像の削除と書き出しは、
    リンクをたどらずに開いた pages/ を足場にする（inside）。
    """
    out = workdir / "pages"
    with inside.open_dir(confine.root_of(workdir), "pages", remake=True, notify=print) as folder:
        return _write_pages(workdir, out, folder, frames, setup, det)


def _write_pages(workdir: Path, out: Path, folder: inside.Folder, frames, setup: Setup, det: Detection) -> Path:
    for old in sorted(os.listdir(folder.fd)):
        if fnmatchcase(old, "p*_*.png"):
            inside.remove(folder, old)
    width = load_rgb(frames[0].path).shape[1]
    lay = layout(setup, width)
    for page in det.pages:
        rgb = load_rgb(page.pick.path)
        for i in range(len(lay.pieces)):
            name = f"p{page.number:03d}_{piece_label(i)}.png"
            inside.write_image(folder, name, render_piece(rgb, setup, lay, i, setup.dy(page.pick.index)), notify=print)
            page.images.append(name)

    step = frames[1].time - frames[0].time if len(frames) > 1 else 1.0
    data = {
        "threshold": round(det.threshold, 4),
        "scale": lay.scale,
        "band": list(setup.band),
        "pieces": [list(p) for p in lay.pieces],
        "lines": lay.lines,
        "pages": [
            {
                "page": p.number,
                "start": round(p.start, 3),
                "end": round(p.frames[-1].time + step, 3),
                "frames": [p.frames[0].index, p.frames[-1].index],
                "pick": p.pick.index,
                "images": p.images,
                "long": p.duration > LONG_PAGE,
                # このページの拡大画像の上での弦の線（最上位の lines は基準の位置）
                "lines": zoomed_lines(setup, lay.scale, setup.dy(p.pick.index)),
            }
            for p in det.pages
        ],
        "no_tab_frames": [f.index for f in det.no_tab],
        "dropped_blend_frames": [f.index for f in det.dropped],
    }
    inside.write_text(folder, "pages.json", json_text(data), notify=print)
    inside.write_text(folder, "index.md", index_markdown(workdir, data), notify=print)
    return out


def index_markdown(workdir: Path, data: dict) -> str:
    # ページによって線の位置が違うときだけ、表に「弦の線」の列を足す（同じなら今までどおりの一覧）
    moving = any(p.get("lines", data["lines"]) != data["lines"] for p in data["pages"])
    if moving:
        lines_row = "- 弦の線の位置はページによって違う（表を参照）。拡大画像の左の余白の番号と印は、そのページの位置に付く"
    else:
        lines_desc = " / ".join(
            "段{}: {}".format(k + 1, ", ".join(f"{n}弦 y={y:g}" for n, y in enumerate(st, start=1)))
            for k, st in enumerate(data["lines"])
        )
        lines_row = f"- 拡大画像の上の弦の線: {lines_desc}（左の余白の番号と印）"
    rows = [
        f"# ページ一覧（{workdir.name}）",
        "",
        f"- 拡大率 {data['scale']}×、帯 y={data['band'][0]}〜{data['band'][1]}、"
        f"横の分割 {', '.join(f'{piece_label(i)}: x={a}〜{b}' for i, (a, b) in enumerate(data['pieces']))}",
        lines_row,
        f"- 切り替えのしきい値 {data['threshold']}。タブの見えないフレーム {len(data['no_tab_frames'])} 枚、"
        f"切り替え途中として除いたフレーム {len(data['dropped_blend_frames'])} 枚",
        "- 「長い」は同じページと判定された時間が長い所。切り替えの取りこぼしを疑い、元フレームも見る。",
        "",
    ]
    if moving:
        rows += ["| ページ | 時刻 | フレーム | 画像 | 弦の線（1〜6 弦の y） | 注意 |", "|---|---|---|---|---|---|"]
    else:
        rows += ["| ページ | 時刻 | フレーム | 画像 | 注意 |", "|---|---|---|---|---|"]
    for p in data["pages"]:
        a, b = p["frames"]
        cells = [
            str(p["page"]),
            f"{fmt_time(p['start'])}〜{fmt_time(p['end'])}",
            f"{a}〜{b}（拡大は {p['pick']}）",
            " ".join(p["images"]),
        ]
        if moving:
            cells.append(" / ".join(", ".join(f"{y:g}" for y in st) for st in p["lines"]))
        cells.append("長い" if p["long"] else "")
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def zoom_frame(workdir: Path, frame: Frame, setup: Setup) -> list[Path]:
    """1 フレームを pages と同じ倍率・分割で拡大する（色枠に隠れた数字の確認などに使う）。
    左の余白の印は、そのフレームの線の位置に付ける。

    読み取りのエージェントから実行されたときは、フレームの画像が作業フォルダの外を指していれば断る
    （confine）。出力のフォルダは、閉じ込めの有無によらず、作業フォルダの外を指していれば断り、
    出力の画像がリンクなどなら通常のファイルに置き換える（inside。拡大画像は作り直せるので知らせない）。
    """
    rgb = load_rgb(confine.guard(frame.path))
    lay = layout(setup, rgb.shape[1])
    out = confine.guard(workdir / "pages" / "zoom")  # 表示するパス（閉じ込めがあれば、たどり終えたパス）
    root = confine.root_of(workdir)
    paths = []
    with inside.open_dir(root, inside.rel_path(root, workdir / "pages" / "zoom")) as folder:
        for i in range(len(lay.pieces)):
            name = f"f{frame.index:04d}_{piece_label(i)}.png"
            inside.write_image(folder, name, render_piece(rgb, setup, lay, i, setup.dy(frame.index)))
            paths.append(out / name)
    return paths
