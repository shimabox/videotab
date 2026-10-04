"""繰り返し・テンポ変化を展開して、各小節が鳴り始める時刻を出す。"""

from __future__ import annotations

from dataclasses import dataclass, field

from videotab.alphatex import Bar


@dataclass
class Schedule:
    order: list[int]  # 演奏順に並べた小節番号
    starts: dict[int, list[float]] = field(default_factory=dict)  # 小節番号 → 鳴り始める秒（通るたび）
    total: float = 0.0


def play_order(bars: dict[int, Bar], limit: int = 100_000) -> list[int]:
    """\\ro / \\rc N / \\ae を展開した演奏順。\\rc N は N 回演奏（N-1 回戻る）。

    N 番かっこ（\\ae）は、今が何回目かに含まれない小節を飛ばす。1 番かっこの小節に
    \\rc があれば、2 回目はそこを飛ばして 2 番かっこへ進む。
    """
    numbers = sorted(bars)
    order: list[int] = []
    i = 0
    start = 0
    passes = 1
    returning = False
    counts: dict[int, int] = {}
    while i < len(numbers):
        if len(order) > limit:
            raise ValueError("繰り返しの展開が終わりません（\\ro / \\rc を確かめてください）")
        bar = bars[numbers[i]]
        if bar.repeat_open and not (returning and i == start):
            start, passes, counts = i, 1, {}
        returning = False
        endings = bar.alternate_endings
        if endings is not None and passes not in endings:
            i += 1
            continue
        order.append(numbers[i])
        if bar.repeat_close:
            counts[i] = counts.get(i, 0) + 1
            if counts[i] < bar.repeat_close:
                i, passes, returning = start, passes + 1, True
                continue
        i += 1
    return order


def schedule(bars: dict[int, Bar], tempo: float, time_signature: tuple[int, int] = (4, 4)) -> Schedule:
    """演奏順に小節を並べ、テンポ変化（小節頭の \\tempo と拍の {tempo N}）を追って時刻を出す。"""
    order = play_order(bars)
    sched = Schedule(order)
    # 拍子は楽譜の並び順で決まる（繰り返しで戻っても、その小節の拍子は変わらない）
    signatures: dict[int, tuple[int, int]] = {}
    ts = time_signature
    for n in sorted(bars):
        ts = bars[n].time_signature or ts
        signatures[n] = ts
    first = min(bars) if bars else None
    t = 0.0
    bpm = tempo
    for n in order:
        bar = bars[n]
        if bar.tempo:
            bpm = bar.tempo
        elif n == first:
            bpm = tempo  # 見出しのテンポは 1 小節目に付くので、曲頭へ戻ると戻る
        sched.starts.setdefault(n, []).append(round(t, 3))
        if bar.is_full_bar_rest:
            # 全休符の拍に付いたテンポも、小節の頭から効く
            bpm = bar.beats[0].tempo or bpm
            t += float(bar.length(signatures[n])) * 60.0 / bpm
            continue
        for beat in bar.beats:
            bpm = beat.tempo or bpm
            t += float(beat.length()) * 60.0 / bpm
    sched.total = t
    return sched

