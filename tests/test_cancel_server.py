"""画面から曲を止める（POST /api/jobs/<ID>/cancel）。"""

import json
import threading
import types
from http.server import ThreadingHTTPServer

import pytest
from test_server import call, saved_job, wait_idle

from videotab import pipeline, server

JOB, OTHER = "abcdefghijk", "bbbbbbbbbbb"


@pytest.fixture
def served(tmp_path, monkeypatch):
    """実行（偽物）は、止める合図か release が出るまで終わらない。実行した曲の ID を ran に残す。

    slow が立っていると、止める合図では終わらない（子プロセスが止まり終えるまでの間の代わり）。
    """
    root = tmp_path / "work"
    root.mkdir()
    release, started, slow, ran = threading.Event(), threading.Event(), threading.Event(), []

    def fake_run(self):
        ran.append(self.workdir.name)
        started.set()
        for _ in range(500):
            if release.is_set() or (self.cancel.is_set() and not slow.is_set()):
                break
            threading.Event().wait(0.02)
        data = self.load()
        data["status"] = "stopped" if self.cancel.is_set() else "done"
        self.save(data)
        return not self.cancel.is_set()

    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ns = types.SimpleNamespace(base=f"http://127.0.0.1:{httpd.server_address[1]}", root=root, app=app,
                               release=release, started=started, slow=slow, ran=ran)  # fmt: skip
    yield ns
    release.set()
    httpd.shutdown()


def cancel(s, job_id, **kwargs):
    return call(f"{s.base}/api/jobs/{job_id}/cancel", {}, **kwargs)


def retry(s, job_id):
    return call(f"{s.base}/api/jobs/{job_id}/retry", {})


def detail(s, job_id):
    return json.loads(call(f"{s.base}/api/jobs/{job_id}")[1])


def test_cancel_running_song(served):
    s = served
    saved_job(s.root, status="failed")
    assert retry(s, JOB)[0] == 200
    assert s.started.wait(5)
    job = s.app.running_job
    assert detail(s, JOB)["stopping"] is False
    assert cancel(s, JOB) == (200, json.dumps({"id": JOB}).encode())
    assert job.cancel.is_set()
    wait_idle(s.app)
    assert detail(s, JOB)["status"] == "stopped"
    assert s.app.running_job is None


def test_detail_shows_stopping_until_run_ends(served):
    s = served
    s.slow.set()
    saved_job(s.root, status="failed")
    assert retry(s, JOB)[0] == 200
    assert s.started.wait(5)
    assert cancel(s, JOB)[0] == 200
    d = detail(s, JOB)
    assert d["status"] == "running" and d["stopping"] is True
    assert retry(s, JOB)[0] == 409  # 止まり終えるまでは、やり直せない
    s.release.set()
    wait_idle(s.app)
    assert detail(s, JOB)["stopping"] is False


def test_cancel_queued_song_takes_it_out_of_line(served):
    s = served
    saved_job(s.root, status="failed")
    saved_job(s.root, OTHER, status="failed")
    assert retry(s, JOB)[0] == 200
    assert s.started.wait(5)
    assert retry(s, OTHER)[0] == 200
    assert s.app.waiting == [OTHER]
    assert cancel(s, OTHER)[0] == 200
    assert s.app.waiting == []
    assert detail(s, OTHER)["status"] == "stopped"
    s.release.set()
    wait_idle(s.app)
    assert s.ran == [JOB]  # 外した曲は実行されない


def test_song_taken_out_of_line_can_be_queued_again(served):
    s = served
    for job_id in (JOB, OTHER, "ccccccccccc"):
        saved_job(s.root, job_id, status="failed")
    assert retry(s, JOB)[0] == 200
    assert s.started.wait(5)
    assert retry(s, OTHER)[0] == 200
    assert retry(s, "ccccccccccc")[0] == 200
    assert cancel(s, OTHER)[0] == 200
    assert retry(s, OTHER)[0] == 200  # 外したあとに入れ直すと、列の最後に並ぶ
    assert s.app.waiting == ["ccccccccccc", OTHER]
    s.release.set()
    wait_idle(s.app)
    assert s.ran == [JOB, "ccccccccccc", OTHER]


def test_cancel_refuses_songs_not_running_here(served):
    s = served
    saved_job(s.root, status="done")
    code, body = cancel(s, JOB)
    assert code == 400 and "実行中でも順番待ちでもありません" in json.loads(body)["error"]
    with pipeline.Job(s.root / JOB).hold():  # videotab run などが実行中
        code, body = cancel(s, JOB)
        assert code == 409 and "videotab run" in json.loads(body)["error"]
    assert cancel(s, "zzzzzzzzzzz")[0] == 404
    assert cancel(s, "..%2f..")[0] == 404
    assert detail(s, JOB)["status"] == "done"


def test_cancel_needs_request_from_page(served):
    s = served
    saved_job(s.root, status="failed")
    assert retry(s, JOB)[0] == 200
    assert s.started.wait(5)
    assert cancel(s, JOB, header=False)[0] == 403
    assert cancel(s, JOB, extra={"Origin": "https://example.com"})[0] == 403
    assert not s.app.running_job.cancel.is_set()
