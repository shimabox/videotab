import json
from fractions import Fraction
from pathlib import Path

import pytest

import videotab
from videotab.alphatex import (
    BEAT_EFFECT_ARGS,
    LIST_NO_PAREN,
    NOTE_EFFECT_ARGS,
    NOTE_ONLY_EFFECTS,
    OPTIONAL,
    REQUIRED,
    REQUIRED_FLOAT,
    VALUE_LIST,
    ParseError,
    check_bars,
    move_note_effects,
    parse_bar,
)

ALPHATAB = Path(videotab.__file__).parent / "templates" / "vendor" / "alphatab.min.js"


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


def test_beat_repeat_is_limited_and_huge_numbers_do_not_crash_the_check():
    assert len(parse_bar("r.16 *16")[0].beats) == 16
    for tex in ("r.4 *100000", "r.4 *0", "r.4 *" + "9" * 5000):
        with pytest.raises(ParseError, match="拍の繰り返し"):
            parse_bar(tex)
    # 桁が多すぎて数字にできない値も、例外で止まらず「読めません」になる
    issues, parsed = check_bars({1: "r.4 *100000", 2: "r." + "9" * 5000, 3: "r.1"})
    assert [(i.bar, i.level) for i in issues if i.level == "error"] == [(1, "error"), (2, "error")]
    assert sorted(parsed) == [3]


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


def test_check_warns_note_effect_after_beat():
    # 音の効果を拍の後ろに書くと alphaTab が読めないので注意を出す（誤りにはしない）
    issues, _ = check_bars({1: "(7.5).8 {pm} (7.5).8 {pm tempo 143} (7.5).4 {b (0 4)} r.4 {lr} (7.5).4"})
    assert all(i.level == "warning" for i in issues)
    messages = [i.message for i in issues]
    assert "{pm} は音の効果です。(7.5{pm}).8 のように音の中に書きます" in messages
    assert "{b} は音の効果です。(7.5{b (0 4)}).8 のように音の中に書きます" in messages
    assert "{lr} は音の効果です。休符には付けられません（組み立てでは外します）" in messages
    assert len(messages) == 3  # 同じ小節の {pm} は 1 つにまとめ、{tempo} には出さない


def test_check_accepts_beat_effects_and_warns_unknown():
    # 拍の効果として正しいものには注意を出さない。音に書いた拍の効果も alphaTab は受け付ける
    bar = ('\\tempo 90 (7.5{pm}).8 {tempo 143} (7.5).8 {d} (7.5).16 (7.5).8 {tu 3} (7.5).8 {tu 3} '
           '(7.5).8 {tu 3} (3.3).16 {gr} (0.6{ch "Am"}).4 {ch "Am"} (0.6{h}).8 {fermata medium}')
    issues, _ = check_bars({1: bar})
    assert issues == []
    # 識別子の引数（tempo の hide、dy の強弱、txt の文字）は効果の名前と取り違えない
    bar = '(7.5).4 {tempo 143 hide} (7.5).4 {dy rf} (7.5).4 {tempo 120 "Slow" hide} (7.5{b (0 4)}).4 {txt hide}'
    issues, _ = check_bars({1: bar})
    assert issues == []
    # 拍の効果としても音の効果としても知らないもの
    issues, _ = check_bars({1: "(7.5).1 {zz}", 2: "(7.5{zz}).1"})
    assert [(i.bar, i.message) for i in issues] == [(1, "知らない拍の効果 {zz}"), (2, "知らない音の効果 {zz}")]


def test_check_warns_effect_arguments_alphatab_cannot_read():
    issues, _ = check_bars({1: "(7.5).2 {fermata} (7.5{lf 1}).2 {dy}", 2: "(7.5{tr}).1"})
    assert [(i.bar, i.message) for i in issues] == [
        (1, "{fermata} の引数の書き方が alphaTab の決まりに合いません"),
        (1, "{dy} の引数の書き方が alphaTab の決まりに合いません"),
        (2, "{tr} の引数の書き方が alphaTab の決まりに合いません"),
    ]
    # 読めない並びがある拍の音の効果は、組み立てで直さないことを伝える
    issues, _ = check_bars({1: "(7.5).1 {pm zz}"})
    assert "{pm} は音の効果です。(7.5{pm}).8 のように音の中に書きます（{ } の中に読めない書き方があるため、組み立てでは直しません）" in [
        i.message for i in issues
    ]


def _vendor_signatures(name: str) -> dict:
    js = ALPHATAB.read_text(encoding="utf-8")
    start = js.index(f"static {name}=t.Zt(") + len(f"static {name}=t.Zt(")
    table, _ = json.JSONDecoder().raw_decode(js, start)
    types = {16: "n", 17: "s", 10: "i"}
    return {
        prop: None
        if overloads is None
        else [
            [(frozenset(types[t] for t in p[0]), p[1], frozenset(p[2]) if len(p) > 2 else None) for p in params]
            for params in overloads
        ]
        for prop, overloads in table
    }


def _our_signatures(table: dict) -> dict:
    return {
        prop: None
        if overloads is None
        else [
            [(frozenset(t), mode, None if a is None else frozenset(a)) for t, mode, a in params]
            for params in overloads
        ]
        for prop, overloads in table.items()
    }


@pytest.mark.parametrize(
    ("name", "table"), [("beatProperties", BEAT_EFFECT_ARGS), ("noteProperties", NOTE_EFFECT_ARGS)]
)
def test_effect_arguments_match_bundled_alphatab(name, table):
    # 効果の名前と引数の決まりは、同梱の alphaTab の表と同じ
    assert _our_signatures(table) == _vendor_signatures(name)


@pytest.mark.parametrize(
    ("tex", "want"),
    [
        ("(7.5).8 {pm tempo 143}", "(7.5{pm}).8 {tempo 143}"),
        ("(5.5 0.6).8 {pm}", "(5.5{pm} 0.6{pm}).8"),
        ("(7.5).8 {pm} (7.5).8 {pm}", "(7.5{pm}).8 (7.5{pm}).8"),
        # すでに付いている音には重ねず、ほかの音の効果のあとに足す
        ("(5.5{pm} 0.6{h}).8 {pm}", "(5.5{pm} 0.6{h pm}).8"),
        # 末尾に足すと前の効果の引数に読まれる（tempo の hide）ときは先頭に足す
        ("(7.5{tempo 143}).1 {hide}", "(7.5{hide tempo 143}).1"),
        ("(7.5{tempo 143} 0.6).1 {hide pm}", "(7.5{hide pm tempo 143} 0.6{hide pm}).1"),
        ("(7.5{gr}).4 {b (0 4)}", "(7.5{b (0 4) gr}).4"),
        # 休符の拍に付いたものは外す
        ("r.8 {pm} (7.5).8 {lr d}", "r.8 (7.5{lr}).8 {d}"),
        # 引数のある効果は引数ごと移す。拍の効果（tu・ch・gr）は拍に残す
        ("(7.3).4 {tu 3 b (0 4)}", "(7.3{b (0 4)}).4 {tu 3}"),
        ('(0.6 2.5).2 {ch "E5" pm}', '(0.6{pm} 2.5{pm}).2 {ch "E5"}'),
        ("(3.3).16 {gr sl}", "(3.3{sl}).16 {gr}"),
        # 拍の効果の識別子の引数（hide・強弱・文字）は拍に残し、そのあとの音の効果だけ移す
        ("(7.5).8 {tempo 143 pm}", "(7.5{pm}).8 {tempo 143}"),
        ('(7.5).8 {tempo 143 "Slow" hide pm}', '(7.5{pm}).8 {tempo 143 "Slow" hide}'),
        ("(7.5).8 {pm dy rf}", "(7.5{pm}).8 {dy rf}"),
        ("(7.5).8 {txt hide lr}", "(7.5{lr}).8 {txt hide}"),
        # 拍に残す効果は、同じ名前が何度あってもすべて、書いた順のまま残す
        ('(7.5).1 {lyrics 0 "a" lyrics 1 "b" pm}', '(7.5{pm}).1 {lyrics 0 "a" lyrics 1 "b"}'),
        ('(7.5).1 {lyrics 1 "b" pm lyrics 0 "a"}', '(7.5{pm}).1 {lyrics 1 "b" lyrics 0 "a"}'),
        ("(7.5).4 {pm tu 3 lr d}", "(7.5{pm lr}).4 {tu 3 d}"),
        ("(7.5).4 { d  pm  tu 3 }", "(7.5{pm}).4 { d  tu 3 }"),
        # 拍に同じ音の効果が何度あっても、音には重ねず最後の指定だけを足す
        ("(7.5).4 {pm tu 3 pm}", "(7.5{pm}).4 {tu 3}"),
        ("(7.5).4 {b (0 4) d b (0 2)}", "(7.5{b (0 2)}).4 {d}"),
        # 音の効果の引数は、音の効果の決まりで読む
        ("(7.5).4 {b (0 4) dy f}", "(7.5{b (0 4)}).4 {dy f}"),
        ("(7.5).4 {tr 7 16 d}", "(7.5{tr 7 16}).4 {d}"),
        ("(7.5).4 {b bend (0 4)}", "(7.5{b bend (0 4)}).4"),
        # 拍の効果と引数だけなら変えない
        ("(7.5).1 {tempo 143 hide}", "(7.5).1 {tempo 143 hide}"),
        ("(7.5).1 {dy rf}", "(7.5).1 {dy rf}"),
        # 引数の決まりに合わない・知らない名前がある並びは、付け直さずに残す
        ("(7.5).8 {pm zz}", "(7.5).8 {pm zz}"),
        ("(7.5).8 {dy pm}", "(7.5).8 {dy pm}"),
        ("(7.5).8 {pm 3}", "(7.5).8 {pm 3}"),
        ("(7.5).8 {gr b (0 4)}", "(7.5).8 {gr b (0 4)}"),
        # *N で繰り返す拍・括弧のない単音
        ("(x.4 x.5).16 {pm} *4", "(x.4{pm} x.5{pm}).16 *4"),
        ("7.5.8 {pm}", "7.5{pm}.8"),
        # 正しく書いたもの・読めないものはそのまま
        ("\\ro (7.5{pm}).8 {tempo 143} (7.5).8", "\\ro (7.5{pm}).8 {tempo 143} (7.5).8"),
        ("(7.5).8 {pm", "(7.5).8 {pm"),
    ],
)
def test_move_note_effects(tex, want):
    out, _ = move_note_effects({1: tex})
    assert out == {1: want}


def test_moved_effect_does_not_become_argument_of_note_effect():
    out, _ = move_note_effects({1: "(7.5{tempo 143}).1 {hide}"})
    note = parse_bar(out[1])[0].beats[0].notes[0]
    assert note.effects == {"tempo": [143], "hide": []}
    assert note.unread == set()


def _sample_arg(param) -> str:
    types, mode, allowed = param
    if mode in (VALUE_LIST, LIST_NO_PAREN):
        return "(0 4)"
    if allowed:
        return allowed[0] if "i" in types or "n" in types else f'"{allowed[0]}"'
    return {"n": "1", "s": '"a"', "i": "a"}[types[0]]


def _effect_forms(table: dict, required_only: bool) -> list[str]:
    """表の効果ごとに、書き方の候補の引数を埋めた書き方。required_only なら省略できる引数を書かない。"""
    forms = []
    for name, signatures in table.items():
        for params in signatures or [[]]:
            args = [
                _sample_arg(p) for p in params if not required_only or p[1] in (REQUIRED, REQUIRED_FLOAT)
            ]
            forms.append(" ".join([name, *args]))
    return forms


def test_moved_effects_keep_argument_boundaries_for_all_effects():
    # 音に付いている効果（省略できる引数を書いていないもの）と、拍の後ろの音の効果のすべての組で、
    # 付け直した結果が意図どおりの効果と引数に読めるか、付け直さずに残すかのどちらかになる。
    # 拍に残す効果は、同じ名前の重なりも含めて書いた順のまま残る
    existing = sorted(set(_effect_forms(NOTE_EFFECT_ARGS, True) + _effect_forms(BEAT_EFFECT_ARGS, True)))
    moving = [f for f in _effect_forms(NOTE_EFFECT_ARGS, True) if f.split()[0] in NOTE_ONLY_EFFECTS]
    moved = 0
    for e in existing:
        before = parse_bar(f"(7.5{{{e}}}).4")[0].beats[0].notes[0]
        if before.unread:
            continue
        for m in moving:
            for beat_fx in [m, f'lyrics 0 "a" {m} lyrics 1 "b"']:
                tex = f"(7.5{{{e}}}).4 {{{beat_fx}}}"
                beat = parse_bar(tex)[0].beats[0]
                name = m.split()[0]
                if beat.unread or name in before.effects:
                    continue
                out, _ = move_note_effects({1: tex})
                if out[1] == tex:
                    continue
                moved += 1
                after = parse_bar(out[1])[0].beats[0]
                kept = [(n, a) for n, a, _ in beat.effect_items if n not in NOTE_ONLY_EFFECTS]
                assert [(n, a) for n, a, _ in after.effect_items] == kept, tex
                assert after.notes[0].effects == {**before.effects, name: beat.effects[name]}, tex
                assert after.notes[0].unread == set(), tex
                current = [(n, a) for n, a, _ in before.effect_items]
                added = [(name, beat.effects[name])]
                assert [(n, a) for n, a, _ in after.notes[0].effect_items] in (current + added, added + current), tex
    assert moved > 2000
    assert "(7.5{hide tempo 1}).4" == move_note_effects({1: "(7.5{tempo 1}).4 {hide}"})[0][1]


def test_move_note_effects_leaves_beat_when_no_order_keeps_boundaries(monkeypatch):
    # 末尾でも先頭でも境界が崩れるなら、その拍は付け直さずに残し、検査でそのことを知らせる
    monkeypatch.setitem(NOTE_EFFECT_ARGS, "hide", [[("i", OPTIONAL, ("tempo",))]])
    tex = "(7.5{tempo 143}).1 {hide}"
    out, count = move_note_effects({1: tex})
    assert out == {1: tex}
    assert (count.moved, count.dropped) == (0, 0)
    issues, _ = check_bars({1: tex})
    assert [i.message for i in issues] == [
        "{hide} は音の効果です。(7.5{hide}).8 のように音の中に書きます"
        "（音の中の効果と合わせると引数の区切りが変わるため、組み立てでは直しません）"
    ]


def test_move_note_effects_counts_and_keeps_input():
    first = "(7.5).8 {pm tempo 143} (5.5 0.6).8 {pm} r.8 {pm} (7.5).8 {d} (7.5).16 (7.5).4 {d}"
    bars = {1: first, 2: "(0.6).1"}
    out, count = move_note_effects(bars)
    assert (count.moved, count.dropped) == (2, 1)
    assert out[2] == "(0.6).1"
    assert bars[1] == first
    # 付け直した小節は長さが変わらず、検査の指摘も出ない
    assert check_bars(out)[0] == []
