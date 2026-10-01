"""本体の書き込みのリンク対策: 作業フォルダの中にリンクを置かれても、本体は外を変えない。

閉じ込め（VIDEOTAB_CONFINE）の無い、videotab serve / run とそのコマンドの動きを確かめる。
外に置いた木（名前・種類・中身）が、実行の前後で同じであることを主に見る。
"""

import json
import os
import subprocess

import pytest
from trees import FOLDER_SHAPES, LAST_SHAPES, is_plain, make_outside, place_folder, place_last, snapshot

from fakes import FakeAgent, make_work

from videotab import add, build, cli, confine, frames, inside, pipeline, read, verify, workdir


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """作業フォルダ wd（work/<ID>）と外の木 out。閉じ込めは無し。"""
    monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)
    wd = tmp_path / "work" / "abcdefghijk"
    wd.mkdir(parents=True)
    out = make_outside(tmp_path)
    return wd, out, snapshot(out)


def no_outside_paths(text, out):
    assert str(out) not in text and "outside" not in text


# --- meta.json（本体の状態・記録）


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_save_meta_replaces_links_and_says_so(ws, shape, capsys):
    wd, out, before = ws
    place_last(wd / "meta.json", out, shape)
    workdir.save_meta(wd, {"id": "abcdefghijk"})
    assert is_plain(wd / "meta.json") and json.loads((wd / "meta.json").read_text()) == {"id": "abcdefghijk"}
    assert snapshot(out) == before
    said = capsys.readouterr().out
    assert said.startswith("meta.json が") and "通常のファイルに置き換えました" in said
    no_outside_paths(said, out)


def test_save_meta_through_another_spelling_of_the_folder(ws, monkeypatch):
    wd, out, before = ws
    (wd.parent / "alias").symlink_to(wd.name)  # work/<ID> をリンクで指す使い方
    monkeypatch.chdir(wd.parent.parent)
    workdir.save_meta(workdir.Path("work/alias"), {"id": "x"})
    assert json.loads((wd / "meta.json").read_text()) == {"id": "x"}


def test_write_json_refuses_folder_linking_outside(ws):
    wd, out, before = ws
    for shape in FOLDER_SHAPES:
        place_folder(wd / "readers", out, shape)
        with pytest.raises(SystemExit) as e:
            workdir.write_json(wd / "readers" / "notes.json", {}, root=wd.resolve())
        assert e.value.code == "readers は作業フォルダの外を指しています"
    assert snapshot(out) == before


# --- job.json・job.log


def new_job(wd):
    return pipeline.Job.create(wd)


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_job_save_replaces_links_and_logs_it(ws, shape):
    wd, out, before = ws
    job = new_job(wd)
    data = job.load()
    place_last(job.path, out, shape)
    data["steps"][0]["status"] = "done"
    job.save(data)
    assert is_plain(job.path)
    assert job.load()["steps"][0]["status"] == "done"
    assert snapshot(out) == before
    log = job.log_tail()
    assert any(line.endswith("job.json が" + ("ハードリンク" if shape == "hardlink" else "リンク")
                             + "だったので、通常のファイルに置き換えました") for line in log)  # fmt: skip
    no_outside_paths("\n".join(log), out)


@pytest.mark.parametrize("shape", [*LAST_SHAPES, "fifo", "folder"])
def test_job_log_never_writes_outside_nor_raises(ws, shape, capsys):
    wd, out, before = ws
    job = new_job(wd)
    if shape == "fifo":
        os.mkfifo(job.log_path)
    elif shape == "folder":
        job.log_path.mkdir()
    else:
        place_last(job.log_path, out, shape)
    job.log("一行目")
    job.log("二行目")
    assert snapshot(out) == before
    if shape == "folder":
        assert capsys.readouterr().err.count("行目") == 2
    else:
        assert is_plain(job.log_path)
        assert [line[9:] for line in job.log_tail()][-2:] == ["一行目", "二行目"]


@pytest.mark.parametrize("shape", ["link", "hardlink"])
def test_run_starts_from_job_json_behind_link_and_replaces_it(ws, shape, monkeypatch):
    wd, out, before = ws
    job = new_job(wd)
    (out / "job.json").write_bytes(job.path.read_bytes())  # 外に、読める job.json を置く
    before = snapshot(out)
    job.path.unlink()
    if shape == "link":
        job.path.symlink_to(out / "job.json")
    else:
        os.link(out / "job.json", job.path)
    monkeypatch.setattr(pipeline.Job, "_run_step", lambda self, name, engine: None)
    assert job.run()
    assert is_plain(job.path) and job.load()["status"] == "done"
    assert snapshot(out) == before


@pytest.mark.parametrize("shape", ["broken", "dangling", "no-steps"])
def test_run_records_failure_when_job_json_is_unreadable(ws, shape, monkeypatch):
    wd, out, before = ws
    job = new_job(wd)
    (wd / "meta.json").write_text(json.dumps({"source_url": "https://example.com/abcdefghijk", "title": "曲"}))
    job.path.unlink()
    if shape == "broken":
        job.path.write_text("{broken")
    elif shape == "dangling":
        job.path.symlink_to(out / "missing.json")
    else:
        job.path.write_text("{}")
    ran = []
    monkeypatch.setattr(pipeline.Job, "_run_step", lambda self, *args: ran.append(args))
    assert job.run() is False  # 例外で抜けない
    assert ran == []
    data = job.load()
    assert is_plain(job.path) and data["status"] == "failed"
    assert [s["status"] for s in data["steps"]] == ["pending"] * len(pipeline.STEPS)
    assert "url" not in data and data["title"] == "曲"
    assert any("job.json を読めません" in line for line in job.log_tail())
    assert snapshot(out) == before
    job.reset_from("add")  # 「やり直す」で最初の段から始められる
    assert job.run() and len(ran) == len(pipeline.STEPS)


BROKEN_SWAPS = ["broken-link", "dangling", "hardlink"]


def swap_job_json(job, out, how):
    """読み手が job.json を差し替えた状態を作る。"""
    (out / "broken.json").write_text("{broken")
    job.path.unlink()
    if how == "broken-link":
        job.path.symlink_to(out / "broken.json")
    elif how == "dangling":
        job.path.symlink_to(out / "missing.json")
    else:
        os.link(out / "h", job.path)


@pytest.mark.parametrize("how", BROKEN_SWAPS)
def test_step_failure_is_recorded_after_job_json_is_swapped(ws, how, monkeypatch):
    wd, out, _ = ws
    job = new_job(wd)
    job.update_step("add", status="done")

    def step(self, name, engine):
        if name == "strip":
            swap_job_json(self, out, how)
            raise pipeline.StepError("帯が見つかりません")
        return f"{name} 済み"

    (out / "broken.json").write_text("{broken")
    before = snapshot(out)
    monkeypatch.setattr(pipeline.Job, "_run_step", step)
    assert job.run() is False
    data = job.load()
    assert is_plain(job.path) and data["status"] == "failed"
    status = {s["name"]: (s["status"], s.get("message")) for s in data["steps"]}
    assert status["frames"] == ("done", "frames 済み")
    assert status["strip"] == ("failed", "帯が見つかりません")
    assert snapshot(out) == before


def test_interruption_is_recorded_from_memory_after_unexpected_exit(ws, monkeypatch):
    wd, out, _ = ws
    job = new_job(wd)
    (out / "broken.json").write_text("{broken")
    before = snapshot(out)

    def step(self, name, engine):
        swap_job_json(self, out, "broken-link")
        raise KeyboardInterrupt  # 段の失敗として扱わない例外

    monkeypatch.setattr(pipeline.Job, "_run_step", step)
    with pytest.raises(KeyboardInterrupt):
        job.run()
    job.mark_interrupted()  # job.json を読み直さない
    data = job.load()
    assert is_plain(job.path) and data["status"] == "failed"
    assert data["steps"][0]["status"] == "failed" and data["steps"][0]["message"] == "中断されました"
    assert snapshot(out) == before


# --- 取り込んだ動画と画像を消す（タブ譜が写っていないとき）


def media_work(wd, out, folder_shape=None):
    (wd / "meta.json").write_text(json.dumps({"id": wd.name, "frames_from": "video.mp4", "strip": {}}))
    (wd / "video.mp4").write_bytes(b"v")
    place_last(wd / "video.webm", out, "link")
    place_last(wd / "video.mov", out, "hardlink")
    for d in ("frames", "strip", "pages"):
        (wd / d / "sub").mkdir(parents=True)
        (wd / d / "a.png").write_bytes(b"a")
        (wd / d / "sub" / "to_out").symlink_to(out / "d")
        (wd / d / "to_x").symlink_to(out / "x")
    if folder_shape:
        place_folder(wd / "pages", out, folder_shape)


@pytest.mark.parametrize("folder_shape", [None, *FOLDER_SHAPES])
def test_discard_media_removes_only_names_inside(ws, folder_shape):
    wd, out, before = ws
    media_work(wd, out, folder_shape)
    job = new_job(wd)
    job._discard_media()
    assert sorted(p.name for p in wd.iterdir()) == ["job.json", "job.log", "meta.json"]
    assert snapshot(out) == before
    assert json.loads((wd / "meta.json").read_text()) == {"id": wd.name}


def test_discard_media_does_not_follow_folders_swapped_during_removal(ws, monkeypatch):
    wd, out, before = ws
    media_work(wd, out)
    job = new_job(wd)
    real_scandir = os.scandir
    swapped = []

    def scandir(arg=None):
        it = real_scandir(arg) if arg is not None else real_scandir()
        if isinstance(arg, int) and not swapped:
            swapped.append(True)
            for d in ("frames", "strip", "pages"):  # 消している途中に、中のフォルダを外へのリンクにする
                os.rename(wd / d / "sub", wd / d / "sub.moved")
                (wd / d / "sub").symlink_to(out / "d")
        return it

    monkeypatch.setattr(os, "scandir", scandir)
    job._discard_media()
    monkeypatch.undo()
    assert swapped
    assert snapshot(out) == before


# --- score.json の雛形（利用者・読み手のデータ）


def test_score_template_is_made_only_when_missing(ws):
    wd, out, before = ws
    score = build.load_score(wd)
    assert is_plain(wd / "score.json") and score["time_signature"] == [4, 4]
    (wd / "store").mkdir()
    (wd / "score.json").unlink()
    (wd / "score.json").symlink_to("store/score.json")  # 中を指す先の無いリンクは、今までどおり先に作る
    build.load_score(wd)
    assert (wd / "store" / "score.json").is_file() and (wd / "score.json").is_symlink()
    assert snapshot(out) == before


def test_score_template_is_not_made_through_link_to_missing_outside_file(ws):
    wd, out, before = ws
    place_last(wd / "score.json", out, "dangling")
    with pytest.raises(confine.Outside) as e:
        build.load_score(wd)
    assert e.value.code == "score.json は作業フォルダの外を指しています"
    assert snapshot(out) == before


@pytest.mark.parametrize("shape", ["link", "hardlink"])
def test_score_behind_link_to_existing_file_is_only_read(ws, shape):
    wd, out, before = ws
    (out / "score.json").write_text(json.dumps({"title": "外", "tempo": 90}))
    before = snapshot(out)
    if shape == "link":
        (wd / "score.json").symlink_to(out / "score.json")
    else:
        os.link(out / "score.json", wd / "score.json")
    assert build.load_score(wd)["tempo"] == 90  # 読む側は対象外（今までどおり読む）
    assert snapshot(out) == before


# --- 前回の読み取り結果の退避（history）


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_stash_refuses_history_linking_outside(ws, shape):
    wd, out, before = ws
    (wd / "parts").mkdir()
    (wd / "parts" / "part_A.json").write_text("{}")
    place_folder(wd / "history", out, shape)
    with pytest.raises(confine.Outside) as e:
        read.stash_previous(wd, print)
    assert e.value.code.startswith("history") and e.value.code.endswith("は作業フォルダの外を指しています")
    assert (wd / "parts" / "part_A.json").is_file()  # 移していない
    assert snapshot(out) == before


def test_stash_moves_links_as_links_including_dangling_ones(ws):
    wd, out, before = ws
    place_folder(wd / "parts", out, "folder-link")
    place_last(wd / "resolve.json", out, "dangling")
    place_last(wd / "marks.json", out, "hardlink")
    logs = []
    read.stash_previous(wd, logs.append)
    (dest,) = list((wd / "history").iterdir())
    assert logs == [f"前回の読み取り結果を history/{dest.name} に移しました"]
    assert (dest / "parts").is_symlink() and (dest / "resolve.json").is_symlink()
    assert not os.path.lexists(wd / "parts") and not os.path.lexists(wd / "resolve.json")
    assert snapshot(out) == before


def test_stash_into_history_that_links_inside(ws):
    wd, out, before = ws
    (wd / "store").mkdir()
    (wd / "history").symlink_to("store")
    (wd / "resolve.json").write_text("{}")
    read.stash_previous(wd, print)
    assert len(list((wd / "store").iterdir())) == 1


# --- 読み取りの段が書くもの（readers/notes*・marks.json）


class SwappingAgent(FakeAgent):
    """読み手の起動中に、作業フォルダの中を差し替える偽のエージェント。"""

    def __init__(self, swap):
        super().__init__()
        self.swap = swap

    def __call__(self, prompt, *, workdir, label, **kwargs):
        result = super().__call__(prompt, workdir=workdir, label=label, **kwargs)
        self.swap(workdir.resolve(), label)
        return result


def run_read_all(wd, monkeypatch, swap):
    monkeypatch.setattr(read, "run_agent", SwappingAgent(swap))
    logs = []
    return logs, lambda: read.read_all(wd, "claude", logs.append)


def pages_work(tmp_path):
    wd = make_work(tmp_path, n_pages=4)
    from videotab import cli

    assert cli.main(["strip", str(wd)]) == 0 and cli.main(["pages", str(wd)]) == 0
    return wd


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_reader_outputs_that_are_links_are_replaced(tmp_path, monkeypatch, shape):
    monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)
    wd = pages_work(tmp_path)
    out = make_outside(tmp_path)
    before = snapshot(out)

    def swap(wd, label):
        if label == "A":  # 読み手が、本体の書く名前にリンクを置く
            for name in ("readers/notes_A.md", "readers/notes_B.md", "readers/notes.json", "marks.json"):
                place_last(wd / name, out, shape)

    logs, run = run_read_all(wd, monkeypatch, swap)
    run()
    for name in ("readers/notes_A.md", "readers/notes.json", "marks.json"):
        assert is_plain(wd / name), name
    assert snapshot(out) == before
    assert any("readers/notes.json が" in line for line in logs)
    no_outside_paths("\n".join(logs), out)


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_readers_swapped_to_link_outside_is_refused(tmp_path, monkeypatch, shape):
    monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)
    wd = pages_work(tmp_path)
    out = make_outside(tmp_path)
    before = snapshot(out)

    def swap(wd, label):
        if label == "A":
            os.rename(wd / "readers", wd / "readers.moved")
            place_folder(wd / "readers", out, shape)

    _, run = run_read_all(wd, monkeypatch, swap)
    with pytest.raises(SystemExit) as e:
        run()
    assert e.value.code == "readers は作業フォルダの外を指しています"
    assert snapshot(out) == before


def test_readers_swapped_after_the_check_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)
    wd = pages_work(tmp_path)
    out = make_outside(tmp_path)
    before = snapshot(out)
    real_guard = confine.guard

    def guard(path, **kwargs):
        result = real_guard(path, **kwargs)
        if kwargs.get("root") is not None and str(path).endswith("/readers") and (wd / "parts" / "part_A.json").exists():
            os.rename(wd / "readers", wd / f"readers.{len(os.listdir(wd))}")  # 確かめたあとに差し替える
            place_folder(wd / "readers", out, "folder-link")
        return result

    _, run = run_read_all(wd, monkeypatch, lambda wd, label: None)
    monkeypatch.setattr(confine, "guard", guard)
    with pytest.raises(SystemExit) as e:
        run()
    assert "readers" in str(e.value.code)
    assert snapshot(out) == before


# --- 組み立てと照合の出力（生成物）


def built_work(wd, conflict=False):
    (wd / "meta.json").write_text(json.dumps({"id": wd.name, "title": "曲"}))
    (wd / "score.json").write_text(json.dumps({"title": "曲", "tempo": 120}))
    (wd / "parts").mkdir()
    (wd / "parts" / "part_A.json").write_text(json.dumps({"1": "r.1", "2": "(0.6).1"}))
    (wd / "parts" / "part_B.json").write_text(json.dumps({"2": "(0.5).1" if conflict else "(0.6).1"}))
    (wd / "marks.json").write_text("[[1, 0.0]]")


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_build_and_verify_replace_outputs_that_are_links(ws, shape, capsys):
    wd, out, before = ws
    built_work(wd)
    for name in (f"{wd.name}.alphatex", f"{wd.name}.html", "timing.json"):
        place_last(wd / name, out, shape)
    place_last(wd / "conflicts.json", out, shape)
    assert build.run_build(wd) == 0
    assert verify.run_verify(wd) == 0
    for name in (f"{wd.name}.alphatex", f"{wd.name}.html", "timing.json"):
        assert is_plain(wd / name), name
    assert not os.path.lexists(wd / "conflicts.json")  # 食い違いが無ければ、リンクだけを消す
    assert snapshot(out) == before
    printed = capsys.readouterr().out
    assert f"{wd.name}.html が" in printed and "timing.json が" in printed
    no_outside_paths(printed.replace(str(wd), ""), out)


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_build_writes_conflicts_without_following(ws, shape):
    wd, out, before = ws
    built_work(wd, conflict=True)
    place_last(wd / "conflicts.json", out, shape)
    assert build.run_build(wd) == 1
    assert is_plain(wd / "conflicts.json") and "2" in json.loads((wd / "conflicts.json").read_text())
    assert snapshot(out) == before


def test_build_output_is_unchanged_without_links(ws, capsys):
    wd, _, _ = ws
    built_work(wd)
    assert build.run_build(wd) == 0
    printed = capsys.readouterr().out
    assert "置き換えました" not in printed and printed.count("出力: ") == 1
    assert (wd / f"{wd.name}.alphatex").read_text().startswith("\\title")


# --- ページ分け・拡大・帯の確認画像（生成物のフォルダ）


def framed(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEOTAB_CONFINE", raising=False)
    wd = make_work(tmp_path, n_pages=2)
    out = make_outside(tmp_path)
    return wd, out


@pytest.mark.parametrize("folder", ["pages", "strip"])
@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_output_folder_linking_outside_is_remade(tmp_path, monkeypatch, capsys, folder, shape):
    wd, out = framed(tmp_path, monkeypatch)
    (out / "d" / "p001_a.png").write_bytes(b"outside page")  # たどると消される名前
    before = snapshot(out)
    if folder == "pages":
        assert cli.main(["strip", str(wd)]) == 0
    place_folder(wd / folder, out, shape)
    assert cli.main(["strip" if folder == "strip" else "pages", str(wd)]) == 0
    assert (wd / folder).is_dir() and not (wd / folder).is_symlink()
    assert any(p.suffix == ".png" for p in (wd / folder).iterdir())
    assert snapshot(out) == before
    printed = capsys.readouterr().out
    assert f"{folder} が作業フォルダの外を指すリンクだったので、リンクを消して作り直しました" in printed
    no_outside_paths(printed.replace(str(wd), ""), out)


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_pages_and_strip_replace_files_that_are_links(tmp_path, monkeypatch, shape):
    wd, out = framed(tmp_path, monkeypatch)
    before = snapshot(out)
    assert cli.main(["strip", str(wd)]) == 0
    assert cli.main(["pages", str(wd)]) == 0
    names = ["pages/p001_a.png", "pages/pages.json", "pages/index.md", "pages/p009_a.png"]
    names += [f"strip/{p.name}" for p in (wd / "strip").iterdir()]
    for name in names:
        place_last(wd / name, out, shape)
    assert cli.main(["strip", str(wd)]) == 0
    assert cli.main(["pages", str(wd)]) == 0
    for name in names[:3] + names[4:]:
        assert is_plain(wd / name), name
    assert not os.path.lexists(wd / "pages" / "p009_a.png")  # 古い画像はリンクだけを消す
    assert snapshot(out) == before


def test_zoom_without_confine_refuses_output_folder_linking_outside(tmp_path, monkeypatch, capsys):
    wd, out = framed(tmp_path, monkeypatch)
    assert cli.main(["strip", str(wd)]) == 0
    capsys.readouterr()
    before = snapshot(out)
    for rel in ("pages", "pages/zoom"):
        (wd / "pages").mkdir(exist_ok=True)
        place_folder(wd / rel, out, "folder-link")
        with pytest.raises(SystemExit) as e:
            cli.main(["zoom", str(wd), "1"])
        assert e.value.code == "pages/zoom は作業フォルダの外を指しています"
        (wd / rel).unlink()
    assert snapshot(out) == before
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_zoom_without_confine_replaces_images_that_are_links(tmp_path, monkeypatch, capsys, shape):
    wd, out = framed(tmp_path, monkeypatch)
    assert cli.main(["strip", str(wd)]) == 0
    before = snapshot(out)
    place_last(wd / "pages" / "zoom" / "f0001_a.png", out, shape)
    capsys.readouterr()
    assert cli.main(["zoom", str(wd), "1"]) == 0
    assert is_plain(wd / "pages" / "zoom" / "f0001_a.png")
    assert snapshot(out) == before
    assert all(line.startswith(str(wd / "pages" / "zoom" / "f0001_")) for line in capsys.readouterr().out.splitlines())


def test_folders_linking_inside_work_as_before(tmp_path, monkeypatch):
    wd, out = framed(tmp_path, monkeypatch)
    before = snapshot(out)
    (wd / "store").mkdir()
    for name in ("frames", "strip", "pages"):
        if (wd / name).exists():
            os.rename(wd / name, wd / "store" / name)
        else:
            (wd / "store" / name).mkdir()
        (wd / name).symlink_to(f"store/{name}")
    assert cli.main(["strip", str(wd)]) == 0
    assert cli.main(["pages", str(wd)]) == 0
    assert cli.main(["zoom", str(wd), "1"]) == 0
    for name in ("frames", "strip", "pages"):
        assert (wd / name).is_symlink()
    assert any((wd / "store" / "strip").iterdir()) and (wd / "store" / "pages" / "pages.json").is_file()
    assert any((wd / "store" / "pages" / "zoom").iterdir())
    assert snapshot(out) == before


# --- 外部のプログラム（ffmpeg）と動画の取り込みの書き出し


def stages_in(place):
    return [p.name for p in place.iterdir() if p.name.startswith(inside.STAGE_PREFIX)]


def fake_ffmpeg(monkeypatch, during=None, fail=False, count=3):
    """ffmpeg の代わり。出力先のパターンに PNG を書き、書いている最中に during() を呼ぶ。"""
    from PIL import Image

    calls = []

    def run(cmd, check):
        calls.append(cmd)
        pattern = cmd[-1]
        for i in range(1, count + 1):
            Image.new("RGB", (8, 8), (i, 0, 0)).save(pattern % i)
            if i == 1 and during:
                during()
        if fail:
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(frames, "ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(frames.subprocess, "run", run)
    return calls


def video_work(ws):
    wd, out, _ = ws
    (wd / "video.mp4").write_bytes(b"video")
    (wd / "frames").mkdir()
    (wd / "frames" / "0001_00m00s000.png").write_bytes(b"old")
    (wd / "meta.json").write_text(json.dumps({"id": wd.name, "frames_from": "video.mp4"}))
    return wd, out


def only_plain_frames(wd, count=3):
    names = sorted(p.name for p in (wd / "frames").iterdir())
    assert names == [workdir.frame_name(i, i - 1) for i in range(1, count + 1)]
    assert all(is_plain(wd / "frames" / n) for n in names)


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_frames_swapped_while_ffmpeg_writes_does_not_reach_outside(ws, monkeypatch, shape):
    wd, out = video_work(ws)
    before = snapshot(out)

    def during():
        place_folder(wd / "frames", out, shape)  # 書き出しの最中に frames/ を外へのリンクにする

    calls = fake_ffmpeg(monkeypatch, during)
    assert frames.extract(wd, force=True) == 3
    assert inside.STAGE_PREFIX in calls[0][-1] and str(wd) not in calls[0][-1]  # 置き場の一時フォルダに書かせる
    assert not (wd / "frames").is_symlink()
    only_plain_frames(wd)
    assert snapshot(out) == before
    assert stages_in(wd.parent) == []
    assert json.loads((wd / "meta.json").read_text())["frames_from"] == "video.mp4"


@pytest.mark.parametrize("shape", LAST_SHAPES)
def test_frame_names_swapped_before_import_are_replaced(ws, monkeypatch, shape):
    wd, out = video_work(ws)
    before = snapshot(out)
    fake_ffmpeg(monkeypatch)
    real_rename = os.rename

    def rename(src, dst, *args, **kwargs):
        if str(dst).startswith("000") and kwargs.get("dst_dir_fd") is not None:
            place_last(wd / "frames" / dst, out, shape)  # 取り込む直前に、取り込み先の名前を差し替える
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    frames.extract(wd, force=True)
    monkeypatch.undo()
    only_plain_frames(wd)
    assert snapshot(out) == before


def test_failed_ffmpeg_leaves_no_temporary_folder(ws, monkeypatch):
    wd, out = video_work(ws)
    before = snapshot(out)
    fake_ffmpeg(monkeypatch, lambda: place_folder(wd / "frames", out, "folder-link"), fail=True)
    with pytest.raises(subprocess.CalledProcessError):
        frames.extract(wd, force=True)
    assert stages_in(wd.parent) == []
    assert snapshot(out) == before


def test_frames_folder_linking_inside_is_replaced_by_a_real_folder(ws, monkeypatch):
    wd, out = video_work(ws)
    (wd / "store").mkdir()
    os.rename(wd / "frames", wd / "store" / "frames")
    (wd / "frames").symlink_to("store/frames")
    fake_ffmpeg(monkeypatch)
    frames.extract(wd)  # 中を指すリンクも、--force なしで作り直す
    assert not (wd / "frames").is_symlink()
    only_plain_frames(wd)
    assert (wd / "store" / "frames" / "0001_00m00s000.png").read_bytes() == b"old"  # 先の中身は残る


def test_frames_without_force_still_refuses_existing_images(ws, monkeypatch):
    wd, out = video_work(ws)
    fake_ffmpeg(monkeypatch)
    with pytest.raises(SystemExit, match="すでに画像があります"):
        frames.extract(wd)


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_frames_without_force_remakes_link_without_looking_behind_it(ws, tmp_path, monkeypatch, capsys, shape):
    from test_confine import run_watched

    wd, out = video_work(ws)
    before = snapshot(out)  # 外のフォルダ d には中身がある
    place_folder(wd / "frames", out, shape)
    result, touched = run_watched(tmp_path, wd, monkeypatch, frames._check_force, wd / "frames", False)
    assert result is None and touched == []  # 止めず、リンクの先の中身を数えない
    fake_ffmpeg(monkeypatch)
    assert cli.main(["frames", str(wd)]) == 0
    assert not (wd / "frames").is_symlink()
    only_plain_frames(wd)
    assert snapshot(out) == before
    assert "frames がリンクだったので、リンクを消して作り直しました" in capsys.readouterr().out


def test_has_entries_does_not_look_behind_links(ws):
    wd, out, _ = ws
    (wd / "full").mkdir()
    (wd / "full" / "a").write_bytes(b"a")
    (wd / "empty").mkdir()
    (wd / "file").write_bytes(b"f")
    (wd / "to_inside").symlink_to("full")
    (wd / "to_outside").symlink_to(out / "d")
    (wd / "dangling").symlink_to(out / "nowhere")
    assert inside.has_entries(wd / "full")
    for name in ("empty", "file", "to_inside", "to_outside", "dangling", "missing"):
        assert not inside.has_entries(wd / name), name


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_run_remakes_frames_link_instead_of_reusing_it(ws, monkeypatch, shape):
    wd, out = video_work(ws)
    before = snapshot(out)
    place_folder(wd / "frames", out, shape)
    fake_ffmpeg(monkeypatch)
    seen = []

    def run_cli(self, *args):
        seen.append(args)
        assert cli.main(list(args)) == 0

    monkeypatch.setattr(pipeline.Job, "_cli", run_cli)
    job = pipeline.Job.create(wd)
    assert job._run_step("frames", "claude") == "3 枚"  # 切り出し済みの画像として使い回さない
    assert seen == [("frames", str(wd), "--force")]
    assert not (wd / "frames").is_symlink()
    only_plain_frames(wd)
    assert snapshot(out) == before


@pytest.mark.parametrize("shape", FOLDER_SHAPES)
def test_import_folder_does_not_write_through_frames_link(ws, tmp_path, shape, capsys):
    wd, out, _ = ws
    src = tmp_path / "src"
    src.mkdir()
    for i in range(2):
        (src / f"{i:04d}_00m0{i}s000.png").write_bytes(b"img")
    before = snapshot(out)
    place_folder(wd / "frames", out, shape)
    assert frames.import_folder(wd, src, force=True) == 2
    assert all(is_plain(p) for p in (wd / "frames").iterdir()) and len(list((wd / "frames").iterdir())) == 2
    assert snapshot(out) == before
    assert "frames がリンクだったので、リンクを消して作り直しました" in capsys.readouterr().out


def test_frames_refuses_video_linking_outside(ws, monkeypatch):
    wd, out = video_work(ws)
    before = snapshot(out)
    (wd / "video.mp4").unlink()
    (out / "secret.mp4").write_bytes(b"secret")
    before = snapshot(out)
    (wd / "video.mp4").symlink_to(out / "secret.mp4")
    calls = fake_ffmpeg(monkeypatch)
    with pytest.raises(SystemExit, match="作業フォルダの外を指しています"):
        frames.extract(wd, force=True)
    assert calls == []  # 外のファイルを ffmpeg に渡さない
    assert snapshot(out) == before


def test_add_checks_the_video_in_temporary_folder(ws, tmp_path, fake_probe):
    wd, out, before = ws
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video")
    new = add.add(src, wd.parent)
    # ffprobe には、置き場（読み手が書けない）の一時フォルダの中の写しを渡す
    assert fake_probe[0].parent.parent == wd.parent.resolve()
    assert fake_probe[0].parent.name.startswith(inside.STAGE_PREFIX)
    assert is_plain(new / "video.mp4") and (new / "video.mp4").read_bytes() == b"video"
    assert stages_in(wd.parent) == []
    assert snapshot(out) == before


def test_failed_add_leaves_no_temporary_folder(ws, tmp_path, monkeypatch):
    wd, out, before = ws

    def broken(path):
        raise add.NotVideo("映像の入った動画として読めません")

    monkeypatch.setattr(add, "probe", broken)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video")
    with pytest.raises(add.NotVideo):
        add.add(src, wd.parent)
    assert stages_in(wd.parent) == []
    assert sorted(p.name for p in wd.parent.iterdir() if not p.name.startswith(".")) == [wd.name]
    assert snapshot(out) == before
