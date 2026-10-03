import json
import os
import shutil

import numpy as np
import pytest
from fakes import FakeAgent, make_work
from synth import video_noise, write_frames

from videotab import agent_settings, cli, pipeline, read, strip
from videotab.agent import AgentResult
from videotab.agent_settings import AgentSettings


def test_steps_start_with_add():
    assert pipeline.STEP_NAMES == ["add", "frames", "strip", "pages", "read", "build", "verify"]
    assert pipeline.STEPS[0] == ("add", "動画の取り込み")


def test_add_step_with_video_reports_name_and_size(tmp_path):
    wd = tmp_path / "work" / "clip-1a2b3c"
    wd.mkdir(parents=True)
    (wd / "video.mp4").write_bytes(b"x")
    (wd / "meta.json").write_text(json.dumps({"id": wd.name, "width": 1920, "height": 1080}), encoding="utf-8")
    job = pipeline.Job.create(wd)
    assert job._run_step("add", "claude") == "video.mp4（1920×1080）"


def test_add_step_with_frames_only_uses_them(tmp_path):
    wd = make_work(tmp_path, n_pages=1)  # 画像だけを取り込んだ作業フォルダ（frames_from あり）
    job = pipeline.Job.create(wd)
    assert job._run_step("add", "claude") == "取り込み済みの画像を使います"


def test_add_step_without_video_fails_and_does_not_read_paths(tmp_path):
    wd = tmp_path / "work" / "clip-1a2b3c"
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    wd.mkdir(parents=True)
    # meta.json / job.json に書かれたパスは開かない（取り込み直しはしない）
    meta = {"id": wd.name, "source_file": str(outside), "frames_from": str(outside)}
    (wd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    job = pipeline.Job.create(wd)
    assert not job.run()
    step = job.load()["steps"][0]
    assert step["name"] == "add" and step["status"] == "failed"
    assert step["message"] == "動画がありません。動画ファイルから新しく作り直してください"
    assert sorted(p.name for p in wd.iterdir()) == ["job.json", "job.lock", "job.log", "meta.json"]


def test_job_json_has_no_link(tmp_path):
    wd = tmp_path / "work" / "clip-1a2b3c"
    (wd).mkdir(parents=True)
    (wd / "meta.json").write_text(json.dumps({"id": wd.name, "title": "曲", "source_url": "https://example.com/v"}))
    data = pipeline.Job.create(wd).load()
    assert "url" not in data and "source_url" not in data
    assert data["title"] == "曲" and data["steps"][0]["name"] == "add"


def test_finished_at_is_latest_step_end_of_finished_job():
    steps = [
        {"name": "add", "status": "done", "ended": "2026-09-29T10:01:00"},
        {"name": "frames", "status": "done", "ended": "2026-09-29T10:05:30"},
        {"name": "strip", "status": "failed", "ended": "2026-09-29T10:03:00"},
        {"name": "pages", "status": "pending", "ended": None},
        {"name": "read", "status": "pending"},
    ]
    assert pipeline.finished_at({"status": "failed", "steps": steps}) == "2026-09-29T10:05:30"
    assert pipeline.finished_at({"status": "done", "steps": steps}) == "2026-09-29T10:05:30"
    for status in ("running", "queued", "stopped", "idle", None):
        assert pipeline.finished_at({"status": status, "steps": steps}) is None
    running = [*steps, {"name": "build", "status": "running", "started": "2026-09-29T10:06:00"}]
    assert pipeline.finished_at({"status": "failed", "steps": running}) is None  # 実行中の段が残っている
    assert pipeline.finished_at({"status": "done", "steps": []}) is None
    assert pipeline.finished_at({"status": "done"}) is None
    assert pipeline.finished_at({"status": "done", "steps": [{"status": "pending", "ended": None}]}) is None


def done_job(status="done"):
    """全段が済んだ job.json の内容。"""
    steps = [{"name": n, "label": lb, "status": "done", "started": "2026-10-03T10:00:00", "ended": "2026-10-03T10:05:00",
              "message": f"{n} の結果"} for n, lb in pipeline.STEPS]  # fmt: skip
    return {"id": "abcdefghijk", "title": "古い題名", "engine": "claude", "status": status, "steps": steps}


def test_rebuild_starts_at_build_step():
    assert pipeline.REBUILD_FROM in pipeline.STEP_NAMES
    assert dict(pipeline.STEPS)[pipeline.REBUILD_FROM] == "タブ譜の組み立て"
    after = pipeline.STEP_NAMES[pipeline.STEP_NAMES.index(pipeline.REBUILD_FROM) :]
    assert after == ["build", "verify"]  # 読み取りは含まない


def test_retitle_resets_build_and_after_when_earlier_steps_are_done():
    data = done_job()
    before = json.loads(json.dumps(data))
    assert pipeline.retitle(data, "新しい題名") is True
    assert data["title"] == "新しい題名" and data["status"] == "queued"
    i = pipeline.STEP_NAMES.index("build")
    assert data["steps"][:i] == before["steps"][:i]  # タブ譜の組み立てより前の段の状態・時刻は変えない
    for s in data["steps"][i:]:
        assert (s["status"], s["started"], s["ended"], s["message"]) == ("pending", None, None, None)


@pytest.mark.parametrize("case", ["read-failed", "read-pending", "missing", "extra", "swapped", "no-steps", "bad-steps"])
def test_retitle_only_renames_unless_rebuildable(case):
    data = done_job(status="failed")
    steps = data["steps"]
    if case == "read-failed":
        steps[4]["status"] = "failed"
    elif case == "read-pending":
        steps[4].update(status="pending", started=None, ended=None)
    elif case == "missing":
        del steps[3]
    elif case == "extra":
        steps.insert(5, {"name": "extra", "status": "done"})
    elif case == "swapped":
        steps[4], steps[5] = steps[5], steps[4]
    elif case == "no-steps":
        del data["steps"]
    else:
        data["steps"] = ["read", "build"]
    before = json.loads(json.dumps(data))
    assert pipeline.retitle(data, "新しい題名") is False
    assert data == {**before, "title": "新しい題名"}  # 段も状態も変えない


def test_reset_from_keeps_its_behavior(tmp_path):
    job = pipeline.Job.create(tmp_path / "abcdefghijk")
    data = job.load()
    data.update(done_job())
    job.save(data)
    job.reset_from("read")
    steps = job.load()["steps"]
    assert [s["status"] for s in steps] == ["done"] * 4 + ["pending"] * 3
    assert steps[4]["message"] is None and steps[3]["message"] == "pages の結果"
    assert job.load()["status"] == "queued"


def test_pipeline_runs_to_html_with_fake_agent(tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(read, "run_agent", fake)
    wd = make_work(tmp_path)
    job = pipeline.Job.create(wd, "claude")
    job.update_step("add", status="done")
    assert job.run(), "\n".join(job.log_tail())

    data = job.load()
    assert data["status"] == "done"
    assert [s["status"] for s in data["steps"]] == ["done"] * 7
    assert data["html"] == "abcdefghijk.html"
    tex = (wd / "abcdefghijk.alphatex").read_text(encoding="utf-8")
    assert "(0.6).1" in tex and "(0.5).1" not in tex  # まとめ役の答えが入る
    assert "\\tempo 120" in tex  # 担当 A が score.json を直した
    by_label = {label: (prompt, writable) for label, prompt, writable in fake.prompts}
    assert set(by_label) == {"A", "B", "まとめ役"}  # 読み手は並行なので順番は決まらない
    # 読み手に渡すプロンプトと、書いてよいファイル
    a_prompt, a_writable = by_label["A"]
    assert "<読み取りの決まり>" in a_prompt and "part_A.json" in a_prompt and "score.json" in a_prompt
    assert {p.name for p in a_writable} == {"part_A.json", "pagebars_A.json", "score.json"}
    assert {p.name for p in by_label["B"][1]} == {"part_B.json", "pagebars_B.json"}
    assert json.loads((wd / "marks.json").read_text(encoding="utf-8"))  # 時刻照合の印
    assert (wd / "readers" / "notes.json").exists()

def test_pipeline_stops_and_resumes_from_failed_step(tmp_path, monkeypatch):
    fake = FakeAgent()

    def broken(*args, **kwargs):
        raise RuntimeError("エージェントが見つかりません")

    monkeypatch.setattr(read, "run_agent", broken)
    wd = make_work(tmp_path, n_pages=4)
    job = pipeline.Job.create(wd)
    job.update_step("add", status="done")
    assert not job.run()
    data = job.load()
    assert data["status"] == "failed"
    status = {s["name"]: s["status"] for s in data["steps"]}
    assert status["pages"] == "done" and status["read"] == "failed" and status["build"] == "pending"
    assert "エージェントが見つかりません" in next(s["message"] for s in data["steps"] if s["name"] == "read")

    monkeypatch.setattr(read, "run_agent", fake)
    job.reset_from("read")
    assert job.run()

def test_reader_without_output_is_retried_then_fails_clearly(tmp_path, monkeypatch):
    calls = []

    def silent(prompt, *, engine, workdir, writable, log, label, settings=None, on_actual=None, cancel=None):
        calls.append(label)
        return AgentResult(False, "", 0.1)  # 何も書かずに終わる

    monkeypatch.setattr(read, "run_agent", silent)
    wd = make_work(tmp_path, n_pages=4)
    job = pipeline.Job.create(wd)
    job.update_step("add", status="done")
    assert not job.run()  # SystemExit ではなく段の失敗として止まる
    read_step = next(s for s in job.load()["steps"] if s["name"] == "read")
    assert read_step["status"] == "failed" and "読み取り結果がない担当" in read_step["message"]
    assert calls == ["A", "A"]  # 1 回だけ読み直させる

class SnapshotAgent(FakeAgent):
    """FakeAgent に加えて、起動のたびに「読み取り」の段の結果欄を記録する（段の途中の表示の確認用）。
    担当 A の最初の読みは検査の誤りを残し、直しの起動も通るようにする。"""

    def __init__(self, actual=None):
        super().__init__(actual)
        self.messages = []

    def __call__(self, prompt, *, workdir, label, **kwargs):
        steps = pipeline.Job(workdir).load()["steps"]
        self.messages.append((label, next(s["message"] for s in steps if s["name"] == "read")))
        if label == "A 直し":
            bars = {"1": "r.1", "2": "(0.6).1"}
            (workdir.resolve() / "parts" / "part_A.json").write_text(json.dumps(bars), encoding="utf-8")
            self.prompts.append((label, prompt, kwargs["writable"]))
            self.settings.append((label, kwargs.get("settings")))
            return AgentResult(True, "直しました", 0.1)
        result = super().__call__(prompt, workdir=workdir, label=label, **kwargs)
        if label == "A":
            bars = {"1": "r.1 r.1", "2": "(0.6).1"}  # 1 小節目が長すぎる
            (workdir.resolve() / "parts" / "part_A.json").write_text(json.dumps(bars), encoding="utf-8")
        return result


def run_read(tmp_path, monkeypatch, engine="claude", fake=None):
    fake = fake or SnapshotAgent()
    monkeypatch.setattr(read, "run_agent", fake)
    wd = make_work(tmp_path)
    job = pipeline.Job.create(wd, engine)
    job.update_step("add", status="done")
    assert job.run(), "\n".join(job.log_tail())
    return job, fake


def read_message(job):
    return next(s["message"] for s in job.load()["steps"] if s["name"] == "read")


def use_claude_settings(tmp_path, monkeypatch, data):
    d = tmp_path / "claude-home"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))


def use_codex_settings(tmp_path, monkeypatch, text):
    d = tmp_path / "codex-home"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(d))


@pytest.mark.parametrize("engine, expected, summary", [
    ("claude", AgentSettings("claude", model="opus", model_efforts={"claude-opus-5-5": "high"}),
     "モデル opus（普段の設定）・推論の強さはモデルごとの普段の設定"),
    ("codex", AgentSettings("codex", model="gpt-6-astra", effort="high"),
     "モデル gpt-6-astra（普段の設定）・推論の強さ high（普段の設定）"),
])  # fmt: skip
def test_read_step_passes_user_settings_to_every_launch(tmp_path, monkeypatch, engine, expected, summary):
    use_claude_settings(tmp_path, monkeypatch, {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}})
    use_codex_settings(tmp_path, monkeypatch, 'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
    job, fake = run_read(tmp_path, monkeypatch, engine)
    labels = [label for label, _ in fake.settings]
    assert sorted(labels) == ["A", "A 直し", "B", "まとめ役"]  # 読み手・直し・まとめ役
    assert all(got == expected for _, got in fake.settings)
    name = {"claude": "Claude Code", "codex": "Codex"}[engine]
    log = job.log_tail(1000)
    assert sum(line.endswith(f"読み取りの設定: {name}・{summary}") for line in log) == 1
    assert all(message == summary for _, message in fake.messages)  # 段の開始時の結果欄
    assert read_message(job) == summary


def test_first_actual_model_is_shown_and_kept(tmp_path, monkeypatch):
    actual = {lb: f"実際のモデル model-{lb}・推論の強さ モデルの既定" for lb in ("A", "B", "まとめ役")}
    job, fake = run_read(tmp_path, monkeypatch, fake=SnapshotAgent(actual))
    at_resolve = dict(fake.messages)["まとめ役"]
    assert at_resolve in (actual["A"], actual["B"])  # 読み手は並行なので、先に分かった方
    assert read_message(job) == at_resolve  # 成功後も最初の 1 回の本文のまま


def test_read_step_without_settings_uses_defaults(tmp_path, monkeypatch):
    job, fake = run_read(tmp_path, monkeypatch)
    assert all(got == AgentSettings("claude") for _, got in fake.settings)
    log = job.log_tail(1000)
    assert any(line.endswith("読み取りの設定: Claude Code・モデル CLI の既定・推論の強さ モデルの既定") for line in log)
    assert not any("ユーザー設定" in line for line in log)  # 注意書きは出ない
    assert read_message(job) == "モデル CLI の既定・推論の強さ モデルの既定"


def test_read_step_with_unusable_settings_still_reads(tmp_path, monkeypatch):
    use_claude_settings(tmp_path, monkeypatch, '{"model": "bad model", "modelSettings": 3}')
    job, fake = run_read(tmp_path, monkeypatch)
    log = job.log_tail(1000)
    assert any(line.endswith("Claude Code のユーザー設定の model は使えない値なので渡しません") for line in log)
    assert any(line.endswith("Claude Code のユーザー設定の modelSettings は表ではないので渡しません") for line in log)
    assert all(got == AgentSettings("claude", notes=fake.settings[0][1].notes) for _, got in fake.settings)
    assert not any("bad model" in line for line in log)

    use_claude_settings(tmp_path, monkeypatch, "{broken")
    shutil.rmtree(job.workdir / "history", ignore_errors=True)  # 前回の結果の退避先は秒単位の名前
    job.reset_from("read")
    assert job.run(), "\n".join(job.log_tail())
    assert sum("Claude Code のユーザー設定は JSON として読めない" in line for line in job.log_tail(1000)) == 1


def test_retry_reads_settings_again_and_follows_engine(tmp_path, monkeypatch):
    use_claude_settings(tmp_path, monkeypatch, {"model": "opus"})
    use_codex_settings(tmp_path, monkeypatch, 'model = "gpt-6-astra"\n')
    fake = SnapshotAgent()
    job, _ = run_read(tmp_path, monkeypatch, fake=fake)
    assert {got.model for _, got in fake.settings} == {"opus"}

    def retry(engine=None):
        # 前回の結果の退避先は秒単位の名前なので、続けてやり直せるよう消しておく
        shutil.rmtree(job.workdir / "history", ignore_errors=True)
        job.reset_from("read", engine=engine)
        fake.settings.clear()

    use_claude_settings(tmp_path, monkeypatch, {"model": "sonnet"})
    retry()
    assert read_message(job) is None  # やり直すと結果欄は消える
    assert job.run()
    assert {got.model for _, got in fake.settings} == {"sonnet"}
    assert read_message(job) == "モデル sonnet（普段の設定）・推論の強さ モデルの既定"

    use_claude_settings(tmp_path, monkeypatch, "{broken")  # もう一方のエンジンの設定は読まない
    job.log_path.write_text("", encoding="utf-8")
    retry(engine="codex")
    assert job.run(), "\n".join(job.log_tail())
    assert fake.settings and all(got == AgentSettings("codex", model="gpt-6-astra") for _, got in fake.settings)
    log = job.log_tail(1000)
    assert not any("Claude Code" in line for line in log)
    assert any(line.endswith("読み取りの設定: Codex・モデル gpt-6-astra（普段の設定）・推論の強さ CLI の既定") for line in log)


def test_read_step_adds_no_job_keys_and_reads_settings_once(tmp_path, monkeypatch):
    use_claude_settings(tmp_path, monkeypatch, {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}},
                                                "permissions": {"allow": ["Bash(SECRET)"]}})  # fmt: skip
    calls = []
    original = agent_settings.load_settings

    def counting(engine):
        calls.append(engine)
        return original(engine)

    monkeypatch.setattr(agent_settings, "load_settings", counting)
    job, _ = run_read(tmp_path, monkeypatch, fake=SnapshotAgent({"A": "実際のモデル claude-opus-5-5・推論の強さ high（普段の設定）"}))
    assert calls == ["claude"]
    data = job.load()
    assert set(data) == {"id", "title", "engine", "status", "created", "steps", "updated", "html"}
    for step in data["steps"]:
        assert set(step) <= {"name", "label", "status", "started", "ended", "message", "seconds"}
    text = job.path.read_text(encoding="utf-8") + job.log_path.read_text(encoding="utf-8")
    assert "SECRET" not in text and "permissions" not in text


def test_job_lock_blocks_second_runner(tmp_path):
    import fcntl

    wd = tmp_path / "work" / "abcdefghijk"
    job = pipeline.Job.create(wd)
    with job.lock_path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert job.is_locked()
        with pytest.raises(pipeline.Busy):
            job.run()
    assert not job.is_locked()

def test_hold_blocks_others_and_run_inside_does_not_lock_again(tmp_path, monkeypatch):
    wd = tmp_path / "work" / "abcdefghijk"
    job = pipeline.Job.create(wd)
    ran = []
    monkeypatch.setattr(pipeline.Job, "_run_locked", lambda self: ran.append(self) or True)
    with job.hold():
        assert pipeline.Job(wd).is_locked()
        with pytest.raises(pipeline.Busy, match="別の videotab が実行中です"):
            with pipeline.Job(wd).hold():
                pass
        with pytest.raises(pipeline.Busy):
            pipeline.Job(wd).run()  # 別の実行は始まらない
        assert job.run()  # 握っている本人は、握り直さずに実行する
    assert ran == [job]
    assert not job.is_locked()
    assert pipeline.Job(wd).run() and len(ran) == 2  # 放したあとは、ほかの実行が握れる

def test_hold_creates_missing_folder(tmp_path):
    wd = tmp_path / "work" / "abcdefghijk"
    with pipeline.Job(wd).hold():
        assert (wd / "job.lock").is_file()
    assert not pipeline.Job(tmp_path / "work" / "nothing").is_locked()

@pytest.mark.parametrize("kind", ["file", "dangling", "dir"])
def test_lock_that_is_not_a_regular_file_is_busy(tmp_path, kind):
    wd = tmp_path / "work" / "abcdefghijk"
    job = pipeline.Job.create(wd)
    target = tmp_path / "outside.txt"
    if kind == "dir":
        job.lock_path.mkdir()
    else:
        if kind == "file":
            target.write_text("そのまま", encoding="utf-8")
            os.utime(target, (1_000_000_000, 1_000_000_000))
        job.lock_path.symlink_to(target)
    assert job.is_locked()  # リンクをたどらずに、ロック中とみなす
    with pytest.raises(pipeline.Busy):
        with job.hold():
            pass
    with pytest.raises(pipeline.Busy):
        job.run()
    if kind == "file":
        assert target.read_text(encoding="utf-8") == "そのまま" and target.stat().st_mtime == 1_000_000_000
    else:
        assert not os.path.lexists(target)  # 壊れたリンクの先を作らない

def test_videotab_run_stops_with_same_message_when_locked(tmp_path):
    wd = tmp_path / "work" / "abcdefghijk"
    job = pipeline.Job.create(wd)
    before = job.path.read_text(encoding="utf-8")
    for extra in ([], ["--step", "read"]):
        with pipeline.Job(wd).hold():
            with pytest.raises(SystemExit, match="^abcdefghijk は別の videotab（画面など）が実行中です$"):
                cli.main(["run", "abcdefghijk", "--root", str(tmp_path / "work"), *extra])
    assert job.path.read_text(encoding="utf-8") == before  # 握れないうちは job を書き換えない

def test_videotab_run_step_keeps_records_of_earlier_steps(tmp_path, monkeypatch):
    job, _ = run_read(tmp_path, monkeypatch, fake=FakeAgent({"A": "実際のモデル claude-opus-5-5・推論の強さ high（普段の設定）"}))
    data = job.load()
    for s in data["steps"][5:]:  # 組み立て直したと分かるように、前の実行の記録を古くしておく
        s.update(started="2000-01-01T00:00:00", ended="2000-01-01T00:00:01", message="古い記録", seconds=999)
    data["steps"][5]["label"] = "組み立て"  # 表示名を変える前に作った曲
    job.save(data)
    before = job.load()

    def no_agent(*args, **kwargs):
        raise AssertionError("タブ譜の組み立てからのやり直しでエージェントを起動しない")

    monkeypatch.setattr(read, "run_agent", no_agent)
    assert cli.main(["run", job.workdir.name, "--root", str(job.workdir.parent), "--step", "build"]) == 0
    after = job.load()
    assert after["steps"][:5] == before["steps"][:5]  # 読み取りまでの時刻・所要時間・結果はそのまま
    assert "実際のモデル claude-opus-5-5" in after["steps"][4]["message"]
    log = job.log_tail(1000)
    assert any(line.endswith(" == タブ譜の組み立て") for line in log)  # ログの見出しは今の表示名
    assert not any(line.endswith(" == 組み立て") for line in log)
    for s in after["steps"][5:]:
        assert s["status"] == "done" and s["message"] != "古い記録" and s["started"] != "2000-01-01T00:00:00"
        assert s["seconds"] != 999
    assert {k: after[k] for k in ("id", "title", "engine", "created")} == {
        k: before[k] for k in ("id", "title", "engine", "created")
    }


def test_videotab_run_step_marks_unfinished_earlier_steps_as_done(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline.Job, "run", lambda self: seen.append(self.load()) or True)
    wd = tmp_path / "work" / "abcdefghijk"
    job = pipeline.Job.create(wd, "codex", choice=agent_settings.Choice("gpt-7", "high"))
    data = job.load()
    for s in data["steps"][:4]:
        s.update(status="done", started="2026-01-01T00:00:00", ended="2026-01-01T00:00:05", message=f"{s['name']} の結果", seconds=5)
    data["steps"][4].update(status="failed", started="2026-01-01T00:00:06", ended="2026-01-01T00:00:09", message="失敗しました")
    data["status"] = "failed"
    job.save(data)
    before = job.load()
    assert cli.main(["run", wd.name, "--root", str(wd.parent), "--step", "build"]) == 0
    steps = seen[-1]["steps"]
    assert steps[:4] == before["steps"][:4]  # 済んでいた段の記録は残す
    assert steps[4]["status"] == "done" and steps[4]["message"] == "済み"  # 済んでいるものとする
    assert steps[4]["started"] is None and steps[4]["ended"] is None
    assert [s["status"] for s in steps[5:]] == ["pending", "pending"]
    assert (seen[-1]["engine"], seen[-1]["choice"], seen[-1]["status"]) == ("codex", {"model": "gpt-7", "effort": "high"}, "queued")


def test_videotab_run_step_recreates_job_with_other_step_list(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline.Job, "run", lambda self: seen.append(self.load()) or True)
    wd = tmp_path / "work" / "abcdefghijk"
    job = pipeline.Job.create(wd)
    data = job.load()
    data["steps"] = data["steps"][:-1]  # 段が欠けている
    data["steps"][0].update(status="done", message="古い記録")
    job.save(data)
    assert cli.main(["run", wd.name, "--root", str(wd.parent), "--step", "build"]) == 0
    steps = seen[-1]["steps"]
    assert [s["name"] for s in steps] == pipeline.STEP_NAMES  # いままでどおり作り直す
    assert [s.get("message") for s in steps] == ["済み"] * 5 + [None, None]

def test_frames_step_redoes_incomplete_extraction(tmp_path, monkeypatch):
    wd = make_work(tmp_path, n_pages=2)
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    del meta["frames_from"]  # 切り出しが途中で止まった
    (wd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    job = pipeline.Job.create(wd)
    seen = []
    monkeypatch.setattr(pipeline.Job, "_cli", lambda self, *args: seen.append(args))
    job._run_step("frames", "claude")
    assert seen and seen[0][0] == "frames" and "--force" in seen[0]

def test_verify_without_marks_is_reported_as_unchecked(tmp_path, monkeypatch):
    wd = tmp_path / "work" / "abcdefghijk"
    job = pipeline.Job.create(wd)
    monkeypatch.setattr(pipeline.Job, "_cli", lambda self, *args: None)
    assert "照合していません" in job._run_step("verify", "claude")
    (wd / "marks.json").write_text("[[1, 0.0]]", encoding="utf-8")
    assert job._run_step("verify", "claude") == "動画の時刻と合っています"


def make_notab_work(tmp_path):
    """タブ譜の写っていない動画を取り込んで切り出し、前回の検出結果も残っている作業フォルダ。"""
    wd = tmp_path / "work" / "abcdefghijk"
    rng = np.random.default_rng(0)
    write_frames(wd, [video_noise(rng).astype(np.uint8) for _ in range(4)])
    for name in ("video.mp4", "video.webm"):
        (wd / name).write_bytes(b"x")
    for d in ("strip", "pages"):
        (wd / d).mkdir()
        (wd / d / "old.png").write_bytes(b"x")
    meta = {"id": wd.name, "title": "テスト曲", "frames_from": "video.mp4", "strip": {"band": [0, 10]}}
    (wd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return wd

def test_strip_command_exits_with_no_tab_code_and_records_given_band(tmp_path, capsys):
    wd = make_notab_work(tmp_path)
    assert cli.main(["strip", str(wd)]) == strip.NO_TAB_EXIT
    assert "タブ譜が写っていないかもしれません" in capsys.readouterr().err
    assert (wd / "video.mp4").exists() and (wd / "frames").exists()  # strip 単体では消さない

    wd = make_work(tmp_path / "tab", n_pages=2)
    assert cli.main(["strip", str(wd), "--band", "200", "355"]) == 0
    assert json.loads((wd / "meta.json").read_text(encoding="utf-8"))["strip"]["band_given"] is True
    assert cli.main(["strip", str(wd)]) == 0  # 指定なしで検出し直すと、手の指定の印は消える
    assert "band_given" not in json.loads((wd / "meta.json").read_text(encoding="utf-8"))["strip"]

def test_pipeline_discards_media_when_no_tab(tmp_path):
    wd = make_notab_work(tmp_path)
    job = pipeline.Job.create(wd)
    job.update_step("add", status="done")
    job.update_step("frames", status="done")
    assert not job.run()

    assert sorted(p.name for p in wd.iterdir()) == ["job.json", "job.lock", "job.log", "meta.json"]
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"id": wd.name, "title": "テスト曲"}
    steps = {s["name"]: s for s in job.load()["steps"]}
    assert "取り込んだ動画と画像を消しました" in steps["strip"]["message"]
    assert steps["strip"]["status"] == "failed"
    assert steps["add"]["status"] == steps["frames"]["status"] == "pending"  # やり直すと取り込みの段から

    job.reset_from("add")
    assert not job.run()  # 動画を消したので、取り込みの段で止まる
    step = job.load()["steps"][0]
    assert step["status"] == "failed" and step["message"].startswith("動画がありません")

def test_pipeline_keeps_media_on_other_strip_failures(tmp_path, monkeypatch):
    wd = make_notab_work(tmp_path)
    job = pipeline.Job.create(wd)

    def broken(self, *args):
        raise pipeline.StepError("画像がありません", 1)

    monkeypatch.setattr(pipeline.Job, "_cli", broken)
    with pytest.raises(pipeline.StepError, match="画像がありません"):
        job._run_step("strip", "claude")
    assert (wd / "video.mp4").exists() and (wd / "frames").exists() and (wd / "strip").exists()
    assert "frames_from" in json.loads((wd / "meta.json").read_text(encoding="utf-8"))

def test_pipeline_passes_given_band_to_strip(tmp_path, monkeypatch):
    wd = make_work(tmp_path, n_pages=2)
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    meta["strip"] = {"band": [200, 355], "band_given": True}
    (wd / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    job = pipeline.Job.create(wd)
    seen = []
    monkeypatch.setattr(pipeline.Job, "_cli", lambda self, *args: seen.append(args))
    job._run_step("strip", "claude")
    assert seen == [("strip", str(wd.resolve()), "--band", "200", "355")]
