"""読み取りのエージェントから実行した videotab check / zoom の閉じ込め（VIDEOTAB_CONFINE）。"""

import builtins
import io
import json
import os
import pathlib

import pytest
from synth import TAB_LINES, frame, write_frames

from videotab import build, cli, confine, read

SEVEN_STRINGS = "e4 b3 g3 d3 a2 e2 b1"
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
needs_permissions = pytest.mark.skipif(AS_ROOT, reason="管理者権限ではファイルの権限が効かない")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def song(tmp_path, **score):
    """meta.json と score.json を持つ作業フォルダ。"""
    wd = tmp_path / "song"
    write(wd / "meta.json", {"id": "song", "title": "Song"})
    write(wd / "score.json", {"title": "曲", "tempo": 100, **score})
    return wd


def confine_to(monkeypatch, wd, on=True):
    if on:
        monkeypatch.setenv("VIDEOTAB_CONFINE", str(wd))
    else:
        monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)


# --- 課題 1: 作業フォルダの決め方


@pytest.mark.parametrize("confined", [False, True])
def test_check_of_file_outside_parts_uses_score_of_workdir(tmp_path, monkeypatch, confined):
    wd = song(tmp_path, time_signature=[3, 4])
    write(wd / "readers" / "extra.json", {"1": "r.2 {d}", "2": "(0.1).2 {d}"})
    confine_to(monkeypatch, wd, confined)
    assert build.run_check(wd / "readers" / "extra.json") == 0


def test_confined_check_uses_confined_score_wherever_the_file_is(tmp_path, monkeypatch, capsys):
    wd = song(tmp_path, time_signature=[3, 4], tuning=SEVEN_STRINGS)
    deep = wd / "a" / "b" / "c" / "d" / "e" / "x.json"  # meta.json を探す 4 階層より深い
    write(deep, {"1": "(0.7).2 {d}"})
    confine_to(monkeypatch, wd, False)
    assert build.run_check(deep) == 1  # 閉じ込めが無ければ既定の 4/4・6 弦
    confine_to(monkeypatch, wd)
    capsys.readouterr()
    assert build.run_check(deep) == 0
    assert "誤り 0、注意 0" in capsys.readouterr().out


@pytest.mark.parametrize("confined", [False, True])
def test_check_of_parts_and_resolve_is_unchanged(tmp_path, monkeypatch, confined):
    wd = song(tmp_path)
    write(wd / "parts" / "part_A.json", {"1": "r.1", "2": "\\ts 3 4 r.2 {d}"})
    write(wd / "parts" / "part_B.json", {"3": "r.2 {d}"})
    write(wd / "resolve.json", {"3": "r.2 {d}"})
    confine_to(monkeypatch, wd, confined)
    assert build.run_check(wd / "parts" / "part_B.json") == 0
    assert build.run_check(wd / "resolve.json") == 0
    write(wd / "resolve.json", {"3": "r.2"})
    assert build.run_check(wd / "resolve.json") == 1
    score = build.load_score(wd)
    assert read.part_issues(wd / "parts" / "part_B.json", score) == []
    assert read.part_issues(wd / "parts" / "part_B.json", score, wd) == []


@pytest.mark.parametrize("confined", [False, True])
def test_check_in_history_uses_time_signature_of_that_history(tmp_path, monkeypatch, confined):
    wd = song(tmp_path)
    # 今の読み取りは 2/4、前回（history）の読み取りは 3/4
    write(wd / "parts" / "part_A.json", {"1": "r.1", "2": "\\ts 2 4 r.2"})
    old = wd / "history" / "20260101-000000"
    write(old / "parts" / "part_A.json", {"1": "r.1", "2": "\\ts 3 4 r.2 {d}"})
    write(old / "parts" / "part_B.json", {"3": "r.2 {d}"})
    write(old / "resolve.json", {"3": "r.2 {d}"})
    confine_to(monkeypatch, wd, confined)
    assert build.run_check(old / "parts" / "part_B.json") == 0
    assert build.run_check(old / "resolve.json") == 0
    score = build.load_score(wd)
    assert read.part_issues(old / "parts" / "part_B.json", score) == []
    # 今の読み取りの続きとして検査すると 2/4 なので誤り
    write(wd / "resolve.json", {"3": "r.2 {d}"})
    assert build.run_check(wd / "resolve.json") == 1


# --- 外のパスへの照会の見張り


class Spy:
    """os と open に渡ったパスを記録し、作業フォルダの外（tmp_path の中で wd の外）に触れた呼び出しを探す。"""

    FUNCS = ("stat", "lstat", "readlink", "open", "scandir", "listdir", "mkdir", "makedirs", "unlink", "remove",
             "rmdir", "rename", "replace", "chmod", "access", "utime")  # fmt: skip
    NOFOLLOW = {"lstat", "readlink", "mkdir", "makedirs", "unlink", "remove", "rmdir", "rename", "replace"}

    def __init__(self, tmp_path, wd):
        self.tmp, self.wd = str(tmp_path), str(wd)
        self.calls = []

    def _wrap(self, name, real):
        def spy(*args, **kwargs):
            for arg in args[:2] if name in ("rename", "replace") else args[:1]:
                if isinstance(arg, (str, bytes, os.PathLike)):
                    p = os.fsdecode(arg)
                    p = p if os.path.isabs(p) else os.path.join(os.getcwd(), p)  # .. は畳まない（OS と同じに解く）
                    self.calls.append((name, p, name not in self.NOFOLLOW and kwargs.get("follow_symlinks", True)))
            return real(*args, **kwargs)

        return spy

    def watch(self, monkeypatch):
        for name in self.FUNCS:
            monkeypatch.setattr(os, name, self._wrap(name, getattr(os, name)))
        monkeypatch.setattr(builtins, "open", self._wrap("open", builtins.open))
        monkeypatch.setattr(io, "open", self._wrap("open", io.open))

    def outside(self):
        """外に触れた呼び出し。見張りを外してから呼ぶ。

        OS がパスを 1 要素ずつたどる道筋（リンクと .. を OS と同じ順に解く）の、どこかで外に
        入ったものを数える。途中で外を通って中へ戻るものも数える。作業フォルダ自身の親は、
        途中に通るのと、VIDEOTAB_CONFINE の値を resolve するための lstat だけは数えない。
        """
        bad = []
        ancestors = {str(p) for p in pathlib.Path(self.wd).parents}
        for name, p, follow in self.calls:
            steps = trail(p, follow)
            for i, where in enumerate(steps):
                if where in ancestors and (i < len(steps) - 1 or (not follow and name in ("lstat", "stat"))):
                    continue
                under_tmp = where == self.tmp or where.startswith(self.tmp + os.sep)
                in_wd = where == self.wd or where.startswith(self.wd + os.sep)
                if under_tmp and not in_wd:
                    bad.append((name, p))
                    break
        return bad


def trail(path, follow_last):
    """OS が path をたどるときに調べる実体のパスを順に返す（最後の要素のリンクは follow_last のときだけたどる）。"""
    visited = []
    todo = list(reversed(pathlib.PurePosixPath(path).parts[1:]))
    here = "/"
    links = 0
    while todo:
        name = todo.pop()
        if name == "..":
            here = os.path.dirname(here)
            visited.append(here)
            continue
        step = os.path.join(here, name)
        visited.append(step)
        if (todo or follow_last) and os.path.islink(step) and links < 40:
            links += 1
            dest = pathlib.PurePosixPath(os.readlink(step))
            if dest.is_absolute():
                here, parts = "/", dest.parts[1:]
            else:
                parts = dest.parts
            todo.extend(reversed(parts))
            continue
        here = step
    return visited


def run_watched(tmp_path, wd, monkeypatch, fn, *args):
    """fn(*args) を見張りの下で実行し、(戻り値か SystemExit の中身, 外に触れた呼び出し) を返す。"""
    spy = Spy(tmp_path, wd)
    with monkeypatch.context() as m:
        spy.watch(m)
        try:
            result = fn(*args)
        except SystemExit as e:
            result = ("SystemExit", e.code)
    return result, spy.outside()


def reader_folder(tmp_path):
    """読み手の作業フォルダ（帯の位置・見出し・読み取り結果・フレーム 1 枚）。"""
    wd = tmp_path / "wd"
    strip = {"band": [240, 350], "polarity": -1, "background": [252, 252, 252], "staves": [TAB_LINES]}
    write(wd / "meta.json", {"id": "wd", "title": "t", "strip": strip})
    write(wd / "score.json", {"title": "t", "tempo": 100})
    write(wd / "parts" / "part_A.json", {"1": "r.1"})
    write_frames(wd, [frame(0)])
    return wd


OUTSIDE_STATES = ["file", "missing", "folder", pytest.param("no-permission", marks=needs_permissions), "link-inside"]


@pytest.fixture(params=OUTSIDE_STATES)
def world(request, tmp_path, monkeypatch):
    """作業フォルダ wd（閉じ込め）と、外の out/x（ある・無い・フォルダ・権限なし・中へのリンク）。

    wd の中に、out/x へのリンク lnk と、out へのリンク dlnk を置く。
    """
    wd = reader_folder(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (wd / "lnk").symlink_to(out / "x")
    (wd / "dlnk").symlink_to(out)
    x = out / "x"
    if request.param in ("file", "no-permission"):
        write(x, {"1": "r.1"})
    elif request.param == "folder":
        write(x / "parts" / "part_A.json", {"1": "r.1"})
    elif request.param == "link-inside":
        x.symlink_to(wd / "parts" / "part_A.json")  # 外を経由して中へ戻る
    if request.param == "no-permission":
        out.chmod(0)
    confine_to(monkeypatch, wd)
    yield wd
    out.chmod(0o755)


# 外を指す引数の形: .. で外へ出る・中のリンクが外を指す・途中のフォルダが外へのリンク
SHAPES = {
    "dotdot": lambda wd: f"{wd}/../out/x",
    "link": lambda wd: str(wd / "lnk"),
    "folder-link": lambda wd: str(wd / "dlnk" / "x"),
}


def must_not_call(*args, **kwargs):
    raise AssertionError("閉じ込めの確認より前に呼ばれた")


def test_spy_sees_touches_through_links(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("{}", encoding="utf-8")
    (wd / "lnk").symlink_to(tmp_path / "out" / "x")
    (wd / "dlnk").symlink_to(tmp_path / "out")
    (tmp_path / "out" / "back").symlink_to(wd / "parts")
    (wd / "round").symlink_to(tmp_path / "out" / "back")
    for touch in (
        lambda: (wd / "lnk").resolve(),  # パス全体の resolve は外をたどる
        lambda: (wd / "lnk").read_text(encoding="utf-8"),
        lambda: pathlib.Path(f"{wd}/dlnk/../wd").exists(),  # OS は .. の前にリンクをたどる
        lambda: (wd / "round" / "part_A.json").read_text(encoding="utf-8"),  # 外を通って中へ戻る
        lambda: os.lstat(tmp_path / "out" / "x"),
    ):
        _, touched = run_watched(tmp_path, wd, monkeypatch, touch)
        assert touched
    _, touched = run_watched(tmp_path, wd, monkeypatch, lambda: (wd / "parts" / "part_A.json").read_text(encoding="utf-8"))
    assert touched == []


@pytest.mark.parametrize("shape", SHAPES)
def test_guard_refuses_without_looking_outside(world, shape, tmp_path, monkeypatch):
    wd = world
    result, touched = run_watched(tmp_path, wd, monkeypatch, lambda: confine.guard(SHAPES[shape](wd), given=True))
    assert result == ("SystemExit", f"指定のパスは作業フォルダ {wd} の外です")
    assert touched == []


def test_guard_names_the_link_inside_for_files_it_reads(world, tmp_path, monkeypatch):
    wd = world
    for path, rel in ((wd / "lnk", "lnk"), (wd / "dlnk" / "x", "dlnk/x")):
        result, touched = run_watched(tmp_path, wd, monkeypatch, confine.guard, path)
        assert result == ("SystemExit", f"{rel} は作業フォルダの外を指しています")
        assert touched == []


def test_guard_follows_links_inside_and_stops_at_loops(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    part = wd / "parts" / "part_A.json"
    (wd / "rel").symlink_to("parts")
    (wd / "abs").symlink_to(part)
    (wd / "sub").mkdir()
    (wd / "sub" / "up").symlink_to("../rel")
    (wd / "l1").symlink_to("l2")
    (wd / "l2").symlink_to("l1")
    confine_to(monkeypatch, wd)

    def guarded():
        assert confine.guard(wd / "rel" / "part_A.json") == part
        assert confine.guard(wd / "abs") == part
        assert confine.guard(wd / "sub" / "up" / "part_A.json") == part
        assert confine.guard(wd / "rel" / "new.json") == wd / "parts" / "new.json"  # 無いパスは中と判定
        assert confine.guard(wd) == wd
        confine.guard(wd / "l1" / "x")

    result, touched = run_watched(tmp_path, wd, monkeypatch, guarded)
    assert result == ("SystemExit", "l1/x はリンクが多すぎてたどれません")
    assert touched == []
    confine_to(monkeypatch, wd, False)
    assert confine.guard(tmp_path / "anything") == tmp_path / "anything"  # 閉じ込めが無ければ何もしない


# --- 課題 2: 確認の順序


CMDS = {"check": [], "zoom": ["1"]}


@pytest.mark.parametrize("cmd", CMDS)
@pytest.mark.parametrize("shape", SHAPES)
def test_cli_refuses_outside_the_same_way_whatever_is_there(world, cmd, shape, tmp_path, monkeypatch, capsys):
    wd = world
    monkeypatch.setattr(cli, "resolve_target", must_not_call)
    monkeypatch.setattr(pathlib.Path, "is_file", must_not_call)
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, [cmd, SHAPES[shape](wd), *CMDS[cmd]])
    assert result == ("SystemExit", f"指定のパスは作業フォルダ {wd} の外です")
    assert capsys.readouterr().out == ""
    assert touched == []


def test_cli_folds_dotdot_before_following_links(world, tmp_path, monkeypatch, capsys):
    wd = world
    monkeypatch.setattr(cli, "resolve_target", must_not_call)

    def run():
        assert cli.main(["check", f"{wd}/dlnk/../parts/part_A.json"]) == 0
        assert cli.main(["check", f"{wd}/lnk/../parts/part_A.json"]) == 0
        assert cli.main(["zoom", f"{wd}/dlnk/..", "1"]) == 0

    result, touched = run_watched(tmp_path, wd, monkeypatch, run)
    assert touched == []
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["part_A.json: 1 小節（1〜1）、誤り 0、注意 0"] * 2
    assert out[2:] and all(line.startswith(str(wd / "pages" / "zoom" / "f0001_")) for line in out[2:])


def test_paths_given_to_readers_pass_but_aliases_are_refused(tmp_path, monkeypatch, capsys):
    real = reader_folder(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    (real / "via-alias").symlink_to(alias / "parts")
    monkeypatch.setenv("VIDEOTAB_CONFINE", str(alias))  # 作業フォルダは resolve して使う
    wd = real.resolve()
    assert cli.main(["check", f"{wd}/parts/part_A.json"]) == 0
    assert cli.main(["check", str(wd)]) == 0
    assert cli.main(["zoom", str(wd), "1"]) == 0
    monkeypatch.chdir(wd)
    assert cli.main(["check", "parts/part_A.json"]) == 0
    for arg in (alias / "parts" / "part_A.json", wd / "via-alias" / "part_A.json"):
        with pytest.raises(SystemExit) as e:
            cli.main(["check", str(arg)])
        assert e.value.code == f"指定のパスは作業フォルダ {wd} の外です"


def test_confined_cli_does_not_read_ids_as_work_folders(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    confine_to(monkeypatch, wd)
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "work" / "song" / "parts" / "part_A.json", {"1": "r.1"})
    with pytest.raises(SystemExit) as e:
        cli.main(["check", "song"])  # work/song ではなく ./song として扱う
    assert e.value.code == f"指定のパスは作業フォルダ {wd} の外です"


@needs_permissions
def test_confined_unreadable_file_inside_ends_with_short_message(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    part = wd / "parts" / "part_A.json"
    image = next((wd / "frames").iterdir())
    locked = wd / "locked"
    locked.mkdir()
    confine_to(monkeypatch, wd)
    for p in (part, image, locked):
        p.chmod(0)
    try:
        cases = (
            (["check", str(part)], "parts/part_A.json を扱えません（Permission denied）"),
            (["zoom", str(wd), "1"], f"frames/{image.name} を扱えません（Permission denied）"),
            (["check", str(locked / "x.json")], "locked/x.json を調べられません（Permission denied）"),
        )
        for argv, message in cases:
            with pytest.raises(SystemExit) as e:
                cli.main(argv)
            assert e.value.code == message
    finally:
        for p in (part, image):
            p.chmod(0o644)
        locked.chmod(0o755)


def test_short_message_keeps_paths_outside_out(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    confine_to(monkeypatch, wd)
    inside = str(wd / "parts" / "part_A.json")
    assert confine.describe_os_error(PermissionError(13, "Permission denied", inside)) == \
        "parts/part_A.json を扱えません（Permission denied）"
    for filename in (str(tmp_path / "out" / "x"), os.fsencode(tmp_path / "out" / "x"), 3, None):
        assert confine.describe_os_error(OSError(5, "Input/output error", filename)) == "ファイルを扱えません（Input/output error）"


def test_without_confine_other_folders_are_checked_as_before(tmp_path, monkeypatch):
    other = reader_folder(tmp_path)
    confine_to(monkeypatch, other, False)
    assert cli.main(["check", str(other)]) == 0
    assert cli.main(["check", str(other / "parts" / "part_A.json")]) == 0
    assert cli.main(["zoom", str(other), "1"]) == 0


def test_confined_check_does_not_look_into_neighbour_parts_linking_outside(world, tmp_path, monkeypatch):
    wd = world
    write(wd / "readers" / "extra.json", {"1": "r.1"})
    (wd / "readers" / "parts").symlink_to(tmp_path / "out" / "x")
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, ["check", str(wd / "readers" / "extra.json")])
    assert result == ("SystemExit", "readers/parts は作業フォルダの外を指しています")
    assert touched == []


def test_confined_folder_named_parts_does_not_read_its_parent(tmp_path, monkeypatch):
    wd = tmp_path / "parts"
    write(wd / "meta.json", {"id": "parts", "title": "t"})
    write(wd / "score.json", {"title": "t", "tempo": 100, "time_signature": [3, 4]})
    write(wd / "part_A.json", {"1": "r.2 {d}"})
    write(tmp_path / "meta.json", {"id": "outside", "title": "t"})
    write(tmp_path / "score.json", {"title": "t", "tempo": 100, "time_signature": [4, 4]})
    confine_to(monkeypatch, wd)
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, ["check", str(wd / "part_A.json")])
    assert result == ("SystemExit", f"作業フォルダ {wd} の外は扱えません")
    assert touched == []


# --- 課題 3: 中のリンク


def link_outside(tmp_path, wd, rel, *, data=None, image=False, folder=False):
    """wd/rel を、外の out/<名前> を指すリンクに置き換える。外には、たどれば使える中身を置く。"""
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    inside = wd / rel
    target = out / inside.name
    if data is not None:
        write(target, data)
    elif image:
        from PIL import Image

        Image.fromarray(frame(0)).save(target)
    elif folder:
        target.mkdir(parents=True, exist_ok=True)
    if inside.is_dir() and not inside.is_symlink():  # フォルダは中身ごと外へ移す
        for child in inside.iterdir():
            os.rename(child, target / child.name)
        inside.rmdir()
    elif inside.exists() or inside.is_symlink():
        inside.unlink()
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.symlink_to(target)
    return target


STRIP = {"band": [240, 350], "polarity": -1, "background": [252, 252, 252], "staves": [TAB_LINES]}

# (リンクに置き換えるもの, 外に置く中身, 実行するコマンド)
READ_LINKS = {
    "meta.json": ({"data": {"id": "x", "title": "t", "strip": STRIP}}, ["zoom", "{wd}", "1"]),
    "score.json": ({"data": {"title": "t", "tempo": 100, "time_signature": [1, 4]}}, ["check", "{wd}/parts/part_A.json"]),
    "parts": ({"folder": True}, ["check", "{wd}"]),
    "parts/part_A.json": ({"data": {"1": "r.1"}}, ["check", "{wd}"]),
    "frames": ({"folder": True}, ["zoom", "{wd}", "1"]),
    "frames/0001_00m00s000.png": ({"image": True}, ["zoom", "{wd}", "1"]),
}


@pytest.mark.parametrize("rel", READ_LINKS)
def test_confined_commands_do_not_read_through_links_to_outside(rel, tmp_path, monkeypatch, capsys):
    wd = reader_folder(tmp_path)
    kind, argv = READ_LINKS[rel]
    link_outside(tmp_path, wd, rel, **kind)
    confine_to(monkeypatch, wd)
    argv = [a.format(wd=wd) for a in argv]
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, argv)
    assert result == ("SystemExit", f"{rel} は作業フォルダの外を指しています")
    assert touched == []
    assert capsys.readouterr().out == ""


def test_confined_check_makes_nothing_through_score_link_to_missing_file(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    (wd / "score.json").unlink()
    (tmp_path / "out").mkdir()
    missing = tmp_path / "out" / "score.json"
    (wd / "score.json").symlink_to(missing)
    confine_to(monkeypatch, wd)
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, ["check", str(wd / "parts" / "part_A.json")])
    assert result == ("SystemExit", "score.json は作業フォルダの外を指しています")
    assert touched == []
    assert not missing.exists() and list((tmp_path / "out").iterdir()) == []


def test_confined_zoom_replaces_links_among_outputs(tmp_path, monkeypatch, capsys):
    wd = reader_folder(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    victims = {name: out / name for name in ("sym.png", "hard.png")}
    for p in victims.values():
        p.write_bytes(b"keep")
    zoom = wd / "pages" / "zoom"
    zoom.mkdir(parents=True)
    (zoom / "f0001_a.png").symlink_to(victims["sym.png"])
    os.link(victims["hard.png"], zoom / "f0001_b.png")
    confine_to(monkeypatch, wd)
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, ["zoom", str(wd), "1"])
    assert result == 0
    assert touched == []
    assert all(p.read_bytes() == b"keep" for p in victims.values())
    for name in ("f0001_a.png", "f0001_b.png"):
        made = zoom / name
        assert not made.is_symlink() and made.stat().st_nlink == 1 and made.read_bytes().startswith(b"\x89PNG")
    assert f"{zoom / 'f0001_a.png'}" in capsys.readouterr().out


@pytest.mark.parametrize("rel", ["pages", "pages/zoom"])
@pytest.mark.parametrize("exists", [True, False])
def test_confined_zoom_refuses_output_folder_linking_outside(rel, exists, tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    target = out / "dest"
    if exists:
        target.mkdir()
    (wd / rel).parent.mkdir(parents=True, exist_ok=True)
    (wd / rel).symlink_to(target)
    confine_to(monkeypatch, wd)
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, ["zoom", str(wd), "1"])
    assert result == ("SystemExit", "pages/zoom は作業フォルダの外を指しています")
    assert touched == []
    assert list(out.iterdir()) == ([target] if exists else [])
    assert not exists or list(target.iterdir()) == []


def test_confined_check_skips_time_signature_behind_link_to_outside(tmp_path, monkeypatch, capsys):
    wd = reader_folder(tmp_path)
    link_outside(tmp_path, wd, "parts/part_A.json", data={"1": "r.1", "2": "\\ts 3 4 r.2 {d}"})
    write(wd / "parts" / "part_B.json", {"3": "r.2 {d}"})
    confine_to(monkeypatch, wd)
    result, touched = run_watched(tmp_path, wd, monkeypatch, cli.main, ["check", str(wd / "parts" / "part_B.json")])
    assert result == 1  # 外の 3/4 を引き継がず、4/4 で検査する
    assert touched == []
    assert "4/4 なら 4 拍" in capsys.readouterr().out
    confine_to(monkeypatch, wd, False)
    assert cli.main(["check", str(wd / "parts" / "part_B.json")]) == 0  # 閉じ込めが無ければ今までどおり


def test_links_inside_the_folder_still_work_when_confined(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    (wd / "store").mkdir()
    os.rename(wd / "score.json", wd / "store" / "score.json")
    (wd / "score.json").symlink_to("store/score.json")
    os.rename(wd / "frames", wd / "store" / "frames")
    (wd / "frames").symlink_to(wd / "store" / "frames")
    confine_to(monkeypatch, wd)
    result, touched = run_watched(tmp_path, wd, monkeypatch, lambda: (
        cli.main(["check", str(wd / "parts" / "part_A.json")]), cli.main(["zoom", str(wd), "1"])))
    assert result == (0, 0)
    assert touched == []


# --- 一時フォルダを使わない（Codex の読み手は /tmp と $TMPDIR に書けない）


TEMPFILE_ENTRIES = ("mkstemp", "mkdtemp", "mktemp", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile",
                    "TemporaryDirectory", "gettempdir", "gettempdirb")  # fmt: skip


def test_check_and_zoom_use_no_temporary_folder(tmp_path, monkeypatch, capsys):
    import tempfile

    wd = reader_folder(tmp_path)
    systmp = tmp_path / "systmp"
    systmp.mkdir()
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(systmp))
    monkeypatch.setattr(tempfile, "tempdir", None)
    called = []

    def refuse(name):
        def fail(*args, **kwargs):
            called.append(name)
            raise AssertionError(f"tempfile.{name} を呼びました")

        return fail

    for name in TEMPFILE_ENTRIES:
        monkeypatch.setattr(tempfile, name, refuse(name))
    confine_to(monkeypatch, wd)
    result, touched = run_watched(tmp_path, wd, monkeypatch, lambda: (
        cli.main(["check", str(wd / "parts" / "part_A.json")]), cli.main(["check", str(wd)]),
        cli.main(["zoom", str(wd), "1"])))  # fmt: skip
    assert result == (0, 0, 0)
    assert called == [] and touched == []  # systmp も作業フォルダの外なので、触れれば touched に出る
    assert list(systmp.iterdir()) == []
    made = sorted((wd / "pages" / "zoom").iterdir())
    assert made and all(p.read_bytes().startswith(b"\x89PNG") for p in made)  # 出力は作業フォルダの中
    assert str(wd / "pages" / "zoom") in capsys.readouterr().out


# --- 基準の作業フォルダを渡す（閉じ込めが無いとき、本体の書き込みが使う）


def test_guard_with_root_works_without_confine(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    (tmp_path / "out").mkdir()
    (wd / "lnk").symlink_to(tmp_path / "out" / "x")
    (wd / "rel").symlink_to("parts")
    (wd / "via").symlink_to("lnk")
    confine_to(monkeypatch, wd, False)
    root = wd.resolve()
    assert confine.guard(wd / "rel" / "part_A.json", root=root) == root / "parts" / "part_A.json"
    for path, rel, link in ((wd / "lnk", "lnk", "lnk"), (wd / "lnk" / "y", "lnk/y", "lnk"), (wd / "via", "via", "lnk")):
        with pytest.raises(confine.Outside) as e:
            confine.guard(path, root=root)
        assert e.value.code == f"{rel} は作業フォルダの外を指しています"
        assert e.value.link == link  # 外を指していた中のリンク
    with pytest.raises(confine.Outside) as e:
        confine.guard(tmp_path / "out", root=root)
    assert e.value.code == f"作業フォルダ {root} の外は扱えません" and e.value.link is None
    assert not (tmp_path / "out" / "x").exists()


def test_outside_is_system_exit_with_the_same_message(world, tmp_path, monkeypatch):
    wd = world
    with pytest.raises(SystemExit) as e:
        confine.guard(wd / "lnk")
    assert type(e.value) is confine.Outside and e.value.code == "lnk は作業フォルダの外を指しています"
    with pytest.raises(confine.Outside) as e:
        confine.guard(wd / "lnk", given=True)
    assert e.value.code == f"指定のパスは作業フォルダ {wd} の外です" and e.value.link == "lnk"


def test_root_of_prefers_confine(tmp_path, monkeypatch):
    wd = reader_folder(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(wd)
    confine_to(monkeypatch, wd, False)
    assert confine.root_of(alias) == wd.resolve()
    monkeypatch.chdir(tmp_path)
    assert confine.root_of("alias") == wd.resolve()
    confine_to(monkeypatch, wd)
    assert confine.root_of(tmp_path / "elsewhere") == wd.resolve()
