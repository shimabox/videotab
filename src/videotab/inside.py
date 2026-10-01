"""作業フォルダに書く・消す・移すとき、リンクがどう置かれていても作業フォルダの外を変えない部品。

読み取りのエージェント（特に Codex）は、作業フォルダの中にシンボリックリンクやハードリンクを
作れる。本体（videotab serve / run とそのコマンド）がそれをたどって書くと、利用者の権限で外の
ファイルを変えてしまう。そこで、本体が作業フォルダに書くときは次の決まりを守る。

1. 基準は resolve 済みの作業フォルダ（root）。作業フォルダ自身とその親は読み手が差し替えられない
2. 途中のフォルダは confine.guard で中のリンクを畳んで外でないことを確かめ、root から 1 要素ずつ
   リンクをたどらずに開き直した fd を足場にする（確かめたあとにリンクへ差し替えられたら断る）
3. 最後の要素はたどらない。書くときは同じフォルダに一時ファイルを作り、名前を付け替える
   （シンボリックリンク・ハードリンク・FIFO のどれが置かれていても、その名前だけが置き換わる）
4. フォルダを消すときは、固定した親の fd から dir_fd 付きの shutil.rmtree で消す（途中のリンクへの
   差し替えをたどらない）
5. 断りの文と知らせには、作業フォルダからの相対パスだけを出す（リンクの行き先は出さない）

外部のプログラム（ffmpeg）と動画の取り込み（add）は、作業フォルダの親（置き場）に作る本体専用の
一時フォルダ（stage）に書き出し、書き終えてから作業フォルダへ付け替えで取り込む。置き場には読み手が
書けない。
"""

from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import stat
import sys
from contextlib import contextmanager
from pathlib import Path, PurePath

from videotab import confine

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
STAGE_PREFIX = ".videotab-tmp-"  # 置き場の一時フォルダの名前の頭（ID_PATTERN に合わないので一覧に出ない）
STAGE_LOCK = ".videotab-tmp.lock"  # 置き場の一時フォルダの作成と掃除を排他にするロック
STAGE_INNER_LOCK = ".lock"  # 一時フォルダを使っている間、作った処理が握るロック


class Folder:
    """root から、リンクをたどらずに開いたフォルダ。fd を足場に書く・消す・移す。

    rel は作業フォルダからの相対パス（断りの文と知らせに使う）。
    """

    def __init__(self, fd: int, rel: str, path: Path | None = None):
        self.fd = fd
        self.rel = rel
        self.path = path  # 外部のプログラムに渡すパス（置き場の一時フォルダだけ）

    def name_of(self, name: str) -> str:
        return name if self.rel in ("", ".") else f"{self.rel}/{name}"

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "Folder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _say(notify, text: str) -> None:
    if notify is not None:
        notify(text)


def reason(e: BaseException) -> str:
    """例外の短い説明（OSError はパスを入れず、理由だけ）。"""
    if isinstance(e, OSError):
        return e.strerror or type(e).__name__
    if isinstance(e, SystemExit):
        return str(e.code)
    return f"{type(e).__name__}: {e}"


def rel_path(root: Path, path: str | os.PathLike) -> PurePath:
    """作業フォルダ（root）の中のパス path を、root からの相対パスにする。

    path は root から作ったパスでも、作業フォルダを別の書き方（相対パス・リンクの work/<ID>）で
    指したパスでもよい。.. は字句だけで畳む。作業フォルダを指す部分は、上から順に resolve して
    root と同じになる所を探す（作業フォルダ自身とその親だけを resolve し、中はたどらない）。
    """
    root = Path(root)
    p = Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))
    if p == root or root in p.parents:
        return PurePath(p.relative_to(root))
    for above in reversed(p.parents):
        if above.resolve() == root:
            return PurePath(p.relative_to(above))
    raise confine.Outside(f"作業フォルダ {root} の外は扱えません")


def _open_root(root: Path) -> Folder:
    return Folder(os.open(root, os.O_RDONLY | os.O_DIRECTORY), ".")


def _replaced(folder: Folder, rel: str) -> confine.Outside:
    return confine.Outside(f"{rel} は確かめたあとにリンクへ差し替えられたので、扱いません", rel)


def sub(folder: Folder, name: str, *, create: bool = True) -> Folder:
    """folder の中のフォルダ name を、リンクをたどらずに開く（無ければ作る）。

    name がリンクなら差し替えとして断る。作業フォルダの中を指すリンクをたどるのは open_dir。
    """
    rel = folder.name_of(name)
    for _ in range(2):
        try:
            return Folder(os.open(name, DIR_FLAGS, dir_fd=folder.fd), rel)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, dir_fd=folder.fd)
            except FileExistsError:
                pass
            create = False
        except OSError:
            # リンクを O_NOFOLLOW | O_DIRECTORY で開いたときの errno は OS で違うので、種類で見る
            if stat.S_ISLNK(os.stat(name, dir_fd=folder.fd, follow_symlinks=False).st_mode):
                raise _replaced(folder, rel) from None
            raise
    return Folder(os.open(name, DIR_FLAGS, dir_fd=folder.fd), rel)


def open_dir(root: Path, rel: str | PurePath = ".", *, remake: bool = False, create: bool = True, notify=None) -> Folder:
    """作業フォルダ root の中のフォルダ rel（root からの相対パス）を開く。無ければ作る。

    中を指すリンクはたどる。外を指すリンクがあれば断る（Outside）。remake は
    作り直せる生成物のフォルダ（frames・pages・strip）で、そのフォルダ自身が外へのリンクなら、
    リンクを消して新しいフォルダを作る（リンクの先には触れない）。
    """
    root = Path(root)
    rel = PurePath(rel)
    try:
        target = confine.guard(root / rel, root=root)
    except confine.Outside as e:
        if not (remake and len(rel.parts) == 1 and e.link is not None):
            raise
        with _open_root(root) as top:
            if not stat.S_ISLNK(os.stat(rel.name, dir_fd=top.fd, follow_symlinks=False).st_mode):
                raise
            os.unlink(rel.name, dir_fd=top.fd)
        _say(notify, f"{rel} が作業フォルダの外を指すリンクだったので、リンクを消して作り直しました")
        target = root / rel
    folder = _open_root(root)
    try:
        for name in target.relative_to(root).parts:
            nxt = sub(folder, name, create=create)
            folder.close()
            folder = nxt
    except BaseException:
        folder.close()
        raise
    folder.rel = str(target.relative_to(root)) if target != root else "."
    return folder


def fresh_dir(root: Path, name: str, *, notify=None) -> Folder:
    """作業フォルダの直下の生成物のフォルダ name を空にして作り直し、開く。

    name がリンクならリンクだけを消し（先には触れない）、フォルダなら中身ごと消す。
    """
    with _open_root(root) as top:
        try:
            was_link = stat.S_ISLNK(os.stat(name, dir_fd=top.fd, follow_symlinks=False).st_mode)
        except FileNotFoundError:
            was_link = False
        remove(top, name)
        if was_link:
            _say(notify, f"{name} がリンクだったので、リンクを消して作り直しました")
        os.mkdir(name, dir_fd=top.fd)
        folder = sub(top, name, create=False)
    folder.rel = name
    return folder


def has_entries(path: Path) -> bool:
    """path がリンクでない実フォルダで、中に何かあるか。リンクなら先を見ずに False を返す。

    生成物のフォルダを使い回すか・作り直すかの判定に使う（リンクなら作り直す）。
    """
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return False
        fd = os.open(path, DIR_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        # 見たあとにリンクへ差し替えられたとき。O_NOFOLLOW | O_DIRECTORY の errno は OS で違うので、種類で見る
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return False
        raise
    try:
        return bool(os.listdir(fd))
    finally:
        os.close(fd)


def _odd(folder: Folder, name: str) -> str | None:
    """name が通常のファイルでない・ほかの名前と中身を共有しているなら、その種類。"""
    try:
        st = os.stat(name, dir_fd=folder.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "リンク"
    if not stat.S_ISREG(st.st_mode):
        return None if stat.S_ISDIR(st.st_mode) else "通常のファイルではないもの"
    return "ハードリンク" if st.st_nlink > 1 else None


def _check_name(name: str) -> None:
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"ファイルの名前ではありません: {name}")


def write_with(folder: Folder, name: str, fill, *, notify=None) -> None:
    """folder の中の name を、fill(開いたファイル) で書いた中身に置き換える。

    同じフォルダに一時ファイル（. で始め .tmp で終える名前）を作って書き、名前を付け替える。
    name がリンク・ハードリンク・FIFO なら、その名前だけが通常のファイルに置き換わる。
    """
    _check_name(name)
    tmp = f".{name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=folder.fd)
    try:
        with os.fdopen(fd, "wb") as f:
            fill(f)
        was = _odd(folder, name)
        os.rename(tmp, name, src_dir_fd=folder.fd, dst_dir_fd=folder.fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=folder.fd)
        except OSError:
            pass
        raise
    if was:
        _say(notify, f"{folder.name_of(name)} が{was}だったので、通常のファイルに置き換えました")


def write_bytes(folder: Folder, name: str, data: bytes, *, notify=None) -> None:
    write_with(folder, name, lambda f: f.write(data), notify=notify)


def write_text(folder: Folder, name: str, text: str, *, notify=None) -> None:
    write_bytes(folder, name, text.encode("utf-8"), notify=notify)


def write_image(folder: Folder, name: str, image, *, notify=None) -> None:
    write_with(folder, name, lambda f: image.save(f, format="PNG"), notify=notify)


def create_text(folder: Folder, name: str, text: str) -> None:
    """name が無いときだけ新しく作って書く（リンクを含め、何かあれば FileExistsError）。"""
    _check_name(name)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=folder.fd)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))


def exists(folder: Folder, name: str) -> bool:
    """name があるか（リンクはたどらない。先の無いリンクもあるとみなす）。"""
    try:
        os.stat(name, dir_fd=folder.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def remove(folder: Folder, name: str, *, ignore_errors: bool = False) -> bool:
    """folder の中の name を消す。無ければ何もしない。消したら True。

    リンクはリンクだけを消す。フォルダは dir_fd 付きの shutil.rmtree で中身ごと消す（Python 3.11 の
    fd を使う実装で、途中のリンクをたどらない。その実装が使えない環境では消さずに断る）。
    """
    _check_name(name)
    try:
        st = os.stat(name, dir_fd=folder.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(st.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise SystemExit(f"{folder.name_of(name)} を消せません（この環境ではリンクをたどらずにフォルダを消せません）")
        shutil.rmtree(name, ignore_errors=ignore_errors, dir_fd=folder.fd)
        return True
    try:
        os.unlink(name, dir_fd=folder.fd)
    except FileNotFoundError:
        return False
    except OSError:
        if not ignore_errors:
            raise
        return False
    return True


def move(src: Folder, name: str, dst: Folder, dst_name: str | None = None, *, notify=None) -> None:
    """src の中の name を dst の中へ付け替える（リンクはリンクのまま移す。先はたどらない）。"""
    dst_name = dst_name or name
    _check_name(name)
    _check_name(dst_name)
    was = _odd(dst, dst_name)
    os.rename(name, dst_name, src_dir_fd=src.fd, dst_dir_fd=dst.fd)
    if was:
        _say(notify, f"{dst.name_of(dst_name)} が{was}だったので、通常のファイルに置き換えました")


# --- ログの追記

_APPEND = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def append_line(root: Path, name: str, line: str, *, stamp: str = "") -> None:
    """作業フォルダの直下のログ name に 1 行足す。例外は出さない（書けなければ標準エラーに出す）。

    name がリンク・FIFO・ほかの名前と中身を共有するファイルなら、その名前を消して新しいファイルを
    作り、置き換えた旨を先頭の行に書いてから足す。フォルダなら置き換えずに、行を標準エラーに出す。
    stamp は置き換えた旨の行の頭に付ける文字列（ログの時刻）。
    """
    data = (line + "\n").encode("utf-8")
    try:
        with _open_root(Path(root)) as top:
            try:
                fd = os.open(name, _APPEND, 0o666, dir_fd=top.fd)
            except OSError:
                fd = None  # リンク（ELOOP）・読み手のいない FIFO（ENXIO）・フォルダ（EISDIR）など
            if fd is not None:
                try:
                    st = os.fstat(fd)
                    if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                        _write_all(fd, data)
                        return
                finally:
                    os.close(fd)
            try:
                st = os.stat(name, dir_fd=top.fd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if st is not None and stat.S_ISDIR(st.st_mode):
                print(line, file=sys.stderr, flush=True)
                return
            if st is not None:
                os.unlink(name, dir_fd=top.fd)
            fd = os.open(name, _APPEND | os.O_EXCL, 0o666, dir_fd=top.fd)
            try:
                notice = f"{stamp}{name} がリンクか通常のファイルではなかったので、新しいファイルに置き換えました\n"
                _write_all(fd, (notice if st is not None else "").encode("utf-8") + data)
            finally:
                os.close(fd)
    except OSError:
        print(line, file=sys.stderr, flush=True)


# --- 置き場の一時フォルダ（外部のプログラムの書き出し先）


def _open_stage_lock(place: Folder) -> int:
    fd = os.open(STAGE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=place.fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SystemExit(f"{STAGE_LOCK} が通常のファイルではないので、一時フォルダを扱えません")
    return fd


def _make_stage(place: Folder) -> tuple[str, int, int]:
    """一時フォルダを作り、中のロックを握る。（名前, フォルダの fd, 中のロックの fd）。

    作ってから中のロックを握るまでは、共通のロックを共有で握る（掃除は共通のロックを排他で握って
    調べるので、作りかけのフォルダを消さない）。
    """
    common = _open_stage_lock(place)
    try:
        fcntl.flock(common, fcntl.LOCK_SH)
        try:
            for _ in range(100):
                name = STAGE_PREFIX + secrets.token_hex(6)
                try:
                    os.mkdir(name, 0o700, dir_fd=place.fd)
                    break
                except FileExistsError:
                    continue
            else:
                raise FileExistsError("一時フォルダの名前を決められません")
            try:
                folder_fd = os.open(name, DIR_FLAGS, dir_fd=place.fd)
                try:
                    lock = os.open(
                        STAGE_INNER_LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder_fd
                    )
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                    except BaseException:
                        os.close(lock)
                        raise
                except BaseException:
                    os.close(folder_fd)
                    raise
            except BaseException:
                remove(place, name, ignore_errors=True)
                raise
        finally:
            fcntl.flock(common, fcntl.LOCK_UN)
    finally:
        os.close(common)
    return name, folder_fd, lock


@contextmanager
def stage(place: Path):
    """置き場 place（作業フォルダの親、resolve 済み）に本体専用の一時フォルダを作る。

    Folder（path に一時フォルダのパス）を返す。外部のプログラムにはそこへ書き出させ、終わったら
    move で作業フォルダへ取り込む。抜けるときは成功でも失敗でも一時フォルダを消す。
    """
    place = Path(place)
    with _open_root(place) as top:
        name, folder_fd, lock = _make_stage(top)
        try:
            with Folder(folder_fd, name, place / name) as folder:
                yield folder
        finally:
            try:
                _remove_stage(top, name)
            except (OSError, SystemExit) as e:
                print(f"videotab: 一時フォルダ {name} を消せませんでした（{reason(e)}）", file=sys.stderr, flush=True)
            finally:
                os.close(lock)


def _remove_stage(place: Folder, name: str) -> None:
    """使い終えた一時フォルダを消す。消している途中（中のロックが消えたあと）を掃除に見せないよう、
    共通のロックを共有で握って消す。"""
    common = _open_stage_lock(place)
    try:
        fcntl.flock(common, fcntl.LOCK_SH)
        try:
            remove(place, name)
        finally:
            fcntl.flock(common, fcntl.LOCK_UN)
    finally:
        os.close(common)


def _stage_in_use(place: Folder, name: str) -> bool:
    """一時フォルダ name を、いま使っている処理があるか（中のロックを握られているか）。"""
    try:
        folder_fd = os.open(name, DIR_FLAGS, dir_fd=place.fd)
    except OSError:
        return False
    try:
        try:
            lock = os.open(STAGE_INNER_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder_fd)
        except OSError:
            return False  # ロックが無い・通常のファイルでない（作りかけで終わった）
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                return False
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(lock)
    finally:
        os.close(folder_fd)


def sweep_stages(place: Path) -> list[str]:
    """置き場に残った、使われていない一時フォルダを消す。消した名前を返す。

    共通のロックを排他で握って調べるので、作りかけの一時フォルダは見えない。使っている処理が
    中のロックを握っている一時フォルダは残す。一時フォルダが 1 つも無ければ、ロックも作らずに終える
    （このあとに作られるものは、使っている処理があるので消さなくてよい）。
    """
    removed = []
    with _open_root(Path(place)) as top:
        with os.scandir(top.fd) as entries:
            if not any(e.name.startswith(STAGE_PREFIX) for e in entries):
                return removed
        common = _open_stage_lock(top)
        try:
            fcntl.flock(common, fcntl.LOCK_EX)
            try:
                with os.scandir(top.fd) as entries:
                    names = sorted(
                        e.name for e in entries if e.name.startswith(STAGE_PREFIX) and e.is_dir(follow_symlinks=False)
                    )
                for name in names:
                    if _stage_in_use(top, name):
                        continue
                    try:
                        remove(top, name)
                        removed.append(name)
                    except (OSError, SystemExit) as e:
                        print(f"videotab: 一時フォルダ {name} を消せませんでした（{reason(e)}）", file=sys.stderr, flush=True)
            finally:
                fcntl.flock(common, fcntl.LOCK_UN)
        finally:
            os.close(common)
    return removed
