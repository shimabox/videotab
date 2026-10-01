"""画面のアップロード（POST /api/uploads）: 受け付け、本文を読む前の断り、後始末、詳細の元動画のページ。"""

import json
import shutil
import socket
import threading
import types
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest
from conftest import PROBED
from test_server import call, upload

from videotab import add, inside, pipeline, server
from videotab.workdir import ID_PATTERN


@pytest.fixture
def up(tmp_path, monkeypatch, fake_probe):
    """アップロードを試す画面。実行（偽物）は何もせず、実行した曲の ID を ran に残す。"""
    root = tmp_path / "work"
    ran = []

    def fake_run(self):
        ran.append(self.workdir.name)
        return True

    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": False})
    app = server.App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ns = types.SimpleNamespace(base=f"http://127.0.0.1:{httpd.server_address[1]}", port=httpd.server_address[1],
                               root=root, app=app, ran=ran, probed=fake_probe)  # fmt: skip
    yield ns
    httpd.shutdown()


def everything(root):
    """置き場の中身（共通のロックのファイルは除く）。"""
    return sorted(p.name for p in root.iterdir() if p.name != inside.STAGE_LOCK) if root.exists() else []


def raw(s, headers, body=b"", *, path=None, query=None, close_write=False):
    """ソケットで直接送る。本文を送り切らなくても、応答（状態, JSON）が来るまで待つ（来なければ timeout で失敗）。"""
    path = path or "/api/uploads?" + urlencode({"name": "clip.mp4", **(query or {})})
    head = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{s.port}"] + [f"{k}: {v}" for k, v in headers.items()]
    with socket.create_connection(("127.0.0.1", s.port), timeout=5) as sock:
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        if close_write:
            sock.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, json.loads(data.split(b"\r\n\r\n", 1)[1])


PAGE = {"X-Videotab": "1"}


def test_upload_success_creates_song_and_queues_it(up):
    status, body = upload(up.base, data=b"\x00video\xff", name="My Clip.webm", title="曲", creator="作成者",
                          source_url="https://example.com/v")  # fmt: skip
    assert status == 201
    job_id = body["id"]
    assert ID_PATTERN.fullmatch(job_id) and job_id.startswith("My-Clip-")
    wd = up.root / job_id
    assert (wd / "video.webm").read_bytes() == b"\x00video\xff"
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"id": job_id, "title": "曲", "creator": "作成者", "source_url": "https://example.com/v",
                    "source_file": "My Clip.webm", **PROBED}  # fmt: skip
    data = pipeline.Job(wd).load()
    assert data["status"] == "queued" and data["engine"] == "claude" and "url" not in data
    for _ in range(100):
        if up.ran:
            break
        threading.Event().wait(0.02)
    assert up.ran == [job_id]  # 順番待ちに入って実行された
    assert everything(up.root) == [job_id]  # 一時フォルダは残らない


def test_upload_into_missing_root(up):
    shutil.rmtree(up.root)  # 起動のあとに置き場が無くなっても、取り込みで作る
    status, body = upload(up.base)
    assert status == 201 and (up.root / body["id"] / "video.mp4").exists()


@pytest.mark.parametrize("headers", [
    {},  # 画面からの操作の見出しが無い
    {**PAGE, "Origin": "https://evil.example"},
    {**PAGE, "Origin": "null"},
    {**PAGE, "Host": "evil.example"},
])  # fmt: skip
def test_not_from_page_is_403_without_reading_body(up, headers):
    if "Host" in headers:
        head = {k: v for k, v in headers.items() if k != "Host"}
        status, body = raw_with_host(up, head, headers["Host"])
    else:
        status, body = raw(up, {**headers, "Content-Length": "100000000"})  # 本文は送らない
    assert status == 403 and body["error"] == "画面からの操作ではありません"
    assert everything(up.root) == [] and up.probed == []


def raw_with_host(s, headers, host):
    path = "/api/uploads?name=clip.mp4"
    head = [f"POST {path} HTTP/1.1", f"Host: {host}", "Content-Length: 100000000"] + [f"{k}: {v}" for k, v in headers.items()]
    with socket.create_connection(("127.0.0.1", s.port), timeout=5) as sock:
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1]), json.loads(data.split(b"\r\n\r\n", 1)[1])


def test_missing_length_is_411(up):
    status, body = raw(up, PAGE, close_write=True)
    assert status == 411 and "Content-Length" in body["error"]


def test_non_numeric_length_is_400(up):
    for value in ("abc", "-1", "1e3", "１２"):
        status, body = raw(up, {**PAGE, "Content-Length": value})
        assert status == 400, value


def test_too_large_is_413_without_reading_body(up):
    status, body = raw(up, {**PAGE, "Content-Length": str(add.MAX_BYTES + 1)})
    assert status == 413 and "4 GB" in body["error"]
    assert everything(up.root) == []


@pytest.mark.parametrize("query, word", [
    ({"name": "notes.txt"}, "動画ファイル"),
    ({"name": ""}, "ファイル名"),
    ({"engine": "nope"}, "engine"),
    ({"engine": "codex"}, "codex コマンドが見つかりません"),
    ({"model": "a b"}, "モデル"),
    ({"effort": "minimal"}, "推論の強さ"),
    ({"source_url": "javascript:alert(1)"}, "元動画のページ"),
    ({"title": "x" * 201}, "題名"),
    ({"creator": "a\nb"}, "作成者"),
])  # fmt: skip
def test_bad_fields_are_400_without_reading_body(up, query, word):
    status, body = raw(up, {**PAGE, "Content-Length": "100000000"}, query=query)  # 本文は送らない
    assert status == 400 and word in body["error"]
    assert everything(up.root) == [] and up.probed == []


def test_empty_body_is_400(up):
    status, body = raw(up, {**PAGE, "Content-Length": "0"})
    assert status == 400 and body["error"] == "動画が空です"


def test_short_body_is_400_and_leaves_nothing(up):
    status, body = raw(up, {**PAGE, "Content-Length": "1000"}, b"x" * 10, close_write=True)
    assert status == 400 and "最後まで受け取れません" in body["error"]
    assert everything(up.root) == [] and up.probed == [] and not up.app.waiting


def test_not_a_video_after_reading_is_400(up, monkeypatch):
    def broken(path):
        raise add.NotVideo("映像の入った動画として読めません")

    monkeypatch.setattr(add, "probe", broken)
    status, body = upload(up.base)
    assert status == 400 and body["error"] == "映像の入った動画として読めません"
    assert everything(up.root) == []


@pytest.mark.parametrize("error", [SystemExit("ffprobe が見つかりません（ffmpeg を入れると一緒に入ります）"),
                                   OSError(28, "No space left on device")])  # fmt: skip
def test_tool_or_disk_failure_is_500_with_reason(up, monkeypatch, error):
    def broken(path):
        raise error

    monkeypatch.setattr(add, "probe", broken)
    status, body = upload(up.base)
    assert status == 500
    assert ("ffprobe が見つかりません" if isinstance(error, SystemExit) else "No space left on device") in body["error"]
    assert everything(up.root) == []


def test_post_jobs_is_gone(up):
    assert call(up.base + "/api/jobs", {"source_url": "https://example.com/v"})[0] == 404
    assert everything(up.root) == []


def test_large_json_body_for_retry_and_cancel_is_413(up):
    for action in ("retry", "cancel"):
        status, body = raw(up, {**PAGE, "Content-Length": str(1024 * 1024 + 1), "Content-Type": "application/json"},
                           path=f"/api/jobs/abcdefghijk/{action}")  # fmt: skip
        assert status == 413, action


def test_detail_shows_only_https_source_url(up):
    status, body = upload(up.base, creator="作成者", source_url="https://example.com/v")
    job_id = body["id"]
    detail = json.loads(call(f"{up.base}/api/jobs/{job_id}")[1])
    assert detail["source_url"] == "https://example.com/v" and detail["creator"] == "作成者"
    meta_path = up.root / job_id / "meta.json"
    for bad in ("javascript:alert(1)", "http://example.com/", " data:text/html,x", 5):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["source_url"] = bad  # 読み手は meta.json を書き換えられる
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        detail = json.loads(call(f"{up.base}/api/jobs/{job_id}")[1])
        assert detail["source_url"] is None, bad


def test_page_has_file_picker_and_drop_and_no_link_input():
    from importlib import resources

    page = resources.files("videotab").joinpath("templates", "app.html").read_text(encoding="utf-8")
    assert 'type="file"' in page and '"/api/uploads?"' in page and 'setRequestHeader("X-Videotab", "1")' in page
    assert "upload.onprogress" in page and ".abort()" in page
    assert 'window.addEventListener("drop"' in page and 'window.addEventListener("dragover"' in page
    assert 'type="url"' not in page and "input[type=url]" not in page and "d.url" not in page
    assert '"videotab.agentChoice"' in page
