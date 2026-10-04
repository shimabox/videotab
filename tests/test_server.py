import json
import os
import re
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from videotab import build, pipeline, server


@pytest.fixture
def running_server(tmp_path, monkeypatch, fake_probe):
    root = tmp_path / "work"
    root.mkdir()
    ran = threading.Event()

    def fake_run(self):
        data = self.load()
        for s in data["steps"]:
            s["status"] = "done"
        data["status"] = "done"
        self.save(data)
        ran.set()
        return True

    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": False})
    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    app = server.App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, root, ran
    httpd.shutdown()

def call(url, body=None, header=True, extra=None, method=None):
    """画面の代わりに API を呼ぶ。header は、本文があるときか DELETE のときに X-Videotab を付けるか。"""
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method)
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if header and (body is not None or method == "DELETE"):
        req.add_header("X-Videotab", "1")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def upload(base, data=b"video", header=True, extra=None, **query):
    """画面の代わりに動画を送る（本文は動画のバイト列、項目はクエリ）。"""
    from urllib.parse import urlencode

    query = {"name": "テスト曲.mp4", **query}
    req = urllib.request.Request(f"{base}/api/uploads?{urlencode(query)}", data=data, method="POST")
    if header:
        req.add_header("X-Videotab", "1")
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_page_and_job_flow(running_server):
    base, root, ran = running_server
    status, body = call(base + "/")
    assert status == 200 and "タブ譜を作る" in body.decode()

    assert upload(base, header=False)[0] == 403  # 画面以外からの POST は受けない
    assert upload(base, extra={"Origin": "https://evil.example"})[0] == 403  # 別のサイトのページからの POST
    assert upload(base, extra={"Host": "evil.example"})[0] == 403  # DNS rebinding

    status, body = upload(base, engine="claude", creator="作成者", source_url="https://example.com/v")
    assert status == 201
    job_id = body["id"]
    assert ran.wait(5)

    status, body = call(base + "/api/jobs")
    jobs = json.loads(body)["jobs"]
    assert jobs[0]["id"] == job_id and jobs[0]["title"] == "テスト曲"
    status, body = call(base + f"/api/jobs/{job_id}")
    detail = json.loads(body)
    assert detail["source_url"] == "https://example.com/v" and detail["creator"] == "作成者"
    assert "url" not in detail and len(detail["steps"]) == 7

    assert upload(base, engine="nope")[0] == 400
    assert json.loads(call(base + "/api/jobs")[1])["engines"] == {"claude": True, "codex": False}
    status, body = upload(base, engine="codex")
    assert status == 400 and "codex コマンドが見つかりません" in body["error"]
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == [job_id]  # 断ったものは残らない

def test_server_serves_only_files_inside_the_work_folder(running_server):
    base, root, _ = running_server
    wd = root / "abcdefghijk"
    wd.mkdir()
    (wd / "notes.md").write_text("# notes", encoding="utf-8")
    (root / "secret.md").write_text("secret", encoding="utf-8")
    (wd / "meta.json").write_text("{}", encoding="utf-8")
    assert call(base + "/files/abcdefghijk/notes.md") == (200, b"# notes")
    assert call(base + "/files/abcdefghijk/../secret.md")[0] == 404
    assert call(base + "/files/abcdefghijk/%2e%2e/secret.md")[0] == 404
    assert call(base + "/files/abcdefghijk/meta.json")[0] == 404

def test_server_worker_survives_unexpected_exit(tmp_path, monkeypatch, fake_probe):
    import io

    def exploding(self):
        raise SystemExit("壊れた JSON")

    monkeypatch.setattr(pipeline.Job, "run", exploding)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(tmp_path / "work")
    app.receive(io.BytesIO(b"a"), 1, "a.mp4", "claude")
    second = app.receive(io.BytesIO(b"b"), 1, "b.mp4", "claude")
    for _ in range(100):
        if not app.waiting and app.running is None:
            break
        threading.Event().wait(0.05)
    assert not app.waiting and app.running is None  # 2 曲目も順番が回ってきた
    assert "止まりました" in "\n".join(pipeline.Job(tmp_path / "work" / second).log_tail())


# --- 終了時刻


@pytest.fixture
def app(tmp_path, monkeypatch):
    """実行しない画面（順番待ちに入れても、偽物の実行は何もしない）。"""
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    return server.App(tmp_path / "work")


def saved_job(root, job_id="abcdefghijk", status="done", ends=("2026-09-29T10:01:00", "2026-09-29T10:09:30"), running=False):
    """段の ended を並べた job.json を作る。running なら最後の段を実行中にする。"""
    job = pipeline.Job.create(root / job_id)
    data = job.load()
    for s, end in zip(data["steps"], ends):
        s.update(status="done", started="2026-09-29T10:00:00", ended=end)
    if running:
        data["steps"][len(ends)].update(status="running", started="2026-09-29T10:10:00")
    data["status"] = status
    job.save(data)
    return job


def test_detail_finished_is_latest_step_end_for_done_and_failed(app):
    for status in ("done", "failed"):
        saved_job(app.root, status=status)
        detail = app.detail("abcdefghijk")
        assert detail["status"] == status and detail["finished"] == "2026-09-29T10:09:30"


@pytest.mark.parametrize("case", ["stopped", "no_steps", "saved_running", "running", "queued", "locked"])
def test_detail_finished_is_null_unless_finished(app, case):
    import fcntl

    job = saved_job(app.root, status={"stopped": "stopped", "saved_running": "running"}.get(case, "done"),
                    ends=() if case == "no_steps" else ("2026-09-29T10:01:00",), running=case == "saved_running")
    if case == "running":
        app.running = "abcdefghijk"
    if case == "queued":
        app.waiting.append("abcdefghijk")
    with job.lock_path.open("a") as f:
        if case == "locked":  # videotab run など、別のプロセスで実行中
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        detail = app.detail("abcdefghijk")
    expected = {"saved_running": "failed", "running": "running", "queued": "queued", "locked": "running"}
    assert detail["status"] == expected.get(case, detail["status"])
    assert detail["finished"] is None


# --- 段の表示名


def test_detail_shows_current_step_labels_for_old_job_json(app):
    job = saved_job(app.root)
    data = job.load()
    for s in data["steps"]:
        if s["name"] == "build":
            s["label"] = "組み立て"  # 表示名を変える前に作った曲
    data["steps"].append({"name": "extra", "label": "手で足した段", "status": "pending"})
    job.save(data)
    saved = job.path.read_text(encoding="utf-8")
    steps = app.detail("abcdefghijk")["steps"]
    assert [s["label"] for s in steps] == [lb for _, lb in pipeline.STEPS] + ["手で足した段"]
    assert steps[pipeline.STEP_NAMES.index("build")]["label"] == "タブ譜の組み立て"
    assert job.path.read_text(encoding="utf-8") == saved  # job.json は書き換えない


# --- 読み手の報告


def test_detail_notes_blocks_follow_notes(app):
    job = saved_job(app.root)
    readers = job.workdir / "readers"
    readers.mkdir()
    (readers / "notes_A.md").write_text("- 1〜8 小節\n- **5 小節**に自信がない\n", encoding="utf-8")
    detail = app.detail("abcdefghijk")  # notes.json がまだ無い（読み取りの途中）
    assert list(detail["notes_blocks"]) == list(detail["notes"]) == ["A"]
    assert [b["t"] for b in detail["notes_blocks"]["A"]] == ["list"]  # 末尾の改行で空の段落を作らない

    notes = {"A": "## 構成\n<script>alert(1)</script>", "B": "Bars 9-16", "まとめ役 1": "`2` を直した", "C": 5, "D": None}
    (readers / "notes.json").write_text(json.dumps(notes, ensure_ascii=False), encoding="utf-8")
    detail = app.detail("abcdefghijk")
    assert detail["notes"] == notes  # 原文はそのまま
    assert list(detail["notes_blocks"]) == ["A", "B", "まとめ役 1"]  # 文字列でない値の担当は入れない
    assert detail["notes_blocks"]["A"][1] == {"t": "p", "inline": [{"t": "text", "text": "<script>alert(1)</script>"}]}
    assert detail["notes_blocks"]["まとめ役 1"] == [{"t": "p", "inline": [{"t": "code", "text": "2"}, {"t": "text", "text": " を直した"}]}]

    (readers / "notes.json").write_text("[1, 2]", encoding="utf-8")  # 形が違う
    assert app.detail("abcdefghijk")["notes_blocks"] == {}


def page_section(title):
    """app.html の「// --- title」から次の「// ---」までの本文。"""
    from importlib import resources

    page = resources.files("videotab").joinpath("templates", "app.html").read_text(encoding="utf-8")
    start = page.index(f"// --- {title}\n")
    end = page.index("// ---", start + 1)
    return page[start:end]


def test_report_rendering_uses_no_html_injection():
    section = page_section("読み手の報告")
    assert "createTextNode" in section and "textContent" in section
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "eval", "setAttribute"):
        assert word not in section, word


# --- 曲の削除

JOB = "abcdefghijk"


@pytest.fixture
def served(tmp_path, monkeypatch, fake_probe):
    """削除を試す画面。実行（偽物）は release が立つまで終わらない。"""
    root = tmp_path / "work"
    root.mkdir()
    release, started = threading.Event(), threading.Event()

    def fake_run(self):
        started.set()
        release.wait(10)
        return True

    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ns = types.SimpleNamespace(base=f"http://127.0.0.1:{httpd.server_address[1]}", root=root, app=app,
                               release=release, started=started)  # fmt: skip
    yield ns
    release.set()
    httpd.shutdown()


def delete(s, job_id, **kwargs):
    return call(f"{s.base}/api/jobs/{job_id}", method="DELETE", **kwargs)


def listed(s):
    return [j["id"] for j in json.loads(call(s.base + "/api/jobs")[1])["jobs"]]


def wait_idle(app):
    for _ in range(250):
        if not app.waiting and app.running is None:
            return
        threading.Event().wait(0.02)
    raise AssertionError("順番待ちが終わりません")


def tree(base):
    """base の下のパスと、ファイルなら中身（リンクはたどらない）。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            key = os.path.relpath(path, base)
            if os.path.islink(path):
                out[key] = "-> " + os.readlink(path)
            elif os.path.isfile(path):
                with open(path, "rb") as f:
                    out[key] = f.read()
            else:
                out[key] = "dir"
    return out


@pytest.mark.parametrize("status", ["done", "failed", "stopped"])
def test_delete_finished_song(served, status):
    s = served
    job = saved_job(s.root, status=status)
    (job.workdir / "history" / "old").mkdir(parents=True)
    (job.workdir / f"{JOB}.html").write_text("<p>tab</p>", encoding="utf-8")
    assert listed(s) == [JOB]
    code, body = delete(s, JOB)
    assert (code, json.loads(body)) == (200, {"deleted": JOB})
    assert os.listdir(s.root) == []  # 名前を変えたフォルダも残らない
    assert listed(s) == []
    assert call(f"{s.base}/api/jobs/{JOB}")[0] == 404
    assert delete(s, JOB)[0] == 404  # もう無い


def test_delete_refuses_running_queued_and_locked_songs(served):
    s = served
    saved_job(s.root, status="failed")
    saved_job(s.root, "bbbbbbbbbbb", status="failed")
    assert call(f"{s.base}/api/jobs/{JOB}/retry", {})[0] == 200
    assert s.started.wait(5) and s.app.running == JOB
    assert call(f"{s.base}/api/jobs/bbbbbbbbbbb/retry", {})[0] == 200
    assert s.app.waiting == ["bbbbbbbbbbb"]
    before = tree(s.root)
    assert delete(s, JOB)[0] == 409  # 実行中
    assert delete(s, "bbbbbbbbbbb")[0] == 409  # 順番待ち
    assert tree(s.root) == before
    s.release.set()
    wait_idle(s.app)

    with pipeline.Job(s.root / JOB).hold():  # videotab run などが job.lock を握っている
        code, body = delete(s, JOB)
        assert code == 409 and "消せません" in json.loads(body)["error"]
    assert (s.root / JOB / "job.json").exists()


def test_delete_needs_request_from_page(served):
    s = served
    saved_job(s.root)
    assert delete(s, JOB, header=False)[0] == 403
    assert delete(s, JOB, extra={"Origin": "https://evil.example"})[0] == 403
    assert delete(s, JOB, extra={"Host": "evil.example"})[0] == 403
    assert (s.root / JOB / "job.json").exists()


@pytest.mark.parametrize("job_id", [
    "..", "%2e%2e", "%2E%2E", f"{JOB}%2f..", "a/b", "", "a" * 65, ".", f"{JOB}/", f"{JOB}%0a", f"{JOB}/..",
])  # fmt: skip
def test_delete_rejects_bad_ids(served, tmp_path, job_id):
    s = served
    saved_job(s.root)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "job.json").write_text("{}", encoding="utf-8")
    before = tree(tmp_path)
    assert delete(s, job_id)[0] == 404
    assert tree(tmp_path) == before


def test_other_spelling_is_not_the_song(served):
    s = served
    saved_job(s.root, status="failed")
    before = tree(s.root)
    upper = JOB.upper()
    assert delete(s, upper)[0] == 404  # 大文字と小文字を区別しない場所でも、綴りの違う ID では消さない
    assert call(f"{s.base}/api/jobs/{upper}/retry", {})[0] == 404
    assert call(f"{s.base}/api/jobs/{upper}")[0] == 404
    assert not s.app.waiting and s.app.running is None and not s.started.is_set()
    assert tree(s.root) == before
    for method in (s.app.delete, s.app.detail):
        with pytest.raises(KeyError):
            method(upper)


@pytest.mark.parametrize("state", ["waiting", "running"])
def test_delete_refuses_case_variant_of_queued_or_running_id(served, state):
    s = served
    saved_job(s.root, status="failed")
    with s.app.lock:
        if state == "waiting":
            s.app.waiting.append(JOB.upper())
        else:
            s.app.running = JOB.upper()
    assert delete(s, JOB)[0] == 409
    assert (s.root / JOB / "job.json").exists()
    with s.app.lock:
        s.app.waiting.clear()
        s.app.running = None


@pytest.mark.parametrize("state", ["running", "waiting"])
def test_delete_refuses_song_queued_through_link(served, state):
    s = served
    saved_job(s.root, status="failed")
    saved_job(s.root, "bbbbbbbbbbb", status="failed")
    (s.root / "alias").symlink_to(s.root / JOB, target_is_directory=True)
    if state == "waiting":  # 別の曲で実行を埋めておく
        assert call(f"{s.base}/api/jobs/bbbbbbbbbbb/retry", {})[0] == 200
        assert s.started.wait(5)
    assert call(f"{s.base}/api/jobs/alias/retry", {})[0] == 200  # 一覧はリンクのフォルダも曲として出す
    if state == "running":
        assert s.started.wait(5) and s.app.running == "alias"
    else:
        assert s.app.waiting == ["alias"]
    before = tree(s.root / JOB)
    assert delete(s, JOB)[0] == 409
    assert tree(s.root / JOB) == before


def test_delete_refuses_while_videotab_run_holds_lock(served, monkeypatch):
    from videotab import cli

    s = served
    saved_job(s.root, status="failed")
    seen = []

    def run_and_try_delete(self):
        # videotab run は準備から実行の終わりまで job.lock を握っている。その間に画面から消そうとする
        seen.append(delete(s, JOB)[0])
        return True

    monkeypatch.setattr(pipeline.Job, "run", run_and_try_delete)
    assert cli.main(["run", JOB, "--root", str(s.root)]) == 0
    assert seen == [409]
    assert (s.root / JOB / "job.json").exists()


def test_run_opened_before_rename_stops_without_recreating_folder(served, monkeypatch):
    s = served
    saved_job(s.root, status="failed")
    real_flock = pipeline.fcntl.flock
    state = {"deleted": False}

    def flock_after_delete(fd, op):
        # job.lock を開いてからロックを取るまでの間に、画面から消される
        if op & pipeline.fcntl.LOCK_EX and not state["deleted"]:
            state["deleted"] = True
            s.app.delete(JOB)
        return real_flock(fd, op)

    monkeypatch.setattr(pipeline.fcntl, "flock", flock_after_delete)
    job = pipeline.Job(s.root / JOB)
    with pytest.raises(pipeline.Busy):
        with job.hold():
            raise AssertionError("消えたフォルダで実行してはいけない")
    assert state["deleted"] and os.listdir(s.root) == []  # 元の ID のフォルダを作り直さない


@pytest.mark.parametrize("dangling", [False, True])
def test_delete_refuses_song_whose_lock_is_a_link(served, tmp_path, dangling):
    s = served
    job = saved_job(s.root, status="failed")
    target = tmp_path / "outside.txt"
    if not dangling:
        target.write_text("大事なファイル", encoding="utf-8")
        os.utime(target, (1_000_000_000, 1_000_000_000))
    job.lock_path.unlink(missing_ok=True)
    job.lock_path.symlink_to(target)
    before = tree(s.root)
    assert delete(s, JOB)[0] == 409
    assert call(f"{s.base}/api/jobs/{JOB}/retry", {})[0] == 409
    assert tree(s.root) == before
    if dangling:
        assert not os.path.lexists(target)  # 壊れたリンクの先は作らない
    else:
        assert target.read_text(encoding="utf-8") == "大事なファイル" and target.stat().st_mtime == 1_000_000_000


def test_delete_leaves_links_and_what_they_point_to(served, tmp_path):
    s = served
    saved_job(s.root, status="done")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("残す", encoding="utf-8")
    (s.root / "alias").symlink_to(s.root / JOB, target_is_directory=True)
    assert delete(s, "alias")[0] == 404  # root の直下のリンク
    assert (s.root / "alias").is_symlink() and (s.root / JOB / "job.json").exists()

    (s.root / JOB / "outside").symlink_to(outside, target_is_directory=True)  # 曲の中の外向きのリンク
    (s.root / "alias").unlink()
    assert delete(s, JOB)[0] == 200
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "残す"


def test_delete_only_songs_but_also_broken_ones(served):
    s = served
    (s.root / "notasong").mkdir()
    (s.root / "notasong" / "video.mp4").write_bytes(b"x")
    assert delete(s, "notasong")[0] == 404
    assert (s.root / "notasong" / "video.mp4").exists()
    (s.root / "afile").write_text("x", encoding="utf-8")
    assert delete(s, "afile")[0] == 404 and (s.root / "afile").exists()

    (s.root / JOB).mkdir()
    (s.root / JOB / "job.json").write_text("{壊れた", encoding="utf-8")
    assert delete(s, JOB)[0] == 200
    (s.root / "ccccccccccc").mkdir()
    (s.root / "ccccccccccc" / "meta.json").write_text("{}", encoding="utf-8")
    assert delete(s, "ccccccccccc")[0] == 200
    assert sorted(os.listdir(s.root)) == ["afile", "notasong"]


def test_same_file_twice_makes_two_songs(served):
    s = served
    s.release.set()
    first = upload(s.base, data=b"same")
    second = upload(s.base, data=b"same")
    assert first[0] == second[0] == 201 and first[1]["id"] != second[1]["id"]  # 続きは「やり直す」で行う
    wait_idle(s.app)
    assert sorted(listed(s)) == sorted([first[1]["id"], second[1]["id"]])
    assert delete(s, first[1]["id"])[0] == 200
    assert listed(s) == [second[1]["id"]]


def test_delete_failure_is_500_and_leftover_is_hidden(served, monkeypatch):
    s = served
    saved_job(s.root, status="done")

    def broken(path, *args, **kwargs):
        raise OSError("消せない")

    monkeypatch.setattr(server.shutil, "rmtree", broken)
    code, body = delete(s, JOB)
    assert code == 500 and "消せない" in json.loads(body)["error"]
    (left,) = os.listdir(s.root)
    assert left.startswith(f".deleting-{JOB}-")
    assert listed(s) == []
    assert call(f"{s.base}/api/jobs/{left}")[0] == 404


# --- /files/ のページを画面の出どころから切り離す


def request(url, method="GET", headers=None, body=None):
    """(ステータス, 見出し, 本文) を返す。見出しは付けたものだけ（X-Videotab も自動では付けない）。"""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def grants_cors(headers):
    return [k for k in headers.keys() if k.lower().startswith("access-control-allow-")]


# 読み手が置けるページの例。返さないことを確かめるだけで、テストでは動かさない
EVIL = b'<script>fetch("/api/jobs/abcdefghijk", {method: "DELETE", headers: {"X-Videotab": "1"}})</script>'
TEX = '\\title "曲"\n\\tempo 120\n.\n\\tuning e4 b3 g3 d3 a2 e2\n\\ts 4 4\nr.1\n'
SERVED_FILES = {
    "p001.png": b"\x89PNG\r\n\x1a\n",
    "f0001.jpg": b"\xff\xd8\xff\xe0",
    f"{JOB}.alphatex": TEX.encode("utf-8"),
    "notes.md": b"# notes\n",
}


def test_files_are_sandboxed_and_not_sniffed(served):
    s = served
    saved_job(s.root)
    for name, data in SERVED_FILES.items():
        (s.root / JOB / name).write_bytes(data)
    assert server.FILES_CSP == (
        "sandbox allow-scripts allow-downloads allow-modals allow-popups allow-popups-to-escape-sandbox"
    )
    for name in [*SERVED_FILES, f"{JOB}.html"]:  # <ID>.html は、<ID>.alphatex から組み立てたページ
        status, headers, body = request(f"{s.base}/files/{JOB}/{name}")
        assert status == 200, name
        assert name not in SERVED_FILES or body == SERVED_FILES[name], name
        assert headers.get_all("Content-Security-Policy") == [server.FILES_CSP], name
        assert headers.get_all("X-Content-Type-Options") == ["nosniff"], name
        assert grants_cors(headers) == [], name
    # 画面と同じ出どころ・フォームの送信・最上位の画面の移動は許さない
    tokens = server.FILES_CSP.split()
    assert tokens[0] == "sandbox" and "allow-scripts" in tokens
    for word in ("allow-same-origin", "allow-forms", "allow-top-navigation"):
        assert word not in server.FILES_CSP, word


def test_tab_page_is_built_from_alphatex_and_stored_html_is_never_served(served):
    """タブ譜のページは <ID>.alphatex から組み立てる。読み手が置いた HTML は、<ID>.html の名前でも返さない。"""
    s = served
    saved_job(s.root, status="failed")
    wd = s.root / JOB
    (wd / f"{JOB}.html").write_bytes(EVIL)
    (wd / "evil.html").write_bytes(EVIL)
    page_url = f"{s.base}/files/{JOB}/{JOB}.html"

    def shown():
        detail = json.loads(call(f"{s.base}/api/jobs/{JOB}")[1])["html"]
        return detail, [j["html"] for j in json.loads(call(s.base + "/api/jobs")[1])["jobs"]]

    # <ID>.alphatex が無ければ、<ID>.html があってもページは無い（画面も iframe を作らない）
    assert request(page_url)[0] == 404 and shown() == (None, [None])
    (wd / f"{JOB}.alphatex").write_text(TEX, encoding="utf-8")
    status, headers, body = request(page_url)
    page = body.decode("utf-8")
    assert status == 200 and headers["Content-Type"] == "text/html; charset=utf-8"
    assert EVIL not in body and "<title>曲 - videotab</title>" in page and "小節数: 1" in page
    assert shown() == (f"{JOB}.html", [f"{JOB}.html"])
    assert request(f"{s.base}/files/{JOB}/evil.html")[0] == 404  # ほかの .html は返さない
    # 読み手が書いた alphaTex と meta.json は、ページの中で文字として扱う
    (wd / f"{JOB}.alphatex").write_text(
        '\\title "</title><script>alert(1)</script>"\n\\ts 4 4\n</script><script>alert(2)</script>\n', encoding="utf-8"
    )
    (wd / "meta.json").write_text(
        json.dumps({"title": "<script>alert(3)</script>", "creator": "<img src=x>", "source_url": "javascript:alert(4)"}),
        encoding="utf-8",
    )
    page = request(page_url)[2].decode("utf-8")
    assert "<script>alert(" not in page and "<img src=x>" not in page and "javascript:" not in page
    # 読めない meta.json・大きすぎる・UTF-8 でない alphaTex でも、保存された HTML には戻らない
    (wd / "meta.json").write_text("[", encoding="utf-8")
    assert request(page_url)[0] == 200
    (wd / f"{JOB}.alphatex").write_bytes(b"\xff\xfe")
    assert request(page_url)[0] == 404
    (wd / f"{JOB}.alphatex").write_bytes(b" " * (server.MAX_TEX + 1))
    assert request(page_url)[0] == 404


def test_tab_page_is_the_same_as_the_built_html(served, capsys):
    s = served
    saved_job(s.root)
    wd = s.root / JOB
    (wd / "parts").mkdir()
    (wd / "parts" / "part_A.json").write_text(json.dumps({"1": "r.1", "2": '(0.6{ch "A|m"}).1'}), encoding="utf-8")
    (wd / "score.json").write_text(
        json.dumps({"title": 'A "B" C', "tempo": 137.5, "capo": 2, "tuning": "d4 a3 f3 c3 g2 d2"}), encoding="utf-8"
    )
    (wd / "meta.json").write_text(
        json.dumps({"id": JOB, "title": "Song", "creator": "Creator", "source_url": "https://example.com/v"}),
        encoding="utf-8",
    )
    assert build.run_build(wd) == 0
    built = (wd / f"{JOB}.html").read_text(encoding="utf-8")
    assert '<h1 class="tm-title">A &#34;B&#34; C</h1>' in built
    for text in ("Tempo: 137.5 BPM", "Tuning: D G C F A D", "Capo: 2", "小節数: 2", "（作成: Creator）"):
        assert text in built, text
    status, _, body = request(f"{s.base}/files/{JOB}/{JOB}.html")

    def undated(page):
        return re.sub(r"Generated \d{4}-\d\d-\d\d \d\d:\d\d:\d\d", "Generated", page)

    assert status == 200 and undated(body.decode("utf-8")) == undated(built)


def test_requests_from_sandboxed_page_are_refused(served):
    """/files/ のページからのリクエストは Origin が null になる。X-Videotab を付けても断り、何も変えない。"""
    s = served
    saved_job(s.root, status="failed")
    before = tree(s.root)
    page = {"Origin": "null"}
    assert upload(s.base, extra=page)[0] == 403
    assert call(s.base + "/api/jobs", {}, extra=page)[0] == 403
    assert call(f"{s.base}/api/jobs/{JOB}/retry", {}, extra=page)[0] == 403
    assert delete(s, JOB, extra=page)[0] == 403
    assert tree(s.root) == before  # 曲が増えず、状態が変わらず、フォルダが残る
    assert not s.app.waiting and s.app.running is None and not s.started.is_set()
    assert listed(s) == [JOB]


@pytest.mark.parametrize("path", ["/", "/api/jobs", f"/api/jobs/{JOB}", f"/api/jobs/{JOB}/retry", f"/files/{JOB}/{JOB}.html"])
def test_preflight_is_not_granted(served, path):
    """別の出どころから X-Videotab 付きで送る前の事前確認（OPTIONS）に応じない。"""
    s = served
    saved_job(s.root)
    (s.root / JOB / f"{JOB}.alphatex").write_text(TEX, encoding="utf-8")
    ask = {"Origin": "null", "Access-Control-Request-Method": "DELETE", "Access-Control-Request-Headers": "content-type, x-videotab"}
    status, headers, _ = request(s.base + path, "OPTIONS", ask)
    assert not 200 <= status < 300
    assert grants_cors(headers) == []
    assert headers.get_all("X-Content-Type-Options") == ["nosniff"]


def test_every_response_is_not_sniffed_and_grants_no_cors(served):
    s = served
    saved_job(s.root)
    (s.root / JOB / f"{JOB}.alphatex").write_text(TEX, encoding="utf-8")
    page = {"Origin": "null"}
    cases = [
        ("GET", "/", None, {}, 200),
        ("GET", "/favicon.ico", None, {}, 204),
        ("GET", "/api/jobs", None, page, 200),
        ("GET", f"/api/jobs/{JOB}", None, page, 200),
        ("GET", "/api/jobs/nothing", None, {}, 404),
        ("GET", f"/files/{JOB}/{JOB}.html", None, page, 200),
        ("GET", f"/files/{JOB}/job.json", None, {}, 404),
        ("GET", "/api/jobs", None, {"Host": "evil.example"}, 403),
        ("POST", "/api/jobs", b"{}", {**page, "X-Videotab": "1"}, 403),
        ("POST", "/api/jobs", b"{}", {"X-Videotab": "1"}, 404),  # 曲を作る口は /api/uploads だけ
        ("POST", "/api/uploads?name=a.mp4", b"x", {**page, "X-Videotab": "1"}, 403),
        ("POST", "/api/uploads?name=a.txt", b"x", {"X-Videotab": "1"}, 400),
        ("POST", f"/api/jobs/{JOB}/retry", b"{", {"X-Videotab": "1"}, 400),
        ("DELETE", f"/api/jobs/{JOB}", None, {**page, "X-Videotab": "1"}, 403),
        ("OPTIONS", "/api/jobs", None, page, 501),  # 標準ライブラリが返すエラー
        ("PUT", "/api/jobs", b"{}", page, 501),
    ]
    for method, path, body, headers, expected in cases:
        status, got, _ = request(s.base + path, method, headers, body)
        assert status == expected, (method, path)
        assert got.get_all("X-Content-Type-Options") == ["nosniff"], (method, path)
        assert grants_cors(got) == [], (method, path)
    assert (s.root / JOB / "job.json").exists()


def test_result_iframe_is_sandboxed_like_files():
    import re
    from importlib import resources

    page = resources.files("videotab").joinpath("templates", "app.html").read_text(encoding="utf-8")
    frames = re.findall(r'el\("iframe", (\{[^}]*\})', page)
    assert len(frames) == 1 and "<iframe" not in page  # タブ譜の iframe はここで作るものだけ
    attr = re.search(r'sandbox: "([^"]*)"', frames[0])
    assert attr and attr.group(1) == server.FILES_SANDBOX
    assert frames[0].index("sandbox:") < frames[0].index("src:")  # src より先に付ける


def test_switching_songs_keeps_previous_view_until_new_one_is_ready():
    section = page_section("一覧と詳細")
    select = section.split("function select(")[1]
    assert 'textContent = ""' not in select and "innerHTML" not in select  # 選んだ瞬間に詳細の欄を空にしない
    assert 'classList.add("switching")' in select and 'setAttribute("aria-busy", "true")' in select
    assert "loadDetail(id)" in select  # 一覧を待たずに詳細を取りに行く
    assert "dropPendingResult()" in select.split("\n  }\n")[0]  # 前に選んだ曲の読み込み待ちのタブ譜を捨てる
    load = section.split("function loadResult(")[1].split("\n  }\n")[0]
    assert 'addEventListener("load", swap)' in load and "setTimeout(swap, RESULT_WAIT)" in load
    assert "p.id !== selected" in load  # 選んだ曲のものでなければ差し替えない
    assert "shownResult = p.key" in load  # 見えるようになったパネルの結果キーを記録する
    render = section.split("function renderDetail(")[1].split("\n  }\n")[0]
    assert "shownResult = resultKey" not in render  # 読み込み待ちの間は、見えているパネルの結果キーのまま
    assert 'area.result.textContent = "";  // HTML の無い曲では、前のパネルをすぐ消す\n        shownResult = null;' in render


def test_log_box_is_kept_between_refreshes_so_the_reading_position_stays():
    # 実行中は 2 秒ごとに詳細を描き直す。ログの欄ごと作り直すと、スクロール位置が先頭に戻る
    section = page_section("一覧と詳細")
    render = section.split("function renderDetail(")[1].split("\n  }\n")[0]
    assert 'bottom.textContent = ""' not in render
    assert "if (c !== logBox) bottom.removeChild(c);" in render  # ログの欄は残し、ほかを作り直す
    assert "bottom.insertBefore(notes, logBox);" in render and 'updateLog(logBox.querySelector("pre.log"), d.log);' in render
    update = section.split("function updateLog(")[1].split("\n  }\n")[0]
    # 増えた行を足し、頭から外れた行を除く。いちばん下にいるときだけ追い、途中では同じ行を同じ位置に残す
    assert "pre.removeChild(pre.firstChild)" in update and 'pre.appendChild(el("div", { text: lines[j] }))' in update
    assert "pre.scrollTop = stick ? pre.scrollHeight : Math.max(keepTop, 0);" in update
    assert "innerHTML" not in update


def test_retry_row_has_title_named_fields_and_button_saying_the_step():
    import re
    from importlib import resources

    page = resources.files("videotab").joinpath("templates", "app.html").read_text(encoding="utf-8")
    build = page_section("一覧と詳細").split("function buildRetryRow(")[1].split("\n  }\n\n")[0]
    # 見出しと、段とエージェントの名前。段の選択肢は段の名前だけで、読み上げ名は前のまま
    assert 'el("span", { class: "info-title", text: "やり直す" })' in build
    assert 'text: "どの段から" }), sel]' in build and 'text: "読み取りに使う AI" }), eng]' in build
    assert 'el("option", { value: s.name, text: s.label })' in build
    assert '"aria-label": "やり直す段"' in build and '"aria-label": "読み取りに使うエージェント"' in build
    assert "やり直す:" not in build and '"実行"' not in build and '" から"' not in build
    # ボタンは選んだ段を言い、初めにも段を変えたときにも文言を作る。押すと選んだ段からやり直す
    assert 'go.textContent = (o ? o.textContent : "") + "からやり直す"' in build
    assert build.index("goLabel();") < build.index('sel.addEventListener("change"')  # 初期選択のあとで作る
    assert re.search(r'sel\.addEventListener\("change", function \(\) \{[^}]*goLabel\(\);', build)
    assert 'class: "retry-go", onclick: function () { retry(sel.value); }' in build
    # ボタンはモデルの段（.pick-row）に入れず、その下の独立した段に置く
    rows = build.split('row.el = el("div", { class: "actions retry" }, [')[1]
    assert 'el("div", { class: "pick-row" }, [picker.root]),' in rows
    assert rows.index('class: "pick-row"') < rows.index('el("div", { class: "retry-go-row" }, [go])')
    # 枠と文字だけのアクセント色。曲の情報とは線で区切る
    css = page.split("<style>")[1].split("</style>")[0]
    go = re.search(r"button\.retry-go \{([^}]*)\}", css).group(1)
    for decl in ("border-color: var(--accent)", "color: var(--accent)", "background: var(--panel)", "font-weight: 600"):
        assert decl in go, decl
    assert "border-top: 1px solid var(--line)" in re.search(r"\.actions\.retry \{([^}]*)\}", css).group(1)
    # 狭い画面では、4 つの欄の名前を同じ幅にして select の左端をそろえ、「その他」の入力欄も同じだけ右に寄せる
    narrow = [b.split("\n  }\n")[0] for b in css.split("@media (max-width: 560px) {")[1:]]
    retry_narrow = next(b for b in narrow if ".retry-main" in b)
    label = re.search(r"\.actions\.retry \{ --retry-label: ([\d.]+)rem; \}", retry_narrow)
    assert label and float(label.group(1)) == 9 * 0.8  # 名前の文字（0.8rem）の 9 文字分
    names = re.search(r"([^{}\n]+)\{ flex: none; width: var\(--retry-label\); \}", retry_narrow).group(1)
    assert {s.strip() for s in names.split(",")} == {".retry-main .pick > span", ".actions.retry .pick-row .pick > span"}
    assert ".actions.retry .picker .pick-custom { margin-left: calc(var(--retry-label) + 6px); }" in retry_narrow
    for rule in (r"\.retry-main \.pick \{([^}]*)\}", r"\.picker \.pick, \.pick-row \.engine-pick \{([^}]*)\}"):
        assert "gap: 6px" in re.search(rule, css).group(1)  # 名前と select の間は どの欄も 6px
    # 新規フォームのモデル欄は前のまま
    assert any(".pick-row .pick > span { flex: none; width: 5.5em; }" in b for b in narrow)
    assert css.count("--retry-label") == len(re.findall(r"--retry-label", retry_narrow))


def test_startup_removes_leftovers_of_deleting(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    root = tmp_path / "work"
    (root / ".deleting-abcdefghijk-0123abcd" / "frames").mkdir(parents=True)
    (root / ".deleting-abcdefghijk-0123abcd" / "job.json").write_text("{}", encoding="utf-8")
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "x.txt").write_text("x", encoding="utf-8")
    (root / ".deleting-link").symlink_to(keep, target_is_directory=True)
    saved_job(root, status="done")
    server.App(root)
    assert sorted(os.listdir(root)) == [".deleting-link", JOB]  # リンクはたどらず、消さない
    assert (keep / "x.txt").exists()
