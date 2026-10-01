"""videotab serve: 動画ファイルを送るとタブ譜まで作る画面を、手元のブラウザに出す。

127.0.0.1 でだけ待ち受ける。実行は 1 曲ずつ順番に行う（読み取りでエージェントを
何体も動かすので、同時に何曲も走らせない）。

POST /api/uploads は、本文に動画の生のバイト列を受け取る（ファイル名・題名などはクエリ）。
本文を読む前に、画面からの操作か・大きさ・入力の項目を確かめて断る。本文はメモリに読まず、
置き場の一時フォルダへ書き写してから取り込む（add.receive）。

/files/ は作業フォルダの中のファイル（タブ譜のページなど）を返す。作業フォルダには読み取りの
エージェントも書けるので、/files/ の応答には CSP の sandbox を付け、画面の iframe にも同じ値の
sandbox 属性を付ける。allow-same-origin を与えないので、ページは画面とは別の出どころ（opaque
origin）で動く。そこからの POST / DELETE は Origin が null になって _from_page で断られ、別の
出どころからの X-Videotab 付きのリクエストは事前確認（OPTIONS）に応じないので送られない。
どの応答にも Access-Control-Allow-* は付けない。
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import stat
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, unquote, urlparse

from videotab import add, agent_settings, inside, notes_md
from videotab.agent import DEFAULT_ENGINE, ENGINES, available_engines
from videotab.build import source_link, video_creator
from videotab.pipeline import STEP_NAMES, Busy, Job, finished_at
from videotab.workdir import ID_PATTERN, load_meta, read_json

SERVED_SUFFIXES = {".html", ".png", ".jpg", ".alphatex", ".md"}
MAX_JSON = 1024 * 1024  # 止める・やり直すの JSON の本文の上限（1 MB）
DELETING = ".deleting-"  # 消している途中の曲のフォルダ名の頭（ID_PATTERN に合わないので一覧に出ない）
# /files/ のページに与える許可。描画と再生（allow-scripts）、ダウンロード、印刷（allow-modals）、
# 元動画などへのリンク（allow-popups と、開いた先を sandbox にしない allow-popups-to-escape-sandbox）。
# allow-same-origin・allow-forms・allow-top-navigation 系は与えない。app.html の iframe の sandbox
# 属性も同じ値にする
FILES_SANDBOX = "allow-scripts allow-downloads allow-modals allow-popups allow-popups-to-escape-sandbox"
FILES_CSP = f"sandbox {FILES_SANDBOX}"


class App:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue[str] = queue.Queue()
        self.running: str | None = None
        self.running_job: Job | None = None  # 実行中の曲（止める合図を出す相手）
        self.waiting: list[str] = []
        self.dropped: dict[str, int] = {}  # 順番待ちから外した曲が、queue に残っている数（取り出したら捨てる）
        self.lock = threading.Lock()
        # 前回、消している途中で止まった曲の残り（一覧には出ないが、場所を取る）
        with os.scandir(self.root) as entries:
            leftovers = [e.path for e in entries if e.name.startswith(DELETING) and e.is_dir(follow_symlinks=False)]
        for path in leftovers:
            try:
                shutil.rmtree(path)
            except OSError as e:
                print(f"videotab: {Path(path).name} を消せませんでした: {e}", flush=True)
        # 前回、アップロードの受け取りや切り出しの途中で止まった一時フォルダ（使っている処理があるものは残す）
        try:
            inside.sweep_stages(self.root)
        except (OSError, SystemExit) as e:
            print(f"videotab: 一時フォルダを片付けられませんでした（{inside.reason(e)}）", flush=True)
        for d in self.root.iterdir():
            if (d / "job.json").exists() and not Job(d).is_locked():
                try:
                    Job(d).mark_interrupted()
                except (Exception, SystemExit) as e:  # noqa: BLE001 - 読めない job.json の曲は飛ばし、起動は止めない
                    print(f"videotab: {d.name} の中断を記録できませんでした（{inside.reason(e)}）", flush=True)
        threading.Thread(target=self._worker, daemon=True).start()

    # --- 実行の順番待ち

    def _worker(self) -> None:
        while True:
            job_id = self.queue.get()
            with self.lock:
                if self.dropped.get(job_id):
                    self.dropped[job_id] -= 1
                    continue
                self.waiting.remove(job_id)
                self.running = job_id
                job = self.running_job = Job(self.root / job_id)
            try:
                job.run()
            except Busy as e:
                job.log(f"始められません: {e}")
            except BaseException as e:  # noqa: BLE001 - 1 曲の失敗で順番待ち全体を止めない
                job.log(f"止まりました: {type(e).__name__}: {e}")
                try:
                    job.mark_interrupted()
                except BaseException as e2:  # noqa: BLE001 - 中断の記録に失敗しても、次の曲に進む
                    print(f"videotab: {job_id} の中断を記録できませんでした（{inside.reason(e2)}）", flush=True)
            finally:
                with self.lock:
                    self.running = None
                    self.running_job = None

    def _busy(self, job_id: str) -> bool:
        """この画面か、別のプロセス（videotab run）で実行中・順番待ちか。self.lock を握って呼ぶ。"""
        return job_id == self.running or job_id in self.waiting or Job(self.root / job_id).is_locked()

    def _entry(self, job_id: str) -> os.DirEntry | None:
        """root の直下で、名前が job_id と完全に同じ項目（大文字と小文字も区別する）。

        大文字と小文字を区別しないファイルシステムでは、root / "ABC" でも "abc" の曲を開けてしまい、
        ID の文字列で行う実行中・順番待ちの判定がずれるので、名前を並べて比べる。
        """
        with os.scandir(self.root) as entries:
            for e in entries:
                if e.name == job_id:
                    return e
        return None

    def _folder(self, job_id: str) -> None:
        """やり直し・詳細で受け付ける曲か。名前が完全に同じフォルダ（リンクのフォルダも含む）でなければ KeyError。"""
        e = self._entry(job_id)
        if e is None or not e.is_dir():
            raise KeyError(job_id)

    def _enqueue_locked(self, job_id: str) -> None:
        self.waiting.append(job_id)
        self.queue.put(job_id)

    @staticmethod
    def _check_engine(engine: str | None) -> None:
        if engine is None:
            return
        if engine not in ENGINES:
            raise ValueError("engine が違います")
        if not available_engines()[engine]:
            raise ValueError(f"{engine} コマンドが見つかりません（入れてログインしてから使ってください）")

    def check_upload(
        self,
        name: str,
        length: int,
        engine: str,
        model: str | None = None,
        effort: str | None = None,
        *,
        title: str | None = None,
        creator: str | None = None,
        source_url: str | None = None,
    ) -> agent_settings.Choice:
        """動画の中身を読む前に確かめられることを確かめ、読み取りの選択を返す。合わなければ ValueError
        （add.NotVideo を含む）。model / effort は None で普段の設定。"""
        self._check_engine(engine)
        choice = agent_settings.normalize_choice(engine, model, effort)
        add.check_request(name, length, title=title, creator=creator, source_url=source_url)
        return choice

    def receive(
        self,
        stream: BinaryIO,
        length: int,
        name: str,
        engine: str,
        model: str | None = None,
        effort: str | None = None,
        *,
        title: str | None = None,
        creator: str | None = None,
        source_url: str | None = None,
    ) -> str:
        """送られた動画（stream の length バイト）を新しい作業フォルダに取り込み、順番待ちに入れる。ID を返す。

        中身を読む前に検査する（合わなければ ValueError）。長い書き写しで一覧や他の操作を止めないよう、
        取り込みは self.lock を握らずに行い、job.json の作成と順番待ちへの登録だけを排他区間で行う。
        """
        fields = {"title": title, "creator": creator, "source_url": source_url}
        choice = self.check_upload(name, length, engine, model, effort, **fields)
        workdir = add.receive(stream, length, name, self.root, **fields)
        with self.lock:
            Job.create(workdir, engine, choice=choice)
            self._enqueue_locked(workdir.name)
        return workdir.name

    def retry(self, job_id: str, step: str | None, engine: str | None, fields: dict | None = None) -> None:
        """やり直す。fields はリクエストにあった model / effort だけの表。

        キーが無い項目は引き継ぐ（エンジンが変わるときは普段の設定）。None は普段の設定、
        文字列はその値（検査して、使えなければ ValueError）。
        """
        self._check_engine(engine)
        if step is not None and step not in STEP_NAMES:
            raise ValueError("step が違います")
        fields = fields or {}
        with self.lock:  # 状態の確認・書き換え・登録を 1 つの排他区間で行う
            self._folder(job_id)  # 順番待ちに入る ID を、実際のフォルダ名と同じ綴りに限る
            job = Job(self.root / job_id)
            data = job.load()
            if not data:
                raise KeyError(job_id)
            saved_engine = data.get("engine") or DEFAULT_ENGINE
            target = engine or saved_engine
            choice = None  # どちらのキーも無ければ、reset_from が引き継ぐか消すかを決める
            if fields:
                stored = data.get("choice") if target == saved_engine else None
                stored = stored if isinstance(stored, dict) else {}
                # 引き継ぐ項目は保存された値のまま（使えなければ読み取りの段で失敗として出す）
                model = stored.get("model")
                if "model" in fields:
                    model = agent_settings.normalize_model(target, fields["model"])
                effort = stored.get("effort")
                if "effort" in fields:
                    effort = agent_settings.normalize_effort(target, fields["effort"])
                choice = agent_settings.Choice(model, effort)
            if self._busy(job_id):
                raise Busy("実行中か順番待ちです")
            if step is None:
                step = next((s["name"] for s in data["steps"] if s["status"] != "done"), "add")
            job.reset_from(step, engine, choice)
            self._enqueue_locked(job_id)

    def cancel(self, job_id: str) -> None:
        """実行中の曲を止める（子プロセスを止め、止めたと記録する）か、順番待ちから外す。

        実行中でも順番待ちでもなければ ValueError。別のプロセス（videotab run）で実行中なら Busy。
        実行中の曲は、合図を出したところで返る（止まり終えるまでは実行中のまま）。
        """
        with self.lock:
            self._folder(job_id)
            if job_id in self.waiting:
                self.waiting.remove(job_id)
                self.dropped[job_id] = self.dropped.get(job_id, 0) + 1
                Job(self.root / job_id).mark_stopped()
                return
            if job_id == self.running and self.running_job is not None:
                self.running_job.cancel.cancel()
                return
        if Job(self.root / job_id).is_locked():
            raise Busy("別の videotab（videotab run など）で実行中なので、画面からは止められません")
        raise ValueError("実行中でも順番待ちでもありません")

    def _stopping(self, job_id: str) -> bool:
        """止める合図を出したが、まだ止まり終えていないか。"""
        with self.lock:
            return job_id == self.running and self.running_job is not None and self.running_job.cancel.is_set()

    def delete(self, job_id: str) -> None:
        """曲の作業フォルダを消す（元に戻せない）。

        無い・ID が違う・綴りが違う・リンク・曲ではないときは KeyError、実行中・順番待ち・
        job.lock を握れないときは Busy。self.lock と job.lock を握ったままフォルダの名前を
        .deleting-<ID>-<乱数> に変え（一覧・詳細から一度に消え、名前を変える前に job.lock を
        開いた実行は hold() の確かめで止まる）、両方を放してから中身を消す。
        """
        if not ID_PATTERN.fullmatch(job_id):
            raise KeyError(job_id)
        with self.lock:
            e = self._entry(job_id)
            if e is None or e.is_symlink() or not e.is_dir(follow_symlinks=False):
                raise KeyError(job_id)  # リンクはたどらない（リンクも先のフォルダも残す）
            d = self.root / job_id
            # 一覧に出る曲だけ。中身は読まない（JSON が壊れた曲も消せるように）
            if not ((d / "job.json").is_file() or (d / "meta.json").is_file()):
                raise KeyError(job_id)
            if self._busy_for_delete(job_id, e.stat(follow_symlinks=False)):
                raise Busy("実行中か順番待ちです")
            with Job(d).hold():
                gone = self.root / f"{DELETING}{job_id}-{secrets.token_hex(4)}"
                os.rename(d, gone)
        try:
            shutil.rmtree(gone)  # 中のリンクはたどらず、リンクだけを消す
        except OSError:
            print(f"videotab: {job_id} を消しきれませんでした（{gone.name} が残っています）", flush=True)
            raise
        print(f"videotab: {job_id} を消しました", flush=True)

    def _busy_for_delete(self, job_id: str, target: os.stat_result) -> bool:
        """消す曲が、この画面で実行中・順番待ちか。self.lock を握って呼ぶ。

        大文字と小文字だけが違う ID（同じフォルダを指しうる）と、リンク経由で順番待ちに入った
        同じフォルダも、消さない側に倒す。
        """
        ids = [*([self.running] if self.running else []), *self.waiting]
        if any(i.casefold() == job_id.casefold() for i in ids):
            return True
        for i in ids:
            try:
                st = os.stat(self.root / i)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                return True
        return False

    # --- 見せるための情報

    def list_jobs(self) -> list[dict]:
        out = []
        for d in self.root.iterdir():
            if not d.is_dir() or not ID_PATTERN.match(d.name):
                continue
            meta = load_meta(d)
            job = Job(d).load() if (d / "job.json").exists() else {}
            html = d / f"{d.name}.html"
            if not job and not meta:
                continue
            status = job.get("status") or ("done" if html.exists() else "idle")
            out.append(
                {
                    "id": d.name,
                    "title": job.get("title") or meta.get("title") or d.name,
                    "status": self._live_status(d.name, status),
                    "updated": job.get("updated") or _mtime(d),
                    "html": html.name if html.exists() else None,
                }
            )
        return sorted(out, key=lambda j: j["updated"] or "", reverse=True)

    def _live_status(self, job_id: str, status: str) -> str:
        with self.lock:
            if job_id == self.running:
                return "running"
            if job_id in self.waiting:
                return "queued"
        if Job(self.root / job_id).is_locked():
            return "running"  # videotab run など、別のプロセスで実行中
        return "failed" if status == "running" else status

    def detail(self, job_id: str) -> dict:
        self._folder(job_id)
        d = self.root / job_id
        job = Job(d)
        data = job.load()
        meta = load_meta(d)
        html = d / f"{job_id}.html"
        notes = {}
        notes_path = d / "readers" / "notes.json"
        if notes_path.exists():
            notes = read_json(notes_path)
        else:
            for p in sorted((d / "readers").glob("notes_*.md")) if (d / "readers").exists() else []:
                notes[p.stem.removeprefix("notes_")] = p.read_text(encoding="utf-8")
        engine = data.get("engine") or DEFAULT_ENGINE
        status = self._live_status(job_id, data.get("status") or ("done" if html.exists() else "idle"))
        return {
            "id": job_id,
            "title": data.get("title") or meta.get("title") or job_id,
            # meta.json は読み手も書けるので、出すときに検査し直す（https のリンクだけ）
            "source_url": source_link(meta),
            "creator": video_creator(meta),
            "engine": engine,
            "choice": agent_settings.shown_choice(engine, data.get("choice")),
            "status": status,
            "stopping": status == "running" and self._stopping(job_id),
            "steps": data.get("steps") or [],
            "created": data.get("created"),
            "updated": data.get("updated"),
            # 画面に出す状態が終わった（done / failed）ときだけ。実行中・順番待ちに変わった曲では出さない
            "finished": finished_at(data) if status in ("done", "failed") else None,
            "html": html.name if html.exists() else None,
            "log": job.log_tail(300),
            "notes": notes,
            # 報告を Markdown として解析した木。文字列でない値（手で書き換えた場合）の担当は入れず、
            # 画面は原文のまま出す
            "notes_blocks": {k: notes_md.parse(v) for k, v in notes.items() if isinstance(v, str)}
            if isinstance(notes, dict)
            else {},
        }

    def file_path(self, job_id: str, rel: str) -> Path | None:
        if not ID_PATTERN.match(job_id):
            return None
        base = (self.root / job_id).resolve()
        target = (base / rel).resolve()
        if base not in target.parents or target.suffix.lower() not in SERVED_SUFFIXES or not target.is_file():
            return None
        return target

    def read_file(self, job_id: str, rel: str) -> tuple[Path, bytes] | None:
        """/files/ で返すファイルの（パス, 中身）。返さないものは None。

        resolve した先が作業フォルダの外なら返さない（外へのリンクは、途中のフォルダも含めて外になる）。
        さらに、確かめた先をリンクをたどらずに開き、通常のファイルで、ほかの名前と中身を共有していない
        （外のファイルとのハードリンクでない）ことを確かめてから、開いたものを読む。
        """
        target = self.file_path(job_id, rel)
        if target is None:
            return None
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return None
            return target, f.read()


def _size_text(n: int) -> str:
    return f"{n // 1024**3} GB" if n >= 1024**3 else f"{n // 1024**2} MB"


def _mtime(d: Path) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(d.stat().st_mtime).isoformat(timespec="seconds")


def make_handler(app: App):
    page = resources.files("videotab").joinpath("templates", "app.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "videotab"

        def log_message(self, fmt, *args):  # 画面のポーリングで端末を埋めない
            pass

        def end_headers(self):
            # すべての応答（/favicon.ico の 204 や、標準ライブラリが返す 501 などのエラーも）で、
            # 中身の種類をブラウザに推測させない
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _local_host(self) -> bool:
            """Host が 127.0.0.1 / localhost のときだけ応じる（DNS rebinding で別のサイトの名前から
            この画面を操作されないように）。"""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost")

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlparse(origin).hostname in ("127.0.0.1", "localhost") and origin.endswith(
                f":{self.server.server_address[1]}"
            )

        def do_GET(self):
            if not self._local_host():
                return self._error(HTTPStatus.FORBIDDEN, "127.0.0.1 か localhost で開いてください")
            path = unquote(urlparse(self.path).path)
            if path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/favicon.ico":  # タブ譜のページを単独で開いたときにブラウザが求める
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return None
            if path == "/api/jobs":
                # 普段の値はリクエストのたびに読む（設定を変えると次の更新で表示が変わる）
                return self._json(
                    {
                        "jobs": app.list_jobs(),
                        "engines": available_engines(),
                        "default_engine": DEFAULT_ENGINE,
                        "agent_options": agent_settings.agent_options(),
                    }
                )
            if path.startswith("/api/jobs/"):
                job_id = path.removeprefix("/api/jobs/")
                if not ID_PATTERN.match(job_id):
                    return self._error(404, "ありません")
                try:
                    return self._json(app.detail(job_id))
                except KeyError:
                    return self._error(404, "ありません")
            if path.startswith("/files/"):
                parts = path.removeprefix("/files/").split("/", 1)
                found = app.read_file(parts[0], parts[1]) if len(parts) == 2 else None
                if found is None:
                    return self._error(404, "ありません")
                target, body = found
                ctype = mimetypes.guess_type(target.name)[0] or "text/plain"
                if ctype.startswith("text/"):
                    ctype += "; charset=utf-8"
                # 読み手が中身を決められるので、どの開き方でも画面とは別の出どころで動かす
                return self._send(200, body, ctype, {"Content-Security-Policy": FILES_CSP})
            return self._error(404, "ありません")

        def _from_page(self) -> bool:
            """ほかのサイトのページから勝手に操作されないよう、画面が付ける見出しと、同じ出どころを求める。"""
            return self._local_host() and self._same_origin() and self.headers.get("X-Videotab") == "1"

        def _length(self, limit: int, *, required: bool) -> int | tuple[int, str]:
            """Content-Length の値。使えなければ（応答の状態, 文言）。本文は読まない。"""
            raw = self.headers.get("Content-Length")
            if raw is None:
                if required:
                    return HTTPStatus.LENGTH_REQUIRED, "本文の大きさ（Content-Length）がありません"
                return 0
            if not re.fullmatch(r"[0-9]+", raw.strip()):
                return HTTPStatus.BAD_REQUEST, "本文の大きさ（Content-Length）が数字ではありません"
            length = int(raw)
            if length > limit:
                return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"大きすぎます（{_size_text(limit)} まで）"
            return length

        def do_POST(self):
            if not self._from_page():
                return self._error(HTTPStatus.FORBIDDEN, "画面からの操作ではありません")
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/uploads":
                return self._upload(parse_qs(parsed.query))
            if path.startswith("/api/jobs/") and path.endswith(("/cancel", "/retry")):
                return self._job_action(path)
            return self._error(404, "ありません")

        def _upload(self, query: dict[str, list[str]]):
            """動画を受け取って取り込み、順番待ちに入れる。断るときは、本文を読む前に断る。"""
            length = self._length(add.MAX_BYTES, required=True)
            if isinstance(length, tuple):
                return self._error(*length)

            def q(key: str) -> str | None:
                return (query.get(key) or [None])[0] or None

            try:
                job_id = app.receive(
                    self.rfile, length, q("name") or "", q("engine") or DEFAULT_ENGINE, q("model"), q("effort"),
                    title=q("title"), creator=q("creator"), source_url=q("source_url"),
                )  # fmt: skip
            except ValueError as e:  # 入力の項目の誤り（読む前）と、動画として読めない（add.NotVideo）
                return self._error(400, str(e))
            except (OSError, SystemExit) as e:  # ffprobe が無い・書けないなど
                return self._error(500, f"取り込めませんでした（{inside.reason(e)}）")
            return self._json({"id": job_id}, 201)

        def _job_action(self, path: str):
            length = self._length(MAX_JSON, required=False)
            if isinstance(length, tuple):
                return self._error(*length)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._error(400, "JSON が読めません")
            if not isinstance(body, dict):
                return self._error(400, "JSON の形が違います")
            action = "cancel" if path.endswith("/cancel") else "retry"
            job_id = path.removeprefix("/api/jobs/").removesuffix(f"/{action}")
            if not ID_PATTERN.match(job_id):
                return self._error(404, "ありません")
            try:
                if action == "cancel":
                    app.cancel(job_id)
                else:
                    fields = {k: body[k] for k in ("model", "effort") if k in body}
                    app.retry(job_id, body.get("step"), body.get("engine"), fields)
            except Busy as e:
                return self._error(409, str(e))
            except KeyError:
                return self._error(404, "ありません")
            except ValueError as e:
                return self._error(400, str(e))
            except (Exception, SystemExit) as e:  # noqa: BLE001 - job.json を書けないなどを画面に返す
                return self._error(500, f"{type(e).__name__}: {e}")
            return self._json({"id": job_id})

        def do_DELETE(self):
            if not self._from_page():
                return self._error(HTTPStatus.FORBIDDEN, "画面からの操作ではありません")
            # パスはデコードしない（%2e%2e などは ID の形に合わず 404）。本文は読まない
            path = urlparse(self.path).path
            job_id = path.removeprefix("/api/jobs/")
            if not path.startswith("/api/jobs/") or not ID_PATTERN.fullmatch(job_id):
                return self._error(404, "ありません")
            try:
                app.delete(job_id)
            except KeyError:
                return self._error(404, "ありません")
            except Busy as e:
                return self._error(409, f"消せません: {e}")
            except Exception as e:  # noqa: BLE001 - 消す途中の失敗を画面に返す
                return self._error(500, f"消す途中で失敗しました: {type(e).__name__}: {e}")
            return self._json({"deleted": job_id})

    return Handler


def serve(root: Path, port: int = 8765, open_browser: bool = True) -> None:
    app = App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"videotab: {url} （止めるときは Ctrl+C）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
