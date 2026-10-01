"""verify: 繰り返し・テンポを展開した各小節の開始時刻を出し、動画で見た時刻と比べる。

動画で見た時刻（marks）は「この小節がこの秒に鳴り始めた」の組。marks.json（[[小節, 秒], ...]）
か --mark 小節=秒 で渡す。動画の頭には前奏やタブの出ない時間があるので、ずれ（オフセット）は
いちばん多くの印が許容内に収まる値を選び、その上で 1 つずつの差を見る。繰り返しで同じ小節を
何度も通るときは、見た時刻にいちばん近い回と比べる。
"""

from __future__ import annotations

import statistics
from pathlib import Path

from videotab import confine
from videotab.alphatex import check_bars
from videotab.build import load_score, merge
from videotab.timing import schedule
from videotab.workdir import fmt_time, list_frames, load_meta, read_json, write_json


def parse_marks(workdir: Path, cli_marks: list[str]) -> list[tuple[int, float]]:
    marks: list[tuple[int, float]] = []
    path = workdir / "marks.json"
    if path.exists():
        data = read_json(path)
        items = data.items() if isinstance(data, dict) else data
        marks += [(int(b), float(t)) for b, t in items]
    for m in cli_marks:
        bar, _, sec = m.partition("=")
        marks.append((int(bar), _seconds(sec)))
    return sorted(marks, key=lambda m: m[1])


def _seconds(text: str) -> float:
    """"83.5" / "1:23.5" のどちらでも秒にする。"""
    if ":" in text:
        m, s = text.split(":", 1)
        return int(m) * 60 + float(s)
    return float(text)


def fit_offset(marks: list[tuple[int, float]], starts: dict[int, list[float]], tolerance: float) -> float:
    candidates = [t - s for bar, t in marks for s in starts.get(bar, [])]
    if not candidates:
        return 0.0

    def inliers(off: float) -> list[float]:
        """ずれを off としたとき許容内に収まる印の、（見た時刻 − 計算上の時刻）。"""
        out = []
        for bar, t in marks:
            if starts.get(bar):
                s = min(starts[bar], key=lambda s: abs(t - off - s))  # いちばん近い回
                if abs(t - off - s) <= tolerance:
                    out.append(t - s)
        return out

    best = max(candidates, key=lambda off: len(inliers(off)))
    # 許容内の印の中央値で整える
    return statistics.median(inliers(best))


def video_length(workdir: Path) -> float | None:
    meta = load_meta(workdir)
    if meta.get("duration"):
        return float(meta["duration"])
    frames = list_frames(workdir)
    if len(frames) >= 2:
        return frames[-1].time + (frames[-1].time - frames[-2].time)
    return None


def run_verify(workdir: Path, marks: list[str] | None = None, tolerance: float = 2.0) -> int:
    merged = merge(workdir)
    if merged.conflicts or merged.missing:
        print("食い違いか抜けがあります（先に videotab build を通してください）。今ある小節だけで計算します。")
    score = load_score(workdir)
    if not score.get("tempo"):
        raise SystemExit(f"{workdir / 'score.json'} に tempo を書いてください")
    issues, parsed = check_bars(merged.bars, tuple(score["time_signature"]), len(score["tuning"].split()))
    errors = [i for i in issues if i.level == "error"]
    if errors:
        print(f"検査の誤りが {len(errors)} 件あります（時刻は誤りのある小節もそのまま数えて計算します）:")
        for i in errors:
            print(f"  {i}")
    sched = schedule(parsed, float(score["tempo"]), tuple(score["time_signature"]))
    mark_list = parse_marks(workdir, marks or [])
    offset = fit_offset(mark_list, sched.starts, tolerance) if mark_list else 0.0

    repeats = len(sched.order) - len(set(sched.order))
    print(f"演奏順 {len(sched.order)} 小節（繰り返しで {repeats} 小節ぶん増えた）、演奏時間 {sched.total:.1f} 秒（{fmt_time(sched.total)}）")
    length = video_length(workdir)
    if mark_list:
        print(f"動画のずれ: 1 小節目が {offset:.1f} 秒から（印 {len(mark_list)} 個から推定）")
        if length:
            print(f"曲の終わり: 計算 {offset + sched.total:.1f} 秒 / 動画の長さ {length:.1f} 秒")
    elif length:
        print(f"動画の長さ {length:.1f} 秒（印がないので、ずれは 0 として比べていません）")

    bad = 0
    rows = []
    for bar, t in mark_list:
        if not sched.starts.get(bar):
            print(f"  {bar} 小節: 演奏順にありません")
            bad += 1
            continue
        calc = min(sched.starts[bar], key=lambda s: abs(t - offset - s)) + offset
        diff = t - calc
        ok = abs(diff) <= tolerance
        bad += 0 if ok else 1
        rows.append((bar, t, calc, diff, ok))
    if rows:
        print("  小節 | 動画 | 計算 | 差")
        for bar, t, calc, diff, ok in rows:
            print(f"  {bar:>4} | {fmt_time(t)} | {fmt_time(calc)} | {diff:+.1f}{'' if ok else '  ← 許容 ' + str(tolerance) + ' 秒を超える'}")

    write_json(
        workdir / "timing.json",
        {
            "offset": round(offset, 3),
            "total": round(sched.total, 3),
            "order": sched.order,
            "starts": {str(n): [round(s + offset, 3) for s in v] for n, v in sorted(sched.starts.items())},
        },
        root=confine.root_of(workdir),
    )
    print(f"各小節の時刻: {workdir / 'timing.json'}")
    return 1 if bad or errors or merged.conflicts or merged.missing else 0
