import pytest

from videotab.alphatex import check_bars
from videotab.timing import play_order, schedule


def parsed(bars: dict[int, str]):
    return check_bars(bars)[1]


def test_simple_repeat_plays_n_times():
    bars = {1: "r.1", 2: "\\ro r.1", 3: "\\rc 3 r.1", 4: "r.1"}
    assert play_order(parsed(bars)) == [1, 2, 3, 2, 3, 2, 3, 4]


def test_single_bar_repeat():
    bars = {1: "\\ro \\rc 4 r.1", 2: "r.1"}
    assert play_order(parsed(bars)) == [1, 1, 1, 1, 2]


def test_alternate_endings():
    bars = {1: "\\ro r.1", 2: "r.1", 3: "\\rc 2 \\ae 1 r.1", 4: "\\ae 2 r.1", 5: "r.1"}
    assert play_order(parsed(bars)) == [1, 2, 3, 1, 2, 4, 5]


def test_close_without_open_repeats_from_start():
    bars = {1: "r.1", 2: "\\rc 2 r.1", 3: "r.1"}
    assert play_order(parsed(bars)) == [1, 2, 1, 2, 3]


def test_consecutive_repeats():
    bars = {1: "\\ro r.1", 2: "\\rc 2 r.1", 3: "\\ro r.1", 4: "\\rc 2 r.1"}
    assert play_order(parsed(bars)) == [1, 2, 1, 2, 3, 4, 3, 4]


def test_schedule_with_tempo_changes():
    # 120BPM で 1 小節 2 秒。2 小節目の頭で 60BPM（4 秒）、3 小節目の 3 拍目で 120BPM に戻る
    # （3 小節目は 2 秒 + 1 秒）。繰り返しの 2 周目の 1 小節目は 120BPM のまま
    bars = {1: "r.1", 2: "\\tempo 60 r.1", 3: "r.2 r.2 {tempo 120}", 4: "\\rc 2 r.1"}
    sched = schedule(parsed(bars), tempo=120)
    assert sched.order == [1, 2, 3, 4, 1, 2, 3, 4]
    assert sched.starts[1] == [0.0, 11.0]
    assert sched.starts[2] == [2.0, 13.0]
    assert sched.starts[3] == [6.0, 17.0]
    assert sched.starts[4] == [9.0, 20.0]
    assert sched.total == pytest.approx(22.0)


def test_repeat_to_the_top_restores_the_initial_tempo():
    bars = {1: "\\ro r.1", 2: "\\tempo 60 \\rc 2 r.1"}
    sched = schedule(parsed(bars), tempo=120)
    assert sched.starts[1] == [0.0, 6.0]
    assert sched.total == pytest.approx(12.0)


def test_full_bar_rest_takes_the_time_signature_length():
    bars = {1: "\\ts 3 4 r.1", 2: "r.2 {d}"}
    sched = schedule(parsed(bars), tempo=60)
    assert sched.starts[2] == [3.0]
    assert sched.total == pytest.approx(6.0)
