"""紙の楽譜の領域・補正・通し実行。素材は合成し、読み手は偽物を使う。"""

import io
import json
import shlex
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from videotab import build, cli, documents, pages, paper, pipeline, read, server
from videotab.agent import AgentResult
from videotab.agent_settings import Choice
from videotab.workdir import list_frames, load_meta, read_json, save_meta
from synth import write_frames


def score_image(strings=6, curve=True):
    im = Image.new("RGB", (480, 320), "white")
    d = ImageDraw.Draw(im)
    d.text((15, 10), "Guitar I", fill="black")
    for n in range(5):
        d.line((15, 38 + n * 4, 465, 38 + n * 4), fill=(90, 90, 90))
    for n in range(strings):
        points = []
        for x in range(15, 466):
            shift = 12 * ((x - 240) / 225) ** 2 if curve else 0
            points.append((x, round(85 + n * 6 + shift)))
        d.line(points, fill=(80, 80, 80))
    d.text((180, 88), "3", fill="black")
    # 別パートの五線・TAB。選択された領域に混ざらないことを確認する。
    d.text((15, 180), "Guitar II", fill="black")
    for n in range(5):
        d.line((15, 205 + n * 4, 465, 205 + n * 4), fill="black")
    for n in range(6):
        d.line((15, 245 + n * 6, 465, 245 + n * 6), fill="black")
    return im


REGION = {"box": [.02, .05, .98, .46], "tab": [.02, .24, .98, .43]}


def make_paper(tmp_path, strings=6):
    wd = tmp_path / "work" / "paper-123abc"
    write_frames(wd, [np.asarray(score_image(strings)) for _ in range(3)])
    save_meta(wd, {"id": wd.name, "title": "合成の楽譜", "frames_from": "synthetic"})
    paper.configure(wd, "Bass" if strings == 4 else "Guitar I", strings)
    (wd / "paper").mkdir()
    (wd / "paper" / "layout.json").write_text(json.dumps({"pages": [{"frame": 2, "regions": [REGION]}], "warnings": []}))
    return wd


@pytest.mark.parametrize("strings", [4, 6])
def test_curved_selected_tab_is_flattened_and_notation_retained(strings):
    rgb, lines = paper.straighten_region(score_image(strings), REGION["box"], REGION["tab"], strings)
    assert lines is not None and len(lines) == strings
    assert np.allclose(np.diff(lines), 6, atol=.5)
    assert rgb.shape[0] < 150  # Guitar II は入らない
    assert np.min(rgb[20:45]) < 150  # 五線も残る
    for x in range(70, rgb.shape[1] - 70, 20):
        for y in lines:
            lo, hi = round(y) - 1, round(y) + 2
            assert rgb[lo:hi, x, 0].min() < 180


def test_unreadable_region_is_kept_without_invented_line_marks():
    im = Image.new("RGB", (480, 320), "white")
    rgb, lines = paper.straighten_region(im, REGION["box"], REGION["tab"], 6)
    assert lines is None and rgb.size
    pictures = paper._pieces(rgb, lines)
    assert pictures[0][1] == []


@pytest.mark.parametrize("strings", [4, 6])
def test_line_spacing_and_enlargement_do_not_depend_on_string_count(strings):
    lines = [40.0 + 12 * n for n in range(strings)]
    assert pages.Setup((0, 140), -1, (255, 255, 255), [lines]).spacing == 12.0
    rgb = np.full((140, 2000, 3), 255, np.uint8)
    for y in lines:
        rgb[round(y)] = 80
    pictures = paper._pieces(rgb, lines)
    assert len(pictures) == 4
    for _, marks in pictures:
        assert len(marks) == strings and np.allclose(np.diff(marks), 30, atol=.5)


@pytest.mark.parametrize("change", [
    {"frame": 999},
    {"frame": True},
    {"regions": [{"box": [0, 0, 1, float("nan")], "tab": [0, 0, 1, 1]}]},
    {"regions": [{"box": [0, 0, .5, .5], "tab": [0, 0, 1, 1]}]},
    {"regions": [REGION, REGION]},
    {"regions": []},
])
def test_invalid_layout_is_rejected_before_cropping(tmp_path, change):
    wd = make_paper(tmp_path)
    data = {"pages": [{"frame": 2, "regions": [REGION], **change}]}
    with pytest.raises(ValueError):
        paper.validate_layout(data, list_frames(wd))


def test_page_images_preserve_context_and_paper_reading_rules(tmp_path):
    wd = make_paper(tmp_path, 4)
    assert cli.main(["pages", str(wd)]) == 0
    data = read_json(wd / "pages" / "pages.json")
    assert data["source_mode"] == "paper" and data["strings"] == 4
    row = data["pages"][0]
    assert len(row["lines"][0]) == 4
    assert Image.open(wd / "pages" / row["context"]).size == (480, 320)
    prompt = read.reader_prompt(wd, "A", [1], True)
    assert "最下段が 4 弦" in prompt and "Guitar I" not in prompt
    assert "撮影時刻からテンポを推定してはいけません" in prompt
    assert "元ページ画像" in prompt
    assert "紙の楽譜" in read.fix_prompt(wd, "A", ["拍数"])
    assert "紙の楽譜" in read.resolve_prompt(wd, ["抜け"])
    assert read.make_marks(wd) == [] and read_json(wd / "marks.json") == []
    assert build.load_score(wd)["tuning"] == "g2 d2 a1 e1"
    assert cli.main(["verify", str(wd)]) == 0


def test_zoom_unselected_frame_preserves_whole_page_and_confines_writes(tmp_path, monkeypatch):
    wd = make_paper(tmp_path)
    monkeypatch.setenv("VIDEOTAB_CONFINE", str(wd.resolve()))
    assert cli.main(["zoom", str(wd), "1"]) == 0
    assert Image.open(wd / "pages" / "zoom" / "f0001_context.png").size == (960, 640)
    outside = tmp_path / "outside"
    outside.mkdir()
    (wd / "pages" / "zoom").rename(wd / "pages" / "old")
    (wd / "pages" / "zoom").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit):
        cli.main(["zoom", str(wd), "1"])
    assert list(outside.iterdir()) == []


def test_photos_are_all_kept_and_video_chooses_sharp_frames_per_interval(tmp_path):
    wd = make_paper(tmp_path)
    assert len(paper.prepare(wd)) == 1
    meta = load_meta(wd)
    meta["source_kind"] = "document"
    save_meta(wd, meta)
    assert len(paper.prepare(wd)) == 3


def image_bytes():
    data = io.BytesIO()
    score_image().save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize("kind", ["image", "zip", "folder"])
def test_document_import_has_pages_without_fake_performance_time(tmp_path, kind):
    raw = image_bytes()
    src = tmp_path / "source.png"
    if kind == "zip":
        src = tmp_path / "source.zip"
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("../../first.png", raw)
            z.writestr("next.png", raw)
    elif kind == "folder":
        src = tmp_path / "photos"
        src.mkdir()
        (src / "first.png").write_bytes(raw)
        (src / "next.png").write_bytes(raw)
    else:
        src.write_bytes(raw)
    wd = documents.import_source(src, tmp_path / "work", title="写真")
    assert load_meta(wd)["source_kind"] == "document"
    assert load_meta(wd)["title"] == "写真"
    frames = list_frames(wd)
    assert len(frames) == (1 if kind == "image" else 2) and all(f.time == 0 for f in frames)
    assert not (tmp_path / "first.png").exists()


def test_pdf_uses_local_renderer_and_retains_page_order(tmp_path, monkeypatch):
    monkeypatch.setattr(documents.shutil, "which", lambda _: "/bin/pdftoppm")
    def run(cmd, **kwargs):
        assert "-scale-to" in cmd and "2400" in cmd
        for n in (10, 2, 1):
            Image.new("RGB", (30, 40), (n, n, n)).save(cmd[-1] + f"-{n}.png")
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(documents.subprocess, "run", run)
    wd = documents.receive(io.BytesIO(b"pdf"), 3, "score.pdf", tmp_path / "work")
    assert [Image.open(f.path).getpixel((0, 0))[0] for f in list_frames(wd)] == [1, 2, 10]


def test_bad_document_does_not_leave_a_job(tmp_path):
    root = tmp_path / "work"
    with pytest.raises(ValueError, match="画像として読めません"):
        documents.receive(io.BytesIO(b"bad"), 3, "bad.png", root)
    assert not [p for p in root.iterdir() if p.is_dir()]


def test_paper_pipeline_selects_part_builds_score_and_skips_timing(tmp_path, monkeypatch):
    wd = make_paper(tmp_path)
    calls = []
    def agent(prompt, *, workdir, writable, label, settings=None, images=None, **kwargs):
        calls.append((label, settings, writable))
        assert images and all(path.is_file() for path in images)
        if label == "ページとパート":
            assert [p.name for p in writable] == ["layout.json"]
            writable[0].write_text(json.dumps({"pages": [{"frame": 2, "regions": [REGION]}]}))
        else:
            assert label == "A"
            (wd / "parts" / "part_A.json").write_text('{"1": "(3.2).1", "2": "r.1"}')
            (wd / "readers" / "pagebars_A.json").write_text('{"1": 1}')
            s = build.load_score(wd)
            s["tempo"] = 120
            (wd / "score.json").write_text(json.dumps(s))
        return AgentResult(True, "合成の読み取り結果", .1)
    monkeypatch.setattr(paper, "run_agent", agent)
    monkeypatch.setattr(read, "run_agent", agent)
    monkeypatch.setattr(pipeline.Job, "_cli", lambda self, *argv: cli.main(list(argv)))
    job = pipeline.Job.create(wd, "codex", choice=Choice("gpt-5", "high"))
    assert job.run()
    assert job.load()["status"] == "done"
    assert job.load()["steps"][-1]["message"].startswith("小節と拍の検査済み")
    assert (wd / (wd.name + ".html")).exists()
    assert all(s.chosen_model == "gpt-5" for _, s, _ in calls)
    assert read_json(wd / "marks.json") == []


def test_paper_selection_failure_keeps_source_images(tmp_path, monkeypatch):
    wd = make_paper(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("パートを特定できません")
    monkeypatch.setattr(paper, "analyze", fail)
    job = pipeline.Job.create(wd)
    assert not job.run()
    assert len(list_frames(wd)) == 3 and load_meta(wd)["frames_from"]


def test_upload_document_and_part_are_validated_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(server.App, "_worker", lambda *_: None)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(tmp_path / "work")
    raw = image_bytes()
    job_id = app.receive(io.BytesIO(raw), len(raw), "score.png", "codex", paper_mode=True, part="Bass", strings=4)
    assert load_meta(app.root / job_id)["paper"] == {"part": "Bass", "strings": 4}
    assert app.detail(job_id)["steps"][2]["label"] == "ページとパートの選択"
    with pytest.raises(ValueError, match="弦数"):
        app.check_upload("score.png", len(raw), "codex", paper_mode=True, strings=5)


def test_changing_part_in_ui_restarts_selection_and_keeps_reading_history(tmp_path, monkeypatch):
    wd = make_paper(tmp_path)
    paper.write_pages(wd)
    job = pipeline.Job.create(wd)
    for name in pipeline.STEP_NAMES:
        job.update_step(name, status="done")
    monkeypatch.setattr(server.App, "_worker", lambda *_: None)
    app = server.App(wd.parent)
    assert app.detail(wd.name)["paper_previews"]
    (wd / "parts").mkdir()
    (wd / "parts" / "part_A.json").write_text('{"1":"r.1"}')
    app.retry(wd.name, "build", None, paper_choice={"part": "Bass", "strings": 4})
    states = {s["name"]: s["status"] for s in job.load()["steps"]}
    assert states["frames"] == "done" and states["strip"] == states["read"] == "pending"
    assert load_meta(wd)["paper"] == {"part": "Bass", "strings": 4}
    assert (wd / "parts" / "part_A.json").exists()  # 読み直しの開始時に history へ移す
    assert app.detail(wd.name)["paper_previews"] == []


def test_broken_zip_returns_input_error_and_cleans_up(tmp_path):
    with pytest.raises(ValueError, match="ZIP を読めません"):
        documents.receive(io.BytesIO(b"zip"), 3, "score.zip", tmp_path / "work")
    assert not [p for p in (tmp_path / "work").iterdir() if p.is_dir()]


def test_paper_frame_links_outside_workdir_are_rejected(tmp_path):
    wd = make_paper(tmp_path)
    outside = tmp_path / "outside.png"
    score_image().save(outside)
    frame = list_frames(wd)[0]
    frame.path.unlink()
    frame.path.symlink_to(outside)
    with pytest.raises(SystemExit, match="作業フォルダ"):
        paper.prepare(wd)


@pytest.mark.parametrize("old_strings,new_strings", [(6, 4), (4, 6)])
def test_part_change_updates_tuning_before_reading_and_preserves_old_score(tmp_path, old_strings, new_strings):
    wd = make_paper(tmp_path, old_strings)
    old = build.load_score(wd)
    old.update(tempo=137, capo=2, title="曲の見出し")
    (wd / "score.json").write_text(json.dumps(old))
    paper.configure(wd, "次のパート", new_strings)
    score = build.load_score(wd)
    assert len(score["tuning"].split()) == new_strings
    assert score["capo"] == 0 and score["tempo"] == 137 and score["title"] == "曲の見出し"
    saved = list((wd / "history").glob("part-*/score.json"))
    assert len(saved) == 1 and read_json(saved[0]) == old
    # ギターの6弦を含む読み取りを、切替後の弦数で検査できる。
    (wd / "parts").mkdir()
    part = wd / "parts" / "part_A.json"
    part.write_text('{"1":"(0.6).1"}')
    assert bool(read.part_issues(part, score, wd)) == (new_strings == 4)


def stored_bass(tmp_path):
    """Bass・4 弦を保存し、見出しに標準でないチューニングとカポを入れた作業フォルダ。"""
    wd = make_paper(tmp_path, 4)
    score = build.load_score(wd)
    score.update(tuning="g2 d2 a1 d1", capo=2)
    (wd / "score.json").write_text(json.dumps(score))
    return wd, score


def test_paper_command_without_options_keeps_stored_part_and_score(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "prepare", lambda _: [])
    wd, score = stored_bass(tmp_path)
    meta, text = load_meta(wd), (wd / "score.json").read_text()
    assert meta["paper"] == {"part": "Bass", "strings": 4}
    assert cli.main(["paper", str(wd)]) == 0
    assert load_meta(wd) == meta
    assert (wd / "score.json").read_text() == text
    assert read_json(wd / "score.json") == score and (score["tuning"], score["capo"]) == ("g2 d2 a1 d1", 2)
    assert not (wd / "history").exists()


def test_paper_command_part_only_keeps_stored_strings(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "prepare", lambda _: [])
    wd, _ = stored_bass(tmp_path)
    assert cli.main(["paper", str(wd), "--part", "Bass II"]) == 0
    assert load_meta(wd)["paper"] == {"part": "Bass II", "strings": 4}


def test_paper_command_explicit_options_replace_stored_part_and_keep_old_score(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "prepare", lambda _: [])
    wd, score = stored_bass(tmp_path)
    assert cli.main(["paper", str(wd), "--part", "Guitar I", "--strings", "6"]) == 0
    assert load_meta(wd)["paper"] == {"part": "Guitar I", "strings": 6}
    new = read_json(wd / "score.json")
    assert new["tuning"] == build.DEFAULT_TUNING and new["capo"] == 0
    saved = list((wd / "history").glob("part-*/score.json"))
    assert len(saved) == 1 and read_json(saved[0]) == score


def test_paper_command_without_stored_part_falls_back_to_guitar(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "prepare", lambda _: [])
    wd = tmp_path / "work" / "paper-456def"
    write_frames(wd, [np.asarray(score_image())])
    save_meta(wd, {"id": wd.name, "title": "合成の楽譜", "frames_from": "synthetic"})
    assert "paper" not in load_meta(wd)
    assert cli.main(["paper", str(wd)]) == 0
    assert load_meta(wd)["paper"] == {"part": "Guitar I", "strings": 6}


def test_invalid_model_choice_does_not_partially_save_part_change(tmp_path, monkeypatch):
    wd = make_paper(tmp_path)
    old_score = build.load_score(wd)
    old_meta = load_meta(wd)
    job = pipeline.Job.create(wd, "claude")
    data = job.load()
    data["status"] = "done"
    for step in data["steps"]:
        step["status"] = "done"
    job.save(data)
    argv = ["run", wd.name, "--root", str(wd.parent), "--part", "Bass", "--strings", "4"]
    with pytest.raises(SystemExit, match="推論の強さ"):
        cli.main([*argv, "--effort", "minimal"])
    assert load_meta(wd) == old_meta and build.load_score(wd) == old_score
    assert job.load()["status"] == "done"
    seen = []
    monkeypatch.setattr(pipeline.Job, "run", lambda self: seen.append(self.load()) or True)
    assert cli.main([*argv, "--effort", "high"]) == 0
    assert seen and seen[0]["steps"][2]["status"] == "pending"
    assert load_meta(wd)["paper"] == {"part": "Bass", "strings": 4}


def test_add_photo_gives_a_command_to_resume_existing_images(tmp_path, monkeypatch, capsys):
    source = tmp_path / "photo.png"
    source.write_bytes(image_bytes())
    root = tmp_path / "scores"
    assert cli.main(["add", str(source), "--root", str(root)]) == 0
    output = capsys.readouterr().out
    assert "--step strip" in output and "次: videotab frames" not in output
    wd = next(p for p in root.iterdir() if p.is_dir())
    seen = []
    monkeypatch.setattr(pipeline.Job, "run", lambda self: seen.append(self.workdir) or True)
    assert cli.main(["run", str(wd), "--step", "strip"]) == 0
    assert seen == [wd.resolve()]


def test_native_image_input_contains_context_and_selected_regions_only(tmp_path):
    wd = make_paper(tmp_path)
    paper.write_pages(wd)
    paths = read.image_inputs(wd, [1])["images"]
    row = read_json(wd / "pages" / "pages.json")["pages"][0]
    assert {p.name for p in paths} == {row["context"], *row["images"]}
    assert read.image_inputs(wd, [99])["images"] == []


def test_resolver_attaches_problem_regions_and_neighbours(tmp_path):
    wd = make_paper(tmp_path)
    (wd / "readers").mkdir()
    (wd / "readers" / "pagebars_A.json").write_text('{"1":1,"2":5,"3":9,"4":13}')
    assert read.problem_pages(wd, ["13 小節の食い違い（A: 音／B: 音）"], [1, 2, 3, 4]) == [3, 4]
    assert read.problem_pages(wd, ["抜けている小節: 13, 14"], [1, 2, 3, 4]) == [3, 4]
    assert read.problem_pages(wd, ["原因不明"], [1, 2, 3, 4]) == [1, 2, 3, 4]


def test_codex_native_images_are_snapshotted_and_do_not_follow_external_links(tmp_path):
    from videotab import agent

    wd = make_paper(tmp_path)
    source = list_frames(wd)[0].path
    with agent._image_snapshots(wd, [source]) as images:
        assert len(images) == 1 and images[0].read_bytes() == source.read_bytes()
        stage = images[0].parent
        command = agent._codex_command("読む", wd, tmp_path / "last.txt", images=images)
        assert command[command.index("--image") + 1] == str(images[0])
        assert 'sandbox_workspace_write.network_access=false' in command
    assert not stage.exists()
    outside = tmp_path / "other.png"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(SystemExit, match="作業フォルダ"):
        with agent._image_snapshots(wd, [source]):
            pass


def test_failed_run_by_folder_path_gives_a_working_resume_command(tmp_path, monkeypatch, capsys):
    wd = make_paper(tmp_path)
    monkeypatch.setattr(pipeline.Job, "run", lambda *_: False)
    assert cli.main(["run", str(wd)]) == 1
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("続きは "))
    command = shlex.split(line.removeprefix("続きは "))
    assert str(wd.parent.resolve()) in command
    seen = []
    monkeypatch.setattr(pipeline.Job, "run", lambda self: seen.append(self.workdir) or True)
    assert cli.main(command[1:]) == 0
    assert seen == [wd.resolve()]
