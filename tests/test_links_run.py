"""通しの実行（Job.run と画面の順番待ち）のリンク対策。偽の読み手と合成画像で、各段にリンクを置く。"""

import json
import os
import shutil
import threading

import pytest
from fakes import FakeAgent, make_work
from test_pipeline import make_notab_work
from trees import is_plain, make_outside, place_folder, place_last, snapshot

from videotab import pipeline, read, server

@pytest.fixture(autouse=True)
def no_confine(monkeypatch):
    monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)


def no_outside_paths(text, out):
    assert str(out) not in text


def test_full_run_with_links_everywhere_keeps_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(read, "run_agent", FakeAgent())
    wd = make_work(tmp_path, n_pages=4)
    out = make_outside(tmp_path)
    job = pipeline.Job.create(wd, "claude")
    job.update_step("add", status="done")
    # 本体が書く名前と生成物のフォルダを、外を指す形にしておく
    place_last(job.log_path, out, "link")
    shapes = {
        "abcdefghijk.html": "link",
        "abcdefghijk.alphatex": "hardlink",
        "timing.json": "dangling",
        "conflicts.json": "link",
    }
    for name, shape in shapes.items():
        place_last(wd / name, out, shape)
    meta = (wd / "meta.json").read_bytes()
    (out / "meta.json").write_bytes(meta)
    before = snapshot(out)
    (wd / "meta.json").unlink()
    os.link(out / "meta.json", wd / "meta.json")  # 外のファイルとのハードリンク（読めば中身は同じ）
    place_folder(wd / "pages", out, "folder-link")
    place_folder(wd / "strip", out, "folder-dangling")

    assert job.run(), "\n".join(job.log_tail())
    assert snapshot(out) == before
    for name in ["job.json", "job.log", "meta.json", *shapes]:
        if name != "conflicts.json":
            assert is_plain(wd / name), name
    assert not os.path.lexists(wd / "conflicts.json")
    for folder in ("pages", "strip"):
        assert (wd / folder).is_dir() and not (wd / folder).is_symlink()
    log = job.log_path.read_text(encoding="utf-8")
    assert "job.log がリンクか通常のファイルではなかったので" in log
    assert "pages が作業フォルダの外を指すリンクだったので" in log and "meta.json がハードリンクだったので" in log
    no_outside_paths(log + job.path.read_text(encoding="utf-8"), out)


class FailingSwapAgent(FakeAgent):
    """担当 A が読み取りの途中で job.json を差し替えてから失敗する、偽の読み手（ほかの曲では普通に読む）。"""

    def __init__(self, out, how, song):
        super().__init__()
        self.out, self.how, self.song = out, how, song

    def __call__(self, prompt, *, workdir, label, **kwargs):
        wd = workdir.resolve()
        if wd.name == self.song and label == "A":
            path = wd / "job.json"
            path.unlink()
            if self.how == "broken-link":
                path.symlink_to(self.out / "broken.json")
            elif self.how == "dangling":
                path.symlink_to(self.out / "missing.json")
            else:
                os.link(self.out / "h", path)
            raise RuntimeError("読み手が失敗しました")
        return super().__call__(prompt, workdir=workdir, label=label, **kwargs)


def two_songs(tmp_path):
    first = make_work(tmp_path, n_pages=2)
    second = first.parent / "bbbbbbbbbbb"
    shutil.copytree(first, second)
    meta = json.loads((second / "meta.json").read_text(encoding="utf-8"))
    meta["id"] = second.name
    (second / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return first, second


@pytest.mark.parametrize("how", ["broken-link", "dangling", "hardlink"])
def test_job_json_swapped_while_reading_is_recorded_and_worker_goes_on(tmp_path, monkeypatch, how):
    first, second = two_songs(tmp_path)
    out = make_outside(tmp_path)
    (out / "broken.json").write_text("{broken")
    before = snapshot(out)
    monkeypatch.setattr(read, "run_agent", FailingSwapAgent(out, how, first.name))
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(first.parent)
    for song in (first, second):  # 画像を取り込み済みの曲を、画面の「やり直す」で順番待ちに入れる
        pipeline.Job.create(song, "claude")
        app.retry(song.name, None, None)
    for _ in range(1200):
        if not app.waiting and app.running is None:
            break
        threading.Event().wait(0.05)
    assert not app.waiting and app.running is None

    failed = pipeline.Job(first)
    data = failed.load()  # (a) 失敗が job.json に記録される（通常のファイルとして置き換わる）
    assert is_plain(failed.path) and data["status"] == "failed"
    step = next(s for s in data["steps"] if s["name"] == "read")
    assert step["status"] == "failed" and "読み手が失敗しました" in step["message"]
    assert snapshot(out) == before  # (b) 外のファイルが変わらない
    done = pipeline.Job(second).load()  # (c) 画面のワーカーが次の曲を実行できる
    assert done["status"] == "done", "\n".join(pipeline.Job(second).log_tail())
    no_outside_paths(failed.log_path.read_text(encoding="utf-8"), out)


def test_readers_swapped_to_outside_during_run_stops_the_read_step(tmp_path, monkeypatch):
    wd = make_work(tmp_path, n_pages=2)
    out = make_outside(tmp_path)
    before = snapshot(out)

    class Swapper(FakeAgent):
        def __call__(self, prompt, *, workdir, label, **kwargs):
            result = super().__call__(prompt, workdir=workdir, label=label, **kwargs)
            if label == "A":
                os.rename(workdir / "readers", workdir / "readers.moved")
                place_folder(workdir / "readers", out, "folder-link")
            return result

    monkeypatch.setattr(read, "run_agent", Swapper())
    job = pipeline.Job.create(wd, "claude")
    job.update_step("add", status="done")
    assert not job.run()
    step = next(s for s in job.load()["steps"] if s["name"] == "read")
    assert step["status"] == "failed" and step["message"] == "readers は作業フォルダの外を指しています"
    assert snapshot(out) == before


def test_discarding_media_with_links_keeps_outside(tmp_path):
    wd = make_notab_work(tmp_path)
    out = make_outside(tmp_path)
    before = snapshot(out)
    place_last(wd / "video.mkv", out, "link")
    place_last(wd / "video.avi", out, "hardlink")
    place_folder(wd / "pages", out, "folder-link")
    (wd / "strip" / "to_out").symlink_to(out / "d")
    job = pipeline.Job.create(wd)
    job.update_step("add", status="done")
    job.update_step("frames", status="done")
    assert not job.run()
    assert sorted(p.name for p in wd.iterdir()) == ["job.json", "job.lock", "job.log", "meta.json"]
    assert snapshot(out) == before
