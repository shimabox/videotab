import json
import os
import shlex
import sys

import pytest

from videotab import agent, read
from videotab.agent_settings import AgentSettings, load_settings


def test_plan_groups_overlap_boundary_pages():
    assert read.plan_groups([1, 2, 3]) == [[1, 2, 3]]
    groups = read.plan_groups(list(range(1, 21)))
    assert len(groups) == 3
    for a, b in zip(groups, groups[1:]):
        assert a[-1] == b[0]  # 境目のページは 2 人が読む
    assert sorted(set(sum(groups, []))) == list(range(1, 21))
    assert len(read.plan_groups(list(range(1, 100)))) == 4

def test_plan_message_describes_the_split():
    assert read.plan_message(10, "A", [list(range(1, 11))], "Claude Code") == (
        "10 ページを Claude Code 1 つで読み取ります（担当 A: 1〜10 ページ）"
    )
    assert read.plan_message(10, "AB", [[1, 2, 3, 4, 5, 6], [6, 7, 8, 9, 10]], "Codex") == (
        "10 ページを 2 つに分け、Codex を 2 つ同時に動かして読み取ります"
        "（担当 A: 1〜6 ページ、B: 6〜10 ページ）。境目のページは両隣の担当が読み、結果を突き合わせます"
    )
    assert read.plan_message(10, "ABCD", [[1, 2, 3], [3, 4, 5, 6], [6, 7, 8], [8, 9, 10]], "Claude Code") == (
        "10 ページを 4 つに分け、Claude Code を 4 つ同時に動かして読み取ります"
        "（担当 A: 1〜3 ページ、B: 3〜6 ページ、C: 6〜8 ページ、D: 8〜10 ページ）。"
        "境目のページは両隣の担当が読み、結果を突き合わせます"
    )

def test_claude_command_limits_tools(tmp_path):
    cmd = agent._claude_command("読む", [tmp_path / "parts" / "part_A.json"], tmp_path)
    allowed = cmd[cmd.index("--allowedTools") + 1 : cmd.index("--disallowedTools")]
    assert "Read" in allowed
    assert f"Write(/{(tmp_path / 'parts' / 'part_A.json').resolve()})" in allowed
    # 実行できるのは videotab check / zoom だけ（外のパスは videotab 自身が VIDEOTAB_CONFINE で拒む）
    yt = agent.videotab_bin()
    assert [a for a in allowed if a.startswith("Bash")] == [f"Bash({yt} check:*)", f"Bash({yt} zoom:*)"]
    assert not any(a in ("Bash", "Write", "Edit") for a in allowed)
    # git の読むだけのコマンドは、許可が無くても Claude Code が通す（作業フォルダを含むリポジトリの
    # コミット済みの内容まで読める）ので、明示して禁じる
    assert cmd[cmd.index("--disallowedTools") + 1 : cmd.index("--output-format")] == ["Bash(git:*)"]

def test_codex_ignores_user_config(tmp_path):
    cmd = agent._codex_command("読む", tmp_path, tmp_path / "last.txt")
    assert "--ignore-user-config" in cmd and "sandbox_workspace_write.writable_roots=[]" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"

def test_codex_cannot_write_to_temporary_folders(tmp_path):
    cmd = agent._codex_command("読む", tmp_path, tmp_path / "last.txt")
    configs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "-c"]  # -c の直後の値
    # workspace-write が既定で書ける /tmp と $TMPDIR を外す。書き込み先の追加とネットワークも切ったまま
    for value in (
        "sandbox_workspace_write.exclude_slash_tmp=true",
        "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        "sandbox_workspace_write.writable_roots=[]",
        "sandbox_workspace_write.network_access=false",
    ):
        assert value in configs, value
    assert "--ignore-user-config" in cmd

def test_codex_keeps_only_shell_and_image_viewing(tmp_path):
    cmd = agent._codex_command("読む", tmp_path, tmp_path / "last.txt")
    configs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "-c"]
    # sandbox のネットワークの制限はシェルのコマンドにしか効かないので、ほかの機能と web 検索を切る
    for name in ("apps", "plugins", "remote_plugin", "tool_suggest", "multi_agent", "image_generation",
                 "browser_use", "computer_use", "hooks", "skill_mcp_dependency_install", "shell_snapshot"):  # fmt: skip
        assert f"features.{name}=false" in configs, name
    assert 'web_search="disabled"' in configs
    # シェルのコマンドには、秘密らしい名前の環境変数を渡さない
    assert "shell_environment_policy.ignore_default_excludes=false" in configs
    assert not any(c.startswith(("features.shell_tool", "features.unified_exec")) for c in configs)  # シェルは残す
    assert "--enable" not in cmd and "--add-dir" not in cmd

def test_codex_does_not_trust_the_work_folder_nor_its_repository(tmp_path):
    # 作業フォルダと、それを含むリポジトリを untrusted として渡す（そこの AGENTS.md を自動で読ませない。
    # 読み手が作業フォルダに置いた AGENTS.md が、次の起動の指示にならないように）
    repo = tmp_path / "re po"
    wd = repo / "work" / "song"
    wd.mkdir(parents=True)
    (repo / ".git").mkdir()
    assert agent._project_root(wd.resolve()) == repo.resolve()
    cmd = agent._codex_command("読む", wd, tmp_path / "last.txt")
    configs = [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "-c"]
    untrusted = '={trust_level="untrusted"}'
    assert f"projects={{{json.dumps(str(repo.resolve()))}{untrusted},{json.dumps(str(wd.resolve()))}{untrusted}}}" in configs
    # リポジトリの中でなければ、作業フォルダだけ
    alone = tmp_path / "alone"
    alone.mkdir()
    assert agent._project_root(alone.resolve()) == alone.resolve()
    cmd = agent._codex_command("読む", alone, tmp_path / "last.txt")
    assert f"projects={{{json.dumps(str(alone.resolve()))}{untrusted}}}" in cmd


def test_child_git_does_not_use_an_implicit_bare_repository():
    # 作業フォルダの直下に裸リポジトリの形を置かれても、エージェントの子プロセスの git がそこを使わない
    env = {}
    agent._add_git_config(env, *agent.GIT_ENV)
    assert env == {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.bareRepository", "GIT_CONFIG_VALUE_0": "explicit"}
    env = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "1", "GIT_CONFIG_KEY_1": "c.d", "GIT_CONFIG_VALUE_1": "2"}
    agent._add_git_config(env, *agent.GIT_ENV)  # 利用者が同じ仕組みで渡している設定の後ろに足す
    assert env["GIT_CONFIG_COUNT"] == "3" and env["GIT_CONFIG_KEY_2"] == "safe.bareRepository" and env["GIT_CONFIG_KEY_0"] == "a.b"


def test_videotab_check_refuses_paths_outside_confined_folder(tmp_path, monkeypatch):
    from videotab import cli

    inside = tmp_path / "wd" / "parts"
    inside.mkdir(parents=True)
    (inside / "part_A.json").write_text('{"1": "r.1"}', encoding="utf-8")
    other = tmp_path / "other" / "parts"
    other.mkdir(parents=True)
    (other / "part_A.json").write_text('{"1": "r.1"}', encoding="utf-8")
    monkeypatch.setenv("VIDEOTAB_CONFINE", str(tmp_path / "wd"))
    assert cli.main(["check", str(inside / "part_A.json")]) == 0
    with pytest.raises(SystemExit, match="作業フォルダ"):
        cli.main(["check", str(tmp_path / "wd" / "parts" / ".." / ".." / "other" / "parts" / "part_A.json")])

def confined_folders(tmp_path, monkeypatch):
    """作業フォルダ wd と、その外の other を作り、VIDEOTAB_CONFINE を wd にする。"""
    wd, other = tmp_path / "wd", tmp_path / "other"
    for d in (wd, other):
        (d / "parts").mkdir(parents=True)
        (d / "parts" / "part_A.json").write_text('{"1": "r.1"}', encoding="utf-8")
    monkeypatch.setenv("VIDEOTAB_CONFINE", str(wd))
    return wd, other

def test_videotab_zoom_refuses_folder_outside_confined_folder(tmp_path, monkeypatch):
    from videotab import cli

    _, other = confined_folders(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="の外です"):
        cli.main(["zoom", str(other), "1"])

def test_videotab_check_refuses_folder_outside_confined_folder(tmp_path, monkeypatch):
    from videotab import cli

    wd, other = confined_folders(tmp_path, monkeypatch)
    assert cli.main(["check", str(wd)]) == 0
    with pytest.raises(SystemExit, match="の外です"):
        cli.main(["check", str(other)])

def test_videotab_check_refuses_link_to_outside(tmp_path, monkeypatch):
    from videotab import cli

    wd, other = confined_folders(tmp_path, monkeypatch)
    link = wd / "parts" / "part_B.json"
    link.symlink_to(other / "parts" / "part_A.json")
    with pytest.raises(SystemExit, match="の外です"):
        cli.main(["check", str(link)])

def test_describe_claude_event():
    line = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/p001_a.png"}}]}})
    assert agent._describe_claude_event(line) == ("Read p001_a.png", None)
    line = json.dumps({"type": "result", "subtype": "success", "result": "1〜8 小節", "permission_denials": [{}]})
    desc, final = agent._describe_claude_event(line)
    assert final == "1〜8 小節" and "1 件" in desc

def test_claude_is_confined_and_rules_are_inlined(tmp_path):
    cmd = agent._claude_command("読む", [tmp_path / "parts" / "part_A.json"], tmp_path)
    assert "--restricted" in cmd  # 利用者の設定（auto モードなど）を読まず、ファイル操作を作業フォルダに限る
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"  # 許していない操作は聞かずに拒否
    text = read.rules("3")
    assert text.startswith("## 3.") and "## 4." not in text
    assert "## 5." in read.rules("3", "5")

def test_reader_prompt_takes_table_columns_from_index(tmp_path):
    # ページによって線の位置が違う動画では、一覧に「弦の線」の列が増える。担当に渡す表の見出しも合わせる
    (tmp_path / "pages").mkdir()
    head = "| ページ | 時刻 | フレーム | 画像 | 弦の線（1〜6 弦の y） | 注意 |\n|---|---|---|---|---|---|\n"
    rows = "| 1 | 0:00 | 1-3 | p001_a.png | 70.2, 100.3 | |\n| 2 | 0:03 | 4-6 | p002_a.png | 80.2, 110.3 | |\n"
    (tmp_path / "pages" / "index.md").write_text("- 画像の説明\n\n" + head + rows, encoding="utf-8")
    prompt = read.reader_prompt(tmp_path, "B", [2], first=False)
    assert head + "| 2 | 0:03 | 4-6 | p002_a.png | 80.2, 110.3 | |" in prompt
    assert "| 1 | 0:00" not in prompt and "| ページ | 時刻 | フレーム | 画像 | 注意 |" not in prompt


def test_every_prompt_asks_for_reply_in_japanese(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "index.md").write_text(
        "- 画像の説明\n\n| ページ | 時刻 | フレーム | 画像 | 注意 |\n|---|---|---|---|---|\n| 1 | 0:00 | 1-3 | p001.png | |\n",
        encoding="utf-8",
    )
    assert "日本語" in read.REPLY_LANGUAGE
    prompts = {
        "最初の担当": (read.reader_prompt(tmp_path, "A", [1], first=True), "最後の返答は短く"),
        "ほかの担当": (read.reader_prompt(tmp_path, "B", [1], first=False), "最後の返答は短く"),
        "直し": (read.fix_prompt(tmp_path, "A", ["1 小節: 長すぎます"]), "最後の返答は、直した小節"),
        "まとめ役": (read.resolve_prompt(tmp_path, ["2 小節の食い違い"]), "最後の返答は、直した小節"),
    }
    for name, (prompt, reply) in prompts.items():
        assert prompt.count(read.REPLY_LANGUAGE) == 1, name
        assert prompt.index(reply) < prompt.index(read.REPLY_LANGUAGE), name
    assert "最後の返答で見積もりだと知らせる" in prompts["最初の担当"][0]  # 最初の担当だけの指定は変わらない
    assert "見積もり" not in prompts["ほかの担当"][0]

def test_stash_previous_and_marks_skip_single_frame_pages(tmp_path):
    wd = tmp_path / "w"
    (wd / "parts").mkdir(parents=True)
    (wd / "parts" / "part_A.json").write_text("{}", encoding="utf-8")
    (wd / "resolve.json").write_text("{}", encoding="utf-8")
    logs = []
    read.stash_previous(wd, logs.append)
    assert not (wd / "parts").exists() and not (wd / "resolve.json").exists()
    moved = list((wd / "history").iterdir())
    assert len(moved) == 1 and (moved[0] / "parts" / "part_A.json").exists()

    (wd / "pages").mkdir()
    pages = [{"page": 1, "start": 0.0, "frames": [1, 5]}, {"page": 2, "start": 5.0, "frames": [6, 6]},
             {"page": 3, "start": 6.0, "frames": [7, 9]}]
    (wd / "pages" / "pages.json").write_text(json.dumps({"pages": pages}), encoding="utf-8")
    (wd / "readers").mkdir()
    (wd / "readers" / "pagebars_A.json").write_text(json.dumps({"1": 1, "2": 4, "3": 5}), encoding="utf-8")
    assert read.make_marks(wd) == [[1, 0.0], [5, 6.0]]

def test_part_check_uses_time_signature_from_earlier_parts(tmp_path):
    from videotab import build

    wd = tmp_path / "w"
    (wd / "parts").mkdir(parents=True)
    (wd / "parts" / "part_A.json").write_text(json.dumps({"1": "r.1", "2": "\\ts 3 4 r.2 {d}"}), encoding="utf-8")
    (wd / "parts" / "part_B.json").write_text(json.dumps({"3": "r.2 {d}"}), encoding="utf-8")
    assert build.run_check(wd / "parts" / "part_B.json") == 0
    score = {"time_signature": [4, 4], "tuning": "e4 b3 g3 d3 a2 e2"}
    assert read.part_issues(wd / "parts" / "part_B.json", score) == []


# --- モデルと推論の強さの受け渡し


def base_claude_command(tmp_path):
    """モデルの設定を渡さないときの Claude Code の起動コマンド（渡すものが無いときは、これと完全に同じ）。"""
    yt = agent.videotab_bin()
    rule = f"/{(tmp_path / 'parts' / 'part_A.json').resolve()}"
    return [
        "claude", "-p", "読む",
        "--restricted", "--strict-mcp-config", "--permission-mode", "dontAsk",
        "--tools", "Read", "Write", "Edit", "Glob", "Grep", "Bash",
        "--allowedTools", "Read", "Glob", "Grep", f"Bash({yt} check:*)", f"Bash({yt} zoom:*)",
        f"Write({rule})", f"Edit({rule})",
        "--disallowedTools", "Bash(git:*)",
        "--output-format", "stream-json", "--verbose",
    ]  # fmt: skip


def base_codex_command(tmp_path):
    return [
        "codex", "exec", "--ignore-user-config", "--ignore-rules",
        "--sandbox", "workspace-write",
        "-c", "sandbox_workspace_write.writable_roots=[]",
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        *[arg for name in agent.CODEX_FEATURES_OFF for arg in ("-c", f"features.{name}=false")],
        "-c", 'web_search="disabled"',
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        "-c", f'projects={{{json.dumps(str(tmp_path.resolve()))}={{trust_level="untrusted"}}}}',
        "--skip-git-repo-check", "--color", "never",
        "-C", str(tmp_path.resolve()), "-o", str((tmp_path / "last.txt").resolve()), "読む",
    ]  # fmt: skip


CLAUDE_SETTINGS = AgentSettings("claude", model="opus", model_efforts={"claude-opus-5-5": "high"})


def test_claude_command_passes_only_model_settings(tmp_path):
    writable = [tmp_path / "parts" / "part_A.json"]
    base = base_claude_command(tmp_path)
    assert agent._claude_command("読む", writable, tmp_path) == base
    assert agent._claude_command("読む", writable, tmp_path, settings=AgentSettings("claude")) == base
    assert agent._claude_command("読む", writable, tmp_path, settings=load_settings("claude")) == base

    cmd = agent._claude_command("読む", writable, tmp_path, settings=CLAUDE_SETTINGS)
    i = cmd.index("--settings")
    assert cmd[i - 2 : i] == ["--permission-mode", "dontAsk"] and cmd[i + 2] == "--tools"
    assert json.loads(cmd[i + 1]) == {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    assert cmd[:i] + cmd[i + 2 :] == base  # ほかは変わらない（--allowedTools の中身も）
    assert "--model" not in cmd and "--effort" not in cmd


def test_codex_command_passes_model_and_effort(tmp_path):
    base = base_codex_command(tmp_path)
    last = tmp_path / "last.txt"
    assert agent._codex_command("読む", tmp_path, last) == base
    assert agent._codex_command("読む", tmp_path, last, settings=load_settings("codex")) == base

    settings = AgentSettings("codex", model="gpt-6-astra", effort="high")
    cmd = agent._codex_command("読む", tmp_path, last, settings=settings)
    i = cmd.index("-m")
    assert cmd[i : i + 4] == ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"']
    assert cmd[i + 4] == "--skip-git-repo-check"
    assert cmd[:i] + cmd[i + 4 :] == base
    assert "--ignore-user-config" in cmd and "-p" not in cmd and "--profile" not in cmd
    assert cmd[-1] == "読む"
    only_effort = agent._codex_command("読む", tmp_path, last, settings=AgentSettings("codex", effort="low"))
    assert "-m" not in only_effort and 'model_reasoning_effort="low"' in only_effort


def test_actual_model_from_first_event():
    assert agent._actual_model(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5"})) == "claude-opus-5-5"
    for line in (
        json.dumps({"type": "system", "subtype": "init"}),
        "not json",
        "",
        json.dumps({"model": 5}),
        json.dumps({"model": "bad model"}),
        json.dumps({"model": "-x"}),
        json.dumps(["model"]),
    ):
        assert agent._actual_model(line) is None


FAKE_CLAUDE = """\
import json, os, sys
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(sys.argv[3:], f)
with open(os.path.join(os.path.dirname(sys.argv[1]), "confine.txt"), "w", encoding="utf-8") as f:
    f.write(os.environ.get("VIDEOTAB_CONFINE", ""))
first = {"type": "system", "subtype": "init"}
if sys.argv[2] == "with-model":
    first["model"] = "claude-opus-5-5"
print(json.dumps(first))
print(json.dumps({"type": "system", "model": "claude-sonnet-5"}))  # 2 行目以降は調べない
print(json.dumps({"type": "result", "subtype": "success", "result": "おわり"}))
"""


def install_fake_claude(tmp_path, monkeypatch, mode):
    """tmp_path に偽の claude を作り、PATH の先頭に置く。受け取った引数は args.json に、
    環境変数 VIDEOTAB_CONFINE は confine.txt に書く。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    args = tmp_path / "args.json"
    command = bin_dir / "claude"
    command.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(args))} {mode} "$@"\n',
        encoding="utf-8",
    )
    command.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return args


def test_run_agent_passes_settings_and_logs_actual_model(tmp_path, monkeypatch):
    args = install_fake_claude(tmp_path, monkeypatch, "with-model")
    wd = tmp_path / "wd"
    wd.mkdir()
    logs, actual = [], []
    result = agent.run_agent("読む", engine="claude", workdir=wd, writable=[wd / "part_A.json"], log=logs.append,
                             label="A", settings=CLAUDE_SETTINGS, on_actual=actual.append)  # fmt: skip
    assert result.ok and result.text == "おわり"
    got = json.loads(args.read_text(encoding="utf-8"))
    i = got.index("--settings")
    assert json.loads(got[i + 1]) == {"model": "opus", "modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}}
    line = "[A] 実際のモデル claude-opus-5-5・推論の強さ high（普段の設定）"
    assert [x for x in logs if "実際のモデル" in x] == [line]
    assert actual == ["実際のモデル claude-opus-5-5・推論の強さ high（普段の設定）"]


def test_run_agent_without_model_in_first_event(tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, "without-model")
    wd = tmp_path / "wd"
    wd.mkdir()
    logs, actual = [], []
    result = agent.run_agent("読む", engine="claude", workdir=wd, writable=[], log=logs.append, label="A",
                             settings=CLAUDE_SETTINGS, on_actual=actual.append)  # fmt: skip
    assert result.ok
    assert not any("実際のモデル" in x for x in logs) and actual == []


def test_run_agent_confines_reader_to_resolved_workdir(tmp_path, monkeypatch):
    # 読み手が実行する videotab check / zoom のパスの守りは VIDEOTAB_CONFINE だけなので、必ず渡す
    args = install_fake_claude(tmp_path, monkeypatch, "without-model")
    real = tmp_path / "real"
    real.mkdir()
    wd = tmp_path / "wd"
    wd.symlink_to(real)
    monkeypatch.setenv("VIDEOTAB_CONFINE", str(tmp_path / "elsewhere"))  # 親の値は引き継がない
    result = agent.run_agent("読む", engine="claude", workdir=wd, writable=[], log=print, label="A")
    assert result.ok
    assert (args.parent / "confine.txt").read_text(encoding="utf-8") == str(real.resolve())


def test_run_agent_rejects_bad_settings_before_start(tmp_path, monkeypatch):
    def must_not_look(name):
        raise AssertionError("コマンドを探す前に止まるはず")

    monkeypatch.setattr(agent.shutil, "which", must_not_look)
    for engine, settings in (
        ("claude", AgentSettings("codex", model="gpt-6-astra")),
        ("codex", AgentSettings("claude")),
        ("claude", AgentSettings("claude", model="opus --dangerously")),
        ("claude", AgentSettings("claude", model_efforts={"claude-opus-5-5": "max"})),
        ("codex", AgentSettings("codex", effort='high"')),
    ):
        with pytest.raises(ValueError):
            agent.run_agent("読む", engine=engine, workdir=tmp_path, writable=[], log=print, label="A", settings=settings)
