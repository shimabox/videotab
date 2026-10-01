"""実行中の曲を止める（子プロセスの停止と、止めたことの記録）。"""

import os
import shlex
import subprocess
import sys
import threading

import pytest
from fakes import FakeAgent, make_work

from videotab import agent, cancel, pipeline, read
from videotab.cancel import Cancel, Cancelled

SLEEPER = "import time\nprint('start', flush=True)\ntime.sleep(30)\n"


def sleeper(code=SLEEPER):
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)


def test_cancel_stops_watched_process():
    c = Cancel()
    proc = sleeper()
    with c.watch(proc):
        assert proc.stdout.readline() == "start\n"
        c.cancel()
        assert proc.wait(5) != 0
    with pytest.raises(Cancelled):
        c.check()


def test_process_started_after_cancel_is_stopped_at_once():
    c = Cancel()
    c.cancel()
    proc = sleeper()
    with c.watch(proc):
        assert proc.wait(5) != 0


def test_process_ignoring_signal_is_killed_after_grace(monkeypatch):
    monkeypatch.setattr(cancel, "GRACE", 0.2)
    c = Cancel()
    proc = sleeper("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('start', flush=True)\ntime.sleep(30)\n")
    with c.watch(proc):
        assert proc.stdout.readline() == "start\n"
        c.cancel()
        assert proc.wait(5) == -9


def test_cancel_also_stops_descendants_holding_output():
    # 子孫（エージェントが起動したツールなど）が出力のパイプを握ったまま残ると、読む側が終わらない
    c = Cancel()
    proc = subprocess.Popen(["/bin/sh", "-c", "trap 'exit 143' TERM; echo start; sleep 30 & wait $!"],
                            stdout=subprocess.PIPE, text=True)  # fmt: skip
    with c.watch(proc):
        assert proc.stdout.readline() == "start\n"
        threading.Event().wait(0.2)  # sleep が起動するまで
        done = threading.Event()
        threading.Thread(target=lambda: (proc.stdout.read(), done.set()), daemon=True).start()
        c.cancel()
        assert done.wait(5), "子孫が残って出力が閉じない"
        assert proc.wait(5) != 0


def test_cancel_without_process_only_sets_flag():
    c = Cancel()
    c.check()
    c.cancel()
    c.cancel()  # 何度呼んでもよい
    assert c.is_set()


SLOW_CLAUDE = """\
import sys, time
open(sys.argv[1], "w").close()
time.sleep(30)
"""


def test_run_agent_stops_on_cancel(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = tmp_path / "slow_claude.py"
    script.write_text(SLOW_CLAUDE, encoding="utf-8")
    command = bin_dir / "claude"
    started = tmp_path / "started"
    command.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(started))}\n",
                       encoding="utf-8")  # fmt: skip
    command.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    wd = tmp_path / "wd"
    wd.mkdir()
    c, logs, errors = Cancel(), [], []

    def run():
        try:
            agent.run_agent("読む", engine="claude", workdir=wd, writable=[], log=logs.append, label="A", cancel=c)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(500):
        if started.exists():
            break
        threading.Event().wait(0.02)
    assert started.exists()
    c.cancel()
    t.join(10)
    assert not t.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], Cancelled)
    assert "[A] 止めました" in logs


def test_cli_command_gets_interrupt_and_cleans_up(tmp_path, monkeypatch):
    # videotab のコマンドには SIGINT を送る（Python の後始末が走る）
    marker = tmp_path / "cleaned"
    script = tmp_path / "videotab_cmd"
    script.write_text(
        f"#!{sys.executable}\nimport time\nprint('start', flush=True)\n"
        f"try:\n    time.sleep(30)\nexcept KeyboardInterrupt:\n    open({str(marker)!r}, 'w').close()\n    raise\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(pipeline, "videotab_bin", lambda: str(script))
    job = pipeline.Job.create(tmp_path / "work" / "abcdefghijk")
    errors = []

    def run():
        try:
            job._cli("frames")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(250):
        if "start" in "".join(job.log_tail()):
            break
        threading.Event().wait(0.02)
    job.cancel.cancel()
    t.join(10)
    assert not t.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], Cancelled)
    assert marker.exists()


class BlockingAgent(FakeAgent):
    """担当 A の 1 回目だけ、止める合図が出るまで終わらない読み手。"""

    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()
        self.blocked = False

    def __call__(self, prompt, *, cancel=None, **kwargs):
        if kwargs["label"] == "A" and not self.blocked:
            self.blocked = True
            self.waiting.set()
            for _ in range(500):
                if cancel.is_set():
                    raise Cancelled()
                threading.Event().wait(0.02)
            raise AssertionError("止める合図が来ません")
        return super().__call__(prompt, cancel=cancel, **kwargs)


def test_cancel_during_reading_marks_stopped_and_retry_resumes(tmp_path, monkeypatch):
    fake = BlockingAgent()
    monkeypatch.setattr(read, "run_agent", fake)
    wd = make_work(tmp_path)
    job = pipeline.Job.create(wd, "claude")
    job.update_step("add", status="done")
    result = []
    t = threading.Thread(target=lambda: result.append(job.run()))
    t.start()
    assert fake.waiting.wait(60)
    job.cancel.cancel()
    t.join(30)
    assert result == [False]
    data = job.load()
    assert data["status"] == "stopped"
    steps = {s["name"]: s for s in data["steps"]}
    assert [steps[n]["status"] for n in ("add", "frames", "strip", "pages")] == ["done"] * 4
    assert steps["read"]["status"] == "pending" and steps["read"]["message"] == "止めました"
    assert pipeline.finished_at(data) is None
    assert any("止めました" in line for line in job.log_tail())

    # やり直すと、止めた段から続ける（Job は作り直すので、止める合図は残らない）
    again = pipeline.Job(wd)
    again.reset_from("read")
    assert again.run(), "\n".join(again.log_tail())
    assert again.load()["status"] == "done"


def test_cancel_before_next_step_stops_there(tmp_path, monkeypatch):
    monkeypatch.setattr(read, "run_agent", FakeAgent())
    wd = make_work(tmp_path)
    job = pipeline.Job.create(wd, "claude")
    job.update_step("add", status="done")
    job.cancel.cancel()  # 実行が始まる前に止められた
    assert job.run() is False
    data = job.load()
    assert data["status"] == "stopped"
    assert data["steps"][1]["status"] == "pending" and data["steps"][1]["message"] == "止めました"


def test_mark_stopped_only_changes_queued(tmp_path):
    job = pipeline.Job.create(tmp_path / "work" / "abcdefghijk")
    assert job.load()["status"] == "queued"
    job.mark_stopped()
    assert job.load()["status"] == "stopped"
    data = job.load()
    data["status"] = "done"
    job.save(data)
    job.mark_stopped()
    assert job.load()["status"] == "done"
