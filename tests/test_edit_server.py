"""画面から曲の情報（題名・作成者・元動画のページ）を書き換える（POST /api/jobs/<ID>/info）。"""

import json
import os
import threading
import types
from http.server import ThreadingHTTPServer

import pytest
from test_server import call, page_section
from trees import make_outside, snapshot

from videotab import build, pipeline, server

JOB = "abcdefghijk"
OLD_META = {
    "id": JOB, "title": "古い題名", "creator": "Old", "source_url": "https://example.com/old",
    "source_file": "元の動画.mp4", "width": 1920, "height": 1080, "duration": 12.5,
    "strip": {"band": [100, 300], "lines": [110, 130, 150, 170, 190, 210]},
}  # fmt: skip
OLD_SCORE = {
    "title": "古い題名", "subtitle": "Old さんの動画のタブ譜から書き起こし", "tab_by": "Old", "tempo": 137,
    "time_signature": [4, 4], "tuning": "e4 b3 g3 d3 a2 e2", "capo": 0,
}  # fmt: skip
NEW = {"title": "新しい題名", "creator": "New", "source_url": "https://example.com/new"}


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def make_song(root, job_id=JOB, *, job=True, score=True, undone=None):
    """全段が済んだ曲。undone の段は失敗にする。job / score が偽なら job.json / score.json を作らない。"""
    wd = root / job_id
    wd.mkdir(parents=True)
    write(wd / "meta.json", {**OLD_META, "id": job_id})
    if score:
        write(wd / "score.json", OLD_SCORE)
    if job:
        j = pipeline.Job.create(wd)
        data = j.load()
        for s in data["steps"]:
            s.update(status="done", started="2026-10-03T10:00:00", ended="2026-10-03T10:05:00", message=f"{s['name']} 済み")
        if undone:
            next(s for s in data["steps"] if s["name"] == undone).update(status="failed", message="失敗")
        data["status"] = "failed" if undone else "done"
        j.save(data)
    return wd


def files(top):
    """top の下の木（job.lock は排他のためのファイルなので除く）。"""
    return {k: v for k, v in snapshot(top).items() if os.path.basename(k) != "job.lock"}


@pytest.fixture
def served(tmp_path, monkeypatch):
    """曲の情報を試す画面。順番待ちに入った曲は、偽物の実行が job.json の中身を記録して終わる。"""
    root = tmp_path / "work"
    root.mkdir()
    ran = []
    done = threading.Event()

    def fake_run(self):
        ran.append((self.workdir.name, self.load()))
        done.set()
        return True

    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield types.SimpleNamespace(base=f"http://127.0.0.1:{httpd.server_address[1]}", root=root, app=app, ran=ran, done=done)
    httpd.shutdown()


def info(s, job_id=JOB, body=None, **kwargs):
    code, raw = call(f"{s.base}/api/jobs/{job_id}/info", NEW if body is None else body, **kwargs)
    return code, json.loads(raw)


def wait_idle(app):
    for _ in range(250):
        with app.lock:
            if not app.waiting and app.running is None:
                return
        threading.Event().wait(0.02)
    raise AssertionError("順番待ちが終わりません")


# --- 書き換える内容


def test_edit_rewrites_three_files_and_rebuilds(served):
    s = served
    wd = make_song(s.root)
    before = read(wd / "job.json")
    assert info(s) == (200, {"id": JOB, "rebuilt": True})
    assert read(wd / "meta.json") == {**OLD_META, **NEW}  # ほかのキーは残す
    assert read(wd / "score.json") == {**OLD_SCORE, "title": "新しい題名", "tab_by": "New",
                                       "subtitle": "New さんの動画のタブ譜から書き起こし"}  # fmt: skip
    assert s.done.wait(5)
    wait_idle(s.app)
    ((name, queued),) = s.ran
    assert name == JOB and queued["status"] == "queued" and queued["title"] == "新しい題名"
    i = pipeline.STEP_NAMES.index("build")
    assert queued["steps"][:i] == before["steps"][:i]  # タブ譜の組み立てより前の段の状態・時刻は変えない
    assert [(x["name"], x["status"], x["started"], x["ended"]) for x in queued["steps"][i:]] == [
        ("build", "pending", None, None), ("verify", "pending", None, None)]  # fmt: skip
    jobs = json.loads(call(s.base + "/api/jobs")[1])["jobs"]
    assert [(j["id"], j["title"]) for j in jobs] == [(JOB, "新しい題名")]
    detail = json.loads(call(f"{s.base}/api/jobs/{JOB}")[1])
    assert (detail["title"], detail["creator"], detail["source_url"]) == ("新しい題名", "New", "https://example.com/new")


def test_edit_keeps_hand_written_subtitle(served):
    s = served
    wd = make_song(s.root)
    write(wd / "score.json", {**OLD_SCORE, "subtitle": "Old さんの演奏から（耳で補った所あり）"})
    assert info(s)[0] == 200
    score = read(wd / "score.json")
    assert score["subtitle"] == "Old さんの演奏から（耳で補った所あり）" and score["tab_by"] == "New"


@pytest.mark.parametrize("undone", ["read", "pages"])
def test_edit_only_saves_when_earlier_steps_are_not_done(served, undone):
    s = served
    wd = make_song(s.root, undone=undone)
    before = read(wd / "job.json")
    assert info(s) == (200, {"id": JOB, "rebuilt": False})
    after = read(wd / "job.json")
    assert after["title"] == "新しい題名" and after["status"] == "failed"
    assert after["steps"] == before["steps"]
    assert not s.app.waiting and s.app.running is None and s.ran == []
    assert read(wd / "meta.json")["title"] == "新しい題名"


def test_edit_does_not_rebuild_when_steps_differ(served):
    s = served
    wd = make_song(s.root)
    data = read(wd / "job.json")
    data["steps"].insert(4, {"name": "extra", "label": "余分", "status": "done"})
    write(wd / "job.json", data)
    assert info(s) == (200, {"id": JOB, "rebuilt": False})
    after = read(wd / "job.json")
    assert after["steps"] == data["steps"]  # 全段 done でも、読み取りの段の状態を変えない
    assert not s.app.waiting and s.ran == []


def test_edit_song_without_job_json_only_saves(served):
    s = served
    wd = make_song(s.root, job=False)
    assert info(s) == (200, {"id": JOB, "rebuilt": False})
    assert not (wd / "job.json").exists()
    assert read(wd / "meta.json") == {**OLD_META, **NEW}
    assert not s.app.waiting and s.ran == []
    assert [j["title"] for j in s.app.list_jobs()] == ["新しい題名"]


def test_edit_does_not_create_score_json(served):
    s = served
    wd = make_song(s.root, score=False)
    assert info(s)[0] == 200
    assert not (wd / "score.json").exists()
    wait_idle(s.app)


def test_edit_then_build_reflects_new_info(served):
    s = served
    wd = make_song(s.root, undone="read")
    (wd / "parts").mkdir()
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    assert info(s)[0] == 200
    assert build.run_build(wd) == 0
    tex = (wd / f"{JOB}.alphatex").read_text(encoding="utf-8")
    assert tex.startswith('\\title "新しい題名"\n\\subtitle "New さんの動画のタブ譜から書き起こし"\n\\tab "New"\n')
    html = (wd / f"{JOB}.html").read_text(encoding="utf-8")
    assert 'href="https://example.com/new"' in html and "（作成: New）" in html
    assert "https://example.com/old" not in html and "作成: Old" not in html


def test_empty_creator_and_link_become_null_and_leave_output(served):
    s = served
    wd = make_song(s.root, undone="read")
    (wd / "parts").mkdir()
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    assert info(s, body={"title": "題名", "creator": "  ", "source_url": None})[0] == 200
    meta = read(wd / "meta.json")
    assert meta["creator"] is None and meta["source_url"] is None
    score = read(wd / "score.json")
    assert score["tab_by"] is None and score["subtitle"] == "動画のタブ譜から書き起こし"
    assert info(s, body={"title": "題名", "creator": None, "source_url": ""})[0] == 200  # 空の文字列も null
    assert read(wd / "meta.json")["source_url"] is None
    assert build.run_build(wd) == 0
    assert "\\tab" not in (wd / f"{JOB}.alphatex").read_text(encoding="utf-8")
    html = (wd / f"{JOB}.html").read_text(encoding="utf-8")
    assert "元動画" not in html and "example.com" not in html


# --- 空の題名


@pytest.mark.parametrize("title", ["", "   ", None])
def test_empty_title_falls_back_to_file_name(served, title):
    s = served
    wd = make_song(s.root, undone="read")
    assert info(s, body={**NEW, "title": title})[0] == 200
    assert read(wd / "meta.json")["title"] == read(wd / "score.json")["title"] == read(wd / "job.json")["title"] == "元の動画"


@pytest.mark.parametrize("source", ["missing", 3, "", "a\x07b.mp4", "長" * 201 + ".mp4", "dir/" + "x" * 201 + ".mp4"])
def test_empty_title_falls_back_to_id_when_file_name_is_unusable(served, source):
    s = served
    wd = make_song(s.root, undone="read")
    meta = read(wd / "meta.json")
    if source == "missing":
        del meta["source_file"]
    else:
        meta["source_file"] = source
    write(wd / "meta.json", meta)
    assert info(s, body={**NEW, "title": ""})[0] == 200
    assert read(wd / "meta.json")["title"] == JOB


def test_file_title_uses_name_without_folder_and_suffix():
    assert server.file_title({"source_file": "../somewhere/元の動画.mov"}) == "元の動画"
    assert server.file_title({"source_file": " 曲 .mp4"}) == "曲"
    assert server.file_title({}) is None


def test_detail_shows_file_title_and_rebuildable(served):
    s = served
    make_song(s.root)
    make_song(s.root, "bbbbbbbbbbb", undone="read")
    make_song(s.root, "ccccccccccc", job=False)
    assert (s.app.detail(JOB)["source_stem"], s.app.detail(JOB)["rebuildable"]) == ("元の動画", True)
    assert s.app.detail("bbbbbbbbbbb")["rebuildable"] is False
    assert s.app.detail("ccccccccccc")["rebuildable"] is False


# --- 断る要求


@pytest.mark.parametrize("body", [
    {**NEW, "title": "改\n行"},
    {**NEW, "creator": "制御\x07文字"},
    {**NEW, "title": "x" * 201},
    {**NEW, "creator": "x" * 201},
    {**NEW, "source_url": "http://example.com/v"},
    {**NEW, "source_url": "https://example.com/a b"},
    {**NEW, "source_url": "https://example.com/" + "x" * 1981},
    {**NEW, "title": 3},
    {**NEW, "creator": ["New"]},
    {**NEW, "source_url": {"url": "https://example.com/v"}},
    {"title": "題名", "creator": "New"},
    {"creator": None, "source_url": None},
    {},
    ["題名"],
    "題名",
])  # fmt: skip
def test_bad_input_is_400_and_changes_nothing(served, body):
    s = served
    make_song(s.root)
    before = snapshot(s.root)
    code, got = info(s, body=body)
    assert code == 400 and got["error"]
    assert snapshot(s.root) == before  # 1 バイトも変えない（job.lock も作らない）
    assert not s.app.waiting and s.ran == []


def test_url_of_2000_chars_is_accepted(served):
    s = served
    make_song(s.root, undone="read")
    url = "https://example.com/" + "x" * 1980
    assert len(url) == 2000 and info(s, body={**NEW, "source_url": url})[0] == 200


def test_broken_json_body_is_400(served):
    s = served
    make_song(s.root)
    before = snapshot(s.root)
    from test_server import request

    assert request(f"{s.base}/api/jobs/{JOB}/info", "POST", {"X-Videotab": "1"}, b"{")[0] == 400
    assert snapshot(s.root) == before


@pytest.mark.parametrize("extra", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Host": "evil.example"}])
def test_requests_not_from_page_are_403(served, extra):
    s = served
    make_song(s.root)
    before = snapshot(s.root)
    assert info(s, header=bool(extra), extra=extra)[0] == 403  # 見出しが無い、または付けても出どころが違う
    assert snapshot(s.root) == before


@pytest.mark.parametrize("state", ["running", "waiting", "locked"])
def test_busy_song_is_409(served, state):
    s = served
    make_song(s.root)
    before = files(s.root)
    if state == "locked":  # videotab run などが job.lock を握っている
        with pipeline.Job(s.root / JOB).hold():
            assert info(s)[0] == 409
    else:
        with s.app.lock:
            if state == "running":
                s.app.running = JOB
            else:
                s.app.waiting.append(JOB)
        assert info(s)[0] == 409
        with s.app.lock:
            s.app.running = None
            s.app.waiting.clear()
    assert files(s.root) == before


@pytest.mark.parametrize("state", ["running", "waiting"])
def test_case_variant_of_busy_id_is_409(served, state):
    s = served
    make_song(s.root)
    before = files(s.root)
    with s.app.lock:
        if state == "running":
            s.app.running = JOB.upper()
        else:
            s.app.waiting.append(JOB.upper())
    assert info(s)[0] == 409
    assert files(s.root) == before
    with s.app.lock:
        s.app.running = None
        s.app.waiting.clear()


def test_song_queued_through_link_is_409(served):
    s = served
    make_song(s.root)
    (s.root / "alias").symlink_to(s.root / JOB, target_is_directory=True)
    with s.app.lock:
        s.app.waiting.append("alias")  # 同じ曲が別名で順番待ち
    before = files(s.root / JOB)
    assert info(s)[0] == 409
    assert files(s.root / JOB) == before
    with s.app.lock:
        s.app.waiting.clear()


@pytest.mark.parametrize("job_id", ["nothing", JOB.upper(), "%2e%2e", ".hidden", "a" * 65, "notasong"])
def test_unknown_ids_are_404(served, job_id):
    s = served
    make_song(s.root)
    (s.root / "notasong").mkdir()
    (s.root / "notasong" / "video.mp4").write_bytes(b"x")
    before = snapshot(s.root)
    assert info(s, job_id)[0] == 404
    assert snapshot(s.root) == before
    with pytest.raises(KeyError):
        s.app.edit(JOB.upper(), "題名", None, None)


# --- 読めない・書けないファイル


@pytest.mark.parametrize("name, content", [
    ("meta.json", "{壊れた"), ("meta.json", "[1, 2]"), ("meta.json", b"\xff\xfe"),
    ("score.json", "{壊れた"), ("score.json", '"文字列"'),
    ("job.json", "{壊れた"), ("job.json", "null"),
])  # fmt: skip
def test_broken_files_are_500_and_change_nothing(served, name, content):
    s = served
    wd = make_song(s.root)
    if isinstance(content, bytes):
        (wd / name).write_bytes(content)
    else:
        (wd / name).write_text(content, encoding="utf-8")
    before = files(s.root)
    code, got = info(s)
    assert code == 500 and name in got["error"]
    assert files(s.root) == before
    assert not s.app.waiting and s.ran == []
    with pytest.raises(server.InfoError):
        s.app.edit(JOB, "題名", None, None)


@pytest.mark.parametrize("name", ["meta.json", "score.json", "job.json"])
@pytest.mark.parametrize("shape", ["link", "hardlink", "inner-link", "fifo"])
def test_files_that_are_links_are_refused(served, tmp_path, name, shape):
    s = served
    wd = make_song(s.root)
    out = make_outside(tmp_path)
    original = {"meta.json": {**OLD_META}, "score.json": OLD_SCORE, "job.json": read(wd / "job.json")}[name]
    write(out / "x", original)  # 外のファイルも、曲の情報として読める中身にしておく
    os.unlink(wd / name)
    if shape == "link":
        (wd / name).symlink_to(out / "x")
    elif shape == "hardlink":
        os.link(out / "x", wd / name)
    elif shape == "inner-link":
        write(wd / "copy.json", original)
        (wd / name).symlink_to("copy.json")
    else:
        os.mkfifo(wd / name)
    before_out, before = snapshot(out), files(s.root)
    code, got = info(s)
    assert code == 500 and name in got["error"]
    assert snapshot(out) == before_out  # 外のファイルを変えない
    assert files(s.root) == before  # 外の中身を作業フォルダへ書き写さない
    assert not s.app.waiting and s.ran == []


def test_save_again_after_meta_write_failed_aligns_files(served, monkeypatch):
    s = served
    wd = make_song(s.root, undone="read")
    real = server.save_meta

    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(server, "save_meta", broken)
    code, got = info(s)
    assert code == 500 and "No space left on device" in got["error"]
    assert read(wd / "score.json")["tab_by"] == "New"  # score.json だけ書けた
    assert read(wd / "meta.json") == OLD_META and read(wd / "job.json")["title"] == "古い題名"
    assert not s.app.waiting and s.ran == []

    monkeypatch.setattr(server, "save_meta", real)
    assert info(s)[0] == 200
    meta, score, job = read(wd / "meta.json"), read(wd / "score.json"), read(wd / "job.json")
    assert (meta["title"], score["title"], job["title"]) == ("新しい題名",) * 3
    assert (meta["creator"], score["tab_by"]) == ("New", "New")
    assert score["subtitle"] == "New さんの動画のタブ譜から書き起こし"


# --- 画面


def test_page_sends_empty_link_as_null(served):
    """画面の欄が空のまま保存できること・既存のリンクを消すと null になることを、送る本文の形で確かめる。"""
    s = served
    wd = make_song(s.root, undone="read")
    assert info(s, body={"title": "新しい題名", "creator": None, "source_url": None})[0] == 200
    assert read(wd / "meta.json")["source_url"] is None
    section = page_section("曲の情報")
    assert '"/info"' in section and "api(" in section
    assert "source_url: link || null" in section and "creator: creator || null" in section
    assert "/^https:\\/\\/\\S+$/.test(link)" in section and "if (link && " in section


def test_page_info_row_is_kept_between_refreshes():
    from importlib import resources

    page = resources.files("videotab").joinpath("templates", "app.html").read_text(encoding="utf-8")
    areas = page_section("一覧と詳細")
    assert areas.index('"p-top"') < areas.index('"p-edit"') < areas.index('"p-retry"')  # 段の下、やり直しの上
    section = page_section("曲の情報")
    for text in ("曲の情報", "保存してタブ譜に反映", "保存する", "元に戻す", "https:// で始まる URL にしてください",
                 "空なら楽譜に出ません", "タブ譜に反映しています（読み取りはやり直しません）",
                 "タブ譜には、続きを実行して組み立てたときに出ます"):  # fmt: skip
        assert text in section, text
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval"):
        assert word not in section, word
    assert "infoRow = null" in page_section("一覧と詳細").split("function select(")[1]  # 曲を選び直したら捨てる
