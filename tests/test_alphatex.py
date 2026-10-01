from fractions import Fraction

import pytest

from videotab.alphatex import ParseError, check_bars, parse_bar


def length(tex: str) -> Fraction:
    return parse_bar(tex)[0].length()


@pytest.mark.parametrize(
    ("tex", "want"),
    [
        ("r.1", 4),
        ("(5.5 0.6).8 " * 8, 4),
        ("(7.3).4 {d} (7.3).8 (5.3).2", 4),
        ("(1.1).8 {tu 3} (2.1).8 {tu 3} (3.1).8 {tu 3} (4.1).4 (5.1).2", 4),
        ("(3.3).16 {gr} (5.3).4 (5.3).4 (5.3).2", 4),  # 装飾音は長さに数えない
        ("(0.6).8 *8", 4),
        (":8 (0.6) (0.6) (0.6) (0.6) (0.6) (0.6) (0.6) (0.6)", 4),
        ("7.3.8 7.3.8 7.3.4 r.2", 4),
        ("(12.1{b (0 4)}).4 (x.4 x.5).4 (-.1{b (4 0)}).2", 4),
        ('(2.2 2.3 4.4).2 {ch "Bb"} (0.6{pm}).4 (0.6).4', 4),
        ("(1.1).16 {tu 5} " * 5 + "(1.1).4 (1.1).2", 4),
    ],
)
def test_bar_length(tex, want):
    assert length(tex) == want


def test_metadata_is_read_and_beats_follow():
    bar, _ = parse_bar("\\tempo 174 \\ro \\rc 7 (0.6{pm}).8 (0.6).8 (0.6).4 r.2")
    assert bar.tempo == 174
    assert bar.repeat_open and bar.repeat_close == 7
    assert len(bar.beats) == 4
    bar, _ = parse_bar("\\ae (1 2) r.1")
    assert bar.alternate_endings == {1, 2}
    bar, _ = parse_bar("\\ts 3 4 r.2 {d}")
    assert bar.time_signature == (3, 4)


def test_metadata_after_beats_is_an_error():
    with pytest.raises(ParseError):
        parse_bar("(0.6).2 \\ro (0.6).2")


def test_default_duration_carries_to_next_bar():
    _, dur = parse_bar(":16 (0.6) (0.6)")
    bar, _ = parse_bar("(0.6) " * 16, dur)
    assert bar.length() == 4


def test_check_reports_length_ties_and_notation():
    bars = {
        1: "(5.5 0.6).8 " * 7,  # 1 拍足りない
        2: "(-.5 -.6).4 (7.4).4 (7.4).2",  # 前の小節の 5・6 弦からのタイ（正しい）
        3: "(-.3).4 (7.4).4 (7.4).2",  # 3 弦の音は前にない
        4: "7.3.4 (7.9).4 r.2",  # 括弧なし・9 弦
        5: "(0.6).2 (0.6).3",
        6: "(1.1 2.1).1",
    }
    issues, _ = check_bars(bars)
    by_bar = {}
    for i in issues:
        by_bar.setdefault(i.bar, []).append((i.level, i.message))
    assert by_bar[1] == [("error", "長さが 7/2 拍（4/4 なら 4 拍）")]
    assert 2 not in by_bar
    assert ("error", "3 弦のタイのつながり先（直前の拍の同じ弦の音）がありません") in by_bar[3]
    assert ("warning", "括弧のない単音があります（(7.3).8 のように括弧で囲む）") in by_bar[4]
    assert ("error", "9 弦はありません（1〜6）") in by_bar[4]
    assert ("error", "長さ .3 は使えません") in by_bar[5]
    assert ("error", "同じ拍に 1 弦が 2 回あります") in by_bar[6]


def test_check_follows_time_signature_changes():
    bars = {1: "r.1", 2: "\\ts 3 4 r.2 {d}", 3: "r.2 {d}", 4: "\\ts 4 4 r.1"}
    issues, _ = check_bars(bars)
    assert issues == []


def test_check_reports_unreadable_bar_and_rest_before_tie():
    issues, _ = check_bars({1: "(0.6).4 r.4 r.2", 2: "(-.6).1", 3: "(7.3.8"})
    msgs = [(i.bar, i.level) for i in issues]
    assert (2, "error") in msgs  # 休符のあとのタイ
    assert (3, "error") in msgs  # 読めない


def test_full_bar_rest_fits_any_time_signature():
    issues, parsed = check_bars({1: "\\ts 3 4 r.1", 2: "r.2 {d}", 3: "\\ts 6 8 r.1"})
    assert issues == []
    assert parsed[1].length((3, 4)) == 3


@pytest.mark.parametrize(
    "tex", ["\\tempo r.1", "\\ts 3 r.1", "\\rc r.1", "\\rc (0.6).1", "\\ae r.1", "\\ts 3 5 r.1"]
)
def test_metadata_without_required_values_is_an_error(tex):
    with pytest.raises(ParseError):
        parse_bar(tex)


def test_tie_at_start_of_a_part_file_is_not_checked():
    issues, _ = check_bars({20: "(-.6).2 (0.6).2", 21: "(-.6).1"})
    assert issues == []
    # 1 小節目のタイは前がないので誤り
    issues, _ = check_bars({1: "(-.6).1"})
    assert [i.level for i in issues] == ["error"]
