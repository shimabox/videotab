"""読み取りのエージェントに渡すモデルと推論の強さを決める。

画面か videotab run で曲ごとに選んだ値（選択）があればそれを、無ければ利用者の普段の
設定の値を渡す。選択は項目ごとに独立で、選ばなかった項目は普段の設定のまま動く。

読み手のプロセスには利用者の設定を読ませない（agent.py）。代わりに videotab が
ユーザー設定のファイルを読み、モデルと推論の強さだけを取り出して起動の引数で渡す。
ほかの値（権限・フックなど）は持ち出さない。普段の設定のファイルには書き込まない。

- Claude Code: settings.json のトップレベルの model と、modelSettings.<モデル名>.effortLevel
- Codex: config.toml のトップレベルの model と model_reasoning_effort

設定に無い項目は何も渡さない。ファイルが壊れている・値が使えないときは、その項目を
渡さずに job.log へ注意書きを 1 行出し、読み取りは止めない。選択の値が検査に通らない
ときは、普段の設定に落とさずに失敗にする。

選択肢の一覧と選択の検査はここに置き、画面（server.py）・videotab run（cli.py）・
読み取りの段（pipeline.py）・起動の直前（agent.py）が同じものを使う。
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

ENGINE_NAMES = {"claude": "Claude Code", "codex": "Codex"}
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh")  # 設定ファイルで受け付けられる値
CLAUDE_ENV_EFFORTS = (*CLAUDE_EFFORTS, "max")  # 環境変数 CLAUDE_CODE_EFFORT_LEVEL で受け付けられる値
CODEX_EFFORT_RE = re.compile(r"[a-z]{1,16}")  # 受け付ける値の一覧が公式に無いので、形だけを見る
MAX_BYTES = 1024 * 1024
MAX_MODEL_SETTINGS = 64  # 引数の長さを抑えるための上限

# 画面と videotab run で選べる値。Claude Code のモデルは CLI の別名（常にその系統の最新を指す）。
# Codex には最新を指す別名が無く、決まった選択肢はすぐ古くなるので、モデル名の選択肢は置かない
# （どちらのエンジンも「その他」で MODEL_RE の形の名前を受ける）
CHOICE_MODELS: dict[str, tuple[str, ...] | None] = {"claude": ("opus", "sonnet", "fable", "haiku"), "codex": None}
CHOICE_EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),  # --effort で受け付けられる値
    "codex": ("minimal", "low", "medium", "high", "xhigh"),
}
CLAUDE_EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"

FROM_FILE = "（普段の設定）"
FROM_ENV = "（環境変数）"
FROM_CHOICE = "（videotab で指定）"


def valid_model(value: object) -> bool:
    return isinstance(value, str) and MODEL_RE.fullmatch(value) is not None


def valid_claude_effort(value: object) -> bool:
    return isinstance(value, str) and value in CLAUDE_EFFORTS


def valid_claude_env_effort(value: object) -> bool:
    return isinstance(value, str) and value in CLAUDE_ENV_EFFORTS


def valid_codex_effort(value: object) -> bool:
    return isinstance(value, str) and CODEX_EFFORT_RE.fullmatch(value) is not None


# --- 選択（画面と videotab run で選んだ値）


def valid_choice_model(engine: str, value: object) -> bool:
    """選んだモデルがそのエンジンで受け付ける値か（一覧の別名か、MODEL_RE の形の名前）。"""
    if not isinstance(value, str) or engine not in CHOICE_MODELS:
        return False
    return value in (CHOICE_MODELS[engine] or ()) or valid_model(value)


def valid_choice_effort(engine: str, value: object) -> bool:
    return isinstance(value, str) and value in CHOICE_EFFORTS.get(engine, ())


@dataclass(frozen=True)
class Choice:
    """曲ごとに選んだモデルと推論の強さ。None の項目は普段の設定。"""

    model: str | None = None
    effort: str | None = None

    def to_json(self) -> dict[str, str]:
        """job.json の choice に書く形（指定した項目だけ）。"""
        return {k: v for k, v in (("model", self.model), ("effort", self.effort)) if v is not None}


def _check_engine(engine: str) -> None:
    if engine not in ENGINE_NAMES:
        raise ValueError(f"engine は {tuple(ENGINE_NAMES)} のどれか")


def normalize_model(engine: str, value: object) -> str | None:
    """選んだモデルを検査して返す。None は普段の設定。使えない値は ValueError（値は文に入れない）。"""
    _check_engine(engine)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("モデルは文字列で指定してください")
    value = value.strip()
    if not value:
        raise ValueError("モデルが空です")
    if not valid_choice_model(engine, value):
        raise ValueError("モデル名は英数字で始まる 128 文字までの名前にしてください（使える記号は . _ : / [ ] -）")
    return value


def normalize_effort(engine: str, value: object) -> str | None:
    """選んだ推論の強さを検査して返す。None は普段の設定。使えない値は ValueError（値は文に入れない）。"""
    _check_engine(engine)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("推論の強さは文字列で指定してください")
    value = value.strip()
    if not value:
        raise ValueError("推論の強さが空です")
    if not valid_choice_effort(engine, value):
        raise ValueError(f"{ENGINE_NAMES[engine]} の推論の強さは {' / '.join(CHOICE_EFFORTS[engine])} のどれかです")
    return value


def normalize_choice(engine: str, model: object = None, effort: object = None) -> Choice:
    """画面・videotab run・job.json から来た値を検査して Choice にする。"""
    return Choice(normalize_model(engine, model), normalize_effort(engine, effort))


def choice_from_job(engine: str, raw: object) -> Choice:
    """job.json の choice（無ければ普段の設定）を検査し直す。使えなければ ValueError。"""
    if raw is None:
        return Choice()
    if not isinstance(raw, dict):
        raise ValueError("選択の形が違います")
    return normalize_choice(engine, raw.get("model"), raw.get("effort"))


def shown_choice(engine: str, raw: object) -> dict[str, str | None]:
    """画面に返す選択。常に {model, effort} の形で、検査に通らない項目は None にする。"""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, str | None] = {}
    for key, check in (("model", normalize_model), ("effort", normalize_effort)):
        try:
            out[key] = check(engine, raw.get(key))
        except ValueError:
            out[key] = None
    return out


@dataclass(frozen=True)
class AgentSettings:
    """読み取りの設定。1 回の読み取りの全起動に同じものを渡す。"""

    engine: str
    model: str | None = None
    model_efforts: dict[str, str] = field(default_factory=dict)  # Claude Code だけ: {モデル名: 強さ}
    effort: str | None = None  # Codex だけ
    # Claude Code だけ（表示用）: 環境変数の有無と、検査に通った値
    env_model_set: bool = False
    env_model: str | None = None
    env_effort_set: bool = False
    env_effort: str | None = None
    notes: tuple[str, ...] = ()  # job.log に出す注意書き
    # 曲ごとに選んだ値（apply_choice で入れる）。ファイルから読んだ値より優先する
    chosen_model: str | None = None
    chosen_effort: str | None = None


def apply_choice(settings: AgentSettings, choice: Choice | None) -> AgentSettings:
    """普段の設定に、曲ごとの選択を重ねる。選択が settings のエンジンで使えなければ ValueError。"""
    if choice is None:
        return settings
    checked = normalize_choice(settings.engine, choice.model, choice.effort)
    return replace(settings, chosen_model=checked.model, chosen_effort=checked.effort)


def validate(settings: AgentSettings, engine: str) -> None:
    """起動の直前の検査。エンジンが違う・値が検査に通らなければ ValueError。"""
    if not isinstance(settings, AgentSettings):
        raise ValueError("読み取りの設定の形が違います")
    if settings.engine != engine:
        raise ValueError(f"読み取りの設定のエンジン（{settings.engine}）が起動するエンジン（{engine}）と違います")
    if settings.chosen_model is not None and not valid_choice_model(engine, settings.chosen_model):
        raise ValueError("選んだモデルが使えない値です")
    if settings.chosen_effort is not None and not valid_choice_effort(engine, settings.chosen_effort):
        raise ValueError("選んだ推論の強さが使えない値です")
    if settings.model is not None and not valid_model(settings.model):
        raise ValueError("読み取りの設定のモデルが使えない値です")
    if settings.env_model is not None and not valid_model(settings.env_model):
        raise ValueError("環境変数のモデルが使えない値です")
    if settings.env_effort is not None and not valid_claude_env_effort(settings.env_effort):
        raise ValueError("環境変数の推論の強さが使えない値です")
    efforts = settings.model_efforts
    if engine == "claude":
        if settings.effort is not None:
            raise ValueError("Claude Code の推論の強さはモデルごとに渡します")
        if not isinstance(efforts, dict) or len(efforts) > MAX_MODEL_SETTINGS:
            raise ValueError("読み取りの設定のモデルごとの推論の強さが使えない形です")
        if not all(valid_model(k) and valid_claude_effort(v) for k, v in efforts.items()):
            raise ValueError("読み取りの設定のモデルごとの推論の強さに使えない値があります")
    else:
        if efforts:
            raise ValueError("Codex にモデルごとの推論の強さは渡せません")
        if settings.effort is not None and not valid_codex_effort(settings.effort):
            raise ValueError("読み取りの設定の推論の強さが使えない値です")


# --- 読み出し


def _env(name: str) -> str | None:
    return os.environ.get(name) or None  # 空文字は未設定として扱う


def claude_settings_path() -> Path:
    base = _env("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "settings.json"


def codex_config_path() -> Path:
    base = _env("CODEX_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".codex") / "config.toml"


def _read_table(path: Path, engine: str, parse, kind: str) -> tuple[dict | None, list[str]]:
    """設定ファイルを読んで表（dict）にする。無ければ (None, [])、使えなければ (None, [注意書き])。"""
    name = ENGINE_NAMES[engine]

    def unusable(reason: str) -> tuple[None, list[str]]:
        # ファイルの中身とパスは書かない
        return None, [f"{name} のユーザー設定は{reason}ので、モデルと推論の強さは渡しません"]

    try:
        with path.open("rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None, []
    except OSError:
        return unusable("読み込めない")
    if len(raw) > MAX_BYTES:
        return unusable(" 1 MB を超えている")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return unusable(" UTF-8 ではない")
    try:
        data = parse(text)
    except (ValueError, RecursionError):
        return unusable(f" {kind} として読めない")
    if not isinstance(data, dict):
        return unusable("トップレベルが表ではない")
    return data, []


def _load_claude() -> AgentSettings:
    name = ENGINE_NAMES["claude"]
    data, notes = _read_table(claude_settings_path(), "claude", json.loads, "JSON")
    data = data or {}
    model = None
    if "model" in data:
        if valid_model(data["model"]):
            model = data["model"]
        else:
            notes.append(f"{name} のユーザー設定の model は使えない値なので渡しません")
    efforts: dict[str, str] = {}
    if "modelSettings" in data:
        table = data["modelSettings"]
        if not isinstance(table, dict):
            notes.append(f"{name} のユーザー設定の modelSettings は表ではないので渡しません")
        else:
            dropped = 0
            for key, value in table.items():
                if isinstance(value, dict) and "effortLevel" not in value:
                    continue  # 推論の強さ以外の設定だけを持つ項目
                if valid_model(key) and isinstance(value, dict) and valid_claude_effort(value["effortLevel"]):
                    efforts[key] = value["effortLevel"]
                else:
                    dropped += 1  # max は設定ファイルでは受け付けられないので、ここで除く
            if len(efforts) > MAX_MODEL_SETTINGS:
                efforts = {}
                notes.append(
                    f"{name} のユーザー設定の modelSettings は {MAX_MODEL_SETTINGS} 個を超えるので渡しません"
                )
            elif dropped:
                notes.append(f"{name} のユーザー設定の modelSettings のうち、使えない項目 {dropped} 個は渡しません")
    env_model = _env("ANTHROPIC_MODEL")
    env_effort = _env("CLAUDE_CODE_EFFORT_LEVEL")
    return AgentSettings(
        engine="claude",
        model=model,
        model_efforts=efforts,
        env_model_set=env_model is not None,
        env_model=env_model if valid_model(env_model) else None,
        env_effort_set=env_effort is not None,
        env_effort=env_effort if valid_claude_env_effort(env_effort) else None,
        notes=tuple(notes),
    )


def _load_codex() -> AgentSettings:
    # profile（--profile で選ぶ設定）は読まない。読み手は --profile なしで起動するので、
    # 効くのはトップレベルだけ
    name = ENGINE_NAMES["codex"]
    data, notes = _read_table(codex_config_path(), "codex", tomllib.loads, "TOML")
    data = data or {}
    values: dict[str, str | None] = {"model": None, "model_reasoning_effort": None}
    checks = {"model": valid_model, "model_reasoning_effort": valid_codex_effort}
    for key, check in checks.items():
        if key not in data:
            continue
        if check(data[key]):
            values[key] = data[key]
        else:
            notes.append(f"{name} のユーザー設定の {key} は使えない値なので渡しません")
    return AgentSettings(
        engine="codex", model=values["model"], effort=values["model_reasoning_effort"], notes=tuple(notes)
    )


def load_settings(engine: str) -> AgentSettings:
    """engine のユーザー設定から、読み取りの設定を読み出す。ファイルの問題では例外にしない。"""
    if engine == "claude":
        return _load_claude()
    if engine == "codex":
        return _load_codex()
    raise ValueError(f"engine は {tuple(ENGINE_NAMES)} のどれか")


# --- 渡す内容


def claude_settings_json(settings: AgentSettings) -> str | None:
    """--settings に渡す JSON（model と modelSettings の 2 キーだけ）。渡すものが無ければ None。

    選んだ項目は --model / --effort で渡すので、その項目は普段の設定から入れない。
    """
    data: dict = {}
    if settings.chosen_model is None and settings.model is not None:
        data["model"] = settings.model
    if settings.chosen_effort is None and settings.model_efforts:
        data["modelSettings"] = {k: {"effortLevel": v} for k, v in settings.model_efforts.items()}
    if not data:
        return None
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def claude_args(settings: AgentSettings) -> list[str]:
    """claude に足す引数（--settings・--model・--effort）。渡すものが無ければ空。

    選んだモデルは --model で渡す（--settings の model は ANTHROPIC_MODEL に負けるため）。
    選んだ推論の強さは --effort で渡す（設定ファイルの形では max を渡せず、modelSettings は
    実際のモデル名が要るため）。
    """
    args = []
    text = claude_settings_json(settings)
    if text:
        args += ["--settings", text]
    if settings.chosen_model is not None:
        args += ["--model", settings.chosen_model]
    if settings.chosen_effort is not None:
        args += ["--effort", settings.chosen_effort]
    return args


def removed_env(settings: AgentSettings) -> tuple[str, ...]:
    """読み手の子プロセスの環境変数から外す名前。

    Claude Code で推論の強さを選んだときは CLAUDE_CODE_EFFORT_LEVEL を外す（--effort との
    優劣が資料で確かめられないので、外して選んだ値を確実に効かせる）。ANTHROPIC_MODEL は
    --model が優先するので外さない。
    """
    if settings.engine == "claude" and settings.chosen_effort is not None:
        return (CLAUDE_EFFORT_ENV,)
    return ()


def removed_env_notes(settings: AgentSettings) -> list[str]:
    """環境変数を外すときに job.log へ出す注意書き（今の環境にあるものだけ）。"""
    return [
        f"推論の強さを videotab で指定したので、環境変数 {name} は読み取りのエージェントに渡しません"
        for name in removed_env(settings)
        if name in os.environ
    ]


def codex_args(settings: AgentSettings) -> list[str]:
    """codex exec に足す引数。渡すものが無ければ空。

    Codex はユーザー設定を読まずに起動するので、選ばなかった項目は videotab が読んだ普段の値を渡す。
    """
    model = settings.chosen_model if settings.chosen_model is not None else settings.model
    effort = settings.chosen_effort if settings.chosen_effort is not None else settings.effort
    args = []
    if model is not None:
        args += ["-m", model]
    if effort is not None:
        args += ["-c", f'model_reasoning_effort="{effort}"']
    return args


# --- 表示


def _env_text(label: str, value: str | None) -> str:
    return f"{label} {value}{FROM_ENV}" if value else f"{label}{FROM_ENV}"


def summary(settings: AgentSettings) -> str:
    """段の開始時に結果欄へ出す本文（どのモデル・推論の強さで読むか）。選んだ項目を先に見る。"""
    if settings.engine == "codex":
        if settings.chosen_model:
            model = f"モデル {settings.chosen_model}{FROM_CHOICE}"
        else:
            model = f"モデル {settings.model}{FROM_FILE}" if settings.model else "モデル CLI の既定"
        if settings.chosen_effort:
            effort = f"推論の強さ {settings.chosen_effort}{FROM_CHOICE}"
        else:
            effort = f"推論の強さ {settings.effort}{FROM_FILE}" if settings.effort else "推論の強さ CLI の既定"
        return f"{model}・{effort}"
    if settings.chosen_model:
        model = f"モデル {settings.chosen_model}{FROM_CHOICE}"
    elif settings.env_model_set:
        model = _env_text("モデル", settings.env_model)
    elif settings.model:
        model = f"モデル {settings.model}{FROM_FILE}"
    else:
        model = "モデル CLI の既定"
    if settings.chosen_effort:
        effort = f"推論の強さ {settings.chosen_effort}{FROM_CHOICE}"
    elif settings.env_effort_set:
        effort = _env_text("推論の強さ", settings.env_effort)
    elif settings.model_efforts:
        effort = "推論の強さはモデルごとの普段の設定"
    else:
        effort = "推論の強さ モデルの既定"
    return f"{model}・{effort}"


def start_line(settings: AgentSettings) -> str:
    """段の開始時に job.log へ出す 1 行。"""
    return f"読み取りの設定: {ENGINE_NAMES[settings.engine]}・{summary(settings)}"


def actual_text(settings: AgentSettings, model: str) -> str:
    """Claude Code の起動ごとの本文。推論の強さは、渡した内容と環境変数から対応づけた値。"""
    if settings.chosen_effort:
        effort = f"推論の強さ {settings.chosen_effort}{FROM_CHOICE}"
    elif settings.env_effort_set:
        effort = _env_text("推論の強さ", settings.env_effort)
    elif model in settings.model_efforts:
        effort = f"推論の強さ {settings.model_efforts[model]}{FROM_FILE}"
    else:
        effort = "推論の強さ モデルの既定"
    return f"実際のモデル {model}・{effort}"


# --- 画面の選択肢


def _usual_env(value: str | None) -> str:
    return f"環境変数 {value}" if value else "環境変数"


def usual_labels(settings: AgentSettings) -> dict[str, str]:
    """画面の「普段の設定（…）」の括弧に入れる文字列。検査に通った値だけを使う。"""
    if settings.engine == "codex":
        return {"model": settings.model or "CLI の既定", "effort": settings.effort or "CLI の既定"}
    if settings.env_model_set:
        model = _usual_env(settings.env_model)
        usual_model = settings.env_model
    else:
        model = settings.model or "CLI の既定"
        usual_model = settings.model
    # 普段のモデルと同じ名前のキーがあるときだけ値を出す（別名を含むキーからは推定しない）
    if settings.env_effort_set:
        effort = with_model = _usual_env(settings.env_effort)
    elif settings.model_efforts:
        with_model = "モデルごと"
        effort = settings.model_efforts.get(usual_model or "", with_model)
    else:
        effort = with_model = "モデルの既定"
    return {"model": model, "effort": effort, "effort_with_model": with_model}


def agent_options(load=None) -> dict[str, dict]:
    """GET /api/jobs に返す選択肢と普段の値。設定のパス・注意書き・ほかのキーは入れない。"""
    load = load or load_settings
    out = {}
    for engine in ENGINE_NAMES:
        settings = load(engine)  # 注意書きは捨てる（読み取りの段で job.log に出す）
        models = CHOICE_MODELS[engine]
        entry: dict = {
            "models": list(models) if models is not None else None,
            "efforts": list(CHOICE_EFFORTS[engine]),
            "usual": usual_labels(settings),
        }
        if engine == "codex":
            entry["model_hint"] = settings.model
        out[engine] = entry
    return out
