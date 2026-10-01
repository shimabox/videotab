from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_agent_settings(tmp_path_factory, monkeypatch):
    """利用者の本物の設定と環境変数を読まないよう、設定の場所を空の一時フォルダへ向ける。"""
    base = tmp_path_factory.mktemp("agent-config")
    for env, name in (("CLAUDE_CONFIG_DIR", "claude"), ("CODEX_HOME", "codex")):
        (base / name).mkdir()
        monkeypatch.setenv(env, str(base / name))
    for env in ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL"):
        monkeypatch.delenv(env, raising=False)


PROBED = {"width": 1920, "height": 1080, "duration": 12.5}


@pytest.fixture
def fake_probe(monkeypatch):
    """add.probe の偽物（ffprobe を使わない）。確かめたパスを順に記録したリストを返す。

    中身によらず、映像の入った動画（PROBED の幅・高さ・長さ）として通す。
    """
    from videotab import add

    seen: list[Path] = []

    def probe(path):
        seen.append(Path(path))
        return dict(PROBED)

    monkeypatch.setattr(add, "probe", probe)
    return seen


@pytest.fixture
def plain_ids(monkeypatch, fake_probe):
    """取り込んだ曲の ID を、乱数を付けずにファイル名（拡張子を除く）にする。add.probe も偽物にする。"""
    from videotab import add

    monkeypatch.setattr(add, "new_id", lambda name: Path(name).stem)
