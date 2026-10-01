"""書き込みの部品（inside）: リンクがどう置かれていても、作業フォルダの外を変えない。"""

import os
import shutil
import stat
import threading

import pytest
from trees import FOLDER_SHAPES, LAST_SHAPES, is_plain, make_outside, place_folder, place_last, snapshot

from videotab import confine, inside


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """作業フォルダ wd（resolve 済み）と外の木 out。閉じ込めは無し（本体の実行と同じ）。"""
    monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)
    wd = (tmp_path / "work" / "song").resolve()
    wd.mkdir(parents=True)
    out = make_outside(tmp_path)
    return wd, out, snapshot(out)


# --- フォルダを開く


def test_open_dir_creates_and_follows_links_inside(ws):
    wd, out, before = ws
    (wd / "store").mkdir()
    (wd / "readers").symlink_to("store")
    with inside.open_dir(wd, "a/b") as d:
        inside.write_text(d, "x.txt", "a")
    with inside.open_dir(wd, "readers") as d:
        assert d.rel == "store"
        inside.write_text(d, "n.md", "n")
    assert (wd / "a" / "b" / "x.txt").read_text() == "a"
    assert (wd / "store" / "n.md").read_text() == "n"
    assert snapshot(out) == before


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
@pytest.mark.parametrize("rel", ["readers", "history/20260101-000000"])
def test_open_dir_refuses_folder_linking_outside(ws, shape, rel):
    wd, out, before = ws
    place_folder(wd / rel.split("/")[0], out, shape)
    with pytest.raises(confine.Outside) as e:
        inside.open_dir(wd, rel)
    assert e.value.code == f"{rel} は作業フォルダの外を指しています"
    assert str(out) not in e.value.code
    assert snapshot(out) == before


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_open_dir_remakes_output_folder_linking_outside(ws, shape):
    wd, out, before = ws
    place_folder(wd / "pages", out, shape)
    said = []
    with inside.open_dir(wd, "pages", remake=True, notify=said.append) as d:
        inside.write_text(d, "index.md", "x")
    assert not (wd / "pages").is_symlink() and (wd / "pages" / "index.md").read_text() == "x"
    assert said == ["pages が作業フォルダの外を指すリンクだったので、リンクを消して作り直しました"]
    assert snapshot(out) == before


def test_open_dir_refuses_link_swapped_in_after_the_check(ws, monkeypatch):
    wd, out, before = ws
    (wd / "readers").mkdir()
    real_guard = confine.guard

    def swap_after_check(path, **kwargs):
        result = real_guard(path, **kwargs)
        os.rename(wd / "readers", wd / "readers.old")
        (wd / "readers").symlink_to(out / "d")  # 確かめたあとに差し替える
        return result

    monkeypatch.setattr(confine, "guard", swap_after_check)
    with pytest.raises(confine.Outside) as e:
        inside.open_dir(wd, "readers")
    assert "readers" in e.value.code and str(out) not in e.value.code
    assert snapshot(out) == before


def test_opened_folder_stays_inside_when_swapped_later(ws):
    wd, out, before = ws
    with inside.open_dir(wd, "readers") as d:
        os.rename(wd / "readers", wd / "readers.old")
        (wd / "readers").symlink_to(out / "d")  # 開いたあとに差し替える
        inside.write_text(d, "notes_A.md", "A")
    assert (wd / "readers.old" / "notes_A.md").read_text() == "A"
    assert snapshot(out) == before


def test_rel_path_accepts_other_spellings_of_the_work_folder(tmp_path, monkeypatch):
    wd = (tmp_path / "real").resolve()
    wd.mkdir()
    (tmp_path / "alias").symlink_to(wd)
    monkeypatch.chdir(tmp_path)
    assert str(inside.rel_path(wd, "alias/readers/x.json")) == "readers/x.json"
    assert str(inside.rel_path(wd, wd / "a" / ".." / "b")) == "b"
    with pytest.raises(confine.Outside):
        inside.rel_path(wd, tmp_path / "other" / "x")


# --- ファイルを書く


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_write_replaces_last_element_without_following(ws, shape):
    wd, out, before = ws
    place_last(wd / "job.json", out, shape)
    said = []
    with inside.open_dir(wd) as d:
        inside.write_text(d, "job.json", "{}", notify=said.append)
    assert is_plain(wd / "job.json") and (wd / "job.json").read_text() == "{}"
    kind = "ハードリンク" if shape == "hardlink" else "リンク"
    assert said == [f"job.json が{kind}だったので、通常のファイルに置き換えました"]
    assert snapshot(out) == before
    assert [p.name for p in wd.iterdir()] == ["job.json"]  # 一時ファイルは残らない


def test_write_replaces_fifo(ws):
    wd, out, before = ws
    os.mkfifo(wd / "job.log")
    with inside.open_dir(wd) as d:
        inside.write_text(d, "job.log", "x")
    assert is_plain(wd / "job.log")


def test_write_into_folder_name_fails_and_leaves_no_temporary(ws):
    wd, out, before = ws
    (wd / "job.json").mkdir()
    with inside.open_dir(wd) as d, pytest.raises(OSError):
        inside.write_text(d, "job.json", "{}")
    assert sorted(p.name for p in wd.iterdir()) == ["job.json"]
    assert snapshot(out) == before


def test_write_ignores_link_swapped_in_while_writing(ws):
    wd, out, before = ws

    def fill(f):
        f.write(b"new")
        place_last(wd / "html", out, "link")  # 書いている最中に差し替える

    with inside.open_dir(wd) as d:
        inside.write_with(d, "html", fill)
    assert is_plain(wd / "html") and (wd / "html").read_bytes() == b"new"
    assert snapshot(out) == before


def test_write_image_and_plain_rewrite_say_nothing(ws):
    from PIL import Image

    wd, out, before = ws
    said = []
    with inside.open_dir(wd, "strip") as d:
        inside.write_image(d, "check.png", Image.new("RGB", (4, 4)), notify=said.append)
        inside.write_text(d, "a.txt", "1", notify=said.append)
        inside.write_text(d, "a.txt", "2", notify=said.append)
    assert (wd / "strip" / "check.png").read_bytes().startswith(b"\x89PNG")
    assert (wd / "strip" / "a.txt").read_text() == "2" and said == []


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_create_text_does_not_go_through_links(ws, shape):
    wd, out, before = ws
    place_last(wd / "score.json", out, shape)
    with inside.open_dir(wd) as d, pytest.raises(FileExistsError):
        inside.create_text(d, "score.json", "{}")
    assert snapshot(out) == before


# --- 消す・移す


def test_remove_link_keeps_its_destination(ws):
    wd, out, before = ws
    place_folder(wd / "frames", out, "folder-link")
    place_last(wd / "video.mp4.part", out, "link")
    with inside.open_dir(wd) as d:
        assert inside.remove(d, "frames") and inside.remove(d, "video.mp4.part")
        assert not inside.remove(d, "missing")
    assert list(wd.iterdir()) == []
    assert snapshot(out) == before


def test_remove_folder_does_not_follow_links_inside_it(ws):
    wd, out, before = ws
    (wd / "frames" / "sub").mkdir(parents=True)
    (wd / "frames" / "a.png").write_bytes(b"a")
    (wd / "frames" / "sub" / "to_dir").symlink_to(out / "d")
    (wd / "frames" / "to_file").symlink_to(out / "x")
    os.link(out / "h", wd / "frames" / "sub" / "hard")
    with inside.open_dir(wd) as d:
        assert inside.remove(d, "frames")
    assert list(wd.iterdir()) == []
    assert snapshot(out) == before


def swap_to_outside(folder, out):
    """中のフォルダ folder を、外のフォルダへのリンクに差し替える（中身は隣へ退ける）。"""
    os.rename(folder, folder.parent / (folder.name + ".moved"))
    folder.symlink_to(out / "d")


def test_remove_does_not_follow_folders_swapped_during_recursion(ws, monkeypatch):
    wd, out, before = ws
    for sub in ("a", "b"):
        (wd / "frames" / sub).mkdir(parents=True)
        (wd / "frames" / sub / "f.png").write_bytes(b"f")
    real_scandir = os.scandir
    swapped = []

    def scandir(arg=None):
        it = real_scandir(arg) if arg is not None else real_scandir()
        if isinstance(arg, int) and not swapped:  # rmtree が frames の中を並べた直後に差し替える
            swapped.append(True)
            swap_to_outside(wd / "frames" / "a", out)
            swap_to_outside(wd / "frames" / "b", out)
        return it

    monkeypatch.setattr(os, "scandir", scandir)
    with inside.open_dir(wd) as d:
        try:
            inside.remove(d, "frames")
        except OSError:
            pass  # 差し替えに気づいて止まってもよい。外をたどらなければよい
    monkeypatch.undo()
    assert swapped
    assert snapshot(out) == before


def test_remove_does_not_follow_folder_swapped_just_before(ws, monkeypatch):
    wd, out, before = ws
    (wd / "frames").mkdir()
    (wd / "frames" / "f.png").write_bytes(b"f")
    real_lstat = os.lstat
    swapped = []

    def lstat(path, *args, **kwargs):
        if path == "frames" and kwargs.get("dir_fd") is not None and not swapped:
            swapped.append(True)
            swap_to_outside(wd / "frames", out)  # 確かめたあと、rmtree の直前に差し替える
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    with inside.open_dir(wd) as d:
        try:
            inside.remove(d, "frames")
        except OSError:
            pass
    monkeypatch.undo()
    assert swapped
    assert snapshot(out) == before


def test_remove_refuses_where_rmtree_would_follow_links(ws, monkeypatch):
    wd, out, before = ws
    (wd / "frames").mkdir()
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    with inside.open_dir(wd) as d, pytest.raises(SystemExit, match="frames を消せません"):
        inside.remove(d, "frames")
    assert (wd / "frames").is_dir()


def test_rmtree_avoids_symlink_attacks_here():
    assert shutil.rmtree.avoids_symlink_attacks  # macOS・Linux の Python 3.11 では真


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_move_replaces_destination_name_and_keeps_links_as_links(ws, shape):
    wd, out, before = ws
    (wd / "parts").mkdir()
    (wd / "parts" / "a.json").write_text("a")
    (wd / "old").symlink_to(out / "x")
    place_last(wd / "history" / "parts", out, shape)
    said = []
    with inside.open_dir(wd) as top, inside.open_dir(wd, "history") as dest:
        inside.move(top, "old", dest)
    with inside.open_dir(wd, "parts") as src, inside.open_dir(wd, "history") as dest:
        inside.move(src, "a.json", dest, "parts", notify=said.append)
    assert (wd / "history" / "old").is_symlink()  # リンクはリンクのまま移す
    assert is_plain(wd / "history" / "parts") and (wd / "history" / "parts").read_text() == "a"
    assert said and "history/parts" in said[0]
    assert snapshot(out) == before


# --- ログの追記


LOG_SHAPES = ["link", "dangling", "fifo", "hardlink", "folder"]


@pytest.mark.parametrize("shape", LOG_SHAPES)
def test_append_line_never_follows_and_never_raises(ws, shape, capsys):
    wd, out, before = ws
    log = wd / "job.log"
    if shape == "fifo":
        os.mkfifo(log)
    elif shape == "folder":
        log.mkdir()
    else:
        place_last(log, out, shape)
    inside.append_line(wd, "job.log", "12:00:00 一行目", stamp="12:00:00 ")
    inside.append_line(wd, "job.log", "12:00:01 二行目", stamp="12:00:01 ")
    assert snapshot(out) == before
    if shape == "folder":
        assert log.is_dir() and capsys.readouterr().err.splitlines() == ["12:00:00 一行目", "12:00:01 二行目"]
        return
    assert is_plain(log)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "12:00:00 job.log がリンクか通常のファイルではなかったので、新しいファイルに置き換えました",
        "12:00:00 一行目",
        "12:00:01 二行目",
    ]
    assert str(out) not in log.read_text(encoding="utf-8")


def test_append_line_to_plain_log_adds_only_the_line(ws):
    wd, _, _ = ws
    inside.append_line(wd, "job.log", "a")
    inside.append_line(wd, "job.log", "b")
    assert (wd / "job.log").read_text(encoding="utf-8") == "a\nb\n"
    assert stat.S_IMODE(os.stat(wd / "job.log").st_mode) == 0o666 & ~current_umask()


def test_append_line_without_folder_goes_to_stderr(tmp_path, capsys):
    inside.append_line(tmp_path / "missing", "job.log", "行")
    assert capsys.readouterr().err == "行\n"


def current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


# --- 置き場の一時フォルダ


def stages(place):
    return sorted(p.name for p in place.iterdir() if p.name.startswith(inside.STAGE_PREFIX))


def test_stage_is_private_and_removed_on_success_and_failure(tmp_path):
    place = tmp_path.resolve()
    with inside.stage(place) as st:
        assert st.path.parent == place and st.path.name.startswith(".videotab-tmp-")
        assert stat.S_IMODE(os.stat(st.path).st_mode) == 0o700
        (st.path / "raw_00001.png").write_bytes(b"x")
    assert stages(place) == []
    with pytest.raises(RuntimeError), inside.stage(place) as st:
        (st.path / "video.mp4.part").write_bytes(b"x")
        raise RuntimeError("途中で失敗")
    assert stages(place) == []
    assert (place / inside.STAGE_LOCK).is_file()


def test_sweep_removes_unused_stages_and_keeps_used_one(tmp_path):
    place = tmp_path.resolve()
    (place / ".videotab-tmp-nolock").mkdir()  # 作りかけで終わった
    (place / ".videotab-tmp-free").mkdir()
    (place / ".videotab-tmp-free" / ".lock").write_bytes(b"")  # 使っている処理が無い
    (place / ".videotab-tmp-oddlock").mkdir()
    (place / ".videotab-tmp-oddlock" / ".lock").symlink_to(place / "elsewhere")
    (place / "elsewhere").mkdir()
    (place / ".videotab-tmp-link").symlink_to(place / "elsewhere")  # リンクはたどらず残す
    (place / "abcdefghijk").mkdir()
    with inside.stage(place) as st:
        removed = inside.sweep_stages(place)
        assert st.path.is_dir()  # 使っている一時フォルダは残す
        assert sorted(removed) == [".videotab-tmp-free", ".videotab-tmp-nolock", ".videotab-tmp-oddlock"]
    assert stages(place) == [".videotab-tmp-link"]
    assert (place / "elsewhere").is_dir() and (place / "abcdefghijk").is_dir()


def test_sweep_waits_for_stage_being_made(tmp_path, monkeypatch):
    """作る側が中のロックを握るまで、掃除は共通のロックで待つ（作りかけを消さない）。"""
    place = tmp_path.resolve()
    real_mkdir = os.mkdir
    made, go = threading.Event(), threading.Event()

    def mkdir(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        if str(path).startswith(inside.STAGE_PREFIX):
            made.set()
            assert go.wait(10)

    monkeypatch.setattr(os, "mkdir", mkdir)
    kept = []

    def maker():
        with inside.stage(place) as st:
            kept.append(st.path.is_dir())

    t = threading.Thread(target=maker)
    t.start()
    assert made.wait(10)
    swept = []
    s = threading.Thread(target=lambda: swept.append(inside.sweep_stages(place)))
    s.start()
    s.join(0.3)
    assert s.is_alive()  # 作る側が共通のロックを共有で握っている間は待つ
    go.set()
    t.join(10)
    s.join(10)
    assert kept == [True] and swept == [[]]
    assert stages(place) == []
