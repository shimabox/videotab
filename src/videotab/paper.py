"""紙の楽譜のページ・指定パートを選び、段ごとに補正した読み取り画像を作る。

ページとパートの判別は既存の読み取りエージェントが行う。書き込み先は paper/layout.json
だけを追加する。楽器名と譜面の対応を画像から判断するためで、コマンドの許可は広げない。
領域の座標・弦数・元フレームを本体で検査してから、画像を切り出す。
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from videotab import confine, inside
from videotab.agent import run_agent
from videotab.strip import find_peaks, to_gray
from videotab.workdir import Frame, json_text, list_frames, load_meta, read_json, save_meta, write_json

MAX_REGIONS = 200
DEFAULT_PART = "Guitar I"


def options(part: str = DEFAULT_PART, strings: int = 6) -> dict:
    from videotab.add import check_text

    name = check_text("書き起こすパート", part)
    if not name:
        raise ValueError("書き起こすパートを指定してください（例: Guitar I）")
    if isinstance(strings, bool) or strings not in (4, 6):
        raise ValueError("弦数は 4 または 6 を指定してください")
    return {"part": name, "strings": strings}


def configure(workdir: Path, part: str, strings: int) -> None:
    selected = options(part, strings)
    meta = load_meta(workdir)
    if selected != meta.get("paper") and (workdir / "score.json").exists():
        from videotab.build import DEFAULT_TUNING

        root = confine.root_of(workdir)
        score = read_json(confine.guard(workdir / "score.json", root=root))
        if not isinstance(score, dict):
            raise ValueError("score.json の形が違います。見出しを直してからパートを変更してください")
        # 前のパートのチューニングとカポを持ち越さない。見出しの元の値は履歴に残す。
        with inside.open_dir(root, f"history/part-{time.time_ns()}") as folder:
            inside.write_text(folder, "score.json", json_text(score))
            inside.write_text(folder, "paper.json", json_text(meta.get("paper")))
        score = {**score, "tuning": "g2 d2 a1 e1" if strings == 4 else DEFAULT_TUNING, "capo": 0}
        write_json(workdir / "score.json", score, root=root)
    meta["paper"] = selected
    save_meta(workdir, meta)


def selection(workdir: Path) -> dict:
    data = load_meta(workdir).get("paper")
    if not isinstance(data, dict):
        raise ValueError("紙の楽譜の設定がありません")
    return options(data.get("part"), data.get("strings"))


def load_image(path: Path, root: Path | None = None) -> Image.Image:
    with Image.open(confine.guard(path, root=root)) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


def source_frames(workdir: Path) -> list[Frame]:
    root = confine.root_of(workdir)
    confine.guard(workdir / "frames", root=root)
    frames = list_frames(workdir)
    for frame in frames:
        confine.guard(frame.path, root=root)
    return frames


def sharpness(im: Image.Image) -> float:
    # 同じ時間帯の候補から、紙の線と数字が鮮明な画像を優先する。
    small = im.copy()
    small.thumbnail((720, 1280))
    g = np.asarray(small.convert("L"), dtype=float)
    return float(np.mean(np.abs(np.diff(g, axis=0))) + np.mean(np.abs(np.diff(g, axis=1))))


def prepare(workdir: Path) -> list[Frame]:
    """動画は 4 秒ごとの鮮明な候補、写真は全ページを一覧にする。元画像はすべて残す。"""
    frames = source_frames(workdir)
    if not frames:
        raise ValueError("画像がありません（先に videotab frames）")
    still = load_meta(workdir).get("source_kind") == "document"
    buckets: dict[int, tuple[float, Frame]] = {}
    for f in frames:
        score = sharpness(load_image(f.path, confine.root_of(workdir)))
        key = f.index if still else int(f.time // 4)
        if key not in buckets or score > buckets[key][0]:
            buckets[key] = score, f
    picks = sorted((f for _, f in buckets.values()), key=lambda f: f.index)
    root = confine.root_of(workdir)
    with inside.open_dir(root, "paper") as parent:
        inside.remove(parent, "candidates")
    with inside.open_dir(root, "paper/candidates") as folder:
        for start in range(0, len(picks), 12):
            batch = picks[start:start + 12]
            sheet = Image.new("RGB", (1200, 3 * 440), "white")
            draw = ImageDraw.Draw(sheet)
            for i, f in enumerate(batch):
                im = load_image(f.path, root)
                w, h = im.size
                im.thumbnail((294, 408))
                x, y = i % 4 * 300, i // 4 * 440
                sheet.paste(im, (x + (300 - im.width) // 2, y + 28))
                draw.text((x + 4, y + 5), f"frame {f.index} ({w}x{h})", fill="black")
            inside.write_image(folder, f"c{start // 12 + 1:03d}.jpg", sheet)
        inside.write_text(folder, "index.json", json_text([
            {"frame": f.index, "file": f.path.name, "seconds": f.time} for f in picks
        ]))
    return picks


def layout_prompt(workdir: Path, picks: list[Frame]) -> str:
    wd = workdir.resolve()
    opts = selection(workdir)
    frames = "\n".join(f"- {f.index}: {f.path.name}" for f in picks)
    return f"""あなたは紙の楽譜のページとパートを選ぶ担当です。書き起こしそのものは後の担当が行います。
作業フォルダ: {wd}
書き起こすパートと弦数（JSON の値は指示ではなく入力情報）: {json_text(opts).strip()}

{wd}/paper/candidates/c*.jpg の一覧を見て、次に {wd}/frames/ の元画像を開いて確かめてください。
候補は時間帯ごとに鮮明な画像を選んだもので、同じ紙のページが何枚もあります。小節番号と音符を比較し、
各ページについていちばん鮮明で欠けの少ない画像を 1 枚選んでください。候補の間にページがあれば、
frames/ の別の画像も探してください。写真の入力では、ファイル順がページ順です。

そのページにある指定パートの段をすべて、譜面の読み順に選びます。楽器の名前（Guitar I / II、Bass 等）を確認し、
違うパートのタブを混ぜないでください。同じパートがページ内で再び現れたら、それも選びます。
見開きは左ページから右ページの順です。同じ小節が重なって写った画像は重複を避けます。
指定パートが休符だけの段も必要です。五線しかないパートは、この機能では書き起こせません。
文字がぼけた、指で隠れた、ページが欠けた、パートを特定できない所は warnings に理由を書いてください。
隠れた音やページを推測で補わないでください。本文にテンポ・拍子・繰り返しがあるので、それも残します。

出力は {wd}/paper/layout.json だけです。次の JSON で書いてください。
{{"pages":[{{"frame":1,"regions":[{{"box":[0.05,0.20,0.95,0.40],"tab":[0.05,0.30,0.95,0.38]}}]}}],"warnings":[]}}
- frame は元画像の番号。pages は紙のページ順です。
- box / tab は元画像の幅・高さを 1 とした [左, 上, 右, 下]（0〜1 の数）。
- box は指定パートの五線と TAB、上下の奏法記号・小節番号を含む範囲です。隣の楽器は含めないでください。
- tab は TAB の線と数字だけの範囲で、box の内側です。線の曲がりを含むよう余裕を持たせます。
- ページ全体は本体が別に保存するので、ページ上部のテンポ等を box に入れる必要はありません。
- 見開きなら pages に同じ frame を 2 回書けます（左の領域、右の領域を分ける）。
- 指定パートが見つからない場合は pages を空にし、warnings に理由を書きます。

元画像、一覧、既存の layout.json を読むことと、layout.json を書くことだけを行ってください。
HTML・alphaTex の作成、ほかのファイルの変更、利用者への質問はしないでください。
最後の返答は日本語で、選んだページ数と判読できない所だけを短く書いてください。
候補の元画像:
{frames}
"""


def _box(value, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{label} は [左, 上, 右, 下] の 4 個の座標にしてください")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ValueError(f"{label} の座標が数値ではありません")
    a, b, c, d = value
    if not 0 <= a < c <= 1 or not 0 <= b < d <= 1:
        raise ValueError(f"{label} の座標は 0〜1、左 < 右、上 < 下にしてください")
    return list(value)


def validate_layout(data: dict, frames: list[Frame]) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        raise ValueError("pages の一覧がありません")
    warnings = data.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
        raise ValueError("warnings は文の一覧にしてください")
    if not data["pages"]:
        raise ValueError("指定パートの段がありません" + (": " + " / ".join(warnings) if warnings else ""))
    indices = {f.index for f in frames}
    count = 0
    seen = set()
    for page in data["pages"]:
        if not isinstance(page, dict) or type(page.get("frame")) is not int or page["frame"] not in indices:
            raise ValueError("存在しない元フレームが指定されています")
        regions = page.get("regions")
        if not isinstance(regions, list) or not regions:
            raise ValueError("ページに指定パートの領域がありません")
        for r in regions:
            if not isinstance(r, dict):
                raise ValueError("領域は box と tab を持つ表にしてください")
            box, tab = _box(r.get("box"), "box"), _box(r.get("tab"), "tab")
            if not (box[0] <= tab[0] < tab[2] <= box[2] and box[1] <= tab[1] < tab[3] <= box[3]):
                raise ValueError("tab は box の内側にしてください")
            key = page["frame"], tuple(box)
            if key in seen:
                raise ValueError("同じフレームの同じ領域が重複しています")
            seen.add(key)
            count += 1
    if count > MAX_REGIONS:
        raise ValueError(f"段は {MAX_REGIONS} 個までにしてください")
    return data


def analyze(workdir: Path, engine: str, log, *, settings=None, on_actual=None, cancel=None) -> dict:
    picks = prepare(workdir)
    wd = workdir.resolve()
    prompt = layout_prompt(workdir, picks)
    # 初回もやり直しも元画像から選び直す。前の layout は比較のため読めるが採用済みとは扱わない。
    target = wd / "paper" / "layout.json"
    for attempt in range(2):
        with inside.open_dir(confine.root_of(workdir), "paper") as folder:
            if inside.exists(folder, "layout.json"):
                inside.remove(folder, "layout.json")
        result = run_agent(prompt, engine=engine, workdir=workdir, writable=[target], log=log,
                           label="ページとパート", settings=settings, on_actual=on_actual, cancel=cancel,
                           images=sorted((wd / "paper" / "candidates").glob("c*.jpg")))
        try:
            data = validate_layout(read_json(confine.guard(target, root=wd)), source_frames(workdir))
        except (OSError, ValueError) as e:
            if attempt:
                raise RuntimeError(f"ページとパートを選べませんでした: {e}") from None
            prompt += f"\n前回の出力は検査に通りませんでした: {e}\nJSON の形と領域を直し、もう一度書いてください。"
            continue
        with inside.open_dir(confine.root_of(workdir), "paper") as folder:
            report = result.text + "\n" + "\n".join(f"- 注意: {w}" for w in data.get("warnings", []))
            inside.write_text(folder, "notes.md", report + "\n")
        for warning in data.get("warnings", []):
            log(f"紙の楽譜の注意: {warning}")
        return data
    raise AssertionError("unreachable")


def _profile(gray: np.ndarray, x: float, width: int, slope: float) -> np.ndarray:
    h, w = gray.shape
    lo, hi = max(0, round(x - width / 2)), min(w, round(x + width / 2))
    xx = np.arange(lo, hi)
    yy = np.arange(h)[:, None] + slope * (xx - x)
    y0 = np.clip(np.floor(yy).astype(int), 0, h - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    a = np.clip(yy - np.floor(yy), 0, 1)
    v = np.mean(gray[y0, xx] * (1 - a) + gray[y1, xx] * a, axis=1)
    radius = max(6, round(h / 15))
    bg = np.convolve(np.pad(v, (radius, radius), mode="edge"), np.ones(radius * 2 + 1) / (radius * 2 + 1), "valid")
    return bg - v


def _staff(profile: np.ndarray, strings: int) -> tuple[np.ndarray, float] | None:
    peaks = find_peaks(profile, max(2, float(np.percentile(np.maximum(profile, 0), 95)) * .25))
    ys = np.array([p[0] for p in peaks])
    best = None
    for i in range(len(ys) - strings + 1):
        for j in range(i + strings - 1, len(ys)):
            s = (ys[j] - ys[i]) / (strings - 1)
            if not 2.2 <= s <= len(profile) / (strings - 1):
                continue
            ids = np.abs(ys[:, None] - (ys[i] + np.arange(strings) * s)).argmin(axis=0)
            lines = ys[ids]
            if len(set(ids)) != strings or np.max(abs(lines - (ys[i] + np.arange(strings) * s))) > max(.8, s * .18):
                continue
            score = sum(peaks[k][1] for k in ids)
            if best is None or score > best[1]:
                best = lines, score
    return best


def straighten_region(im: Image.Image, box: list, tab: list, strings: int) -> tuple[np.ndarray, list[float] | None]:
    """TAB の局所的な線を追い、縦方向の曲がりと間隔の変化を同じ段の中で補正する。

    線を複数の位置で確認できない画像は補正しない。元画像は読み手の照合用に残す。
    """
    w, h = im.size
    x0, y0, x1, y1 = [round(v * dim) for v, dim in zip(box, (w, h, w, h))]
    tx0, ty0, tx1, ty1 = [round(v * dim) for v, dim in zip(tab, (w, h, w, h))]
    crop = np.array(im.crop((x0, y0, x1, y1)))
    gray = to_gray(np.array(im))[ty0:ty1, tx0:tx1]
    if min(gray.shape) < 6 or min(crop.shape[:2]) < 2:
        return crop, None
    observations = []
    width = min(32, max(12, gray.shape[1] // 15))
    for x in np.linspace(width / 2, gray.shape[1] - width / 2, 13):
        candidates = [_staff(_profile(gray, x, width, slope), strings) for slope in np.linspace(-.2, .2, 9)]
        candidates = [c for c in candidates if c is not None]
        if candidates:
            lines, score = max(candidates, key=lambda c: c[1])
            observations.append((x + tx0 - x0, lines + ty0 - y0, score))
    if len(observations) < 5:
        return crop, None
    spacings = np.array([np.mean(np.diff(row[1])) for row in observations])
    s = float(np.median(spacings))
    observations = [row for row, spacing in zip(observations, spacings) if .8 * s <= spacing <= 1.2 * s]
    if len(observations) < 5:
        return crop, None
    xs = np.array([row[0] for row in observations])
    tops = np.array([row[1][0] for row in observations])
    spans = np.array([row[1][-1] - row[1][0] for row in observations])
    # 弦を 1 本取り違えた局所候補を、曲線からのずれで除く。
    fit = np.polyfit(xs, tops, 2)
    keep = np.abs(tops - np.polyval(fit, xs)) <= max(1.5, s * .4)
    if keep.sum() < 5:
        return crop, None
    xs, tops, spans = xs[keep], tops[keep], spans[keep]
    if np.ptp(xs) < gray.shape[1] * .55:
        return crop, None
    xx = np.arange(crop.shape[1])
    top_curve = np.polyval(np.polyfit(xs, tops, 2), np.clip(xx, xs.min(), xs.max()))
    span_curve = np.polyval(np.polyfit(xs, spans, 1), np.clip(xx, xs.min(), xs.max()))
    span = float(np.median(spans))
    ref = float(np.median(tops))
    if np.any(span_curve < span * .75) or np.any(span_curve > span * 1.25):
        return crop, None
    # 同じ段の五線と上下の記号も TAB と一緒に移す。
    yy = top_curve + (np.arange(crop.shape[0])[:, None] - ref) * span_curve / span
    low = np.floor(yy).astype(int)
    high = low + 1
    a = yy - low
    valid = (low >= 0) & (high < crop.shape[0])
    low, high = np.clip(low, 0, crop.shape[0] - 1), np.clip(high, 0, crop.shape[0] - 1)
    out = crop[low, xx] * (1 - a[..., None]) + crop[high, xx] * a[..., None]
    out[~valid] = 255
    return out.astype(np.uint8), [ref + i * span / (strings - 1) for i in range(strings)]


def _pieces(rgb: np.ndarray, lines: list[float] | None) -> list[tuple[Image.Image, list[float]]]:
    from videotab.pages import Setup, layout, render_piece

    if lines:
        setup = Setup((0, rgb.shape[0]), -1, (255, 255, 255), [lines])
        lay = layout(setup, rgb.shape[1])
        return [(render_piece(rgb, setup, lay, i), lay.lines[0]) for i in range(len(lay.pieces))]
    # 補正を確認できない画像にも数字を読むための拡大画像を作る。弦の印は推測で付けない。
    scale = min(4, 1800 / max(1, rgb.shape[1]))
    im = Image.fromarray(rgb)
    return [(im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS), [])]


def write_pages(workdir: Path, *, zoom: Frame | None = None) -> list[Path]:
    opts = selection(workdir)
    frames = source_frames(workdir)
    root = confine.root_of(workdir)
    data = validate_layout(read_json(confine.guard(workdir / "paper" / "layout.json", root=root)), frames)
    by_index = {f.index: f for f in frames}
    rows, paths = [], []
    out = workdir / "pages" / "zoom" if zoom else workdir / "pages"
    with inside.open_dir(root, inside.rel_path(root, out), remake=not zoom) as folder:
        if not zoom:
            for name in sorted(os.listdir(folder.fd)):
                if name.startswith("p") and name.endswith(".png"):
                    inside.remove(folder, name)
        number = 0
        for physical, page in enumerate(data["pages"], start=1):
            frame = by_index[page["frame"]]
            if zoom and frame.index != zoom.index:
                continue
            im = load_image(frame.path, root)
            context = f"{'f' if zoom else 'p'}{frame.index if zoom else physical:03d}_context.png"
            inside.write_image(folder, context, im)
            paths.append(out / context)
            for system, region in enumerate(page["regions"], start=1):
                number += 1
                rgb, lines = straighten_region(im, region["box"], region["tab"], opts["strings"])
                images, positions = [], []
                for piece, (picture, ys) in enumerate(_pieces(rgb, lines)):
                    prefix = f"f{frame.index:04d}_p{physical:03d}" if zoom else f"p{number:03d}"
                    name = f"{prefix}_s{system:02d}_{piece:02d}.png"
                    inside.write_image(folder, name, picture)
                    paths.append(out / name)
                    images.append(name)
                    positions.append(ys)
                rows.append({"page": number, "source_page": physical, "system": system, "pick": frame.index,
                             "frames": [frame.index, frame.index], "start": frame.time, "end": frame.time,
                             "context": context, "images": images, "lines": positions,
                             "corrected": lines is not None, "box": region["box"], "tab": region["tab"]})
        if zoom and not rows:
            # 未選択のフレームは元ページを拡大する。別ページの領域や線の印を流用しない。
            im = load_image(zoom.path, root)
            im = im.resize((im.width * 2, im.height * 2), Image.Resampling.LANCZOS)
            name = f"f{zoom.index:04d}_context.png"
            inside.write_image(folder, name, im)
            paths.append(out / name)
        if not zoom:
            result = {"source_mode": "paper", **opts, "warnings": data.get("warnings", []), "pages": rows}
            inside.write_text(folder, "pages.json", json_text(result))
            text = [f"# 紙の楽譜のページ一覧（{workdir.name}）", "",
                    f"- 指定パート: {opts['part']}（{opts['strings']} 弦）",
                    "- 時刻は撮影時刻です。演奏時刻ではないので、テンポ推定・時刻照合に使いません。",
                    "- 各行は指定パートの 1 段です。元ページにはテンポ・拍子・繰り返し・小節番号があるので必ず確認します。",
                    "- 補正なしの画像では元画像から弦を確認します。数字が読めなければ推測で埋めません。", ""]
            text += [f"- 注意: {warning.replace(chr(10), ' ')}" for warning in result["warnings"]]
            text += ["", "| ページ | 元ページ・段 | フレーム | 画像 | 元ページ画像 | 弦の線（上から） | 注意 |",
                     "|---|---|---|---|---|---|---|"]
            for row in rows:
                ys = " / ".join(", ".join(f"{y:.1f}" for y in pos) for pos in row["lines"])
                text.append(f"| {row['page']} | {row['source_page']}・{row['system']} | {row['pick']} | "
                            f"{' '.join(row['images'])} | {row['context']} | {ys} | "
                            f"{'段の曲がりを補正' if row['corrected'] else '補正なし・元画像で確認'} |")
            inside.write_text(folder, "index.md", "\n".join(text) + "\n")
    return paths
