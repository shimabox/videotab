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
    assert load_meta(wd)["paper"] == {"part": "Guitar I", "strings": 6}  # パートを明示して作った曲の形
    job = pipeline.Job.create(wd, "codex", choice=Choice("gpt-5", "high"))
    assert job.run()
    assert job.load()["status"] == "done"
    assert job.load()["steps"][-1]["message"].startswith("小節と拍の検査済み")
    assert (wd / (wd.name + ".html")).exists()
    assert [label for label, _, _ in calls] == ["ページとパート", "A"]  # パートの洗い出しは起動しない
    assert not (wd / "paper" / "parts.json").exists()
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


def test_paper_command_without_any_part_saves_pending_and_tells_how_to_continue(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paper, "prepare", lambda _: [])
    wd = tmp_path / "work" / "paper-456def"
    write_frames(wd, [np.asarray(score_image())])
    save_meta(wd, {"id": wd.name, "title": "合成の楽譜", "frames_from": "synthetic"})
    assert "paper" not in load_meta(wd)
    assert cli.main(["paper", str(wd)]) == 0
    assert load_meta(wd)["paper"] == {"part": None, "strings": None}
    output = capsys.readouterr().out
    assert "--step strip" in output and "--part" in output
    assert cli.main(["paper", str(wd), "--strings", "4"]) == 0
    assert load_meta(wd)["paper"] == {"part": None, "strings": 4}
    assert cli.main(["paper", str(wd), "--part", "Bass"]) == 0  # 弦数は先に決めた値のまま
    assert load_meta(wd)["paper"] == {"part": "Bass", "strings": 4}


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


# --- パートの洗い出し

PARTS = {
    "parts": [
        {"name": "Guitar I", "tab": True, "strings": 6, "confidence": "high", "frames": [1, 2]},
        {"name": "Guitar II", "tab": True, "strings": 6},
        {"name": "Bass", "tab": True, "strings": 4, "confidence": "low"},
        {"name": "Drums", "tab": False, "strings": None},
    ],
    "warnings": ["3 ページ目は右端が切れていて楽器名が読めません"],
}


def make_pending(tmp_path, strings=None):
    """パートを選ぶ前の紙の楽譜（合成の 3 枚）。"""
    wd = tmp_path / "work" / "paper-123abc"
    write_frames(wd, [np.asarray(score_image()) for _ in range(3)])
    save_meta(wd, {"id": wd.name, "title": "合成の楽譜", "frames_from": "synthetic"})
    paper.configure_pending(wd, strings)
    return wd


def write_parts(wd, data=PARTS):
    (wd / "paper").mkdir(exist_ok=True)
    (wd / "paper" / "parts.json").write_text(json.dumps(data))


def test_pending_paper_keeps_part_unset_until_chosen(tmp_path):
    wd = make_pending(tmp_path)
    assert load_meta(wd)["paper"] == {"part": None, "strings": None}
    assert not paper.is_chosen(load_meta(wd)["paper"])
    with pytest.raises(ValueError, match="パートを指定"):
        paper.selection(wd)
    paper.configure_pending(wd, 4)
    assert load_meta(wd)["paper"] == {"part": None, "strings": 4}
    with pytest.raises(ValueError, match="弦数"):
        paper.pending_options(5)
    paper.configure(wd, "Guitar II", 6)
    assert paper.is_chosen(load_meta(wd)["paper"])
    assert paper.is_chosen({"part": "Guitar I", "strings": 6})  # パートを明示して作った曲の形


def test_found_parts_keep_spelling_and_known_fields_only(tmp_path):
    wd = make_pending(tmp_path)
    data = {"parts": [{"name": "  Guitar II ", "tab": True, "strings": None, "extra": "<b>"},
                      {"name": "<b>Vocal</b>", "tab": False, "strings": None, "confidence": "medium", "frames": [3]}]}
    assert paper.validate_parts(data, list_frames(wd)) == {
        "parts": [{"name": "Guitar II", "tab": True, "strings": None},
                  {"name": "<b>Vocal</b>", "tab": False, "strings": None, "confidence": "medium", "frames": [3]}],
        "warnings": [],
    }
    assert paper.validate_parts(PARTS, list_frames(wd)) == PARTS


def part_row(**change):
    return {"name": "Guitar I", "tab": True, "strings": 6, **change}


@pytest.mark.parametrize("data", [
    [],
    {"parts": {}},
    {"parts": []},
    {"parts": [part_row(name=f"Guitar {n}") for n in range(41)]},
    {"parts": ["Guitar I"]},
    {"parts": [part_row(name=5)]},
    {"parts": [part_row(name="   ")]},
    {"parts": [part_row(name="Guitar\nII")]},
    {"parts": [part_row(name="Guitar \x1b[31mII")]},
    {"parts": [part_row(name="G" * 81)]},
    {"parts": [part_row(), part_row(name=" Guitar I ")]},
    {"parts": [part_row(tab=1)]},
    {"parts": [part_row(tab="true")]},
    {"parts": [part_row(strings=5)]},
    {"parts": [part_row(strings=True)]},
    {"parts": [part_row(strings="6")]},
    {"parts": [part_row(confidence="certain")]},
    {"parts": [part_row(frames=[999])]},
    {"parts": [part_row(frames=[1, 1])]},
    {"parts": [part_row(frames=[True])]},
    {"parts": [part_row(frames=list(range(201)))]},
    {"parts": [part_row()], "warnings": "読めません"},
    {"parts": [part_row()], "warnings": [1]},
    {"parts": [part_row()], "warnings": ["注意"] * 51},
    {"parts": [part_row()], "warnings": ["あ" * 501]},
])
def test_invalid_found_parts_are_rejected(tmp_path, data):
    wd = make_pending(tmp_path)
    with pytest.raises(ValueError) as e:
        paper.validate_parts(data, list_frames(wd))
    assert not isinstance(e.value, paper.NoTabPart)


def test_found_parts_without_any_tab_fail_with_the_reported_reason(tmp_path):
    wd = make_pending(tmp_path)
    data = {"parts": [{"name": "Piano", "tab": False, "strings": None}], "warnings": ["五線だけの楽譜です"]}
    with pytest.raises(paper.NoTabPart, match="TAB のあるパートが見つかりません: 五線だけの楽譜です"):
        paper.validate_parts(data, list_frames(wd))


def test_found_parts_are_listed_one_per_line():
    assert paper.describe_parts(PARTS["parts"]) == [
        "  1. Guitar I   TAB あり  6 弦の見込み  確信度 高  ページ 1, 2",
        "  2. Guitar II  TAB あり  6 弦の見込み",
        "  3. Bass       TAB あり  4 弦の見込み  確信度 低",
        "  4. Drums      TAB なし（選べません）",
    ]
    assert paper.describe_parts([{"name": "Guitar", "tab": True, "strings": None}]) == [
        "  1. Guitar  TAB あり  弦数の見込みなし"
    ]


def test_discover_writes_one_file_and_retries_once_with_the_error(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    write_parts(wd, {"parts": [{"name": "前回の結果", "tab": True, "strings": 6}]})
    calls, logs = [], []
    def agent(prompt, *, workdir, writable, label, images=None, **kwargs):
        calls.append((label, prompt, list(writable)))
        assert images and all(path.is_file() and path.parent.name == "candidates" for path in images)
        assert not writable[0].exists()  # 前の洗い出しの結果は消してから起動する
        writable[0].write_text("{壊れた JSON" if len(calls) == 1 else json.dumps(PARTS))
        return AgentResult(True, "4 つのパートを見つけました", .1)
    monkeypatch.setattr(paper, "run_agent", agent)
    found = paper.discover(wd, "claude", logs.append)
    assert found == PARTS
    assert [label for label, _, _ in calls] == ["パートの洗い出し"] * 2
    assert all(writable == [wd.resolve() / "paper" / "parts.json"] for _, _, writable in calls)
    assert "検査に通りませんでした" not in calls[0][1] and "検査に通りませんでした" in calls[1][1]
    assert "Guitar II" in calls[0][1] and "0001_" in calls[0][1]  # JSON の例と、候補の元画像の一覧
    report = (wd / "paper" / "parts.md").read_text()
    assert "4 つのパートを見つけました" in report and "右端が切れていて" in report
    assert any("右端が切れていて" in line for line in logs)
    assert paper.load_parts(wd) == PARTS["parts"]


def test_discover_gives_up_after_second_invalid_output(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    calls = []
    def agent(prompt, *, writable, **kwargs):
        calls.append(writable)
        writable[0].write_text(json.dumps({"parts": [part_row(strings=5)]}))
        return AgentResult(True, "", .1)
    monkeypatch.setattr(paper, "run_agent", agent)
    with pytest.raises(RuntimeError, match="洗い出せませんでした.*strings"):
        paper.discover(wd, "claude", lambda _: None)
    assert len(calls) == 2 and all(len(writable) == 1 for writable in calls)
    assert paper.load_parts(wd) == []
    assert len(list_frames(wd)) == 3


def test_discover_does_not_retry_when_no_part_has_tab(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    calls = []
    def agent(prompt, *, writable, **kwargs):
        calls.append(writable)
        writable[0].write_text(json.dumps({"parts": [{"name": "Piano", "tab": False, "strings": None}],
                                           "warnings": ["TAB の段がありません"]}))
        return AgentResult(True, "", .1)
    monkeypatch.setattr(paper, "run_agent", agent)
    with pytest.raises(paper.NoTabPart, match="TAB の段がありません"):
        paper.discover(wd, "claude", lambda _: None)
    assert len(calls) == 1


@pytest.mark.parametrize("text", ["{", "[]", json.dumps({"parts": [part_row(tab=1)]})])
def test_found_parts_are_checked_again_on_every_read(tmp_path, text):
    wd = make_pending(tmp_path)
    assert paper.load_parts(wd) == []  # まだ無い
    write_parts(wd)
    assert paper.load_parts(wd) == PARTS["parts"]
    (wd / "paper" / "parts.json").write_text(text)
    assert paper.load_parts(wd) == []


def fake_agents(wd, monkeypatch, parts=PARTS):
    """洗い出し・ページ選択・読み手を偽物にし、起動された担当の名前が順に入るリストを返す。"""
    calls = []
    def agent(prompt, *, workdir, writable, label, images=None, **kwargs):
        calls.append(label)
        if label == "パートの洗い出し":
            assert [p.name for p in writable] == ["parts.json"]  # 指定する出力先は 1 ファイルだけ
            writable[0].write_text(json.dumps(parts))
        elif label == "ページとパート":
            assert json.dumps(paper.selection(wd)["part"], ensure_ascii=False) in prompt
            writable[0].write_text(json.dumps({"pages": [{"frame": 2, "regions": [REGION]}]}))
        else:
            (wd / "parts" / "part_A.json").write_text('{"1": "(3.2).1", "2": "r.1"}')
            (wd / "readers" / "pagebars_A.json").write_text('{"1": 1}')
            score = build.load_score(wd)
            score["tempo"] = 120
            (wd / "score.json").write_text(json.dumps(score))
        return AgentResult(True, "合成の結果", .1)
    monkeypatch.setattr(paper, "run_agent", agent)
    monkeypatch.setattr(read, "run_agent", agent)
    monkeypatch.setattr(pipeline.Job, "_cli", lambda self, *argv: cli.main(list(argv)))
    return calls


def steps_of(job):
    return {s["name"]: s for s in job.load()["steps"]}


def test_pipeline_without_part_lists_parts_and_waits(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    calls = fake_agents(wd, monkeypatch)
    monkeypatch.setattr(paper, "analyze", lambda *a, **k: pytest.fail("パートを選ぶ前にページを選ばない"))
    job = pipeline.Job.create(wd)
    assert job.load()["source_mode"] == "paper"
    assert job.run() is False
    data, steps = job.load(), steps_of(job)
    assert data["status"] == "waiting" and calls == ["パートの洗い出し"]
    assert steps["frames"]["status"] == "done"
    assert steps["strip"]["status"] == "pending" and steps["strip"]["started"] is None
    assert steps["strip"]["message"] == "パートの選択待ち（TAB あり 3 / 全 4）"
    assert steps["pages"]["status"] == "pending"
    assert len(list_frames(wd)) == 3 and paper.load_parts(wd) == PARTS["parts"]
    assert load_meta(wd)["paper"] == {"part": None, "strings": None}
    log = "\n".join(job.log_tail())
    assert "楽譜で見つかったパート:" in log and "2. Guitar II  TAB あり" in log
    assert pipeline.finished_at(data) is None


def test_waiting_job_continues_from_selection_after_part_is_chosen(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    calls = fake_agents(wd, monkeypatch)
    job = pipeline.Job.create(wd)
    assert not job.run()
    paper.configure(wd, "Guitar II", 6)
    job.reset_from("strip")
    assert job.load()["status"] == "queued"
    assert job.run()
    assert calls == ["パートの洗い出し", "ページとパート", "A"]
    assert job.load()["status"] == "done" and steps_of(job)["strip"]["message"].endswith("指定パートを選びました")
    assert read_json(wd / "pages" / "pages.json")["part"] == "Guitar II"
    assert paper.load_parts(wd) == PARTS["parts"]  # 選んだあとも一覧は残る


def test_waiting_job_lists_parts_again_when_strip_is_rerun_without_part(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    calls = fake_agents(wd, monkeypatch)
    job = pipeline.Job.create(wd)
    assert not job.run()
    job.reset_from("strip")
    assert not job.run()
    assert calls == ["パートの洗い出し"] * 2 and job.load()["status"] == "waiting"


def test_waiting_job_is_not_marked_as_interrupted(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    fake_agents(wd, monkeypatch)
    job = pipeline.Job.create(wd)
    assert not job.run()
    before = job.load()
    pipeline.Job(wd).mark_interrupted()
    pipeline.Job(wd).mark_stopped()
    assert job.load() == before and before["status"] == "waiting"


def test_stopping_during_part_listing_keeps_part_unset(tmp_path, monkeypatch):
    from videotab.cancel import Cancelled

    wd = make_pending(tmp_path)
    def stopped(*args, **kwargs):
        raise Cancelled()
    monkeypatch.setattr(paper, "run_agent", stopped)
    job = pipeline.Job.create(wd)
    assert not job.run()
    assert job.load()["status"] == "stopped" and steps_of(job)["strip"]["status"] == "pending"
    assert load_meta(wd)["paper"] == {"part": None, "strings": None}
    calls = fake_agents(wd, monkeypatch)
    job.reset_from("strip")
    assert not job.run()
    assert calls == ["パートの洗い出し"] and job.load()["status"] == "waiting"


def test_score_without_tab_part_fails_the_step_with_the_reason(tmp_path, monkeypatch):
    wd = make_pending(tmp_path)
    calls = fake_agents(wd, monkeypatch, {"parts": [{"name": "Piano", "tab": False, "strings": None}],
                                          "warnings": ["五線だけの楽譜です"]})
    job = pipeline.Job.create(wd)
    assert not job.run()
    strip = steps_of(job)["strip"]
    assert job.load()["status"] == "failed" and strip["status"] == "failed"
    assert strip["message"] == "TAB のあるパートが見つかりません: 五線だけの楽譜です"
    assert calls == ["パートの洗い出し"] and len(list_frames(wd)) == 3


# --- 画面: パートの選択待ちと選択

def waiting_app(tmp_path, monkeypatch):
    """パートの選択待ちで止まった曲と、順番待ちに入れるだけで実行は始めない画面。"""
    wd = make_pending(tmp_path)
    calls = fake_agents(wd, monkeypatch)
    job = pipeline.Job.create(wd, "codex", choice=Choice("gpt-5", "high"))
    assert not job.run()
    monkeypatch.setattr(server.App, "_worker", lambda *_: None)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    return server.App(wd.parent), wd, job, calls


def test_upload_without_part_is_saved_as_pending_and_checks_strings_only(tmp_path, monkeypatch):
    monkeypatch.setattr(server.App, "_worker", lambda *_: None)
    monkeypatch.setattr(server, "available_engines", lambda: {"claude": True, "codex": True})
    app = server.App(tmp_path / "work")
    raw = image_bytes()
    job_id = app.receive(io.BytesIO(raw), len(raw), "score.png", "codex")
    assert load_meta(app.root / job_id)["paper"] == {"part": None, "strings": None}
    detail = app.detail(job_id)
    assert detail["paper"] == {"part": None, "strings": None} and detail["parts"] == []
    assert detail["steps"][2]["label"] == "ページとパートの選択"
    job_id = app.receive(io.BytesIO(raw), len(raw), "score.png", "codex", paper_mode=True, strings=4)
    assert load_meta(app.root / job_id)["paper"] == {"part": None, "strings": 4}
    job_id = app.receive(io.BytesIO(raw), len(raw), "score.png", "codex", part="Guitar II")
    assert load_meta(app.root / job_id)["paper"] == {"part": "Guitar II", "strings": 6}
    app.check_upload("score.png", len(raw), "codex")
    app.check_upload("score.png", len(raw), "codex", strings=4)
    before = sorted(p.name for p in app.root.iterdir())
    with pytest.raises(ValueError, match="弦数"):
        app.receive(io.BytesIO(raw), len(raw), "score.png", "codex", strings=5)
    assert sorted(p.name for p in app.root.iterdir()) == before


def test_waiting_job_survives_restart_and_detail_lists_found_parts(tmp_path, monkeypatch):
    app, wd, job, _ = waiting_app(tmp_path, monkeypatch)
    assert job.load()["status"] == "waiting"  # 画面を起動しても、中断として記録し直さない
    assert [j["status"] for j in app.list_jobs()] == ["waiting"]
    detail = app.detail(wd.name)
    assert detail["status"] == "waiting" and detail["stopping"] is False and detail["finished"] is None
    assert detail["paper"] == {"part": None, "strings": None}
    assert detail["parts"] == PARTS["parts"]
    assert "合成の結果" in detail["notes"]["パートの洗い出し"]
    assert "パートの洗い出し" in detail["notes_blocks"]
    (wd / "paper" / "parts.json").write_text("{壊れた JSON")
    assert app.detail(wd.name)["parts"] == []
    (wd / "paper" / "parts.json").write_text(json.dumps({"parts": [part_row(name="Guitar\nII")]}))
    assert app.detail(wd.name)["parts"] == []


def test_choosing_listed_part_continues_from_page_selection(tmp_path, monkeypatch):
    app, wd, job, calls = waiting_app(tmp_path, monkeypatch)
    app.choose_part(wd.name, "Bass", 4)
    assert load_meta(wd)["paper"] == {"part": "Bass", "strings": 4}
    data = job.load()
    assert data["status"] == "queued" and app.waiting == [wd.name]
    assert steps_of(job)["frames"]["status"] == "done" and steps_of(job)["strip"]["status"] == "pending"
    assert steps_of(job)["strip"]["message"] is None
    assert data["engine"] == "codex" and data["choice"] == {"model": "gpt-5", "effort": "high"}
    assert app.detail(wd.name)["status"] == "queued"
    with pytest.raises(pipeline.Busy):  # 順番待ちの間は選び直せない
        app.choose_part(wd.name, "Guitar I", 6)
    app.waiting.clear()
    assert pipeline.Job(wd).run()
    assert calls == ["パートの洗い出し", "ページとパート", "A"]
    assert read_json(wd / "pages" / "pages.json")["strings"] == 4
    assert app.detail(wd.name)["parts"] == PARTS["parts"]


@pytest.mark.parametrize("part,strings", [
    ("Drums", 6),  # TAB なし
    ("Guitar 2", 6),  # 一覧に無い綴り
    ("guitar ii", 6),
    (" Guitar II", 6),
    ("", 6),
    (None, 6),
    (["Guitar II"], 6),
    ("Guitar II", 5),
    ("Guitar II", "6"),
    ("Guitar II", None),
    ("Guitar II", True),
])
def test_choosing_part_accepts_only_listed_tab_parts_and_valid_strings(tmp_path, monkeypatch, part, strings):
    app, wd, job, _ = waiting_app(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        app.choose_part(wd.name, part, strings)
    assert load_meta(wd)["paper"] == {"part": None, "strings": None}
    assert job.load()["status"] == "waiting" and app.waiting == []


def test_choosing_part_is_refused_unless_waiting_with_a_valid_list(tmp_path, monkeypatch):
    app, wd, job, _ = waiting_app(tmp_path, monkeypatch)
    with pytest.raises(KeyError):
        app.choose_part("no-such-song", "Guitar II", 6)
    with pipeline.Job(wd).hold():  # videotab run など、別の実行が握っている
        with pytest.raises(pipeline.Busy):
            app.choose_part(wd.name, "Guitar II", 6)
    text = (wd / "paper" / "parts.json").read_text()
    (wd / "paper" / "parts.json").write_text("{壊れた JSON")
    with pytest.raises(ValueError, match="一覧を読めません"):
        app.choose_part(wd.name, "Guitar II", 6)
    (wd / "paper" / "parts.json").write_text(text)
    paper.configure(wd, "Guitar I", 6)  # 選択済みの曲
    with pytest.raises(ValueError, match="選択待ちの曲ではありません"):
        app.choose_part(wd.name, "Guitar II", 6)
    assert load_meta(wd)["paper"] == {"part": "Guitar I", "strings": 6} and app.waiting == []


def test_retrying_waiting_job_lists_parts_again_or_uses_typed_part(tmp_path, monkeypatch):
    app, wd, job, calls = waiting_app(tmp_path, monkeypatch)
    app.retry(wd.name, "strip", None)  # パートを送らない: 洗い出しから
    assert job.load()["status"] == "queued" and load_meta(wd)["paper"]["part"] is None
    app.waiting.clear()
    assert not pipeline.Job(wd).run()
    assert calls == ["パートの洗い出し"] * 2 and job.load()["status"] == "waiting"
    with pytest.raises(ValueError, match="パートを指定"):
        app.retry(wd.name, "strip", None, paper_choice={"part": "", "strings": 6})
    app.retry(wd.name, "read", None, paper_choice={"part": "Guitar III", "strings": 6})  # 一覧に無い名前も入力できる
    assert load_meta(wd)["paper"] == {"part": "Guitar III", "strings": 6}
    assert steps_of(job)["strip"]["status"] == "pending"
    app.waiting.clear()
    assert pipeline.Job(wd).run()
    assert calls[2:] == ["ページとパート", "A"]


def test_chosen_job_keeps_its_part_when_retry_sends_no_part(tmp_path, monkeypatch):
    app, wd, job, calls = waiting_app(tmp_path, monkeypatch)
    app.choose_part(wd.name, "Guitar II", 6)
    app.waiting.clear()
    assert pipeline.Job(wd).run()
    app.retry(wd.name, "strip", None)
    assert load_meta(wd)["paper"] == {"part": "Guitar II", "strings": 6}
    app.waiting.clear()
    assert pipeline.Job(wd).run()
    assert calls == ["パートの洗い出し", "ページとパート", "A", "ページとパート", "A"]


def test_part_is_chosen_through_the_page_api(tmp_path, monkeypatch):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    app, wd, job, _ = waiting_app(tmp_path, monkeypatch)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/jobs/{wd.name}"

    def post(body, header=True):
        req = urllib.request.Request(url + "/part", data=json.dumps(body).encode(), method="POST")
        if header:
            req.add_header("X-Videotab", "1")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        with urllib.request.urlopen(url) as r:
            detail = json.loads(r.read())
        assert detail["status"] == "waiting" and [p["name"] for p in detail["parts"]][:2] == ["Guitar I", "Guitar II"]
        assert post({"part": "Guitar II", "strings": 6}, header=False)[0] == 403
        assert post({"part": "Drums", "strings": 6})[0] == 400
        assert post({"part": "Guitar II"})[0] == 400
        assert post({})[0] == 400
        assert load_meta(wd)["paper"]["part"] is None
        assert post({"part": "Guitar II", "strings": 6}) == (200, {"id": wd.name})
        assert load_meta(wd)["paper"] == {"part": "Guitar II", "strings": 6}
        assert post({"part": "Guitar I", "strings": 6})[0] == 409  # 順番待ちに入った
    finally:
        httpd.shutdown()


# --- コマンド: パートを省くと洗い出して止まり、--part で続ける

def run_cli(wd, *options):
    return cli.main(["run", wd.name, "--root", str(wd.parent), *options])


def test_run_without_part_lists_parts_and_exits_with_wait_code(tmp_path, monkeypatch, capsys):
    wd = make_pending(tmp_path)
    calls = fake_agents(wd, monkeypatch)
    assert pipeline.PART_WAIT_EXIT == 4
    assert run_cli(wd) == 4
    assert calls == ["パートの洗い出し"] and pipeline.Job(wd).load()["status"] == "waiting"
    output = capsys.readouterr().out
    assert "2. Guitar II  TAB あり  6 弦の見込み" in output and "4. Drums      TAB なし（選べません）" in output
    resume = shlex.join(["videotab", "run", wd.name, "--root", str(wd.parent.resolve())])
    assert f"  {resume} --part 'Guitar II'" in output and f"  {resume} --part Bass" in output
    assert "--part Drums" not in output and "途中で止まりました" not in output
    # もう一度実行しても、エージェントを起動せずに一覧を出し直す
    assert run_cli(wd) == 4
    assert calls == ["パートの洗い出し"]
    output = capsys.readouterr().out
    assert "楽譜で見つかったパート:" in output and "1. Guitar I   TAB あり" in output
    assert f"  {resume} --part 'Guitar I'" in output
    # 弦数だけを先に決めても、洗い出しはやり直さない
    assert run_cli(wd, "--strings", "4") == 4
    assert calls == ["パートの洗い出し"] and load_meta(wd)["paper"] == {"part": None, "strings": 4}
    # 洗い出しからやり直すときは --step strip
    assert run_cli(wd, "--step", "strip") == 4
    assert calls == ["パートの洗い出し"] * 2


def test_run_with_part_continues_waiting_job_with_listed_strings(tmp_path, monkeypatch, capsys):
    wd = make_pending(tmp_path)
    calls = fake_agents(wd, monkeypatch)
    assert run_cli(wd) == 4
    capsys.readouterr()
    assert run_cli(wd, "--part", "Bass") == 0  # 弦数は一覧の見込み（4 弦）
    assert load_meta(wd)["paper"] == {"part": "Bass", "strings": 4}
    assert calls == ["パートの洗い出し", "ページとパート", "A"]
    assert pipeline.Job(wd).load()["status"] == "done"
    assert build.load_score(wd)["tuning"] == "g2 d2 a1 e1"
    output = capsys.readouterr().out
    assert "注意" not in output and "できあがり" in output


def test_paper_command_with_part_ends_the_wait_and_run_continues_from_selection(tmp_path, monkeypatch, capsys):
    app, wd, job, calls = waiting_app(tmp_path, monkeypatch)
    waiting = job.load()
    # パートを決めない設定の変更では、選択待ちのまま
    assert cli.main(["paper", str(wd), "--strings", "4"]) == 0
    assert load_meta(wd)["paper"] == {"part": None, "strings": 4} and job.load() == waiting
    assert cli.main(["paper", str(wd), "--part", "Bass"]) == 0
    assert load_meta(wd)["paper"] == {"part": "Bass", "strings": 4}
    data, steps = job.load(), steps_of(job)
    resume = shlex.join(["videotab", "run", wd.name, "--root", str(wd.parent.resolve())])
    assert data["status"] == "stopped" and pipeline.finished_at(data) is None
    assert steps["frames"]["status"] == "done" and steps["strip"]["status"] == "pending"
    assert steps["strip"]["message"] == f"パートを指定しました（{resume} で続ける）"
    assert data["engine"] == "codex" and data["choice"] == {"model": "gpt-5", "effort": "high"}
    # 画面は「パートを選ぶ」を出さず（選択待ちでなく、パートも決まっている）、中断した曲としてやり直せる
    detail = app.detail(wd.name)
    assert detail["status"] == "stopped" and detail["paper"] == {"part": "Bass", "strings": 4}
    assert [j["status"] for j in app.list_jobs()] == ["stopped"]
    assert calls == ["パートの洗い出し"]
    capsys.readouterr()
    assert run_cli(wd) == 0  # --step を付けなくても、ページとパートの選択から続く
    assert calls == ["パートの洗い出し", "ページとパート", "A"]
    assert job.load()["status"] == "done" and read_json(wd / "pages" / "pages.json")["part"] == "Bass"
    assert "楽譜で見つかったパート:" not in capsys.readouterr().out
    # 選択待ちでない曲の記録は書き換えない
    done = job.load()
    assert cli.main(["paper", str(wd), "--part", "Guitar I", "--strings", "6"]) == 0
    assert job.load() == done


def test_paper_command_with_part_does_not_make_a_job_record(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "prepare", lambda _: [])
    wd = make_pending(tmp_path)
    assert cli.main(["paper", str(wd), "--part", "Bass"]) == 0
    assert load_meta(wd)["paper"] == {"part": "Bass", "strings": 6} and not (wd / "job.json").exists()
    # job.json を読めなくても、パートは保存する（記録は次の実行が作り直す）
    paper.configure_pending(wd, None)
    (wd / "job.json").write_text("{壊れた JSON")
    assert cli.main(["paper", str(wd), "--part", "Bass"]) == 0
    assert load_meta(wd)["paper"]["part"] == "Bass" and (wd / "job.json").read_text() == "{壊れた JSON"


@pytest.mark.parametrize("options,strings,note", [
    (["--part", "Guitar II"], 6, None),
    (["--part", "Guitar II", "--strings", "4"], 4, None),  # 明示した弦数が見込みより優先
    (["--part", "Bass", "--strings", "6"], 6, None),
    (["--part", "Guitar 2"], 6, "注意: Guitar 2 は楽譜で見つかったパートの一覧にありません"),
    (["--part", "Guitar 2", "--strings", "4"], 4, "注意: Guitar 2 は楽譜で見つかったパートの一覧にありません"),
    (["--part", "Ukulele"], 6, "注意: Ukulele の弦数が決まっていないので、6 弦として進めます"),
])
def test_run_part_strings_for_waiting_job(tmp_path, monkeypatch, capsys, options, strings, note):
    wd = make_pending(tmp_path)
    write_parts(wd, {"parts": [*PARTS["parts"], {"name": "Ukulele", "tab": True, "strings": None}]})
    pipeline.Job.create(wd)
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    assert run_cli(wd, *options) == 0
    assert load_meta(wd)["paper"] == {"part": options[1], "strings": strings}
    assert steps_of(pipeline.Job(wd))["strip"]["status"] == "pending"
    notes = [line for line in capsys.readouterr().out.splitlines() if line.startswith("注意")]
    assert len(notes) == (1 if note else 0) and all(line.startswith(note) for line in notes)


def test_run_part_keeps_strings_given_at_start_over_listed_guess(tmp_path, monkeypatch, capsys):
    wd = make_pending(tmp_path, strings=4)
    write_parts(wd)
    pipeline.Job.create(wd)
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    assert run_cli(wd, "--part", "Guitar II") == 0
    assert load_meta(wd)["paper"] == {"part": "Guitar II", "strings": 4}
    notes = [line for line in capsys.readouterr().out.splitlines() if line.startswith("注意")]
    assert len(notes) == 1 and "6 弦の見込み" in notes[0] and "4 弦のまま" in notes[0]


def test_run_part_only_keeps_strings_of_chosen_job(tmp_path, monkeypatch, capsys):
    wd, _ = stored_bass(tmp_path)
    write_parts(wd, {"parts": [{"name": "Bass", "tab": True, "strings": 4}, {"name": "Bass II", "tab": True, "strings": 6}]})
    pipeline.Job.create(wd)
    monkeypatch.setattr(pipeline.Job, "run", lambda self: True)
    assert run_cli(wd, "--part", "Bass II") == 0
    assert load_meta(wd)["paper"] == {"part": "Bass II", "strings": 4}  # 見込みが 6 でも、保存済みの弦数のまま
    notes = [line for line in capsys.readouterr().out.splitlines() if line.startswith("注意")]
    assert len(notes) == 1 and "Bass II は 6 弦の見込み" in notes[0] and "--strings 6" in notes[0]
    score = build.load_score(wd)  # パート名が変わるので、チューニングとカポは 4 弦の標準に戻る
    assert (score["tuning"], score["capo"]) == ("g2 d2 a1 e1", 0)
    assert len(list((wd / "history").glob("part-*/score.json"))) == 1
    assert run_cli(wd, "--part", "Bass II", "--strings", "6") == 0
    assert load_meta(wd)["paper"] == {"part": "Bass II", "strings": 6}
    assert not [line for line in capsys.readouterr().out.splitlines() if line.startswith("注意")]


def test_add_and_run_without_part_start_as_pending(tmp_path, monkeypatch, capsys):
    source = tmp_path / "photo.png"
    source.write_bytes(image_bytes())
    root = tmp_path / "scores"

    def added():
        return [load_meta(p)["paper"] for p in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime_ns) if p.is_dir()]

    assert cli.main(["add", str(source), "--root", str(root)]) == 0
    assert cli.main(["add", str(source), "--root", str(root), "--strings", "4"]) == 0
    assert cli.main(["add", str(source), "--root", str(root), "--part", "Guitar II"]) == 0
    assert cli.main(["add", str(source), "--root", str(root), "--part", "Bass", "--strings", "4"]) == 0
    assert sorted(added(), key=json.dumps) == sorted([
        {"part": None, "strings": None}, {"part": None, "strings": 4},
        {"part": "Guitar II", "strings": 6}, {"part": "Bass", "strings": 4},
    ], key=json.dumps)
    # 新しいファイルを通しで実行すると、洗い出して選択待ちで終わる
    capsys.readouterr()
    one_page = {"parts": [{"name": "Guitar II", "tab": True, "strings": 6, "frames": [1]}]}  # 写真は 1 枚
    calls = fake_agents(tmp_path, monkeypatch, one_page)
    fresh = tmp_path / "fresh"
    assert cli.main(["run", str(source), "--root", str(fresh)]) == 4
    wd = next(p for p in fresh.iterdir() if p.is_dir())
    assert calls == ["パートの洗い出し"] and load_meta(wd)["paper"] == {"part": None, "strings": None}
    assert pipeline.Job(wd).load()["status"] == "waiting"
    assert f"{shlex.join(['videotab', 'run', wd.name, '--root', str(fresh.resolve())])} --part 'Guitar II'" in capsys.readouterr().out


def test_page_lists_found_parts_as_text_and_lets_part_be_left_empty():
    from importlib import resources

    page = resources.files("videotab").joinpath("templates", "app.html").read_text(encoding="utf-8")
    start = page.index("// --- 紙の楽譜のパートを選ぶ\n")
    section = page[start:page.index("// ---", start + 1)]
    # 名前はエージェントが楽譜から読んだ文字なので、文字として入れる
    assert "text: p.name" in section and '"/part"' in section and "radio.disabled = !p.tab" in section
    for word in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "eval", "setAttribute"):
        assert word not in section, word
    assert 'waiting: "パートの選択待ち"' in page and ".st-waiting" in page
    # パートが決まっている曲では、状態が選択待ちのままでも選ぶ区画を出さない
    assert 'return d.status === "waiting" && !(d.paper && d.paper.part != null);' in section
    assert "var waiting = choosingPart(d);" in section
    # 新規フォームではパートを聞かない（紙の楽譜はいつも洗い出した一覧から選ぶ）。楽譜の種類も聞かない
    # （動画が画面のタブ譜か紙を撮ったものかは、帯と線の検出の段が見分ける）
    assert 'id="paper-part"' not in page and 'id="paper-hint"' not in page and "fields.part" not in page
    assert 'id="paper-row"' not in page and 'id="source-mode"' not in page and "fields.source_mode" not in page
    assert "showPaperFields" not in page and "isDocument" not in page
    # やり直しの行は、パート欄が空なら paper を送らない。弦数が決まっていない曲の初期値は 6
    assert "if (part) body.paper = { part: part, strings: Number(row.paperStrings.value) };" in page
    assert 'row.paperStrings.value = d.paper.strings === 4 ? "4" : "6";' in page
