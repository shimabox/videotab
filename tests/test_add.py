"""動画の取り込み（add）: 受け付け前の検査、ID、作業フォルダと一時フォルダの後始末、ffprobe / ffmpeg の入力の制限。"""

import http.server
import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import PROBED
from PIL import Image

from videotab import add, frames, inside
from videotab.workdir import ID_PATTERN


def leftovers(root):
    """置き場に残ったもの（共通のロックのファイルは除く）。"""
    return sorted(p.name for p in root.iterdir() if p.name != inside.STAGE_LOCK) if root.exists() else []


def video(tmp_path, name="clip.mp4", data=b"video-bytes"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- 受け付け前の検査


@pytest.mark.parametrize("name", ["a.mp4", "a.M4V", "a.mov", "a.webm", "a.mkv", "a.AVI"])
def test_video_extensions_are_accepted(name):
    assert add.video_suffix(name) == Path(name).suffix.lower()


@pytest.mark.parametrize("name", ["a.txt", "a.flv", "a", "a.mp4.part", ".mp4", "a.m3u8"])
def test_other_extensions_are_refused(name):
    with pytest.raises(add.NotVideo, match="動画ファイル"):
        add.check_name(name)


def test_size_limits():
    add.check_size(1)
    add.check_size(add.MAX_BYTES)
    assert add.MAX_BYTES == 4 * 1024**3
    for bad in (0, -1, add.MAX_BYTES + 1):
        with pytest.raises(add.NotVideo):
            add.check_size(bad)


def test_source_url_checks():
    assert add.check_link(None) is None and add.check_link("  ") is None
    assert add.check_link(" https://example.com/v?x=1 ") == "https://example.com/v?x=1"
    assert add.check_link("https://example.com/" + "a" * (add.MAX_URL - 20)) is not None
    for bad in ("http://example.com/", "javascript:alert(1)", "https://exa mple.com/", "https://e.com/\tx",
                "ftp://example.com/", "https://example.com/" + "a" * add.MAX_URL):  # fmt: skip
        with pytest.raises(add.NotVideo, match="元動画のページ"):
            add.check_link(bad)


def test_title_and_creator_checks():
    assert add.check_text("題名", "  ") is None
    assert add.check_text("題名", " 曲 ") == "曲"
    assert add.check_text("題名", "あ" * add.MAX_TEXT) == "あ" * add.MAX_TEXT
    with pytest.raises(add.NotVideo, match="200 文字まで"):
        add.check_text("題名", "あ" * (add.MAX_TEXT + 1))
    for bad in ("曲\n名", "曲\x00", "作成\x1b者"):
        with pytest.raises(add.NotVideo, match="制御文字"):
            add.check_text("作成者", bad)


@pytest.mark.parametrize("fields", [
    {"name": "a.txt"},
    {"length": 0},
    {"length": add.MAX_BYTES + 1},
    {"source_url": "http://example.com/"},
    {"title": "x" * 201},
    {"creator": "a\nb"},
])  # fmt: skip
def test_bad_request_is_refused_before_reading(tmp_path, fields, fake_probe):
    args = {"name": "clip.mp4", "length": 5, **fields}
    stream = io.BytesIO(b"video")
    root = tmp_path / "work"
    with pytest.raises(add.NotVideo):
        add.receive(stream, args.pop("length"), args.pop("name"), root, **args)
    assert stream.tell() == 0 and fake_probe == []  # 中身を読まない
    assert leftovers(root) == []


# --- ID


@pytest.mark.parametrize("name, stem", [
    ("My Song (live).mp4", "My-Song-live"),
    ("練習.mp4", "video"),
    ("-x.mp4", "x"),
    ("__a__.mp4", "a"),
    ("--.mp4", "video"),
    ("曲-01_take.mov", "01_take"),
    ("a" * 60 + ".mp4", "a" * 40),
])  # fmt: skip
def test_new_id_starts_with_alnum_and_matches_pattern(name, stem):
    job_id = add.new_id(name)
    assert job_id.rsplit("-", 1)[0] == stem
    assert len(job_id.rsplit("-", 1)[1]) == 6 and int(job_id.rsplit("-", 1)[1], 16) >= 0
    assert job_id[0].isascii() and job_id[0].isalnum()
    assert ID_PATTERN.fullmatch(job_id)


def test_id_pattern_refuses_names_that_look_like_options_or_hidden():
    for bad in ("-x", "_x", ".videotab-tmp-0", ".deleting-a", "a/b", "", "a" * 65, "練習"):
        assert not ID_PATTERN.fullmatch(bad), bad


# --- 取り込み


def test_add_creates_workdir_with_video_and_meta(tmp_path, fake_probe):
    src = video(tmp_path, "My Song.mov", b"\x00\x01video")
    root = tmp_path / "new" / "work"  # 置き場がまだ無い
    wd = add.add(src, root, title=" 題名 ", creator="作成者", source_url="https://example.com/v")
    assert wd.parent == root.resolve() and ID_PATTERN.fullmatch(wd.name) and wd.name.startswith("My-Song-")
    assert (wd / "video.mov").read_bytes() == b"\x00\x01video"
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"id": wd.name, "title": "題名", "creator": "作成者", "source_url": "https://example.com/v",
                    "source_file": "My Song.mov", **PROBED}  # fmt: skip
    assert str(tmp_path) not in json.dumps(meta)  # 元のファイルのパスは残さない
    assert leftovers(root) == [wd.name]
    assert len(fake_probe) == 1 and fake_probe[0].name == add.STAGED


def test_add_defaults_title_to_file_name(tmp_path, fake_probe):
    wd = add.add(video(tmp_path, "練習.mp4"), tmp_path / "work")
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    assert meta["title"] == "練習" and meta["creator"] is None and meta["source_url"] is None
    assert wd.name.startswith("video-")


def test_same_file_twice_makes_two_folders(tmp_path, fake_probe):
    src = video(tmp_path)
    first = add.add(src, tmp_path / "work")
    second = add.add(src, tmp_path / "work")
    assert first != second and leftovers(tmp_path / "work") == sorted([first.name, second.name])


def test_id_collision_makes_another_id(tmp_path, fake_probe, monkeypatch):
    (tmp_path / "work" / "clip-aaaaaa").mkdir(parents=True)
    ids = iter(["clip-aaaaaa", "clip-bbbbbb"])
    monkeypatch.setattr(add, "new_id", lambda name: next(ids))
    assert add.add(video(tmp_path), tmp_path / "work").name == "clip-bbbbbb"
    assert not (tmp_path / "work" / "clip-aaaaaa" / "meta.json").exists()  # 既にあるフォルダには入れない


def test_receive_copies_exactly_length_bytes(tmp_path, fake_probe):
    stream = io.BytesIO(b"0123456789extra")
    wd = add.receive(stream, 10, "../../a/clip.webm", tmp_path / "work")
    assert (wd / "video.webm").read_bytes() == b"0123456789"
    assert json.loads((wd / "meta.json").read_text(encoding="utf-8"))["source_file"] == "clip.webm"


def test_short_body_leaves_nothing(tmp_path, fake_probe):
    root = tmp_path / "work"
    with pytest.raises(add.NotVideo, match="最後まで受け取れません"):
        add.receive(io.BytesIO(b"short"), 100, "clip.mp4", root)
    assert leftovers(root) == [] and fake_probe == []


def test_probe_failure_leaves_nothing(tmp_path, monkeypatch):
    def broken(path):
        raise add.NotVideo("映像の入った動画として読めません")

    monkeypatch.setattr(add, "probe", broken)
    with pytest.raises(add.NotVideo):
        add.add(video(tmp_path), tmp_path / "work")
    assert leftovers(tmp_path / "work") == []


@pytest.mark.parametrize("where", ["move", "save_meta"])
def test_failure_after_making_workdir_removes_it(tmp_path, fake_probe, monkeypatch, where):
    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(add.inside if where == "move" else add, where, broken)
    with pytest.raises(OSError):
        add.add(video(tmp_path), tmp_path / "work")
    assert leftovers(tmp_path / "work") == []


def test_add_refuses_missing_file_and_wrong_extension(tmp_path, fake_probe):
    with pytest.raises(add.NotVideo, match="がありません"):
        add.add(tmp_path / "none.mp4", tmp_path / "work")
    with pytest.raises(add.NotVideo, match="動画ファイル"):
        add.add(video(tmp_path, "notes.txt"), tmp_path / "work")
    assert fake_probe == []


# --- ffprobe / ffmpeg の入力の制限


def test_probe_command_limits_input_before_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(add, "ffprobe_bin", lambda: "ffprobe")
    cmd = add.probe_command(tmp_path / "video.part")
    assert cmd[-1] == str(tmp_path / "video.part")
    i, j = cmd.index("-protocol_whitelist"), cmd.index("-format_whitelist")
    assert cmd[i + 1] == "file"
    assert cmd[j + 1] == "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi"
    assert i < len(cmd) - 1 and j < len(cmd) - 1


def test_probe_command_asks_for_size_rotation_and_format(tmp_path, monkeypatch):
    monkeypatch.setattr(add, "ffprobe_bin", lambda: "ffprobe")
    cmd = add.probe_command(tmp_path / "video.part")
    entries = cmd[cmd.index("-show_entries") + 1].split(":")
    assert entries == ["stream=width,height", "stream_side_data=rotation", "stream_tags=rotate",
                       "format=duration,format_name"]  # fmt: skip


def test_extract_command_limits_input_before_dash_i(tmp_path):
    cmd = frames.extract_command("ffmpeg", tmp_path / "video.mp4", tmp_path / "raw_%05d.png", 1.0)
    k = cmd.index("-i")
    assert cmd[k + 1] == str(tmp_path / "video.mp4")
    i, j = cmd.index("-protocol_whitelist"), cmd.index("-format_whitelist")
    assert i < k and j < k
    assert cmd[i + 1] == "file" and cmd[j + 1] == ",".join(add.DEMUXERS)


def test_probe_checks_streams_and_format_name(tmp_path, monkeypatch):
    monkeypatch.setattr(add, "ffprobe_bin", lambda: "ffprobe")
    answers = {
        "ok": (0, {"streams": [{"width": 640, "height": 360}], "format": {"duration": "2.5", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}),
        "audio": (0, {"streams": [], "format": {"duration": "2.5", "format_name": "mp3"}}),
        "other": (0, {"streams": [{"width": 1, "height": 1}], "format": {"format_name": "concat"}}),
        "error": (1, {}),
    }

    def run(cmd, **kwargs):
        code, data = answers[Path(cmd[-1]).name]
        return subprocess.CompletedProcess(cmd, code, json.dumps(data), "")

    monkeypatch.setattr(add.subprocess, "run", run)
    assert add.probe(tmp_path / "ok") == {"width": 640, "height": 360, "duration": 2.5}
    for name in ("audio", "other", "error"):
        with pytest.raises(add.NotVideo):
            add.probe(tmp_path / name)


# --- 回転の情報（縦撮りの動画）


def fake_ffprobe(monkeypatch, stream, fmt=None, code=0):
    """ffprobe の答えを偽物にする。映像は 1280x720 で、stream の項目を足す。"""
    data = {
        "streams": [{"width": 1280, "height": 720, **stream}],
        "format": {"duration": "2.5", "format_name": "mov,mp4,m4a,3gp,3g2,mj2", **(fmt or {})},
    }
    monkeypatch.setattr(add, "ffprobe_bin", lambda: "ffprobe")
    monkeypatch.setattr(
        add.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, code, json.dumps(data), "")
    )


@pytest.mark.parametrize("stream", [
    {"side_data_list": [{"rotation": -90}]},
    {"side_data_list": [{"rotation": 90}]},
    {"side_data_list": [{"rotation": 270}]},
    {"side_data_list": [{"side_data_type": "Other"}, {"rotation": -90}]},
    {"tags": {"rotate": "90"}},
    {"tags": {"rotate": "270"}},
])  # fmt: skip
def test_probe_swaps_size_for_quarter_turns(tmp_path, monkeypatch, stream):
    fake_ffprobe(monkeypatch, stream)
    assert add.probe(tmp_path / "v") == {"width": 720, "height": 1280, "duration": 2.5}


@pytest.mark.parametrize("stream", [
    {},
    {"side_data_list": [{"rotation": 180}]},
    {"side_data_list": [{"rotation": 0}]},
    {"side_data_list": []},
    {"side_data_list": None, "tags": None},
    {"tags": {"rotate": "180"}},
    {"tags": {"rotate": "0"}},
    {"tags": {}},
])  # fmt: skip
def test_probe_keeps_size_for_other_rotations(tmp_path, monkeypatch, stream):
    fake_ffprobe(monkeypatch, stream)
    assert add.probe(tmp_path / "v") == {"width": 1280, "height": 720, "duration": 2.5}


@pytest.mark.parametrize("stream", [
    {"tags": {"rotate": "abc"}},
    {"tags": {"rotate": "inf"}},
    {"tags": {"rotate": "nan"}},
    {"tags": {"rotate": None}},
    {"side_data_list": [{"rotation": "abc"}]},
    {"side_data_list": [{"rotation": "nan"}]},
    {"side_data_list": [{"rotation": "-inf"}]},
    {"side_data_list": [{"rotation": None}]},
])  # fmt: skip
def test_probe_ignores_unreadable_rotation(tmp_path, monkeypatch, stream):
    # 読めない回転の情報では入れ替えず、取り込みも断らない
    fake_ffprobe(monkeypatch, stream)
    assert add.probe(tmp_path / "v") == {"width": 1280, "height": 720, "duration": 2.5}


def test_probe_with_rotation_still_checks_duration_and_format_name(tmp_path, monkeypatch):
    turned = {"side_data_list": [{"rotation": -90}]}
    fake_ffprobe(monkeypatch, turned, {"duration": "N/A"})
    assert add.probe(tmp_path / "v") == {"width": 720, "height": 1280, "duration": None}
    fake_ffprobe(monkeypatch, turned, {"format_name": "concat"})
    with pytest.raises(add.NotVideo):
        add.probe(tmp_path / "v")
    fake_ffprobe(monkeypatch, turned, code=1)
    with pytest.raises(add.NotVideo):
        add.probe(tmp_path / "v")


# --- 実物の ffprobe / ffmpeg（無ければ飛ばす）

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg / ffprobe がありません"
)


def make_real_video(path):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=5:duration=2", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip


@needs_ffmpeg
def test_real_video_is_added_and_cut(tmp_path):
    src = tmp_path / "test.mp4"
    make_real_video(src)
    wd = add.add(src, tmp_path / "work")
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    assert (meta["width"], meta["height"]) == (320, 240) and abs(meta["duration"] - 2.0) < 0.5
    assert frames.extract(wd) >= 2


def make_rotated_video(path, degrees=90):
    """回転の情報が付いた動画。回転なしで作った 320x240 の動画を、映像はそのままに包み直す。
    包み直せない ffmpeg（-display_rotation に対応しない古い版など）では飛ばす。"""
    plain = path.with_name("plain" + path.suffix)
    make_real_video(plain)
    wrapped = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-display_rotation", str(degrees),
         "-i", str(plain), "-c", "copy", str(path)],
        capture_output=True,
    )  # fmt: skip
    if wrapped.returncode != 0:
        pytest.skip("この ffmpeg では回転の情報が付いた動画を作れません")


@needs_ffmpeg
def test_real_rotated_video_is_recorded_in_the_displayed_orientation(tmp_path):
    src = tmp_path / "rotated.mp4"
    make_rotated_video(src)
    wd = add.add(src, tmp_path / "work")
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    assert (meta["width"], meta["height"]) == (240, 320)
    assert frames.extract(wd) >= 2
    with Image.open(sorted((wd / "frames").glob("*.png"))[0]) as first:
        assert first.size == (240, 320) == (meta["width"], meta["height"])  # 切り出した画像と同じ向き


class FifoWatch:
    """FIFO を開いた読み手がいるかを見張る（書き手として非ブロックで開けたら、読み手が開いている）。"""

    def __init__(self, path):
        self.path = path
        self.opened = False
        self.stop = False
        os.mkfifo(path)
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self):
        while not self.stop:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                time.sleep(0.01)
                continue
            self.opened = True
            os.close(fd)  # 読み手に終わりを渡す（開いたまま止まらないように）
            time.sleep(0.05)

    def close(self):
        time.sleep(0.1)
        self.stop = True
        self.thread.join(2)


@needs_ffmpeg
def test_concat_playlist_is_refused_without_opening_the_other_file(tmp_path):
    # (a) 取り込み: 一時フォルダの写しを ffprobe が確かめる。参照先は置き場の一時フォルダの外の FIFO
    watch = FifoWatch(tmp_path / "target.fifo")
    try:
        src = video(tmp_path, "list.mp4", f"ffconcat version 1.0\nfile '{tmp_path / 'target.fifo'}'\n".encode())
        with pytest.raises(add.NotVideo):
            add.add(src, tmp_path / "work")
        assert leftovers(tmp_path / "work") == []

        # 切り出し: 作業フォルダの動画を読み手が差し替えた場合。同じフォルダの FIFO を相対の名前で参照する
        wd = tmp_path / "work" / "clip-000000"
        wd.mkdir()
        (wd / "video.mp4").write_bytes(b"ffconcat version 1.0\nfile 'near.fifo'\n")
        near = FifoWatch(wd / "near.fifo")
        try:
            with pytest.raises(subprocess.CalledProcessError):
                frames.extract(wd, force=True)
            with pytest.raises(add.NotVideo):
                add.probe(wd / "video.mp4")
        finally:
            near.close()
        assert not near.opened
    finally:
        watch.close()
    assert not watch.opened


class Counter(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):
        Counter.hits.append(self.path)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


@needs_ffmpeg
def test_playlist_pointing_to_http_server_is_refused_without_reaching_it(tmp_path):
    # (b) 中身が手元の HTTP サーバーを指す再生リスト（concat から m3u8 を経て http へ、直接 http へ、m3u8 そのもの）
    Counter.hits = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Counter)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    m3u8 = f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{url}/seg.ts\n#EXT-X-ENDLIST\n"
    try:
        wd = tmp_path / "work" / "clip-000000"
        wd.mkdir(parents=True)
        (wd / "list.m3u8").write_text(m3u8)
        cases = {
            "via-m3u8.mp4": "ffconcat version 1.0\nfile 'list.m3u8'\n",
            "direct.mp4": f"ffconcat version 1.0\nfile '{url}/direct.mp4'\n",
            "hls.mp4": m3u8,
        }
        for name, text in cases.items():
            src = video(tmp_path, name, text.encode())
            with pytest.raises(add.NotVideo):
                add.add(src, tmp_path / "work")
            (wd / "video.mp4").write_text(text)
            with pytest.raises(subprocess.CalledProcessError):
                frames.extract(wd, force=True)
            with pytest.raises(add.NotVideo):
                add.probe(wd / "video.mp4")
    finally:
        httpd.shutdown()
    assert Counter.hits == []
    assert leftovers(tmp_path / "work") == ["clip-000000"]
