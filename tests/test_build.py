import json

from videotab import build, verify
from videotab.render import render_html, tuning_label
from videotab.workdir import frame_name, parse_frame_name


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def setup_work(tmp_path, tempo=120):
    wd = tmp_path / "song"
    write(wd / "meta.json", {"id": "song", "title": "Song", "source_url": "https://example.com/v"})
    write(wd / "score.json", {"title": "曲", "tempo": tempo})
    return wd


def test_build_merges_overlap_and_writes_outputs(tmp_path, capsys):
    wd = setup_work(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.1", "2": "(0.6).2  (0.6).2"})
    write(wd / "parts" / "part_B.json", {"2": "(0.6).2 (0.6).2", "3": "\\rc 2 (3.5).1"})
    assert build.run_build(wd) == 0
    tex = (wd / "song.alphatex").read_text(encoding="utf-8")
    assert tex == '\\title "曲"\n\\tempo 120\n.\n' \
        "\\tuning e4 b3 g3 d3 a2 e2\n\\ts 4 4\nr.1 |\n(0.6).2  (0.6).2 |\n\\rc 2 (3.5).1\n"
    html = (wd / "song.html").read_text(encoding="utf-8")
    assert "https://example.com/v" in html and "小節数: 3" in html
    # 同梱の alphaTab（MPL-2.0）のライセンスと、同じ版のソースの入手先を示す
    assert "https://mozilla.org/MPL/2.0/" in html
    assert "https://github.com/CoderLine/alphaTab/tree/v1.8.4" in html


def test_build_stops_on_conflict_until_resolved(tmp_path, capsys):
    wd = setup_work(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.1", "2": "(9.5 7.6).1"})
    write(wd / "parts" / "part_B.json", {"2": "(9.4 7.5).1", "3": "r.1"})
    assert build.run_build(wd) == 1
    out = capsys.readouterr().out
    assert "食い違い 1 小節" in out and "part_A.json: (9.5 7.6).1" in out
    assert json.loads((wd / "conflicts.json").read_text(encoding="utf-8")) == {
        "2": {"part_A.json": "(9.5 7.6).1", "part_B.json": "(9.4 7.5).1"}
    }
    assert not (wd / "song.html").exists()

    write(wd / "resolve.json", {"2": "(9.5 7.6).1"})
    assert build.run_build(wd) == 0
    assert "(9.5 7.6).1" in (wd / "song.alphatex").read_text(encoding="utf-8")
    assert not (wd / "conflicts.json").exists()


def test_build_stops_on_missing_bars_check_errors_and_missing_tempo(tmp_path, capsys):
    wd = setup_work(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.1", "3": "r.2"})
    assert build.run_build(wd) == 1
    out = capsys.readouterr().out
    assert "抜けている小節: 2" in out and "長さが 2 拍" in out

    wd2 = tmp_path / "other"
    write(wd2 / "parts" / "part_A.json", {"1": "r.1"})
    assert build.run_build(wd2) == 1
    assert "tempo" in capsys.readouterr().out
    assert json.loads((wd2 / "score.json").read_text(encoding="utf-8"))["tempo"] is None


def test_title_quotes_do_not_break_alphatex(tmp_path):
    tex = build.alphatex_document({1: "r.1"}, {"title": 'A "B" C', "tempo": 100, "time_signature": [4, 4],
                                                "tuning": "e4 b3 g3 d3 a2 e2"})
    assert tex.splitlines()[0] == '\\title "A ＂B＂ C"'


LINK = '<a href="https://example.com/v" target="_blank" rel="noopener noreferrer">Song</a>'


def test_build_credits_video_creator_with_link(tmp_path):
    wd = tmp_path / "song"
    write(wd / "meta.json", {"id": "song", "title": "Song", "source_url": "https://example.com/v", "creator": " Creator "})
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    assert build.run_build(wd) == 1  # 雛形を作って、tempo がないので止まる
    score = json.loads((wd / "score.json").read_text(encoding="utf-8"))
    assert score["subtitle"] == "Creator さんの動画のタブ譜から書き起こし" and score["tab_by"] == "Creator"
    score["tempo"] = 100
    write(wd / "score.json", score)
    assert build.run_build(wd) == 0
    tex = (wd / "song.alphatex").read_text(encoding="utf-8")
    assert tex.startswith('\\title "Song"\n\\subtitle "Creator さんの動画のタブ譜から書き起こし"\n\\tab "Creator"\n')
    html = (wd / "song.html").read_text(encoding="utf-8")
    assert f"元動画: {LINK}（作成: Creator）<br>" in html


def test_build_credits_creator_without_link(tmp_path):
    # tab_by の欄がない score.json と、https でない元動画のページ（読み手が meta.json を書き換えた場合など）
    wd = tmp_path / "song"
    write(wd / "meta.json", {"id": "song", "title": "Song", "creator": "Creator", "source_url": "javascript:alert(1)"})
    write(wd / "score.json", {"title": "曲", "tempo": 120})
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    assert build.run_build(wd) == 0
    assert '\\tab "Creator"' in (wd / "song.alphatex").read_text(encoding="utf-8")
    html = (wd / "song.html").read_text(encoding="utf-8")
    assert "元動画: Song（作成: Creator）<br>" in html and "javascript:" not in html


def test_build_without_creator_keeps_plain_credit(tmp_path):
    wd = setup_work(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    assert build.run_build(wd) == 0
    assert "\\tab" not in (wd / "song.alphatex").read_text(encoding="utf-8")
    html = (wd / "song.html").read_text(encoding="utf-8")
    assert f"元動画: {LINK}<br>" in html and "作成:" not in html
    (tmp_path / "new").mkdir()
    assert build.load_score(tmp_path / "new")["subtitle"] == "動画のタブ譜から書き起こし"


def test_build_without_link_nor_creator_shows_no_source(tmp_path):
    wd = setup_work(tmp_path)
    write(wd / "meta.json", {"id": "song", "title": "Song"})
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    assert build.run_build(wd) == 0
    assert "元動画" not in (wd / "song.html").read_text(encoding="utf-8")


def test_source_link_is_https_only():
    assert build.source_link({"source_url": " https://example.com/v "}) == "https://example.com/v"
    for bad in ("javascript:alert(1)", "http://example.com/", "https://exa mple.com/", "", None, 3):
        assert build.source_link({"source_url": bad}) is None, bad
    assert build.source_link({}) is None


def test_verify_fails_on_check_errors(tmp_path, capsys):
    wd = setup_work(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.2", "2": "r.1"})
    assert verify.run_verify(wd, ["1=0", "2=1"]) == 1
    assert "検査の誤りが 1 件" in capsys.readouterr().out


def test_find_video_takes_only_video_extensions(tmp_path):
    from videotab.add import find_video

    (tmp_path / "video.webm.part").write_bytes(b"")
    (tmp_path / "video.flv").write_bytes(b"")
    assert find_video(tmp_path) is None
    (tmp_path / "video.MOV").write_bytes(b"")
    assert find_video(tmp_path) == tmp_path / "video.MOV"


def test_check_command(tmp_path, capsys):
    wd = setup_work(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.1", "2": "r.2"})
    assert build.run_check(wd) == 1
    assert build.run_check(wd / "parts" / "part_A.json") == 1
    write(wd / "parts" / "part_A.json", {"1": "r.1", "2": "r.1"})
    assert build.run_check(wd) == 0


def test_verify_fits_offset_and_flags_far_marks(tmp_path, capsys):
    wd = setup_work(tmp_path, tempo=120)  # 1 小節 2 秒
    write(wd / "parts" / "part_A.json", {"1": "r.1", "2": "\\ro r.1", "3": "\\rc 2 r.1", "4": "r.1"})
    # 動画の頭に 10 秒の前奏。4 小節目は計算上 10+10=20 秒
    write(wd / "marks.json", [[1, 10.2], [2, 16.1]])
    assert verify.run_verify(wd, ["4=20.3"]) == 0
    timing = json.loads((wd / "timing.json").read_text(encoding="utf-8"))
    assert abs(timing["offset"] - 10.1) < 0.2
    assert timing["order"] == [1, 2, 3, 2, 3, 4]
    assert verify.run_verify(wd, ["4=26"]) == 1


def test_render_escapes_script_closing_text():
    html = render_html('\\title "</script><b>"', title="<t>", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    assert "</script><b>" not in html.split("var texSource")[1].split(";")[0]
    assert "&lt;t&gt;" in html
    assert tuning_label("e4 b3 g3 d3 a2 e2") == "E A D G B E"
    assert tuning_label("d4 a3 f3 c3 g2 c2") == "C G C F A D"


def test_frame_name_round_trip():
    assert frame_name(12, 83.5) == "0012_01m23s500.png"
    assert parse_frame_name("0012_01m23s500.png") == (12, 83.5)
    assert parse_frame_name("frame.png") is None


def test_rendered_page_offers_downloads():
    from videotab.render import file_stem

    html = render_html("r.1", title='曲 "A/B"', tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    for kind in ("gp", "midi", "alphatex", "print"):
        assert f'data-download="{kind}"' in html
    assert "Gp7Exporter" in html and "downloadMidi" in html
    assert file_stem('曲 "A/B"') == "曲 _A_B"
    assert file_stem("...") == "tab"


def test_rendered_page_switches_audio_output_under_opaque_origin():
    # sandbox の下（origin が "null"）では AudioWorklet のモジュールを読み込めないので、
    # 再生の出力を ScriptProcessor に切り替える。それ以外は AudioWorklet のまま。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    player = html.split("var settings = {")[1].split("player: {")[1].split("}")[0]
    assert 'outputMode: window.origin === "null"' in player
    assert "? alphaTab.PlayerOutputMode.WebAudioScriptProcessor" in player
    assert ": alphaTab.PlayerOutputMode.WebAudioAudioWorklets" in player


def test_rendered_page_carries_disclaimer():
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    assert "誤りを含むことがあります" in html and "公開・再配布・販売をしないでください" in html
