"""job.json の選択（choice）と、読み取りの段での合成・検査。"""

import json
import shutil

import pytest
from fakes import FakeAgent, make_work
from test_pipeline import SnapshotAgent, read_message, use_claude_settings, use_codex_settings

from videotab import pipeline, read
from videotab.agent_settings import AgentSettings, Choice


def new_job(tmp_path, engine="claude", choice=None):
    return pipeline.Job.create(tmp_path / "work" / "abcdefghijk", engine, choice=choice)


def test_create_writes_only_chosen_items(tmp_path):
    job = new_job(tmp_path, choice=Choice("opus", "max"))
    assert job.load()["choice"] == {"model": "opus", "effort": "max"}
    job = new_job(tmp_path, choice=Choice(None, "high"))
    assert job.load()["choice"] == {"effort": "high"}
    for choice in (None, Choice()):
        job = new_job(tmp_path, choice=choice)
        assert "choice" not in job.load()


@pytest.mark.parametrize("engine, choice, expected_engine, expected", [
    (None, None, "codex", {"model": "gpt-7", "effort": "high"}),  # 引き継ぐ
    ("codex", None, "codex", {"model": "gpt-7", "effort": "high"}),  # 同じエンジンなら引き継ぐ
    ("claude", None, "claude", None),  # エンジンが変わると消す
    ("claude", Choice("opus", None), "claude", {"model": "opus"}),  # 置き換える
    ("codex", Choice(None, "low"), "codex", {"effort": "low"}),
    (None, Choice(), "codex", None),  # 普段の設定で置き換える
])  # fmt: skip
def test_reset_from_rules(tmp_path, engine, choice, expected_engine, expected):
    job = new_job(tmp_path, "codex", Choice("gpt-7", "high"))
    job.reset_from("read", engine=engine, choice=choice)
    data = job.load()
    assert data["engine"] == expected_engine
    assert data.get("choice") == expected


def test_reset_from_old_job_without_engine_or_choice(tmp_path):
    job = new_job(tmp_path)
    data = job.load()
    del data["engine"]
    job.save(data)
    job.reset_from("read")
    assert "choice" not in job.load() and "engine" not in job.load()
    job.reset_from("read", engine="claude")  # 項目が無ければ Claude Code として扱う
    assert job.load()["engine"] == "claude" and "choice" not in job.load()


def run_with_choice(tmp_path, monkeypatch, engine, choice, fake=None):
    fake = fake or SnapshotAgent()
    monkeypatch.setattr(read, "run_agent", fake)
    wd = make_work(tmp_path)
    job = pipeline.Job.create(wd, engine, choice=choice)
    job.update_step("add", status="done")
    ok = job.run()
    return job, fake, ok


def test_read_step_passes_choice_to_every_launch(tmp_path, monkeypatch):
    use_claude_settings(tmp_path, monkeypatch, {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}})
    job, fake, ok = run_with_choice(tmp_path, monkeypatch, "claude", Choice("sonnet", None),
                                    SnapshotAgent({"A": "実際のモデル claude-sonnet-5・推論の強さ モデルの既定"}))  # fmt: skip
    assert ok, "\n".join(job.log_tail())
    expected = AgentSettings("claude", model="opus", model_efforts={"claude-opus-5-5": "high"}, chosen_model="sonnet")
    assert sorted(label for label, _ in fake.settings) == ["A", "A 直し", "B", "まとめ役"]
    assert all(got == expected for _, got in fake.settings)
    summary = "モデル sonnet（videotab で指定）・推論の強さはモデルごとの普段の設定"
    assert sum(line.endswith(f"読み取りの設定: Claude Code・{summary}") for line in job.log_tail(1000)) == 1
    assert fake.messages[0][1] == summary
    assert job.load()["choice"] == {"model": "sonnet"}


def test_read_step_with_codex_choice(tmp_path, monkeypatch):
    use_codex_settings(tmp_path, monkeypatch, 'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
    job, fake, ok = run_with_choice(tmp_path, monkeypatch, "codex", Choice(None, "xhigh"))
    assert ok
    assert all(got == AgentSettings("codex", model="gpt-6-astra", effort="high", chosen_effort="xhigh")
               for _, got in fake.settings)  # fmt: skip
    assert read_message(job) == "モデル gpt-6-astra（普段の設定）・推論の強さ xhigh（videotab で指定）"


def test_read_step_notes_removed_effort_env_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    job, fake, ok = run_with_choice(tmp_path, monkeypatch, "claude", Choice(None, "max"))
    assert ok
    log = job.log_tail(1000)
    assert sum("環境変数 CLAUDE_CODE_EFFORT_LEVEL は読み取りのエージェントに渡しません" in line for line in log) == 1
    assert any(line.endswith("読み取りの設定: Claude Code・モデル CLI の既定・推論の強さ max（videotab で指定）") for line in log)


def test_read_step_without_env_has_no_note(tmp_path, monkeypatch):
    job, _, ok = run_with_choice(tmp_path, monkeypatch, "claude", Choice(None, "max"))
    assert ok and not any("CLAUDE_CODE_EFFORT_LEVEL" in line for line in job.log_tail(1000))


@pytest.mark.parametrize("engine, raw", [
    ("claude", {"effort": "minimal"}),  # エンジンに合わない
    ("codex", {"effort": "max"}),
    ("claude", {"model": "bad model"}),
    ("claude", {"model": "-x"}),
    ("claude", {"model": 5}),
    ("claude", "opus"),
])  # fmt: skip
def test_unusable_choice_fails_without_launching(tmp_path, monkeypatch, engine, raw):
    fake = FakeAgent()
    monkeypatch.setattr(read, "run_agent", fake)
    wd = make_work(tmp_path, n_pages=2)
    job = pipeline.Job.create(wd, engine)
    job.update_step("add", status="done")
    data = job.load()
    data["choice"] = raw
    job.save(data)
    assert not job.run()
    assert fake.prompts == []  # エージェントを起動していない
    step = next(s for s in job.load()["steps"] if s["name"] == "read")
    assert step["status"] == "failed"
    assert "指定が使えません" in step["message"] and "選び直して" in step["message"]
    assert "bad model" not in step["message"]


def test_old_job_json_without_choice_reads_with_usual(tmp_path, monkeypatch):
    use_claude_settings(tmp_path, monkeypatch, {"model": "opus"})
    fake = SnapshotAgent()
    monkeypatch.setattr(read, "run_agent", fake)
    wd = make_work(tmp_path)
    job = pipeline.Job.create(wd)
    job.update_step("add", status="done")
    data = job.load()
    del data["engine"]
    job.save(data)
    assert job.run()
    assert all(got == AgentSettings("claude", model="opus") for _, got in fake.settings)


def test_retry_keeps_choice_for_same_engine(tmp_path, monkeypatch):
    job, fake, ok = run_with_choice(tmp_path, monkeypatch, "claude", Choice("opus", "max"))
    assert ok
    shutil.rmtree(job.workdir / "history", ignore_errors=True)
    fake.settings.clear()
    job.reset_from("read")
    assert job.run()
    assert {(got.chosen_model, got.chosen_effort) for _, got in fake.settings} == {("opus", "max")}
    shutil.rmtree(job.workdir / "history", ignore_errors=True)
    fake.settings.clear()
    job.reset_from("read", engine="codex")  # エンジンを変えると普段の設定
    assert job.run()
    assert {(got.engine, got.chosen_model, got.chosen_effort) for _, got in fake.settings} == {("codex", None, None)}
    assert "choice" not in json.loads(job.path.read_text(encoding="utf-8"))
