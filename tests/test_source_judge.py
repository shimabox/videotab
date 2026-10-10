"""画面のタブ譜か、紙を撮った動画かを、帯と線の検出結果から見分ける。検出の結果は手で組む。"""

import pytest
from fakes import make_work

from videotab import cli, strip
from videotab.workdir import list_frames, load_meta, save_meta


def detected(with_tab, used=24, located=None, total=100):
    """検出の結果。located を渡すと、カメラ撮影の補正を採用した結果（total 枚のうち located 枚で線が取れた）。"""
    corrections = [] if located is None else [(i, 0.0, 0.0, i <= located, 1.0) for i in range(1, total + 1)]
    staff = strip.Staff([276.0 + 7 * n for n in range(6)], 60.0)
    found = {row[0] for row in corrections if row[3]}
    return strip.StripResult(-1, [staff], (240, 350), (252, 252, 252), used, with_tab,
                             corrections=corrections, located=found)  # fmt: skip


PAPER_LIKE = dict(with_tab=7, located=25, total=97)  # 紙を撮った動画に見える結果
PAPER_CHOICE = {"mode": "paper", "by": "auto", "tab_frames": [7, 24], "located_frames": [25, 97]}
STRIP_META = {"band": [240, 350], "polarity": -1, "background": [252, 252, 252],
              "staves": [[276.0, 283.0, 290.0, 297.0, 304.0, 311.0]]}  # fmt: skip


# --- 判定


@pytest.mark.parametrize("result, paper", [
    (detected(7), False),  # 補正を採用していなければ、タブが見えたフレームが少なくても画面
    (detected(0), False),
    (detected(7, located=25, total=97), True),
    (detected(2, located=0), True),
    (detected(8, located=26), True),  # 8/24 はちょうど 1/3
    (detected(9, located=26), False),
    (detected(7, located=50), True),  # ちょうど 1/2
    (detected(7, located=51), False),
    (detected(23, located=97), False),
    (detected(21, located=10), False),
])  # fmt: skip
def test_paper_needs_correction_and_few_frames_with_tab_and_few_located_frames(result, paper):
    assert strip.looks_like_paper(result) is paper


def test_thresholds_and_exit_code_do_not_collide():
    from videotab import pipeline

    assert (strip.PAPER_TAB_RATIO, strip.PAPER_LOCATED_RATIO, strip.UNSURE_TAB_RATIO) == (1 / 3, 1 / 2, 2 / 3)
    assert strip.PAPER_EXIT == 5 and len({1, strip.NO_TAB_EXIT, pipeline.PART_WAIT_EXIT, strip.PAPER_EXIT}) == 4


def test_screen_note_always_has_counts_and_adds_the_way_to_paper_when_unsure():
    assert strip.source_note(detected(22)) == "画面のタブ譜として検出（タブが見えたフレーム 22/24）"
    assert strip.source_note(detected(16)) == "画面のタブ譜として検出（タブが見えたフレーム 16/24）"  # ちょうど 2/3
    unsure = strip.source_note(detected(15))
    assert unsure.startswith("画面のタブ譜として検出（タブが見えたフレーム 15/24）")
    assert "紙を撮った動画なら" in unsure and "やり直す" in unsure and "楽譜の種類を紙の楽譜に" in unsure
    assert strip.screen_note([22, 24]) == strip.source_note(detected(22))
    # meta.json は読み手も書けるので、形の違う値からは文を作らない
    for bad in (None, [22], [22, "24"], [True, 24], [25, 24], [-1, 24], "22/24", {"a": 1}):
        assert strip.screen_note(bad) is None, bad


def test_judged_record_has_counts_and_located_frames_only_with_correction():
    assert strip.judged_source(detected(22), "video") == {"mode": "video", "by": "auto", "tab_frames": [22, 24]}
    assert strip.judged_source(detected(**PAPER_LIKE), "paper") == PAPER_CHOICE
    assert strip.paper_counts(PAPER_CHOICE) == "タブが見えたフレーム 7/24、補正で線が取れたフレーム 25/97"
    for bad in (None, [], {"tab_frames": [7, 24]}, {**PAPER_CHOICE, "located_frames": "25/97"}):
        assert strip.paper_counts(bad) is None, bad


# --- videotab strip


@pytest.fixture
def work(tmp_path, monkeypatch):
    """切り出し済みの動画の作業フォルダと、strip.detect の代わりに返す結果を決める関数。"""
    wd = make_work(tmp_path, n_pages=1)
    calls = []

    def use(result):
        def detect(frames, n_samples=24, band=None):
            calls.append(band)
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(strip, "detect", detect)
        return calls

    return wd, use


def test_strip_judged_as_paper_becomes_pending_paper_and_exits_with_paper_code(work, capsys):
    wd, use = work
    use(detected(**PAPER_LIKE))
    before = load_meta(wd)
    save_meta(wd, {**before, "strip": {"band": [0, 10]}})  # 前に画面のタブ譜として検出した結果
    (wd / "strip").mkdir()
    (wd / "strip" / "check_0001.png").write_bytes(b"old")
    assert cli.main(["strip", str(wd)]) == strip.PAPER_EXIT == 5
    assert load_meta(wd) == {**before, "paper": {"part": None, "strings": None}, "source_choice": PAPER_CHOICE}
    assert not (wd / "strip").exists() and len(list_frames(wd)) == 3
    out = capsys.readouterr().out
    assert "紙を撮った動画と判断しました（タブが見えたフレーム 7/24、補正で線が取れたフレーム 25/97）" in out
    assert f"videotab run {wd.name} --root {wd.parent.resolve()} --step strip（パートの洗い出し）" in out
    assert "--step strip --screen" in out and "videotab pages" not in out


@pytest.mark.parametrize("result, choice", [
    (detected(22), {"mode": "video", "by": "auto", "tab_frames": [22, 24]}),
    (detected(22, located=3, total=3), {"mode": "video", "by": "auto", "tab_frames": [22, 24], "located_frames": [3, 3]}),
    (detected(7), {"mode": "video", "by": "auto", "tab_frames": [7, 24]}),  # 補正なしは、少なくても画面
])  # fmt: skip
def test_strip_judged_as_screen_only_adds_the_record(work, capsys, result, choice):
    wd, use = work
    use(result)
    before = load_meta(wd)  # source_choice の無い、これまでの形
    assert "source_choice" not in before and "paper" not in before
    assert cli.main(["strip", str(wd)]) == 0
    meta = load_meta(wd)
    expected = dict(STRIP_META, corrections=[list(row) for row in result.corrections]) if result.corrections else STRIP_META
    assert meta == {**before, "strip": expected, "source_choice": choice}
    assert sorted(p.name for p in (wd / "strip").iterdir()) == [f"check_{n:04d}.png" for n in (1, 2, 3)]
    out = capsys.readouterr().out
    assert strip.source_note(result) in out and f"次: videotab pages {wd}" in out


@pytest.mark.parametrize("case", ["band", "by-user"])
def test_strip_does_not_judge_when_user_decided(work, capsys, case):
    wd, use = work
    calls = use(detected(**PAPER_LIKE))
    before = load_meta(wd)
    if case == "band":
        save_meta(wd, {**before, "source_choice": {"mode": "video", "by": "auto", "tab_frames": [22, 24]}})
        assert cli.main(["strip", str(wd), "--band", "200", "355"]) == 0
        assert calls == [(200, 355)]
        expected = {**before, "strip": {**STRIP_META, "band_given": True}}  # 前に見分けた記録は残さない
    else:
        user = {"mode": "video", "by": "user"}
        save_meta(wd, {**before, "source_choice": user})
        assert cli.main(["strip", str(wd)]) == 0
        expected = {**before, "source_choice": user, "strip": STRIP_META}
    meta = load_meta(wd)
    corrections = meta["strip"].pop("corrections")
    assert meta == expected and len(corrections) == 97
    assert any((wd / "strip").iterdir())
    out = capsys.readouterr().out
    assert "紙を撮った動画" not in out and "画面のタブ譜として検出" not in out


def test_strip_does_not_detect_nor_judge_existing_paper(work):
    wd, use = work
    calls = use(detected(**PAPER_LIKE))
    save_meta(wd, {**load_meta(wd), "paper": {"part": None, "strings": None}})  # source_choice の無い、これまでの紙の曲
    before = load_meta(wd)
    with pytest.raises(SystemExit, match="ページとパートを先に選んでください"):
        cli.main(["strip", str(wd)])
    assert calls == [] and load_meta(wd) == before and not (wd / "strip").exists()


def test_strip_without_tab_keeps_its_exit_code_and_records_nothing(work, capsys):
    wd, use = work
    use(strip.NoTabFound("タブ譜の 6 本線が見つかりませんでした"))
    before = load_meta(wd)
    assert cli.main(["strip", str(wd)]) == strip.NO_TAB_EXIT == 3
    assert load_meta(wd) == before and not (wd / "strip").exists()
