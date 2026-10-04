"""画像を読むエージェント（Claude Code / Codex）をヘッドレスで起動する。

API キーは使わず、手元の claude / codex コマンド（利用者のログインと利用枠）で動かす。

Claude Code は --restricted と --permission-mode dontAsk で起動する。利用者の設定
（auto モードなど）を読まず、ファイル操作は作業フォルダの中だけ、書けるのは指定した
ファイルだけ、実行できるのは videotab check / zoom だけになり、それ以外は聞かずに拒否される。
読み取りの決まりはプロンプトに入れて渡す（作業フォルダの外の AGENTS.md は読めないため）。

Codex は利用者の設定（~/.codex/config.toml）を読まずに workspace-write の sandbox で起動し、
書き込み先の追加とネットワークを明示的に切る。workspace-write が既定で書き込み先に含める
/tmp と $TMPDIR も外す（$TMPDIR には videotab 本体やほかのプログラムの一時ファイルがある）。
書けるのは作業フォルダの中だけだが、読むことと、sandbox の中でのコマンドの実行は制限しきれない。
一時フォルダを外した効果を確かめたのは macOS・codex-cli 0.158.0 で、Linux では確かめていない。
sandbox のネットワークの制限が効くのはシェルのコマンドだけなので、シェルと画像を見ること以外の
機能（ChatGPT のコネクタ・プラグイン・web 検索など）は、起動の引数で切る（CODEX_FEATURES_OFF）。
シェルのコマンドには、名前に KEY・SECRET・TOKEN を含む環境変数を渡さない。

どちらも環境変数 VIDEOTAB_CONFINE に作業フォルダを入れて起動する。読み手が実行する
videotab check / zoom は、これより外のパスを受け付けない（Claude Code の許可はサブコマンド
までなので、パスの守りはこれが担う）。

モデルと推論の強さは、画面か videotab run で曲ごとに選んだ値か、選ばなかった項目は利用者の
普段の設定の値を渡す（agent_settings.py）。設定ファイルはエージェントには読ませず、videotab が
モデルと推論の強さだけを読んで起動の引数で渡す。Claude Code には、普段の設定を model と
modelSettings だけの JSON で --settings に、選んだ値を --model / --effort で渡す（推論の強さを
選んだときは、子プロセスの環境変数から CLAUDE_CODE_EFFORT_LEVEL を外す）。Codex には -m と
-c model_reasoning_effort=... で渡す。ほかの設定（権限・フックなど）は渡さない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from videotab.agent_settings import AgentSettings, actual_text, claude_args, codex_args, removed_env, valid_model, validate
from videotab.cancel import Cancel, Cancelled

ENGINES = ("claude", "codex")
DEFAULT_ENGINE = "claude"
CONFINE_ENV = "VIDEOTAB_CONFINE"
TIMEOUT = 60 * 60  # 1 回の起動の上限（秒）
# Codex の読み手で切る機能（-c features.<名前>=false。その版の Codex に無い名前は無視される）。
# 読み取りに要るのは、シェル（videotab check / zoom）と画像を見ることだけ
CODEX_FEATURES_OFF = (
    "apps",  # ChatGPT のコネクタ。sandbox のネットワークの制限が効かない
    "plugins",
    "remote_plugin",
    "tool_suggest",  # プラグインの導入の提案
    "multi_agent",
    "image_generation",
    "browser_use",
    "computer_use",
    "hooks",
    "skill_mcp_dependency_install",
    "shell_snapshot",  # 起動時に、利用者のログインシェルを作業フォルダで実行して環境を写し取る
)


def available_engines() -> dict[str, bool]:
    """手元で使えるエージェントのコマンド（入っているか）。ログインしているかまでは調べない。"""
    return {name: shutil.which(name) is not None for name in ENGINES}


def videotab_bin() -> str:
    """エージェントに実行させる videotab コマンドの絶対パス（作業フォルダがどこでも動くように）。"""
    here = Path(sys.executable).parent / "videotab"
    return str(here) if here.exists() else (shutil.which("videotab") or "videotab")


@dataclass
class AgentResult:
    ok: bool
    text: str  # エージェントの最後の返答
    seconds: float


def _claude_command(
    prompt: str, writable: list[Path], workdir: Path, *, settings: AgentSettings | None = None
) -> list[str]:
    yt = videotab_bin()
    # Read・Glob・Grep は --restricted で作業フォルダの中に限られる。videotab は check と zoom の
    # サブコマンドまでを許し、引数は問わない。引数のパスを許可の書き方（前置一致）で絞っても
    # "<作業フォルダ>/../外" が通るうえ、作業フォルダまで書いた形では Claude Code が一致と
    # みなさず、check / zoom 自体が断られるため。
    # どちらも引数は対象のパスとフレーム番号だけで、作業フォルダの外を指すパスは videotab 自身が
    # VIDEOTAB_CONFINE で拒む（.. やリンクをたどった先で比べる）
    allowed = ["Read", "Glob", "Grep", f"Bash({yt} check:*)", f"Bash({yt} zoom:*)"]
    for path in writable:
        rule = f"/{path.resolve()}"  # "//絶対パス" の形
        allowed += [f"Write({rule})", f"Edit({rule})"]
    # --restricted でも --settings は適用される。渡すのは model と modelSettings だけと、
    # 選んだ値の --model / --effort。--tools・--allowedTools は複数の値を取るので、その前に置く
    extra = claude_args(settings) if settings is not None else []
    return [
        "claude", "-p", prompt,
        "--restricted", "--strict-mcp-config", "--permission-mode", "dontAsk", *extra,
        "--tools", "Read", "Write", "Edit", "Glob", "Grep", "Bash",
        "--allowedTools", *allowed,
        "--output-format", "stream-json", "--verbose",
    ]  # fmt: skip


def _codex_command(
    prompt: str, workdir: Path, last_message: Path, *, settings: AgentSettings | None = None
) -> list[str]:
    # 利用者の設定を読まず、書けるのは作業フォルダの中だけ（workspace-write の範囲）に固定する。
    # workspace-write は既定で /tmp と $TMPDIR にも書けるので、exclude_* で外す（writable_roots=[] では
    # 外れない）。-o の最後の返答は sandbox の外の codex 本体が書くので、$TMPDIR に置いたままでよい。
    # network_access=false が効くのはシェルのコマンドだけなので、ほかの機能と web 検索は別に切る。
    # シェルのコマンドには、名前に KEY・SECRET・TOKEN を含む環境変数を渡さない（Codex の既定は渡す）。
    # モデルと推論の強さだけは、videotab が読んだ値を引数で渡す
    extra = codex_args(settings) if settings is not None else []
    features_off = [arg for name in CODEX_FEATURES_OFF for arg in ("-c", f"features.{name}=false")]
    return [
        "codex", "exec", "--ignore-user-config", "--ignore-rules",
        "--sandbox", "workspace-write",
        "-c", "sandbox_workspace_write.writable_roots=[]",
        "-c", "sandbox_workspace_write.network_access=false",
        "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
        "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        *features_off,
        "-c", 'web_search="disabled"',
        "-c", "shell_environment_policy.ignore_default_excludes=false",
        *extra,
        "--skip-git-repo-check", "--color", "never",
        "-C", str(workdir.resolve()), "-o", str(last_message.resolve()), prompt,
    ]  # fmt: skip


def _launch_dir(engine: str, workdir: Path) -> Path:
    """エージェントのコマンドを起動するときの cwd。

    Claude Code は --restricted でファイル操作を cwd の中に限るので、作業フォルダで起動する。
    Codex には作業フォルダを -C で渡すので、読み手が書けない作業フォルダの親で起動する。
    作業フォルダを cwd にすると、読み手がそこに置いたファイルで、次に起動する codex コマンドが
    変わりうる（実行する版を cwd の設定ファイルで決める道具を通して入れている場合など）。
    """
    return workdir if engine == "claude" else workdir.resolve().parent


def _describe_claude_event(line: str) -> tuple[str | None, str | None]:
    """stream-json の 1 行から（ログに出す短い説明, 最後の返答）を取り出す。"""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line.strip() or None, None
    if event.get("type") == "assistant":
        for c in event.get("message", {}).get("content", []):
            if c.get("type") == "tool_use":
                inp = c.get("input", {})
                target = inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""
                return f"{c['name']} {Path(target).name if c['name'] in ('Read', 'Write', 'Edit') else target}", None
    if event.get("type") == "result":
        denied = event.get("permission_denials") or []
        note = f"（許していない操作 {len(denied)} 件は止めました）" if denied else ""
        return f"終了 {event.get('subtype')}{note}", event.get("result") or ""
    return None, None


def _actual_model(line: str) -> str | None:
    """stream-json の最初の 1 行から、Claude Code が実際に使うモデルを取り出す。取れなければ None。"""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    model = event.get("model") if isinstance(event, dict) else None
    return model if valid_model(model) else None


def run_agent(
    prompt: str,
    *,
    engine: str,
    workdir: Path,
    writable: list[Path],
    log,
    label: str,
    settings: AgentSettings | None = None,
    on_actual=None,
    cancel: Cancel | None = None,
) -> AgentResult:
    """エージェントを 1 回起動し、終わるまで待つ。log(文字列) に進み具合を流す。

    settings は読み取りの設定（モデルと推論の強さ）。Claude Code で実際のモデルが分かったら、
    その本文を log に 1 回出し、on_actual(本文) を呼ぶ。cancel で止められたら Cancelled を出す。
    """
    if engine not in ENGINES:
        raise ValueError(f"engine は {ENGINES} のどれか")
    if settings is not None:
        validate(settings, engine)
    if shutil.which(engine) is None:
        raise RuntimeError(f"{engine} コマンドが見つかりません")
    if cancel is not None:
        cancel.check()
    fd, name = tempfile.mkstemp(prefix="videotab-codex-", suffix=".txt")
    os.close(fd)
    last_message = Path(name)
    if engine == "claude":
        cmd = _claude_command(prompt, writable, workdir, settings=settings)
    else:
        cmd = _codex_command(prompt, workdir, last_message, settings=settings)
    start = time.monotonic()
    final = ""
    env = {**os.environ, CONFINE_ENV: str(workdir.resolve())}
    for name in removed_env(settings) if settings is not None else ():
        env.pop(name, None)
    proc = subprocess.Popen(
        cmd, cwd=_launch_dir(engine, workdir), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True,
    )  # fmt: skip
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(TIMEOUT, on_timeout)
    timer.start()
    try:
        with cancel.watch(proc) if cancel is not None else nullcontext():
            assert proc.stdout is not None
            first = True
            for line in proc.stdout:
                if engine == "claude":
                    if first and settings is not None and (model := _actual_model(line)):
                        text = actual_text(settings, model)
                        log(f"[{label}] {text}")
                        if on_actual is not None:
                            on_actual(text)
                    first = False
                    desc, result = _describe_claude_event(line)
                    if result is not None:
                        final = result
                else:
                    desc = line.rstrip()
                if desc:
                    log(f"[{label}] {desc}")
            proc.wait()
    finally:
        timer.cancel()
    if cancel is not None and cancel.is_set():
        last_message.unlink(missing_ok=True)
        log(f"[{label}] 止めました")
        raise Cancelled()
    if timed_out.is_set():
        log(f"[{label}] {TIMEOUT // 60} 分を超えたので止めました")
    if engine == "codex":
        final = last_message.read_text(encoding="utf-8") if last_message.exists() else ""
    last_message.unlink(missing_ok=True)
    ok = proc.returncode == 0 and not timed_out.is_set()
    return AgentResult(ok, final.strip(), time.monotonic() - start)
