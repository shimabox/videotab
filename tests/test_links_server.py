"""画面（videotab serve）のリンク対策: /files/、起動時の一時フォルダの掃除、順番待ちの中断の記録。"""

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import send
from test_links import fake_ffmpeg, stages_in
from trees import make_outside, snapshot

from videotab import frames, inside, pipeline, server

JOB = "abcdefghijk"


def wait_idle(app, seconds=10):
    for _ in range(int(seconds / 0.05)):
        if not app.waiting and app.running is None:
            return True
        threading.Event().wait(0.05)
    return False


# --- /files/


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    root = tmp_path / "work"
    wd = root / JOB
    wd.mkdir(parents=True)
    out = make_outside(tmp_path)
    (out / "x.md").write_text("outside")
    (out / "d" / "y.md").write_text("outside y")
    (out / "h.md").write_text("outside h")
    (wd / "notes.md").write_text("# notes")
    (wd / "sym.md").symlink_to(out / "x.md")
    (wd / "sub").symlink_to(out / "d")
    os.link(out / "h.md", wd / "hard.md")
    (wd / "inner.md").symlink_to("notes.md")  # 中を指すリンクは返す
    os.mkfifo(wd / "fifo.md")
    # タブ譜のページのもとになる <ID>.alphatex が、外を指すリンク
    (out / "x.alphatex").write_text('\\title "outside"\n')
    (wd / f"{JOB}.alphatex").symlink_to(out / "x.alphatex")
    app = server.App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_files_serves_plain_files_inside_only(files):
    app, base = files
    assert get(f"{base}/files/{JOB}/notes.md") == (200, b"# notes")
    assert get(f"{base}/files/{JOB}/inner.md") == (200, b"# notes")
    for rel in ("sym.md", "sub/y.md", "hard.md", "fifo.md", f"{JOB}.alphatex"):
        assert get(f"{base}/files/{JOB}/{rel}")[0] == 404, rel
        assert app.read_file(JOB, rel) is None
    assert app.read_file(JOB, "notes.md")[1] == b"# notes"
    # 外を指す <ID>.alphatex からは、タブ譜のページを組み立てない
    assert get(f"{base}/files/{JOB}/{JOB}.html")[0] == 404 and app.page(JOB) is None


def test_files_does_not_follow_a_folder_swapped_after_the_check(files, tmp_path, monkeypatch):
    # パスを確かめたあとに、途中のフォルダを外へのリンクに差し替えられても、外のファイルを返さない
    app, base = files
    wd = app.root / JOB
    (wd / "pages").mkdir()
    (wd / "pages" / "y.md").write_text("inside y")
    assert app.read_file(JOB, "pages/y.md")[1] == b"inside y"
    checked = server.App.file_path

    def swap_after_check(self, job_id, rel):
        found = checked(self, job_id, rel)
        if rel == "pages/y.md" and not (wd / "pages").is_symlink():
            (wd / "pages").rename(wd / "pages.real")
            (wd / "pages").symlink_to(tmp_path / "outside" / "d")  # y.md（中身は outside y）がある外のフォルダ
        return found

    monkeypatch.setattr(server.App, "file_path", swap_after_check)
    assert app.read_file(JOB, "pages/y.md") is None
    assert get(f"{base}/files/{JOB}/pages/y.md")[0] == 404


def test_files_are_sandboxed_and_refusals_are_not_sniffed(files):
    _, base = files
    with urllib.request.urlopen(f"{base}/files/{JOB}/notes.md") as r:
        assert r.headers["Content-Security-Policy"] == server.FILES_CSP
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    for rel in ("sym.md", "hard.md"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/files/{JOB}/{rel}")
        assert e.value.code == 404 and e.value.headers["X-Content-Type-Options"] == "nosniff"
        e.value.close()


# --- 起動時の一時フォルダの掃除


def test_startup_removes_unused_temporary_folders_and_does_not_list_them(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    root = tmp_path / "work"
    (root / ".videotab-tmp-old" / "sub").mkdir(parents=True)
    (root / ".videotab-tmp-old" / "raw_00001.png").write_bytes(b"x")
    (root / ".videotab-tmp-half").mkdir()
    (root / ".videotab-tmp-half" / ".lock").write_bytes(b"")
    out = make_outside(tmp_path)
    (root / ".videotab-tmp-old" / "sub" / "to_out").symlink_to(out / "d")
    before = snapshot(out)
    (root / JOB).mkdir()
    (root / JOB / "meta.json").write_text(json.dumps({"id": JOB, "title": "曲"}))
    app = server.App(root)
    assert stages_in(root) == []
    assert [j["id"] for j in app.list_jobs()] == [JOB]
    assert snapshot(out) == before


def extract_in_thread(wd):
    result = {}

    def work():
        try:
            result["n"] = frames.extract(wd, force=True)
        except BaseException as e:  # noqa: BLE001
            result["error"] = e

    t = threading.Thread(target=work)
    t.start()
    return t, result


def video_song(tmp_path):
    root = (tmp_path / "work").resolve()
    wd = root / JOB
    wd.mkdir(parents=True)
    (wd / "video.mp4").write_bytes(b"v")
    (wd / "meta.json").write_text(json.dumps({"id": JOB, "title": "曲"}))
    return root, wd


@pytest.mark.parametrize("pause", ["made", "lock-opened", "running"])
def test_startup_keeps_temporary_folder_in_use(tmp_path, monkeypatch, pause):
    """一時フォルダを作る処理のどこで止めて画面を起動しても、その一時フォルダは消さず、取り込みも成功する。"""
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    root, wd = video_song(tmp_path)
    paused, go = threading.Event(), threading.Event()

    def hold():
        paused.set()
        assert go.wait(10)

    real_mkdir, real_open = os.mkdir, os.open

    def mkdir(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        if pause == "made" and str(path).startswith(inside.STAGE_PREFIX):
            hold()  # フォルダを作った直後

    def open_(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if pause == "lock-opened" and path == inside.STAGE_INNER_LOCK and flags & os.O_EXCL:
            hold()  # 中の .lock を開いた直後（flock の前）
        return fd

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "open", open_)
    fake_ffmpeg(monkeypatch, during=hold if pause == "running" else None)  # 外部処理の実行中
    swept = []
    real_sweep = inside.sweep_stages
    monkeypatch.setattr(inside, "sweep_stages", lambda place: swept.append(real_sweep(place)) or swept[-1])

    t, result = extract_in_thread(wd)
    assert paused.wait(10)
    assert len(stages_in(root)) == 1
    started = threading.Thread(target=lambda: server.App(root))
    started.start()
    started.join(0.5)
    if pause != "running":
        assert started.is_alive()  # 作る側が中のロックを握るまで、掃除は待つ
    go.set()
    started.join(10)
    t.join(10)
    assert not started.is_alive() and not t.is_alive()
    assert swept == [[]]  # 使っている一時フォルダは消さない
    assert result == {"n": 3}
    assert len(list((wd / "frames").iterdir())) == 3
    assert stages_in(root) == []


# --- 順番待ちの中断の記録


def test_worker_moves_on_when_recording_interruption_fails(tmp_path, monkeypatch, capsys, plain_ids):
    def exploding(self):
        raise RuntimeError("止まった")

    def cannot_record(self):
        raise OSError(5, "Input/output error", str(tmp_path / "outside"))

    monkeypatch.setattr(pipeline.Job, "run", exploding)
    monkeypatch.setattr(pipeline.Job, "mark_interrupted", cannot_record)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(tmp_path / "work")
    send(app, "aaaaaaaaaaa")
    send(app, "bbbbbbbbbbb")
    assert wait_idle(app)
    printed = capsys.readouterr().out
    assert printed.count("の中断を記録できませんでした（Input/output error）") == 2
    assert str(tmp_path) not in printed


def test_worker_moves_on_when_job_json_became_a_folder(tmp_path, monkeypatch, capsys, plain_ids):
    """job.json がフォルダに差し替えられて記録できなくても、次の曲を実行する。"""
    ran = []

    def step(self, name, engine):
        if self.workdir.name == "aaaaaaaaaaa":
            os.rename(self.path, self.workdir / "job.json.old")
            self.path.mkdir()
            raise pipeline.StepError("失敗")
        ran.append(name)

    monkeypatch.setattr(pipeline.Job, "_run_step", step)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(tmp_path / "work")
    send(app, "aaaaaaaaaaa")
    send(app, "bbbbbbbbbbb")
    assert wait_idle(app)
    assert ran == pipeline.STEP_NAMES
    assert pipeline.Job(tmp_path / "work" / "bbbbbbbbbbb").load()["status"] == "done"
    assert "aaaaaaaaaaa の中断を記録できませんでした" in capsys.readouterr().out


def test_startup_skips_songs_whose_job_json_cannot_be_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    root = tmp_path / "work"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "job.json").write_text("{broken")
    job = pipeline.Job.create(root / JOB)
    data = job.load()
    data["status"] = "running"
    job.save(data)
    server.App(root)  # 起動は止めない
    assert "broken の中断を記録できませんでした" in capsys.readouterr().out
    assert job.load()["status"] == "stopped"
