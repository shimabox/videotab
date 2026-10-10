"""取り込んだ動画からタブ譜までを通しで実行する。段ごとの状態は作業フォルダの job.json に残す。

画面（videotab serve）と videotab run の両方がここを使う。動画の取り込みは受け付けたとき
（add.add / add.receive）に済ませ、最初の段（add）は取り込んだ動画があるかを確かめるだけ。
途中で失敗しても、その段からやり直せる。切り出し・検出・組み立て・照合は videotab のコマンドを
子プロセスで実行し、出力を job.log に残す。読み取りは read.py がエージェントを起動して行う。
"""

from __future__ import annotations

import fcntl
import os
import signal
import stat
import subprocess
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path

from videotab import agent_settings, inside
from videotab.add import find_video
from videotab.agent import DEFAULT_ENGINE, ENGINES, videotab_bin
from videotab.cancel import Cancel, Cancelled
from videotab.strip import NO_TAB_EXIT
from videotab.workdir import load_meta, read_json, save_meta, write_json

STEPS = [
    ("add", "動画の取り込み"),
    ("frames", "画像の切り出し"),
    ("strip", "帯と線の検出"),
    ("pages", "ページ分け"),
    ("read", "読み取り"),
    ("build", "タブ譜の組み立て"),
    ("verify", "時刻の照合"),
]
STEP_NAMES = [name for name, _ in STEPS]
STEP_LABELS = dict(STEPS)
PAPER_LABELS = {"add": "楽譜の取り込み", "strip": "ページとパートの選択", "pages": "段の補正と拡大", "verify": "検査結果の確認"}
REBUILD_FROM = "build"  # 曲の情報を書き換えたとき、組み立て直す最初の段（ここから後はエージェントを使わない）

_file_lock = threading.Lock()
# job.lock はリンクをたどらずに開く（リンクの先を作ったり書き換えたりしない）。
# O_NONBLOCK は、job.lock が FIFO などに差し替えられていても開くところで止まらないため
_LOCK_OPEN = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def finished_at(data: dict) -> str | None:
    """終わった（done / failed）曲の終了時刻。段ごとの ended のうちいちばん遅いもの。

    保存された状態が done / failed でない・実行中の段が残っている・ended が 1 つも無いときは None。
    ended は now() が書いた同じ形の ISO 文字列なので、文字列のまま比べる。
    """
    if data.get("status") not in ("done", "failed"):
        return None
    steps = [s for s in data.get("steps") or [] if isinstance(s, dict)]
    if any(s.get("status") == "running" for s in steps):
        return None
    ends = [s["ended"] for s in steps if isinstance(s.get("ended"), str) and s["ended"]]
    return max(ends) if ends else None


def step_label(step: dict) -> str | None:
    """段の表示名。name が STEPS にあれば今の表示名、無ければ job.json に保存された label。

    job.json の label は曲を作ったときの表示名なので、あとで表示名を変えた段も今の名前で出すため。
    """
    name = step.get("name")
    if isinstance(name, str) and name in STEP_LABELS:
        return STEP_LABELS[name]
    return step.get("label")


def shown_steps(data: dict) -> list:
    """画面に出す段の一覧。各段の label を step_label にそろえる（job.json は書き換えない）。"""
    steps = data.get("steps") or []
    if not isinstance(steps, list):
        return steps
    return [{**s, "label": PAPER_LABELS.get(s.get("name"), step_label(s)) if data.get("source_mode") == "paper"
             else step_label(s)} if isinstance(s, dict) else s for s in steps]


def has_steps(data: dict) -> bool:
    """job.json の内容 data の段の名前の並びが、STEPS とちょうど同じ（欠け・余分・順序違いが無い）か。"""
    steps = data.get("steps") if isinstance(data, dict) else None
    if not (isinstance(steps, list) and all(isinstance(s, dict) for s in steps)):
        return False
    return [s.get("name") for s in steps] == STEP_NAMES


def rebuildable(data: dict) -> bool:
    """タブ譜の組み立ての段から組み立て直せる曲か。

    段の一覧が STEPS と同じ（has_steps）で、タブ譜の組み立てより前の段がすべて済んでいるときだけ。
    そうでない曲を順番待ちに入れると、読み取りが走るおそれがあるため。
    """
    if not has_steps(data):
        return False
    return all(s.get("status") == "done" for s in data["steps"][: STEP_NAMES.index(REBUILD_FROM)])


def retitle(data: dict, title: str) -> bool:
    """読んだ job.json の内容 data の題名を書き換え、組み立て直せる曲ならタブ譜の組み立て以降の段を
    未実行に戻して順番待ちの状態にする。戻したら True。保存は呼び出し側（Job.save）が行う。"""
    data["title"] = title
    if not rebuildable(data):
        return False
    _reset_steps(data, REBUILD_FROM)
    return True


def _reset_steps(data: dict, step: str) -> None:
    """step とそのあとの段を未実行に戻し、順番待ちの状態にする（前の実行の所要時間も消す）。"""
    for s in data["steps"][STEP_NAMES.index(step) :]:
        s.update(status="pending", started=None, ended=None, message=None, seconds=None)
    data["status"] = "queued"


class Job:
    """作業フォルダ 1 つぶんの通しの実行。"""

    def __init__(self, workdir: Path):
        self.workdir = workdir.resolve()  # 子プロセスの cwd を変えても同じ場所を指すよう絶対パスにする
        self.path = self.workdir / "job.json"
        self.log_path = self.workdir / "job.log"
        self.lock_path = self.workdir / "job.lock"
        self._held: int | None = None  # hold() の中なら、握っている job.lock の fd
        # 実行中の状態。実行の間は job.json を読み直さず、これを書き換えて保存する（読み手が job.json を
        # 壊れた JSON やリンクに差し替えても、段の結果と失敗を記録できるように）
        self._state: dict | None = None
        self._state_lock = threading.RLock()
        self.cancel = Cancel()  # 画面の「止める」が合図を出す

    # --- 状態

    def load(self) -> dict:
        with _file_lock:
            return read_json(self.path) if self.path.exists() else {}

    def save(self, data: dict) -> None:
        """job.json に書く。job.json がリンクなどなら、通常のファイルに置き換えて job.log に知らせる。"""
        data["updated"] = now()
        notes: list[str] = []
        with _file_lock:
            write_json(self.path, data, root=self.workdir, notify=notes.append)
        for note in notes:  # log も _file_lock を使うので、放してから
            self.log(note)

    def _current(self) -> dict:
        """実行中ならメモリの状態、そうでなければ job.json から読んだ状態。"""
        return self._state if self._state is not None else self.load()

    def _change(self, apply) -> None:
        with self._state_lock:
            data = self._current()
            apply(data)
            self.save(data)

    def update_step(self, name: str, **fields) -> None:
        def apply(data: dict) -> None:
            for step in data["steps"]:
                if step["name"] == name:
                    step.update(fields)

        self._change(apply)

    @classmethod
    def create(
        cls,
        workdir: Path,
        engine: str = DEFAULT_ENGINE,
        title: str | None = None,
        choice: agent_settings.Choice | None = None,
    ) -> "Job":
        """job.json を作り直す。choice は engine に対する選択（検査済みのもの）。"""
        if engine not in ENGINES:
            raise ValueError(f"engine は {ENGINES} のどれか")
        workdir.mkdir(parents=True, exist_ok=True)
        job = cls(workdir)
        old = job.load()
        data = {
            "id": workdir.name,
            "title": title or old.get("title") or load_meta(workdir).get("title"),
            "engine": engine,
            "status": "queued",
            "created": old.get("created") or now(),
            "steps": [{"name": n, "label": lb, "status": "pending"} for n, lb in STEPS],
        }
        _set_choice(data, choice)
        if load_meta(workdir).get("paper"):
            data["source_mode"] = "paper"
        job.save(data)
        return job

    def reset_from(
        self, step: str, engine: str | None = None, choice: agent_settings.Choice | None = None
    ) -> None:
        """step とそのあとの段を未実行に戻す（やり直し）。

        choice が None なら、エンジンが同じときは保存された選択を引き継ぎ、変わるときは消す
        （選べる値がエンジンごとに違うため）。choice があれば、それで置き換える（新しいエンジンで
        検査済みのもの）。
        """
        data = self.load()
        _reset_steps(data, step)
        if choice is not None:
            _set_choice(data, choice)
        elif engine and engine != data.get("engine", DEFAULT_ENGINE):
            data.pop("choice", None)
        if engine:
            data["engine"] = engine
        self.save(data)

    def mark_stopped(self) -> None:
        """順番待ちから外したとき、止めたとして記録する（段はそのまま）。"""
        with self._state_lock:
            data = self._current()
            if data.get("status") == "queued":
                data["status"] = "stopped"
                self.save(data)

    def mark_interrupted(self) -> None:
        """前回の実行が途中で止まったまま（画面を閉じた等）なら、失敗として記録し直す。

        実行が例外で抜けたあとなら、job.json を読み直さずにメモリの状態を使う。
        """
        with self._state_lock:
            data = self._current()
            if data.get("status") in ("running", "queued"):
                for s in data["steps"]:
                    if s["status"] == "running":
                        s.update(status="failed", message="中断されました", ended=now())
                data["status"] = "failed" if any(s["status"] == "failed" for s in data["steps"]) else "stopped"
                self.save(data)

    # --- ログ

    def log(self, message: str) -> None:
        """job.log に 1 行足す。例外は出さない（job.log がリンクなどなら置き換え、書けなければ標準エラー）。"""
        stamp = f"{datetime.now().strftime('%H:%M:%S')} "
        with _file_lock:
            inside.append_line(self.workdir, self.log_path.name, stamp + message, stamp=stamp)

    def log_tail(self, lines: int = 200) -> list[str]:
        if not self.log_path.exists():
            return []
        with _file_lock:
            return self.log_path.read_text(encoding="utf-8").splitlines()[-lines:]

    # --- 実行

    def _cli(self, *args: str) -> None:
        """videotab のコマンドを子プロセスで実行する。失敗したら最後の出力を添えて例外にする。"""
        cmd = [videotab_bin(), *args]
        proc = subprocess.Popen(
            cmd, cwd=self.workdir.parent.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        last = []
        assert proc.stdout is not None
        with self.cancel.watch(proc, signal.SIGINT):  # SIGINT なら、コマンドが一時フォルダなどを片付けて終わる
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log(line)
                    last = (last + [line])[-3:]
            code = proc.wait()
        self.cancel.check()
        if code != 0:
            raise StepError(" / ".join(last) or f"{args[0]} が失敗しました", code)

    def is_locked(self) -> bool:
        """別のプロセス（画面か videotab run）がこの作業フォルダを実行中か。

        job.lock がリンクか通常のファイルでなければ、開かずにロック中とみなす。
        """
        try:
            st = os.lstat(self.lock_path)
        except (FileNotFoundError, NotADirectoryError):
            return False
        if not stat.S_ISREG(st.st_mode):
            return True
        try:
            fd = os.open(self.lock_path, _LOCK_OPEN)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True  # 調べたあとにリンクへ差し替えられた（ELOOP）など
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return False

    @contextmanager
    def hold(self):
        """job.lock を握る（実行・videotab run の準備・削除が使う）。握れなければ Busy。

        1. 作業フォルダを作る（無ければ）
        2. job.lock をリンクをたどらずに開く。開けない・通常のファイルでなければ Busy
        3. flock で排他のロックを取る。取れなければ Busy
        4. 開いたファイルが、いまの「作業フォルダ/job.lock」と同じものかを確かめる。
           違えば（フォルダの名前が変わった・消された・差し替えられた）Busy
        5. 抜けるときに放して閉じる

        4 があるので、削除がフォルダの名前を変える前に job.lock を開いた処理は、あとでロックを
        取れても、消えるフォルダで実行しない。握っている間は run() がロックを取り直さずに実行する。
        """
        busy = f"{self.workdir.name} は別の videotab が実行中です"
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, _LOCK_OPEN | os.O_CREAT, 0o644)
        except OSError:
            raise Busy(busy) from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Busy(busy)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy(busy) from None
            try:
                if not self._is_current_lock(fd):
                    raise Busy(busy)
                self._held = fd
                try:
                    yield self
                finally:
                    self._held = None
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _is_current_lock(self, fd: int) -> bool:
        """開いた fd が、いまの「作業フォルダ/job.lock」（リンクではない）と同じファイルか。"""
        try:
            now_st = os.lstat(self.lock_path)
        except OSError:
            return False
        st = os.fstat(fd)
        return stat.S_ISREG(now_st.st_mode) and (now_st.st_dev, now_st.st_ino) == (st.st_dev, st.st_ino)

    def run(self) -> bool:
        """未実行の段を順に進める。全部終われば True。

        同じ作業フォルダを 2 か所から同時に実行しないよう、実行中は job.lock を握る
        （画面と videotab run の両方から守る）。hold() の中から呼ばれたら、握り直さずに実行する
        （videotab run は準備から実行の終わりまで握っている）。
        """
        if self._held is not None:
            return self._run_locked()
        with self.hold():
            return self._run_locked()

    def _run_locked(self) -> bool:
        data = self._start_state()
        if data is None:
            return False
        # 例外で抜けたときはメモリの状態を残す（画面の順番待ちが、それで中断を記録する）
        self._state = data
        ok = self._run_steps(data)
        self._state = None
        return ok

    def _start_state(self) -> dict | None:
        """実行を始めるときの状態。job.json が読めなければ、失敗として記録し直して None。

        読めないのは、壊れた JSON・段の一覧が無い・リンクで読めないなど。段がどこまで済んだかは
        分からないので、段をすべて未実行に戻した記録を作る（「やり直す」で最初の段から始められる）。
        """
        try:
            data = self.load()
            steps = data.get("steps") if isinstance(data, dict) else None
            if not (isinstance(steps, list) and all(isinstance(s, dict) and "name" in s and "status" in s for s in steps)):
                raise ValueError("段の一覧がありません")
            return data
        except (OSError, ValueError, SystemExit) as e:
            problem = inside.reason(e)
        self.log(f"失敗: job.json を読めません（{problem}）。段を未実行に戻した記録を作り直しました")
        try:
            meta = load_meta(self.workdir)
        except (OSError, ValueError, SystemExit):
            meta = {}
        data = {
            "id": self.workdir.name,
            "title": meta.get("title"),
            "engine": DEFAULT_ENGINE,
            "status": "failed",
            "created": now(),
            "steps": [{"name": n, "label": lb, "status": "pending"} for n, lb in STEPS],
        }
        self.save(data)
        return None

    def _run_steps(self, data: dict) -> bool:
        engine = data.get("engine", DEFAULT_ENGINE)
        data["status"] = "running"
        self.save(data)
        for step in data["steps"]:
            if step["status"] == "done":
                continue
            name = step["name"]
            self.update_step(name, status="running", started=now(), ended=None, message=None)
            self.log(f"== {step_label(step)}")
            t0 = time.monotonic()
            try:
                self.cancel.check()
                message = self._run_step(name, engine)
            except Cancelled:
                # 止めた段は未実行に戻す（やり直すと、その段の最初から始まる）
                self.log("止めました")
                self.update_step(name, status="pending", started=None, ended=None, message="止めました")
                self._change(lambda d: d.update(status="stopped"))
                return False
            except (Exception, SystemExit) as e:  # noqa: BLE001 - 段の失敗は画面に出して止める
                expected = isinstance(e, (StepError, RuntimeError, SystemExit))
                msg = str(e) if expected else f"{type(e).__name__}: {e}"
                if not expected:
                    self.log(traceback.format_exc())
                self.log(f"失敗: {msg}")
                self.update_step(name, status="failed", ended=now(), message=msg)
                self._change(lambda d: d.update(status="failed"))
                return False
            self.update_step(name, status="done", ended=now(), message=message, seconds=round(time.monotonic() - t0))
        html = self.workdir / f"{self.workdir.name}.html"
        meta = load_meta(self.workdir)

        def finish(d: dict) -> None:
            d["status"] = "done"
            d["html"] = html.name if html.exists() else None
            d["title"] = meta.get("title") or d.get("title")

        self._change(finish)
        self.log("== できあがり")
        return True

    def _run_step(self, name: str, engine: str) -> str | None:
        wd = str(self.workdir)
        if name == "add":
            return self._check_video()
        if name == "frames":
            frames = self.workdir / "frames"
            # frames_from は切り出し（画像の取り込み）を最後まで終えたときだけ meta.json に書かれる。
            # frames/ がリンクなら、先の中身を見ずに作り直す
            if load_meta(self.workdir).get("frames_from") and inside.has_entries(frames):
                return "切り出し済みの画像を使います"
            self._cli("frames", wd, "--force")
            return f"{len(list(frames.iterdir()))} 枚"
        if name == "strip":
            if load_meta(self.workdir).get("paper"):
                from videotab import paper

                self._change(lambda d: d.update(source_mode="paper"))
                settings = self._settings(engine)
                selected = paper.analyze(self.workdir, engine, self.log, settings=settings, cancel=self.cancel)
                return f"{len(selected['pages'])} ページから指定パートを選びました"
            strip = load_meta(self.workdir).get("strip") or {}
            band = ["--band", *map(str, strip["band"])] if strip.get("band_given") else []
            try:
                self._cli("strip", wd, *band)
            except StepError as e:
                if e.returncode != NO_TAB_EXIT:
                    raise  # ffmpeg がない等、タブ譜の有無と関係のない失敗では消さない
                self._discard_media()
                raise StepError("タブ譜が写っていないと判断し、取り込んだ動画と画像を消しました") from None
            return None
        if name == "pages":
            self._cli("pages", wd)
            n = len(read_json(self.workdir / "pages" / "pages.json")["pages"])
            return f"{n} ページ"
        if name == "read":
            from videotab.read import read_all

            # 曲ごとの選択は job.json から読み、検査し直す。使えなければエージェントを起動しない
            settings = self._settings(engine)
            shown = [agent_settings.summary(settings)]
            self.update_step(name, message=shown[0])
            lock = threading.Lock()

            def on_actual(text: str) -> None:
                # 読み手は並行で動くので、最初に分かった起動の本文だけを結果欄に出す
                with lock:
                    if len(shown) > 1:
                        return
                    shown.append(text)
                    self.update_step(name, message=text)

            read_all(self.workdir, engine, self.log, settings=settings, on_actual=on_actual, cancel=self.cancel)
            return shown[-1]
        if name == "build":
            self._cli("build", wd)
            return None
        if name == "verify":
            if self._current().get("source_mode") == "paper" or load_meta(self.workdir).get("paper"):
                return "小節と拍の検査済み。紙の楽譜は演奏時刻を持たないため、時刻照合は対象外です"
            marks_path = self.workdir / "marks.json"
            has_marks = marks_path.exists() and bool(read_json(marks_path))
            try:
                self._cli("verify", wd, "--tolerance", "3")
            except StepError:
                # 印とのずれは結果に残し、タブ譜の出力は止めない
                return "動画の時刻と合わない所があります（ログを見てください）"
            if not has_marks:
                return "照合していません（動画の時刻の印がありません）"
            return "動画の時刻と合っています"
        raise ValueError(name)

    def _settings(self, engine: str):
        """ページ選択と読み取りに、同じ曲のモデル・推論の指定を適用する。"""
        try:
            choice = agent_settings.choice_from_job(engine, self._current().get("choice"))
        except ValueError as e:
            raise StepError(
                f"読み取りのモデルと推論の強さの指定が使えません（{e}）。「やり直す」で選び直してください"
            ) from None
        settings = agent_settings.apply_choice(agent_settings.load_settings(engine), choice)
        for note in [*settings.notes, *agent_settings.removed_env_notes(settings)]:
            self.log(note)
        self.log(agent_settings.start_line(settings))
        return settings

    def _check_video(self) -> str:
        """add の段: 取り込んだ動画があるかを確かめる（取り込みは受け付けたときに済んでいる）。

        取り込み直しはしない。job.json や meta.json は読み手が書き換えられるので、そこから読んだ
        取り込む前の動画ファイルのパスを開くと、作業フォルダの外を読むことになるため。
        """
        video = find_video(self.workdir)
        if video is not None:
            meta = load_meta(self.workdir)
            w, h = meta.get("width"), meta.get("height")
            return f"{video.name}（{w}×{h}）" if isinstance(w, int) and isinstance(h, int) else video.name
        # frames_from は切り出し（画像の取り込み）を最後まで終えたときだけ meta.json に書かれる
        if load_meta(self.workdir).get("frames_from") and inside.has_entries(self.workdir / "frames"):
            return "取り込み済みの画像を使います"
        raise StepError("動画がありません。動画ファイルから新しく作り直してください")

    def _discard_media(self) -> None:
        """取り込んだ動画と、そこから作った画像を消す。job.json・job.log・meta.json は残す。

        取り込みと切り出しの段は未実行に戻す（やり直すと、取り込みの段が「動画がありません」で止まる）。
        """
        # 消すのは作業フォルダの中の名前だけ。リンクはリンクだけを消し、先には触れない
        with inside.open_dir(self.workdir) as top:
            for name in sorted(os.listdir(top.fd)):
                if fnmatchcase(name, "video.*"):  # 拡張子によらず、取り込んだ動画の名前をすべて消す
                    os.unlink(name, dir_fd=top.fd)
            for d in ("frames", "strip", "pages"):
                inside.remove(top, d, ignore_errors=True)
        meta = load_meta(self.workdir)
        meta.pop("frames_from", None)
        meta.pop("strip", None)
        save_meta(self.workdir, meta, notify=self.log)
        for name in ("add", "frames"):
            self.update_step(name, status="pending", started=None, ended=None, message=None)
        self.log("タブ譜が写っていないと判断し、動画と frames/・strip/・pages/ を消しました")


def _set_choice(data: dict, choice: agent_settings.Choice | None) -> None:
    """job.json の choice を置き換える。指定した項目だけを書き、どちらも普段の設定なら項目を消す。"""
    values = choice.to_json() if choice is not None else {}
    if values:
        data["choice"] = values
    else:
        data.pop("choice", None)


class StepError(RuntimeError):
    def __init__(self, message: str, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class Busy(RuntimeError):
    pass
