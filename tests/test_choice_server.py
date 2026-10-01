"""画面の API での、モデルと推論の強さの選択。"""

import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from test_server import call, upload

from videotab import add, pipeline, server

JOB = "abcdefghijk"


@pytest.fixture
def app_server(tmp_path, monkeypatch, fake_probe):
    """両方のエンジンが使える画面。実行は段を全部「済み」にするだけの偽物。

    取り込んだ曲の ID は JOB にする（乱数を使わない）。"""
    monkeypatch.setattr(add, "new_id", lambda name: JOB)
    root = tmp_path / "work"
    root.mkdir()
    ran = threading.Semaphore(0)

    def fake_run(self):
        data = self.load()
        for s in data["steps"]:
            s["status"] = "done"
        data["status"] = "done"
        self.save(data)
        ran.release()
        return True

    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    app = server.App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, root, ran, app
    httpd.shutdown()


def wait_idle(app):
    for _ in range(200):
        if not app.waiting and app.running is None:
            return
        threading.Event().wait(0.02)
    raise AssertionError("順番待ちが終わりません")


def job_json(root):
    return json.loads((root / JOB / "job.json").read_text(encoding="utf-8"))


def mark_failed(root):
    job = pipeline.Job(root / JOB)
    data = job.load()
    data["status"] = "failed"
    for s in data["steps"][4:]:
        s["status"] = "pending"
    data["steps"][4]["status"] = "failed"
    job.save(data)


def submit(base, **query):
    """動画を送る。値が None の項目は送らない（普段の設定）。"""
    return upload(base, **{k: v for k, v in query.items() if v is not None})


def retry(base, **body):
    return call(base + f"/api/jobs/{JOB}/retry", body)


def test_submit_stores_only_chosen_items(app_server):
    base, root, ran, app = app_server
    status, _ = submit(base, engine="claude", model="opus", effort="max")
    assert status == 201 and ran.acquire(timeout=5)
    assert job_json(root)["choice"] == {"model": "opus", "effort": "max"}
    detail = json.loads(call(base + f"/api/jobs/{JOB}")[1])
    assert detail["choice"] == {"model": "opus", "effort": "max"}


def test_submit_without_choice_matches_old_shape(app_server):
    base, root, ran, app = app_server
    status, _ = submit(base, engine="claude", model=None)
    assert status == 201 and ran.acquire(timeout=5)
    assert "choice" not in job_json(root)
    assert json.loads(call(base + f"/api/jobs/{JOB}")[1])["choice"] == {"model": None, "effort": None}


@pytest.mark.parametrize("query", [
    {"engine": "claude", "effort": "minimal"},
    {"engine": "codex", "effort": "max"},
    {"engine": "claude", "model": "a b"},
    {"engine": "claude", "model": "-opus"},
    {"engine": "codex", "model": 'gpt"x'},
    {"engine": "claude", "model": "a" * 129},
])  # fmt: skip
def test_submit_rejects_bad_values(app_server, query, fake_probe):
    base, root, ran, app = app_server
    status, body = submit(base, **query)
    assert status == 400 and body["error"]
    assert not (root / JOB).exists() and fake_probe == []  # 動画を読む前に断る


def start_failed(base, root, ran, app, **body):
    submit(base, **body)
    assert ran.acquire(timeout=5)
    wait_idle(app)
    mark_failed(root)


@pytest.mark.parametrize("body, expected_engine, expected", [
    ({}, "claude", {"model": "opus", "effort": "max"}),  # キーなし: 引き継ぐ
    ({"engine": "claude"}, "claude", {"model": "opus", "effort": "max"}),
    ({"engine": "codex"}, "codex", None),  # エンジンが変わると普段の設定
    ({"engine": "codex", "effort": "high"}, "codex", {"effort": "high"}),
    ({"model": None}, "claude", {"effort": "max"}),  # null: 普段の設定、ほかは引き継ぐ
    ({"effort": "low"}, "claude", {"model": "opus", "effort": "low"}),
    ({"model": None, "effort": None}, "claude", None),
    ({"model": "claude-opus-5-5[1m]", "effort": "high"}, "claude", {"model": "claude-opus-5-5[1m]", "effort": "high"}),
])  # fmt: skip
def test_retry_rules(app_server, body, expected_engine, expected):
    base, root, ran, app = app_server
    start_failed(base, root, ran, app, engine="claude", model="opus", effort="max")
    status, _ = retry(base, step="read", **body)
    assert status == 200 and ran.acquire(timeout=5)
    data = job_json(root)
    assert data["engine"] == expected_engine and data.get("choice") == expected


@pytest.mark.parametrize("body", [
    {"effort": "minimal"},  # Claude Code に無い
    {"engine": "codex", "effort": "max"},  # 新しいエンジンで検査する
    {"model": " "},
    {"model": "--x"},
    {"model": 3},
])  # fmt: skip
def test_retry_rejects_bad_values_without_changing_job(app_server, body):
    base, root, ran, app = app_server
    start_failed(base, root, ran, app, engine="claude", model="opus", effort="max")
    before = job_json(root)
    status, text = retry(base, step="read", **body)
    assert status == 400 and json.loads(text)["error"]
    assert job_json(root) == before


def test_detail_hides_values_that_fail_checks(app_server):
    base, root, ran, app = app_server
    start_failed(base, root, ran, app, engine="claude")
    job = pipeline.Job(root / JOB)
    data = job.load()
    data["choice"] = {"model": "bad model", "effort": "max", "other": "SECRET"}
    job.save(data)
    detail = json.loads(call(base + f"/api/jobs/{JOB}")[1])
    assert detail["choice"] == {"model": None, "effort": "max"}
    data["engine"] = "codex"  # Codex に max は無い
    job.save(data)
    assert json.loads(call(base + f"/api/jobs/{JOB}")[1])["choice"] == {"model": None, "effort": None}


def test_old_job_json_without_choice(app_server):
    base, root, ran, app = app_server
    start_failed(base, root, ran, app)
    job = pipeline.Job(root / JOB)
    data = job.load()
    del data["engine"]
    job.save(data)
    detail = json.loads(call(base + f"/api/jobs/{JOB}")[1])
    assert detail["engine"] == "claude" and detail["choice"] == {"model": None, "effort": None}


def test_list_has_agent_options_read_each_time(app_server, tmp_path, monkeypatch):
    base, root, ran, app = app_server
    got = json.loads(call(base + "/api/jobs")[1])["agent_options"]
    assert got["claude"]["usual"] == {"model": "CLI の既定", "effort": "モデルの既定", "effort_with_model": "モデルの既定"}
    assert got["codex"]["models"] is None and got["codex"]["model_hint"] is None

    d = tmp_path / "claude-home"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({"model": "sonnet", "hooks": {"x": "SECRET"}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    text = call(base + "/api/jobs")[1].decode()
    assert json.loads(text)["agent_options"]["claude"]["usual"]["model"] == "sonnet"
    assert "SECRET" not in text and "hooks" not in text and str(d) not in text

    (d / "settings.json").write_text("{broken", encoding="utf-8")  # 壊れていたら既定の表示
    text = call(base + "/api/jobs")[1].decode()
    assert json.loads(text)["agent_options"]["claude"]["usual"]["model"] == "CLI の既定"
    assert "ユーザー設定" not in text


def test_non_object_body_is_rejected(app_server):
    base, root, ran, app = app_server
    assert call(base + f"/api/jobs/{JOB}/retry", [1, 2])[0] == 400
