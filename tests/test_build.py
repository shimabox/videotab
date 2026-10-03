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


def test_build_moves_note_effects_after_beats_into_notes(tmp_path, capsys):
    # 拍の後ろの音の効果は、出力でその拍の音に付け直す。読み取り結果のファイルは書き換えない
    wd = setup_work(tmp_path)
    part = {"1": "(7.5).8 {pm} (7.5).8 {pm tempo 143} (5.5 0.6).4 {pm} r.4 {pm} (7.5).4",
            "2": "(0.6).2 {d pm} (0.6).4"}
    write(wd / "parts" / "part_A.json", part)
    write(wd / "parts" / "part_B.json", {"2": "(0.6).2 {d pm} (0.6).4", "3": "r.1"})
    assert build.run_build(wd) == 0
    out = capsys.readouterr().out
    assert "{pm} は音の効果です。(7.5{pm}).8 のように音の中に書きます" in out  # 検査の注意
    assert "拍の後ろに書かれた音の効果を音の中へ付け直しました: 4 件（休符の拍から外したもの 1 件）" in out
    tex = (wd / "song.alphatex").read_text(encoding="utf-8")
    assert "(7.5{pm}).8 (7.5{pm}).8 {tempo 143} (5.5{pm} 0.6{pm}).4 r.4 (7.5).4 |\n" in tex
    assert "(0.6{pm}).2 {d} (0.6).4 |\n" in tex
    assert json.loads((wd / "parts" / "part_A.json").read_text(encoding="utf-8")) == part


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


def test_default_subtitle_matches_template(tmp_path):
    assert build.default_subtitle("Creator") == "Creator さんの動画のタブ譜から書き起こし"
    assert build.default_subtitle(None) == "動画のタブ譜から書き起こし"
    write(tmp_path / "song" / "meta.json", {"id": "song", "title": "Song", "creator": "Creator"})
    assert build.load_score(tmp_path / "song")["subtitle"] == build.default_subtitle("Creator")


def test_mute_key_reaches_keydown_even_when_a_button_has_focus(tmp_path):
    wd = setup_work(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    assert build.run_build(wd) == 0
    html = (wd / "song.html").read_text(encoding="utf-8")
    keydown = html[html.index('addEventListener("keydown"'):]
    keydown = keydown[: keydown.index("});") + 3]
    # KeyM で判定し、BUTTON の除外（focus が残ったボタンの上）より前に判定する。
    assert "KeyM" in keydown
    assert keydown.index("KeyM") < keydown.index('tag === "BUTTON"')


def test_retitle_score_replaces_title_tab_by_and_default_subtitle():
    score = {"title": "Song", "subtitle": "Old さんの動画のタブ譜から書き起こし", "tab_by": "Old", "tempo": 137,
             "tuning": "d4 a3 f3 c3 g2 c2", "capo": 2}  # fmt: skip
    out = build.retitle_score(score, title="New Song", creator="New", old_meta={"creator": "Old"})
    assert out == {**score, "title": "New Song", "subtitle": "New さんの動画のタブ譜から書き起こし", "tab_by": "New"}
    assert score["title"] == "Song"  # 元の表は変えない
    out = build.retitle_score(score, title="t", creator=None, old_meta={"creator": "Old"})
    assert out["subtitle"] == "動画のタブ譜から書き起こし" and out["tab_by"] is None


def test_retitle_score_keeps_hand_written_subtitle():
    score = {"title": "Song", "subtitle": "Old さんの演奏から（耳コピで補った所あり）", "tab_by": "Old", "tempo": 120}
    out = build.retitle_score(score, title="t", creator="New", old_meta={"creator": "Old"})
    assert out["subtitle"] == score["subtitle"] and out["tab_by"] == "New"
    out = build.retitle_score({"title": "t", "subtitle": ["形の違う値"]}, title="t", creator="New", old_meta={})
    assert out["subtitle"] == ["形の違う値"]


def test_retitle_score_default_forms():
    new = "New さんの動画のタブ譜から書き起こし"
    cases = [
        ({}, {}),  # 欄が無い
        ({"subtitle": ""}, {}),
        ({"subtitle": None}, {}),
        ({"subtitle": "Meta さんの動画のタブ譜から書き起こし"}, {"creator": "Meta"}),  # 書き換える前の meta の作成者
        ({"subtitle": "Tab さんの動画のタブ譜から書き起こし", "tab_by": "Tab"}, {"creator": "Meta"}),  # いまの tab_by
        ({"subtitle": new, "tab_by": "New"}, {"creator": "Old"}),  # score.json だけ書けたあと
        ({"subtitle": "動画のタブ譜から書き起こし"}, {"creator": "Old"}),  # 作成者なし
    ]
    for score, meta in cases:
        assert build.retitle_score({"title": "t", **score}, title="t", creator="New", old_meta=meta)["subtitle"] == new, score
    # どの作成者からも作れない文言は、雛形と同じ形でも手で直したものとして残す
    other = {"title": "t", "subtitle": "Other さんの動画のタブ譜から書き起こし", "tab_by": "Tab"}
    assert build.retitle_score(other, title="t", creator="New", old_meta={"creator": "Meta"})["subtitle"] == other["subtitle"]


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


def controls_html(html):
    return html.split('id="tm-controls"')[1].split('<footer class="tm-footer">')[0]


def button_tag(html, button_id):
    return html.split(f'id="{button_id}"')[1].split(">")[0]


def test_rendered_page_offers_mute():
    # 音を出さずに再生位置を追えるよう、ミュートのトグルのボタンと M キーの案内がある。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    controls = controls_html(html)
    mute = button_tag(controls, "tm-mute")
    assert 'aria-pressed="false"' in mute and 'aria-label="ミュート"' in mute
    assert 'title="音を出さずに再生する（M）"' in mute and 'aria-keyshortcuts="M"' in mute
    assert "M: ミュート" in controls
    assert "api.masterVolume = muted ? 0 : 1" in html
    assert '"videotab.mute-playback"' in html


def test_rendered_page_draws_playback_buttons_as_icons():
    # 再生と停止は文字ではなくアイコンのボタンで、名前とキーは属性で伝える。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    controls = controls_html(html)
    play = button_tag(controls, "tm-play")
    assert "disabled" in play and 'aria-label="再生"' in play
    assert 'title="再生 / 一時停止（Space）"' in play and 'aria-keyshortcuts="Space"' in play
    stop = button_tag(controls, "tm-stop")
    assert 'aria-label="停止"' in stop and 'title="停止（Esc）"' in stop
    assert 'aria-keyshortcuts="Escape"' in stop
    assert "Play" not in controls and "Stop" not in controls
    svgs = [s.split(">")[0] for s in controls.split("<svg")[1:]]
    assert len(svgs) >= 6 and all('aria-hidden="true"' in s for s in svgs)
    # 再生中は一時停止のアイコンと名前に切り替える。
    assert 'playBtn.setAttribute("aria-label", playing ? "一時停止" : "再生")' in html


def test_rendered_page_toggles_follow_with_a_button():
    # 自動スクロールはトグルのボタン。既定はオンで、選択は記憶する。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    controls = controls_html(html)
    follow = button_tag(controls, "tm-follow")
    assert 'aria-pressed="true"' in follow and 'aria-label="自動スクロール"' in follow
    assert 'title="再生位置を追って楽譜を自動でスクロールする"' in follow
    assert 'type="checkbox"' not in controls
    assert '"videotab.follow-playback"' in html


def toggle_html(controls, button_id):
    return controls.split(f'id="{button_id}"')[1].split("</button>")[0]


def test_rendered_page_says_toggle_states_in_words():
    # 自動スクロールとミュートは、いまの状態を文言で言う。両方の文言を重ねて片方を隠し、
    # 切り替えてもボタンの幅が変わらないようにする。読み上げの名前は aria-label で固定し、
    # 状態は aria-pressed で伝えるので、文言は読み上げない。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    controls = controls_html(html)
    expected = {"tm-follow": ("自動スクロール: オン", "自動スクロール: オフ"), "tm-mute": ("ミュート中", "音あり")}
    for button_id, (on, off) in expected.items():
        state = toggle_html(controls, button_id).split('<span class="tm-state" aria-hidden="true">')[1]
        assert f'<span class="tm-state-on">{on}</span><span class="tm-state-off">{off}</span>' in state
    assert ".tm-state > span { grid-area: 1 / 1; }" in html
    # 短いほうの文言もアイコンのすぐ右から始め、余りは文言の右に出す。
    tm_state = html.split(".tm-state {")[1].split("}")[0]
    assert "text-align: left;" in tm_state
    assert 'button.tm-toggle[aria-pressed="false"] .tm-state-on { visibility: hidden; }' in html
    # ミュートは、ミュート中は斜線入りのスピーカー、音ありは音の弧のスピーカー（斜線なし）。
    mute = toggle_html(controls, "tm-mute")
    icon_on = mute.split('<svg class="tm-icon-on"')[1].split("</svg>")[0]
    icon_off = mute.split('<svg class="tm-icon-off"')[1].split("</svg>")[0]
    assert "M1.5 1.5l13 13" in icon_on and "l13 13" not in icon_off
    assert 'button.tm-toggle[aria-pressed="false"] .tm-icon-on { display: none; }' in html


def test_rendered_page_fills_toggles_in_their_usual_state():
    # ふだんの状態のトグルは塗りつぶす。自動スクロールのオンは青、音ありは緑で、文字とアイコンは白。
    # ミュート中だけは聞こえないので赤。色の抜けたボタンは止めている状態と読める。
    # 塗りつぶしでもキーボードのフォーカスの輪郭が見えるよう、輪郭を離す。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    style = html.split("<style>")[1].split("</style>")[0]
    on = style.split('button.tm-toggle[aria-pressed="true"] {')[1].split("}")[0]
    assert "background: #1f6feb;" in on and "border-color: #1f6feb;" in on and "color: #fff;" in on
    muted = style.split('#tm-mute[aria-pressed="true"] {')[1].split("}")[0]
    assert "background: #b3261e;" in muted and "border-color: #b3261e;" in muted
    sound = style.split('#tm-mute[aria-pressed="false"] {')[1].split("}")[0]
    assert "background: #1e7d3c;" in sound and "border-color: #1e7d3c;" in sound and "color: #fff;" in sound
    assert '#tm-mute[aria-pressed="false"]:hover:not(:disabled) {' in style
    focus = style.split('button.tm-toggle[aria-pressed="true"]:focus-visible')[1].split("}")[0]
    assert '#tm-mute[aria-pressed="false"]:focus-visible' in focus and "outline-offset: 2px;" in focus
    # 自動スクロールのオフは色なしのまま。
    assert 'button.tm-toggle[aria-pressed="false"] {' not in style
    assert "#tm-follow" not in style
    assert "#e8f0fe" not in style


def test_rendered_page_updates_toggles_through_one_function():
    # 読み込み時の記憶・クリック・M キーのどれでも、同じ処理でボタンの表示を変える。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    script = html.split('id="tm-alphatab-lib"')[1].split("</script>")[1]
    assert 'function showToggle(button, on) {\n    button.setAttribute("aria-pressed", String(on));' in script
    assert script.count('setAttribute("aria-pressed"') == 1
    load_follow = script.index("showToggle(followEl, follow);")
    assert load_follow < script.index("function keepPlayingBarVisible()")
    assert script.index("showToggle(muteEl, muted);") < script.index("function reserveRoomForControls()")
    click_follow = script.split('followEl.addEventListener("click", function () {')[1].split("});")[0]
    assert "showToggle(followEl, follow);" in click_follow
    set_muted = script.split("function setMuted(value) {")[1].split("}")[0]
    assert "showToggle(muteEl, value);" in set_muted
    click_mute = script.split('muteEl.addEventListener("click", function () {')[1].split("});")[0]
    assert "setMuted(!muted);" in click_mute
    key_m = script.split('e.code === "KeyM"')[1].split("return;")[0]
    assert "setMuted(!muted);" in key_m


def test_rendered_page_reports_player_status_only_when_not_ready():
    # 準備中と再生できないときだけ文を出し、再生できるようになったら空にする。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    status = html.split('id="tm-player-status"')[1].split("</span>")[0]
    assert 'aria-live="polite"' in status and "音源を読み込み中…" in status
    assert 'statusEl.textContent = "オフラインのため再生できません"' in html
    assert 'statusEl.textContent = ""' in html
    assert ".tm-player-status:empty { display: none; }" in html
    assert "Player: ready" not in html and "Player: unavailable" not in html


def test_rendered_page_tells_score_load_failure_from_offline():
    # 楽譜を読み込む前の失敗（alphaTex の誤り）は「楽譜を読み込めませんでした」、読み込んだあとの
    # 失敗（音源を読めない）だけを「オフラインのため再生できません」とし、どちらも ▶ は押せないまま。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    script = html.split("api.error.on(function (err) {")[1].split("});")[0]
    assert "if (!scoreLoaded || formatError) {" in script
    assert "err instanceof alphaTab.importer.UnsupportedFormatError" in script
    assert script.index('"楽譜を読み込めませんでした"') < script.index('"オフラインのため再生できません"')
    assert "playBtn.disabled = true;" in script and 'statusEl.classList.add("is-error");' in script
    assert "scoreLoaded = true;" in html.split("api.scoreLoaded.on(function () {")[1].split("});")[0]
    # 楽譜を読めなかったら、あとで音源が届いても ▶ を押せるようにしない
    ready = html.split("api.playerReady.on(function () {")[1].split("});")[0]
    assert ready.lstrip().startswith("if (scoreFailed) { return; }")


def test_rendered_page_lays_controls_in_two_rows():
    # 再生の操作は役割ごとの 2 段。1 段目は再生の操作と準備の表示、
    # 2 段目はキー操作の案内と右のダウンロード。
    html = render_html("r.1", title="t", tempo=100, tuning="e4 b3 g3 d3 a2 e2")
    rows = controls_html(html).split('<div class="tm-controls-row">')[1:]
    assert len(rows) == 2
    play, sub = rows
    order = ['id="tm-play"', 'id="tm-stop"', 'id="tm-follow"', 'id="tm-mute"', 'id="tm-player-status"']
    positions = [play.index(s) for s in order]
    assert positions == sorted(positions)
    assert "tm-keys" not in play and "data-download" not in play
    assert sub.index('class="tm-keys"') < sub.index('class="tm-download"')
    assert 'id="tm-player-status"' not in sub


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
