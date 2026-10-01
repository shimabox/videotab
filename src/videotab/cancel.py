"""実行中の曲を止める合図。

画面の「止める」で cancel() が呼ばれると、そのとき動いている子プロセス（videotab のコマンドと
読み取りのエージェント）に止まるよう合図を送り、以後に起動しようとした子プロセスもすぐ止める。
合図は子プロセスの子孫（エージェントが起動したツールなど）にも送る。子孫が出力のパイプを握ったまま
残ると、読む側がいつまでも終わらないため。合図のあとしばらく待っても終わらないものは強制的に終わらせる。

止められた処理は Cancelled を出して抜ける。段を進める側（pipeline）は、それを失敗ではなく
「止めた」として記録する。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from contextlib import contextmanager

GRACE = 10  # 合図を送ってから、強制的に終わらせるまでの秒数


class Cancelled(Exception):
    def __init__(self) -> None:
        super().__init__("止めました")


class Cancel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._set = False
        self._procs: dict[subprocess.Popen, int] = {}

    def is_set(self) -> bool:
        with self._lock:
            return self._set

    def check(self) -> None:
        """止める合図が出ていれば Cancelled。"""
        if self.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        """止める合図を出し、動いている子プロセスを止める。何度呼んでもよい。"""
        with self._lock:
            self._set = True
            procs = list(self._procs.items())
        for proc, sig in procs:
            _stop(proc, sig)

    @contextmanager
    def watch(self, proc: subprocess.Popen, sig: int = signal.SIGTERM):
        """proc が動いている間、止める合図で proc に sig を送る。合図がもう出ていれば、すぐ送る。

        videotab のコマンドには SIGINT を送る（Python の後始末が走り、一時フォルダや ffmpeg も片付く）。
        """
        with self._lock:
            self._procs[proc] = sig
            already = self._set
        if already:
            _stop(proc, sig)
        try:
            yield
        finally:
            with self._lock:
                self._procs.pop(proc, None)


def _stop(proc: subprocess.Popen, sig: int) -> None:
    if proc.poll() is not None:
        return
    family = _descendants(proc.pid)  # 親が先に終わると子孫をたどれなくなるので、合図の前に調べる
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass
    _signal_all(family, sig)

    def force() -> None:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        _signal_all(family, signal.SIGKILL)

    timer = threading.Timer(GRACE, force)
    timer.daemon = True
    timer.start()


def _signal_all(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _descendants(pid: int) -> list[int]:
    """pid の子孫のプロセス（ps で調べる）。調べられなければ空。"""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[0].isdigit() and cols[1].isdigit():
            children.setdefault(int(cols[1]), []).append(int(cols[0]))
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child)
    return found
