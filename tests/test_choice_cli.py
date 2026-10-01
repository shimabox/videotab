"""videotab run --model / --effort と、保存された選択の引き継ぎ。"""

import json

import pytest
from fakes import FakeAgent, make_work

from videotab import cli, pipeline, read
from videotab.agent_settings import AgentSettings, Choice

JOB = "abcdefghijk"


@pytest.fixture
def runs(tmp_path, monkeypatch, plain_ids):
    """Job.run を、その時点の job.json を記録するだけの偽物にする。取り込んだ曲の ID はファイル名にする。"""
    seen = []

    def fake_run(self):
        seen.append(self.load())
        return True

    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    return seen


def video(tmp_path, job_id=JOB):
    """取り込む動画ファイル（plain_ids なので、取り込んだ曲の ID は job_id になる）。"""
    path = tmp_path / f"{job_id}.mp4"
    path.write_bytes(b"video")
    return str(path)


def run(tmp_path, *args):
    return cli.main(["run", *args, "--root", str(tmp_path / "work")])


def saved(tmp_path):
    return json.loads((tmp_path / "work" / JOB / "job.json").read_text(encoding="utf-8"))


def stopped_job(tmp_path, engine="codex", choice=None, drop=()):
    """読み取りで止まった曲。drop に書いた項目は job.json から消す（古い job.json の代わり）。"""
    job = pipeline.Job.create(tmp_path / "work" / JOB, engine, choice=choice)
    data = job.load()
    for s in data["steps"][:4]:
        s["status"] = "done"
    data["steps"][4]["status"] = "failed"
    data["status"] = "failed"
    for key in drop:
        data.pop(key, None)
    job.save(data)
    return job


def test_new_song_without_flags_matches_old_shape(tmp_path, runs):
    assert run(tmp_path, video(tmp_path)) == 0
    data = runs[0]
    assert set(data) == {"id", "title", "engine", "status", "created", "steps", "updated"}
    assert data["engine"] == "claude"


def test_new_song_with_flags(tmp_path, runs):
    assert run(tmp_path, video(tmp_path), "--engine", "codex", "--model", "gpt-7", "--effort", "minimal") == 0
    assert (runs[0]["engine"], runs[0]["choice"]) == ("codex", {"model": "gpt-7", "effort": "minimal"})
    assert run(tmp_path, video(tmp_path, "second"), "--model", "opus") == 0
    assert (runs[1]["engine"], runs[1]["choice"]) == ("claude", {"model": "opus"})


@pytest.mark.parametrize("flags", [
    ["--effort", "minimal"],  # Claude Code に無い
    ["--engine", "codex", "--effort", "max"],  # Codex に無い
    ["--model", "a b"],
    ["--model", "-x"],
    ["--model", 'a"b'],
    ["--model", "a" * 129],
    ["--model", ""],
    ["--effort", " "],
])  # fmt: skip
def test_bad_values_exit_without_touching_job(tmp_path, runs, flags, fake_probe):
    with pytest.raises(SystemExit):
        run(tmp_path, video(tmp_path), *flags)
    assert not (tmp_path / "work" / JOB).exists() and fake_probe == []  # 取り込む前に断る
    stopped_job(tmp_path, "claude", Choice("opus", None))
    before = saved(tmp_path)
    with pytest.raises(SystemExit):
        run(tmp_path, JOB, *flags)
    assert saved(tmp_path) == before and runs == []


def test_resume_without_engine_keeps_codex_and_choice(tmp_path, runs):
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    assert run(tmp_path, JOB) == 0
    assert (runs[-1]["engine"], runs[-1]["choice"]) == ("codex", {"model": "gpt-7", "effort": "high"})
    assert runs[-1]["steps"][4]["status"] == "pending"


def test_step_without_engine_keeps_codex_and_choice(tmp_path, runs):
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    data = saved(tmp_path)
    data["status"] = "done"
    (tmp_path / "work" / JOB / "job.json").write_text(json.dumps(data), encoding="utf-8")
    assert run(tmp_path, JOB, "--step", "read") == 0
    assert (runs[-1]["engine"], runs[-1]["choice"]) == ("codex", {"model": "gpt-7", "effort": "high"})


def test_other_engine_drops_saved_choice(tmp_path, runs):
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    assert run(tmp_path, JOB, "--engine", "claude") == 0
    assert runs[-1]["engine"] == "claude" and "choice" not in runs[-1]
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    assert run(tmp_path, JOB, "--engine", "claude", "--effort", "max") == 0
    assert runs[-1]["choice"] == {"effort": "max"}


def test_same_engine_explicit_keeps_choice_and_overrides_given_items(tmp_path, runs):
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    assert run(tmp_path, JOB, "--engine", "codex") == 0
    assert runs[-1]["choice"] == {"model": "gpt-7", "effort": "high"}
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    assert run(tmp_path, JOB, "--effort", "low") == 0
    assert runs[-1]["choice"] == {"model": "gpt-7", "effort": "low"}
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    assert run(tmp_path, JOB, "--model", "usual") == 0  # 普段の設定に戻す
    assert runs[-1]["choice"] == {"effort": "high"}
    stopped_job(tmp_path, "codex", Choice("gpt-7", "high"))
    assert run(tmp_path, JOB, "--model", "usual", "--effort", "usual") == 0
    assert "choice" not in runs[-1]


def test_old_job_json_resumes_as_claude_with_usual(tmp_path, runs):
    stopped_job(tmp_path, "claude", drop=("engine",))
    assert run(tmp_path, JOB) == 0
    assert runs[-1]["engine"] == "claude" and "choice" not in runs[-1]


def test_done_song_is_left_alone(tmp_path, runs, capsys):
    job = stopped_job(tmp_path, "codex")
    data = job.load()
    data["status"] = "done"
    data["choice"] = {"effort": "max"}  # 使えない値が残っていても、何もしないなら止めない
    job.save(data)
    before = saved(tmp_path)
    assert run(tmp_path, JOB, "--model", "gpt-7") == 0
    assert "できあがっています" in capsys.readouterr().out
    assert saved(tmp_path) == before and runs == []


def test_run_passes_choice_to_reading(tmp_path, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(read, "run_agent", fake)
    wd = make_work(tmp_path)
    job = pipeline.Job.create(wd)
    job.update_step("add", status="done")
    assert cli.main(["run", JOB, "--root", str(wd.parent), "--model", "sonnet", "--effort", "max"]) == 0
    assert job.load()["choice"] == {"model": "sonnet", "effort": "max"}
    assert fake.settings and all(got == AgentSettings("claude", chosen_model="sonnet", chosen_effort="max")
                                 for _, got in fake.settings)  # fmt: skip
