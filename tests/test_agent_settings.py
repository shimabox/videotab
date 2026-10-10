import json
from pathlib import Path

import pytest

from videotab import agent_settings as s


def write_claude(tmp_path, monkeypatch, data, raw=None):
    d = tmp_path / "claude-home"
    d.mkdir(exist_ok=True)
    path = d / "settings.json"
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return path


def write_codex(tmp_path, monkeypatch, text):
    d = tmp_path / "codex-home"
    d.mkdir(exist_ok=True)
    path = d / "config.toml"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    monkeypatch.setenv("CODEX_HOME", str(d))
    return path


# --- 値の検査


@pytest.mark.parametrize("value", ["", " ", "opus ", "a b", "opus\n", "\nopus", "-opus", "a" * 129, 'a"b', "a=b", None, 5])
def test_model_check_rejects_unsafe_values(value):
    assert not s.valid_model(value)


@pytest.mark.parametrize("value", ["opus", "claude-opus-5-5", "claude-opus-5-5[1m]", "us.anthropic.x:0", "a/b", "a" * 128])
def test_model_check_accepts_model_names(value):
    assert s.valid_model(value)


def test_effort_checks():
    for v in ("low", "medium", "high", "xhigh"):
        assert s.valid_claude_effort(v) and s.valid_claude_env_effort(v)
    for v in ("max", "High", "", " high", "high\n", "ultra", None, 3):
        assert not s.valid_claude_effort(v)
    assert s.valid_claude_env_effort("max")
    assert not s.valid_claude_env_effort("ultra")
    for v in ("high", "minimal", "x" * 16):
        assert s.valid_codex_effort(v)
    for v in ("", "High", "x" * 17, "hi gh", "high\n", "-high", "h1", None):
        assert not s.valid_codex_effort(v)


# --- Claude Code


def test_fixture_leaves_nothing_to_pass():
    for engine in ("claude", "codex"):
        got = s.load_settings(engine)
        assert got.model is None and got.model_efforts == {} and got.effort is None and got.notes == ()
    claude = s.load_settings("claude")
    assert s.claude_settings_json(claude) is None
    assert s.summary(claude) == "モデル Claude Code の標準・推論の強さ モデルの標準"
    codex = s.load_settings("codex")
    assert s.codex_args(codex) == []
    assert s.summary(codex) == "モデル Codex の標準・推論の強さ Codex の標準"


def test_claude_reads_model_and_model_settings_from_config_dir(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}})
    got = s.load_settings("claude")
    assert got.model == "opus" and got.model_efforts == {"claude-opus-5-5": "high"} and got.notes == ()
    assert s.claude_settings_json(got) == '{"model":"opus","modelSettings":{"claude-opus-5-5":{"effortLevel":"high"}}}'
    assert s.start_line(got) == "読み取りの設定: Claude Code・モデル opus（普段の設定）・推論の強さはモデルごとの普段の設定"
    assert s.summary(got) == "モデル opus（普段の設定）・推論の強さはモデルごとの普段の設定"


def test_claude_reads_default_location(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
    assert s.claude_settings_path() == tmp_path / ".claude" / "settings.json"
    assert s.load_settings("claude").model == "sonnet"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")  # 空文字は未設定として扱う
    assert s.load_settings("claude").model == "sonnet"


def test_claude_top_level_effort_is_not_passed(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"effortLevel": "low", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}})
    got = s.load_settings("claude")
    passed = json.loads(s.claude_settings_json(got))
    assert "effortLevel" not in passed
    assert passed == {"modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    assert s.actual_text(got, "claude-opus-5-5") == "実際のモデル claude-opus-5-5・推論の強さ high（普段の設定）"
    assert "low" not in s.start_line(got)


def test_claude_model_settings_are_filtered(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"modelSettings": {
        "claude-opus-5-5": {"effortLevel": "high", "permissions": {"allow": ["Bash"]}, "extra": 1},
        "claude-sonnet-5": {"effortLevel": "ultra"},  # 値が使えない
        "bad model": {"effortLevel": "low"},  # キーが使えない
        "claude-haiku": "high",  # 表でない
        "claude-fable-5-1": {"contextWindow": 1},  # 推論の強さ以外だけ（注意書きなしで除く）
    }})
    got = s.load_settings("claude")
    assert got.model_efforts == {"claude-opus-5-5": "high"}
    assert json.loads(s.claude_settings_json(got)) == {"modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    assert len(got.notes) == 1 and "modelSettings" in got.notes[0] and "3 個" in got.notes[0]


def test_claude_model_settings_without_effort_need_no_note(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"modelSettings": {"claude-fable-5-1": {"contextWindow": 1}}})
    got = s.load_settings("claude")
    assert got.model_efforts == {} and got.notes == ()
    assert s.claude_settings_json(got) is None


def test_claude_max_in_file_is_dropped_with_note(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"modelSettings": {"claude-opus-5-5": {"effortLevel": "max"}, "claude-sonnet-5": {"effortLevel": "low"}}})
    got = s.load_settings("claude")
    assert got.model_efforts == {"claude-sonnet-5": "low"}
    assert len(got.notes) == 1
    assert "max" not in s.claude_settings_json(got)


def test_claude_model_settings_limit(tmp_path, monkeypatch):
    many = {f"model-{i}": {"effortLevel": "high"} for i in range(s.MAX_MODEL_SETTINGS + 1)}
    write_claude(tmp_path, monkeypatch, {"model": "opus", "modelSettings": many})
    got = s.load_settings("claude")
    assert got.model_efforts == {} and got.model == "opus"
    assert len(got.notes) == 1 and "64 個" in got.notes[0]
    assert json.loads(s.claude_settings_json(got)) == {"model": "opus"}

    write_claude(tmp_path, monkeypatch, {"modelSettings": dict(list(many.items())[:64])})
    got = s.load_settings("claude")
    assert len(got.model_efforts) == 64 and got.notes == ()


def test_claude_json_keys_are_limited(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "xhigh"}},
                                        "permissions": {"allow": ["Bash(*)"]}, "hooks": {"PreToolUse": []},
                                        "env": {"SECRET": "x"}, "effortLevel": "low"})  # fmt: skip
    got = s.load_settings("claude")
    assert set(json.loads(s.claude_settings_json(got))) <= {"model", "modelSettings"}
    write_claude(tmp_path, monkeypatch, {"model": "opus"})
    assert set(json.loads(s.claude_settings_json(s.load_settings("claude")))) == {"model"}
    write_claude(tmp_path, monkeypatch, {"permissions": {"allow": ["Bash(*)"]}})
    assert s.claude_settings_json(s.load_settings("claude")) is None


def test_other_settings_do_not_leak(tmp_path, monkeypatch):
    path = write_claude(tmp_path, monkeypatch, {
        "model": 12345,  # 使えない値
        "permissions": {"allow": ["Bash(rm -rf SECRETPERM)"]},
        "hooks": {"PreToolUse": [{"command": "SECRETHOOK"}]},
        "modelSettings": {"claude-opus-5-5": {"effortLevel": "SECRETEFFORT", "note": "SECRETNOTE"}},
    })
    got = s.load_settings("claude")
    shown = "\n".join([repr(got), s.claude_settings_json(got) or "", s.start_line(got), *got.notes])
    for secret in ("SECRETPERM", "SECRETHOOK", "SECRETEFFORT", "SECRETNOTE", "12345", "permissions", "hooks"):
        assert secret not in shown
    assert len(got.notes) == 2
    assert all(str(path) not in n and str(path.parent) not in n for n in got.notes)
    assert got.notes[0] == "Claude Code のユーザー設定の model は使えない値なので渡しません"


def test_missing_file_gives_no_note(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert s.load_settings("claude").notes == ()
    assert s.load_settings("codex").notes == ()


@pytest.mark.parametrize("raw", [
    b"{broken", b"[1, 2]", b'"opus"', b"\xff\xfe{}", b"{} // comment", b'{"model": "' + b"a" * (1024 * 1024) + b'"}',
])  # fmt: skip
def test_unusable_claude_file_gives_one_note(tmp_path, monkeypatch, raw):
    path = write_claude(tmp_path, monkeypatch, None, raw=raw)
    got = s.load_settings("claude")
    assert got.model is None and got.model_efforts == {}
    assert len(got.notes) == 1 and str(path) not in got.notes[0]
    assert got.notes[0].startswith("Claude Code のユーザー設定は")


def test_unreadable_claude_file_gives_one_note(tmp_path, monkeypatch):
    d = tmp_path / "claude-home"
    (d / "settings.json").mkdir(parents=True)  # ファイルでなくフォルダ
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    assert len(s.load_settings("claude").notes) == 1


def test_claude_value_types(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"model": ["opus"], "modelSettings": ["x"]})
    got = s.load_settings("claude")
    assert got.model is None and got.model_efforts == {}
    assert len(got.notes) == 2


# --- 環境変数（Claude Code、表示だけ）


def test_env_changes_display_but_not_json(tmp_path, monkeypatch):
    data = {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    write_claude(tmp_path, monkeypatch, data)
    before = s.claude_settings_json(s.load_settings("claude"))
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    got = s.load_settings("claude")
    assert s.claude_settings_json(got) == before
    assert s.summary(got) == "モデル claude-sonnet-5（環境変数で指定）・推論の強さ low（環境変数で指定）"
    # 設定が high でも、環境変数があれば起動ごとの行は環境変数
    assert s.actual_text(got, "claude-opus-5-5") == "実際のモデル claude-opus-5-5・推論の強さ low（環境変数で指定）"


def test_env_values_failing_checks_are_not_shown(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "bad model\nX")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "ultra")
    got = s.load_settings("claude")
    assert got.env_model_set and got.env_model is None and got.env_effort_set and got.env_effort is None
    assert s.summary(got) == "モデル（環境変数で指定）・推論の強さ（環境変数で指定）"
    assert s.actual_text(got, "claude-opus-5-5") == "実際のモデル claude-opus-5-5・推論の強さ（環境変数で指定）"
    assert "ultra" not in s.start_line(got) and "bad" not in s.start_line(got)


def test_env_max_effort_is_shown(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    got = s.load_settings("claude")
    assert "推論の強さ max（環境変数で指定）" in s.start_line(got)
    assert s.actual_text(got, "claude-opus-5-5").endswith("推論の強さ max（環境変数で指定）")


def test_actual_text_matches_model_settings_exactly(tmp_path, monkeypatch):
    write_claude(tmp_path, monkeypatch, {"modelSettings": {"claude-opus-5-5": {"effortLevel": "xhigh"}}})
    got = s.load_settings("claude")
    assert s.actual_text(got, "claude-opus-5-5") == "実際のモデル claude-opus-5-5・推論の強さ xhigh（普段の設定）"
    assert s.actual_text(got, "claude-opus-5-5[1m]") == "実際のモデル claude-opus-5-5[1m]・推論の強さ モデルの標準"
    assert s.start_line(got) == "読み取りの設定: Claude Code・モデル Claude Code の標準・推論の強さはモデルごとの普段の設定"


# --- Codex


def test_codex_reads_top_level_from_codex_home(tmp_path, monkeypatch):
    write_codex(tmp_path, monkeypatch, 'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\napproval_policy = "never"\n')
    got = s.load_settings("codex")
    assert (got.model, got.effort, got.notes) == ("gpt-6-astra", "high", ())
    assert s.codex_args(got) == ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"']
    assert s.start_line(got) == "読み取りの設定: Codex・モデル gpt-6-astra（普段の設定）・推論の強さ high（普段の設定）"


def test_codex_reads_default_location(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model_reasoning_effort = "low"\n', encoding="utf-8")
    got = s.load_settings("codex")
    assert (got.model, got.effort) == (None, "low")
    assert s.summary(got) == "モデル Codex の標準・推論の強さ low（普段の設定）"


def test_codex_profiles_are_ignored(tmp_path, monkeypatch):
    path = write_codex(tmp_path, monkeypatch, (
        'profile = "x"\nmodel = "gpt-top"\n\n'
        '[profiles.x]\nmodel = "gpt-profile"\nmodel_reasoning_effort = "xhigh"\n'
    ))  # fmt: skip
    (path.parent / "x.config.toml").write_text('model = "gpt-file"\nmodel_reasoning_effort = "low"\n', encoding="utf-8")
    got = s.load_settings("codex")
    assert (got.model, got.effort, got.notes) == ("gpt-top", None, ())


@pytest.mark.parametrize("text", ["model = ", "= 1", 'model = "a"\nmodel = "b"\n', b"\xff\xfe"])
def test_unusable_codex_file_gives_one_note(tmp_path, monkeypatch, text):
    write_codex(tmp_path, monkeypatch, text)
    got = s.load_settings("codex")
    assert got.model is None and got.effort is None
    assert len(got.notes) == 1 and got.notes[0].startswith("Codex のユーザー設定は")


def test_codex_unusable_values(tmp_path, monkeypatch):
    path = write_codex(tmp_path, monkeypatch, 'model = 5\nmodel_reasoning_effort = "Very High"\n[sandbox]\nsecret = "S"\n')
    got = s.load_settings("codex")
    assert got.model is None and got.effort is None
    assert got.notes == (
        "Codex のユーザー設定の model は使えない値なので渡しません",
        "Codex のユーザー設定の model_reasoning_effort は使えない値なので渡しません",
    )
    assert all(str(path) not in n and "Very" not in n for n in got.notes)


def test_codex_large_file_gives_one_note(tmp_path, monkeypatch):
    write_codex(tmp_path, monkeypatch, "# " + "a" * (1024 * 1024) + "\n")
    assert len(s.load_settings("codex").notes) == 1


# --- 起動の直前の検査


def test_validate_rejects_mismatch_and_bad_values():
    s.validate(s.AgentSettings("claude", model="opus", model_efforts={"claude-opus-5-5": "high"}), "claude")
    s.validate(s.AgentSettings("codex", model="gpt-6-astra", effort="high"), "codex")
    bad = [
        (s.AgentSettings("codex"), "claude"),
        (s.AgentSettings("claude"), "codex"),
        (s.AgentSettings("claude", model="-opus"), "claude"),
        (s.AgentSettings("claude", model_efforts={"claude-opus-5-5": "max"}), "claude"),
        (s.AgentSettings("claude", model_efforts={"a b": "high"}), "claude"),
        (s.AgentSettings("claude", effort="high"), "claude"),
        (s.AgentSettings("codex", effort='high" x'), "codex"),
        (s.AgentSettings("codex", model_efforts={"m": "high"}), "codex"),
        ({"engine": "claude"}, "claude"),
    ]
    for settings, engine in bad:
        with pytest.raises(ValueError):
            s.validate(settings, engine)
