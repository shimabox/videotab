"""曲ごとに選んだモデルと推論の強さ（選択）の検査・合成・起動の引数・表示。"""

import json
import os
from pathlib import Path

import pytest

from videotab import agent
from videotab import agent_settings as s
from videotab.agent_settings import AgentSettings, Choice


def write_claude(tmp_path, monkeypatch, data):
    d = tmp_path / "claude-home"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))


def write_codex(tmp_path, monkeypatch, text):
    d = tmp_path / "codex-home"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(d))


USUAL_CLAUDE = AgentSettings("claude", model="opus", model_efforts={"claude-opus-5-5": "high"})
USUAL_CODEX = AgentSettings("codex", model="gpt-6-astra", effort="high")


# --- 検査


BAD_MODELS = ["", " ", "a b", "-opus", "--dangerously", 'a"b', "a'b", "a=b", "a\nb", "a" * 129, 5, ["opus"], True]


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("value", BAD_MODELS)
def test_bad_models_are_rejected(engine, value):
    with pytest.raises(ValueError):
        s.normalize_choice(engine, value, None)
    assert not s.valid_choice_model(engine, value)


def test_models_accepted():
    for alias in ("opus", "sonnet", "fable", "haiku"):
        assert s.normalize_choice("claude", alias).model == alias
    for name in ("claude-opus-5-5", "claude-opus-5-5[1m]", "us.anthropic.x:0", "a" * 128):
        assert s.normalize_choice("claude", name).model == name
    for name in ("gpt-6-astra", "o9-mini", "a/b"):
        assert s.normalize_choice("codex", name).model == name
    assert s.normalize_choice("claude", "  opus \n").model == "opus"  # 前後の空白は除く
    assert s.CHOICE_MODELS == {"claude": ("opus", "sonnet", "fable", "haiku"), "codex": None}


def test_efforts_follow_engine():
    for v in ("low", "medium", "high", "xhigh", "max"):
        assert s.normalize_choice("claude", None, v).effort == v
    for v in ("minimal", "low", "medium", "high", "xhigh"):
        assert s.normalize_choice("codex", None, v).effort == v
    for engine, v in (("claude", "minimal"), ("codex", "max"), ("claude", "High"), ("codex", "ultra"),
                      ("claude", ""), ("codex", " "), ("claude", 3), ("codex", ["high"]), ("codex", 'high"')):  # fmt: skip
        with pytest.raises(ValueError):
            s.normalize_choice(engine, None, v)
    assert s.normalize_choice("codex", None, " high ").effort == "high"


def test_none_is_usual_and_unknown_engine_fails():
    assert s.normalize_choice("claude", None, None) == Choice()
    assert Choice().to_json() == {}
    assert Choice("opus", None).to_json() == {"model": "opus"}
    assert Choice(None, "max").to_json() == {"effort": "max"}
    with pytest.raises(ValueError):
        s.normalize_choice("gemini", None, None)


def test_error_messages_do_not_echo_values():
    for model, effort in (("SECRET value", None), (None, "SECRETEFFORT")):
        with pytest.raises(ValueError) as e:
            s.normalize_choice("claude", model, effort)
        assert "SECRET" not in str(e.value)


def test_choice_from_job_and_shown_choice():
    assert s.choice_from_job("claude", None) == Choice()
    assert s.choice_from_job("claude", {"model": "opus", "effort": "max"}) == Choice("opus", "max")
    for raw in ([1], "opus", {"effort": "minimal"}, {"model": "bad model"}):
        with pytest.raises(ValueError):
            s.choice_from_job("claude", raw)
    assert s.shown_choice("claude", None) == {"model": None, "effort": None}
    assert s.shown_choice("claude", {"model": "opus", "effort": "minimal"}) == {"model": "opus", "effort": None}
    assert s.shown_choice("codex", {"model": "gpt-6-astra", "effort": "max"}) == {"model": "gpt-6-astra", "effort": None}
    assert s.shown_choice("codex", "broken") == {"model": None, "effort": None}


def test_apply_choice_and_validate():
    got = s.apply_choice(USUAL_CLAUDE, Choice("sonnet", "max"))
    assert (got.model, got.model_efforts, got.chosen_model, got.chosen_effort) == (
        "opus", {"claude-opus-5-5": "high"}, "sonnet", "max")  # fmt: skip
    s.validate(got, "claude")
    assert s.apply_choice(USUAL_CLAUDE, None) is USUAL_CLAUDE
    assert s.apply_choice(USUAL_CLAUDE, Choice()) == USUAL_CLAUDE
    with pytest.raises(ValueError):
        s.apply_choice(USUAL_CODEX, Choice(None, "max"))  # Codex に max は無い
    bad = [
        (AgentSettings("claude", chosen_effort="minimal"), "claude"),
        (AgentSettings("codex", chosen_effort="max"), "codex"),
        (AgentSettings("claude", chosen_model="-x"), "claude"),
        (AgentSettings("codex", chosen_model="a b"), "codex"),
    ]
    for settings, engine in bad:
        with pytest.raises(ValueError):
            s.validate(settings, engine)


# --- 起動の引数


def test_usual_settings_give_same_args_as_before():
    assert s.claude_args(AgentSettings("claude")) == []
    assert s.claude_args(USUAL_CLAUDE) == ["--settings", s.claude_settings_json(USUAL_CLAUDE)]
    assert s.codex_args(USUAL_CODEX) == ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"']
    assert s.removed_env(USUAL_CLAUDE) == () and s.removed_env(USUAL_CODEX) == ()


def test_claude_model_only_keeps_model_settings():
    got = s.apply_choice(USUAL_CLAUDE, Choice("sonnet", None))
    args = s.claude_args(got)
    assert args[0] == "--settings" and args[2:] == ["--model", "sonnet"]
    assert json.loads(args[1]) == {"modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    assert s.removed_env(got) == ()


def test_claude_effort_only_drops_model_settings():
    got = s.apply_choice(USUAL_CLAUDE, Choice(None, "max"))
    args = s.claude_args(got)
    assert args[:2] == ["--settings", '{"model":"opus"}'] and args[2:] == ["--effort", "max"]
    assert s.removed_env(got) == ("CLAUDE_CODE_EFFORT_LEVEL",)


def test_claude_both_chosen_drops_settings():
    got = s.apply_choice(USUAL_CLAUDE, Choice("fable", "low"))
    assert s.claude_settings_json(got) is None
    assert s.claude_args(got) == ["--model", "fable", "--effort", "low"]


def test_codex_fills_unchosen_item_from_usual():
    assert s.codex_args(s.apply_choice(USUAL_CODEX, Choice("gpt-7", None))) == [
        "-m", "gpt-7", "-c", 'model_reasoning_effort="high"']  # fmt: skip
    assert s.codex_args(s.apply_choice(USUAL_CODEX, Choice(None, "minimal"))) == [
        "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="minimal"']  # fmt: skip
    assert s.codex_args(s.apply_choice(AgentSettings("codex"), Choice(None, "xhigh"))) == [
        "-c", 'model_reasoning_effort="xhigh"']  # fmt: skip
    assert s.removed_env(s.apply_choice(USUAL_CODEX, Choice(None, "low"))) == ()


def test_removed_env_notes_only_when_present(monkeypatch):
    got = s.apply_choice(AgentSettings("claude"), Choice(None, "max"))
    assert s.removed_env_notes(got) == []
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    notes = s.removed_env_notes(got)
    assert len(notes) == 1 and "CLAUDE_CODE_EFFORT_LEVEL" in notes[0] and "low" not in notes[0]
    assert s.removed_env_notes(s.apply_choice(AgentSettings("claude"), Choice("opus", None))) == []


# --- 表示


def test_summary_and_lines_show_choice_first(monkeypatch):
    both = s.apply_choice(AgentSettings("claude", model="opus"), Choice("sonnet", "max"))
    assert s.start_line(both) == "読み取りの設定: Claude Code・モデル sonnet（videotab で指定）・推論の強さ max（videotab で指定）"
    model_only = s.apply_choice(USUAL_CLAUDE, Choice("sonnet", None))
    assert s.start_line(model_only) == "読み取りの設定: Claude Code・モデル sonnet（videotab で指定）・推論の強さはモデルごとの普段の設定"
    assert s.actual_text(both, "claude-sonnet-5") == "実際のモデル claude-sonnet-5・推論の強さ max（videotab で指定）"
    assert s.actual_text(model_only, "claude-opus-5-5") == "実際のモデル claude-opus-5-5・推論の強さ high（普段の設定）"
    codex = s.apply_choice(USUAL_CODEX, Choice(None, "xhigh"))
    assert s.start_line(codex) == "読み取りの設定: Codex・モデル gpt-6-astra（普段の設定）・推論の強さ xhigh（videotab で指定）"
    # 環境変数より選んだ値が先
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    env = s.apply_choice(s.load_settings("claude"), Choice("opus", "max"))
    assert s.summary(env) == "モデル opus（videotab で指定）・推論の強さ max（videotab で指定）"
    assert s.actual_text(env, "claude-opus-5-5").endswith("推論の強さ max（videotab で指定）")
    effort_only = s.apply_choice(s.load_settings("claude"), Choice(None, "high"))
    assert s.summary(effort_only) == "モデル claude-haiku-4-5（環境変数で指定）・推論の強さ high（videotab で指定）"


def test_usual_labels(tmp_path, monkeypatch):
    assert s.usual_labels(AgentSettings("claude")) == {
        "model": "Claude Code の標準", "effort": "モデルの標準", "effort_with_model": "モデルの標準"}  # fmt: skip
    assert s.usual_labels(USUAL_CLAUDE) == {"model": "opus", "effort": "モデルごとの設定", "effort_with_model": "モデルごとの設定"}
    exact = AgentSettings("claude", model="claude-opus-5-5", model_efforts={"claude-opus-5-5": "xhigh"})
    assert s.usual_labels(exact) == {"model": "claude-opus-5-5", "effort": "xhigh", "effort_with_model": "モデルごとの設定"}
    assert s.usual_labels(AgentSettings("codex")) == {"model": "Codex の標準", "effort": "Codex の標準"}
    assert s.usual_labels(USUAL_CODEX) == {"model": "gpt-6-astra", "effort": "high"}
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    assert s.usual_labels(s.load_settings("claude")) == {
        "model": "claude-sonnet-5・環境変数で指定", "effort": "max・環境変数で指定", "effort_with_model": "max・環境変数で指定"}  # fmt: skip
    monkeypatch.setenv("ANTHROPIC_MODEL", "bad model")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "ultra")
    assert s.usual_labels(s.load_settings("claude")) == {
        "model": "環境変数で指定", "effort": "環境変数で指定", "effort_with_model": "環境変数で指定"}  # fmt: skip


def test_agent_options_show_only_checked_values(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}},
                                        "permissions": {"allow": ["Bash(SECRET)"]}})  # fmt: skip
    write_codex(tmp_path, monkeypatch, 'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\napproval_policy = "SECRET"\n')
    got = s.agent_options()
    assert got == {
        "claude": {
            "models": ["opus", "sonnet", "fable", "haiku"],
            "efforts": ["low", "medium", "high", "xhigh", "max"],
            "usual": {"model": "opus", "effort": "モデルごとの設定", "effort_with_model": "モデルごとの設定"},
        },
        "codex": {
            "models": None,
            "model_hint": "gpt-6-astra",
            "efforts": ["minimal", "low", "medium", "high", "xhigh"],
            "usual": {"model": "gpt-6-astra", "effort": "high"},
        },
    }
    write_claude(tmp_path, monkeypatch, {"model": "bad model"})
    write_codex(tmp_path, monkeypatch, "{broken")
    text = json.dumps(s.agent_options(), ensure_ascii=False)
    assert "SECRET" not in text and "bad" not in text and str(tmp_path) not in text and "ユーザー設定" not in text
    assert s.agent_options()["codex"]["model_hint"] is None


# --- 起動（偽の Popen で、コマンドと子プロセスの環境変数を見る）


class FakePopen:
    seen = []
    cwds = []

    def __init__(self, cmd, *, cwd, env, stdout, stderr, stdin, text):
        FakePopen.seen.append((cmd, env))
        FakePopen.cwds.append(cwd)
        self.stdout = iter([
            json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-5"}) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "おわり"}) + "\n",
        ])  # fmt: skip
        self.returncode = 0

    def wait(self):
        return 0

    def kill(self):
        pass


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.seen = []
    FakePopen.cwds = []
    monkeypatch.setattr(agent.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}")
    return FakePopen.seen


def launch(tmp_path, engine, settings):
    logs = []
    result = agent.run_agent("読む", engine=engine, workdir=tmp_path, writable=[tmp_path / "part_A.json"],
                             log=logs.append, label="A", settings=settings)  # fmt: skip
    assert result.ok
    return logs


@pytest.mark.parametrize("engine, settings", [
    ("claude", None), ("claude", AgentSettings("claude")), ("claude", USUAL_CLAUDE),
    ("codex", None), ("codex", USUAL_CODEX),
])  # fmt: skip
def test_usual_settings_keep_env_and_command(tmp_path, monkeypatch, fake_popen, engine, settings):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    launch(tmp_path, engine, settings)
    cmd, env = fake_popen[0]
    # 環境変数は、閉じ込めの作業フォルダと、子プロセスの git の設定を足すだけ（利用者が同じ仕組みで渡している
    # git の設定があれば、その後ろに足す）
    expected = {**os.environ, agent.CONFINE_ENV: str(tmp_path.resolve())}
    agent._add_git_config(expected, *agent.GIT_ENV)
    assert env == expected and env[f"GIT_CONFIG_KEY_{int(env['GIT_CONFIG_COUNT']) - 1}"] == "safe.bareRepository"
    if engine == "claude":
        assert cmd == agent._claude_command("読む", [tmp_path / "part_A.json"], tmp_path, settings=settings)
        assert "--model" not in cmd and "--effort" not in cmd
    else:
        last = Path(cmd[cmd.index("-o") + 1])  # run_agent が作る一時ファイル
        assert cmd == agent._codex_command("読む", tmp_path, last, settings=settings)


def test_claude_chosen_effort_removes_env_and_keeps_model_env(tmp_path, monkeypatch, fake_popen):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    settings = s.apply_choice(USUAL_CLAUDE, Choice("sonnet", "max"))
    logs = launch(tmp_path, "claude", settings)
    cmd, env = fake_popen[0]
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env and env["ANTHROPIC_MODEL"] == "claude-haiku-4-5"
    assert os.environ["CLAUDE_CODE_EFFORT_LEVEL"] == "low"  # videotab 自身の環境は変えない
    i = cmd.index("--model")
    assert cmd[i : i + 4] == ["--model", "sonnet", "--effort", "max"] and cmd[i + 4] == "--tools"
    assert "--settings" not in cmd
    assert "[A] 実際のモデル claude-sonnet-5・推論の強さ max（videotab で指定）" in logs


def test_claude_chosen_model_keeps_env(tmp_path, monkeypatch, fake_popen):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    launch(tmp_path, "claude", s.apply_choice(USUAL_CLAUDE, Choice("opus", None)))
    cmd, env = fake_popen[0]
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
    i = cmd.index("--settings")
    assert json.loads(cmd[i + 1]) == {"modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    assert cmd[i + 2 : i + 5] == ["--model", "opus", "--tools"]


def test_claude_command_with_choice_changes_only_model_args(tmp_path):
    writable = [tmp_path / "parts" / "part_A.json"]
    base = agent._claude_command("読む", writable, tmp_path)
    cmd = agent._claude_command("読む", writable, tmp_path, settings=s.apply_choice(AgentSettings("claude"), Choice("opus", "max")))
    i = cmd.index("--model")
    assert cmd[i - 2 : i] == ["--permission-mode", "dontAsk"]
    assert cmd[:i] + cmd[i + 4 :] == base  # 閉じ込めのオプションは変わらない


def test_codex_command_with_choice(tmp_path, fake_popen):
    launch(tmp_path, "codex", s.apply_choice(USUAL_CODEX, Choice("gpt-7", None)))
    cmd, env = fake_popen[0]
    i = cmd.index("-m")
    assert cmd[i : i + 4] == ["-m", "gpt-7", "-c", 'model_reasoning_effort="high"']
    assert "--ignore-user-config" in cmd


def test_codex_starts_in_the_parent_folder_the_reader_cannot_write(tmp_path, fake_popen):
    # 作業フォルダは読み手が書けるので、Codex のコマンドはその親で起動する（作業フォルダは -C で渡す）。
    # Claude Code は、ファイル操作の範囲が cwd で決まるので、作業フォルダで起動する
    wd = tmp_path / "work" / "song"
    wd.mkdir(parents=True)
    launch(wd, "codex", None)
    launch(wd, "claude", None)
    assert FakePopen.cwds == [wd.resolve().parent, wd]
    cmd, env = fake_popen[0]
    assert cmd[cmd.index("-C") + 1] == str(wd.resolve()) and env[agent.CONFINE_ENV] == str(wd.resolve())


def test_run_agent_rejects_bad_choice_before_start(tmp_path, monkeypatch):
    def must_not_look(name):
        raise AssertionError("コマンドを探す前に止まるはず")

    monkeypatch.setattr(agent.shutil, "which", must_not_look)
    for engine, settings in (
        ("claude", AgentSettings("claude", chosen_effort="minimal")),
        ("codex", AgentSettings("codex", chosen_effort="max")),
        ("claude", AgentSettings("claude", chosen_model="--dangerously-skip-permissions")),
        ("codex", AgentSettings("codex", chosen_model='x" y')),
    ):
        with pytest.raises(ValueError):
            agent.run_agent("読む", engine=engine, workdir=tmp_path, writable=[], log=print, label="A", settings=settings)
