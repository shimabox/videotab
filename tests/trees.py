"""リンクの対策のテストで使う、外に置く木と、その前後の比べ方。"""

import os
import stat


def snapshot(top):
    """top の下の木（名前・種類・中身）。リンクはたどらずに行き先の文字列を記録する。"""
    out = {}
    for here, dirs, files in os.walk(top):
        for name in sorted(dirs + files):
            path = os.path.join(here, name)
            st = os.lstat(path)
            rel = os.path.relpath(path, top)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir",)
            elif stat.S_ISREG(st.st_mode):
                with open(path, "rb") as f:
                    out[rel] = ("file", f.read())
            else:
                out[rel] = ("other", stat.S_IFMT(st.st_mode))
    return out


def make_outside(tmp_path):
    """外の木: 中身のあるファイル x、フォルダ d（中にファイル）、ハードリンクの元 h。"""
    out = tmp_path / "outside"
    (out / "d").mkdir(parents=True)
    (out / "x").write_bytes(b"outside x")
    (out / "d" / "y").write_bytes(b"outside y")
    (out / "h").write_bytes(b"outside h")
    return out


def is_plain(path):
    """リンクでない通常のファイルで、ほかの名前と中身を共有していない。"""
    st = os.lstat(path)
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


# 最後の要素の形: 外のファイルへのリンク・外の無いファイルへのリンク・外のファイルとのハードリンク
LAST_SHAPES = ("link", "dangling", "hardlink")


def place_last(path, out, shape):
    """path を、外の木 out を指す形にする。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        path.unlink()
    if shape == "link":
        path.symlink_to(out / "x")
    elif shape == "dangling":
        path.symlink_to(out / "missing")
    elif shape == "hardlink":
        os.link(out / "h", path)
    else:
        raise ValueError(shape)


# 途中のフォルダの形: 外のフォルダへのリンク（先がある・無い）
FOLDER_SHAPES = ("folder-link", "folder-dangling")


def place_folder(path, out, shape):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        import shutil

        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()
    path.symlink_to(out / "d" if shape == "folder-link" else out / "nowhere")
