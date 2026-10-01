"""作業フォルダの外を指すパス（.. やリンク）を、外を調べずに見分ける。

読み取りのエージェントから実行された videotab check / zoom は、作業フォルダの中に閉じ込める。
読み手は環境変数 VIDEOTAB_CONFINE に作業フォルダを入れて起動される（agent.py）。これが無いとき
（人や本体が実行したとき）、guard に基準の作業フォルダ root を渡さなければ、ここの確認は何もしない。
本体が書く・消す・移すときは、閉じ込めの有無によらず root_of の作業フォルダを基準に guard を使う
（inside.py）。

パスの確かめ方（guard）: パス全体を resolve してから比べると、外へのリンクをたどって外を
調べてしまい、外の状態（ある・無い・権限など）で結果が変わる。そこで、.. を字句だけで畳んで
作業フォルダの中かを見てから、作業フォルダから 1 要素ずつ lstat でたどる。リンクは readlink で
行き先を読み、行き先が外なら調べずに断る。調べるのは、いつも中と分かっているパスだけである。
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path

CONFINE_ENV = "VIDEOTAB_CONFINE"
MAX_LINKS = 40  # たどるリンクの数の上限（循環を断る）


class Outside(SystemExit):
    """作業フォルダの外を指すパスの断り。

    文言と code は SystemExit と同じ。link は外を指していた中のリンクの、作業フォルダからの
    相対パス（字句だけで外と分かったときは None）。
    """

    def __init__(self, message: str, link: str | None = None):
        super().__init__(message)
        self.link = link


def base() -> Path | None:
    """閉じ込めの作業フォルダ（resolve 済み）。閉じ込めが無ければ None。"""
    root = os.environ.get(CONFINE_ENV)
    return Path(root).resolve() if root else None


def root_of(workdir: str | os.PathLike) -> Path:
    """書き込みの基準にする作業フォルダ（resolve 済み）。閉じ込めがあればその作業フォルダ。"""
    root = base()
    return root if root is not None else Path(workdir).resolve()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _lexical(path: str | os.PathLike) -> Path:
    """絶対パスにし、.. を字句だけで畳む（ファイルシステムに触れない）。"""
    return Path(os.path.normpath(os.path.join(os.getcwd(), os.fspath(path))))


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)) if path != root else "."


def outside_message(root: Path) -> str:
    return f"指定のパスは作業フォルダ {root} の外です"


def guard(path: str | os.PathLike, *, given: bool = False, root: str | os.PathLike | None = None) -> Path:
    """path が作業フォルダの中かを、中だけをたどって確かめ、たどり終えたパスを返す。

    外なら固定の文で断る（Outside）。given はコマンドの引数として渡されたパスで、断りの文に
    パスを入れない。そうでなければ、作業フォルダからの相対パスで「外を指しています」と断る。
    途中に存在しない要素があれば、そこまでで中と判定する（作る・読むのは呼び出し側）。
    root（resolve 済みの作業フォルダ）を渡せばそれを基準にする。渡さなければ閉じ込めの作業
    フォルダを基準にし、閉じ込めも無ければ path をそのまま返す。
    """
    root = Path(root) if root is not None else base()
    if root is None:
        return Path(path)
    target = _lexical(path)
    if not _inside(target, root):
        raise Outside(outside_message(root) if given else f"作業フォルダ {root} の外は扱えません")
    asked = target

    def refuse(link: Path):
        raise Outside(
            outside_message(root) if given else f"{_rel(asked, root)} は作業フォルダの外を指しています",
            _rel(link, root),
        )

    links = 0
    while True:
        rest = target.relative_to(root).parts
        here = root
        for i, name in enumerate(rest):
            step = here / name
            try:
                mode = os.lstat(step).st_mode
            except (FileNotFoundError, NotADirectoryError):
                return target
            except OSError as e:
                raise SystemExit(f"{_rel(step, root)} を調べられません（{e.strerror or type(e).__name__}）") from None
            if not stat.S_ISLNK(mode):
                here = step
                continue
            links += 1
            if links > MAX_LINKS:
                raise SystemExit(f"{_rel(asked, root)} はリンクが多すぎてたどれません")
            try:
                dest = os.readlink(step)
            except OSError as e:
                raise SystemExit(f"{_rel(step, root)} を調べられません（{e.strerror or type(e).__name__}）") from None
            dest_path = Path(os.path.normpath(os.path.join(here, dest)))
            if not _inside(dest_path, root):
                refuse(step)
            target = dest_path.joinpath(*rest[i + 1 :])
            break
        else:
            return target


def describe_os_error(e: OSError) -> str:
    """OSError の短い文（作業フォルダからの相対パスと理由だけ。外のパスは入れない）。"""
    root = base()
    reason = e.strerror or type(e).__name__
    if root is not None and isinstance(e.filename, (str, bytes, os.PathLike)):
        where = _lexical(os.fsdecode(e.filename))
        if _inside(where, root):
            return f"{_rel(where, root)} を扱えません（{reason}）"
    return f"ファイルを扱えません（{reason}）"


@contextmanager
def short_errors():
    """閉じ込めがあるとき、OSError をトレースバックにせず短い文で終える。"""
    if base() is None:
        yield
        return
    try:
        yield
    except OSError as e:
        raise SystemExit(describe_os_error(e)) from None
