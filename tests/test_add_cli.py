"""videotab add / videotab run FILE|ID: 取り込み、対象の決め方、続行の案内。"""

import json
import shlex

import pytest
from test_links import fake_ffmpeg

from videotab import cli, frames, pipeline
from videotab.workdir import ID_PATTERN


def video(tmp_path, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"video")
    return path


def songs(root):
    return sorted(p.name for p in root.iterdir() if ID_PATTERN.fullmatch(p.name)) if root.exists() else []


def printed_command(out, head):
    """出力の中の「head <コマンド>」の行から、videotab の引数を取り出す。"""
    line = next(line for line in out.splitlines() if line.startswith(head))
    words = shlex.split(line.removeprefix(head))
    assert words[0] == "videotab"
    return words[1:]


class Seen(list):
    ok = True


@pytest.fixture
def seen_runs(monkeypatch):
    """Job.run を、実行した作業フォルダと job.json を記録して seen.ok を返す偽物にする。"""
    seen = Seen()
    seen.ok = True

    def fake_run(self):
        seen.append((self.workdir, self.load()))
        return seen.ok

    monkeypatch.setattr(pipeline.Job, "run", fake_run)
    return seen


# --- add


def test_add_prints_workdir_and_frames_command_with_path(tmp_path, monkeypatch, capsys, fake_probe):
    root = tmp_path / "elsewhere" / "songs"  # 既定でなく、まだ無い置き場
    assert cli.main(["add", str(video(tmp_path)), "--root", str(root), "--title", "曲", "--creator", "作成者",
                     "--source-url", "https://example.com/v"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    (job_id,) = songs(root)
    wd = root.resolve() / job_id
    assert f"ID: {job_id}" in out and str(wd) in out
    args = printed_command(out, "次: ")
    assert args == ["frames", str(wd)]  # ID ではなく作業フォルダのパス
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))
    assert (meta["title"], meta["creator"], meta["source_url"]) == ("曲", "作成者", "https://example.com/v")

    # 表示されたコマンドをそのまま（--root なしで、別の場所から）実行すると、同じ作業フォルダで続けられる
    monkeypatch.chdir(tmp_path)
    fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(frames, "ffmpeg_bin", lambda: "ffmpeg")
    assert cli.main(args) == 0
    assert len(list((wd / "frames").iterdir())) == 3
    assert not (tmp_path / "work").exists()


def test_add_refuses_non_video_with_message(tmp_path, fake_probe):
    with pytest.raises(SystemExit, match="動画ファイル"):
        cli.main(["add", str(video(tmp_path, "notes.txt")), "--root", str(tmp_path / "work")])
    with pytest.raises(SystemExit, match="https:// で始まる"):
        cli.main(["add", str(video(tmp_path)), "--root", str(tmp_path / "work"), "--source-url", "http://x"])
    assert songs(tmp_path / "work") == []


def test_add_into_default_work_folder_that_does_not_exist_yet(tmp_path, monkeypatch, capsys, fake_probe):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["add", str(video(tmp_path))]) == 0
    assert len(songs(tmp_path / "work")) == 1


@pytest.mark.parametrize("name", ["練習.mp4", "-x.mp4"])
def test_ids_from_unusual_names_work_as_command_arguments(tmp_path, monkeypatch, capsys, fake_probe, seen_runs, name):
    monkeypatch.chdir(tmp_path)
    src = video(tmp_path, name)
    assert cli.main(["add", f"./{name}"]) == 0  # - で始まる名前はパスで渡す
    (job_id,) = songs(tmp_path / "work")
    assert job_id[0].isascii() and job_id[0].isalnum() and ID_PATTERN.fullmatch(job_id)
    assert src.exists()

    fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(frames, "ffmpeg_bin", lambda: "ffmpeg")
    assert cli.main(["frames", job_id]) == 0  # ID を引数に渡しても、オプションと紛れない
    assert cli.main(["run", job_id]) == 0
    assert seen_runs[-1][0] == (tmp_path / "work" / job_id).resolve()


# --- run


def test_run_file_adds_and_creates_job(tmp_path, capsys, fake_probe, seen_runs):
    root = tmp_path / "new-root"  # まだ無い置き場
    assert cli.main(["run", str(video(tmp_path)), "--root", str(root), "--engine", "codex",
                     "--creator", "作成者"]) == 0  # fmt: skip
    (job_id,) = songs(root)
    wd, data = seen_runs[0]
    assert wd == root.resolve() / job_id and (wd / "video.mp4").read_bytes() == b"video"
    assert data["engine"] == "codex" and "url" not in data
    assert [s["status"] for s in data["steps"]] == ["pending"] * 7 and data["steps"][0]["name"] == "add"
    assert json.loads((wd / "meta.json").read_text(encoding="utf-8"))["creator"] == "作成者"
    assert "取り込みました" in capsys.readouterr().out


def test_run_id_resumes_from_where_it_stopped(tmp_path, capsys, fake_probe, seen_runs):
    root = tmp_path / "work"
    seen_runs.ok = False
    assert cli.main(["run", str(video(tmp_path)), "--root", str(root)]) == 1
    (job_id,) = songs(root)
    job = pipeline.Job(root / job_id)
    data = job.load()
    for s in data["steps"][:3]:
        s["status"] = "done"
    data["steps"][3]["status"] = "failed"
    data["status"] = "failed"
    job.save(data)
    seen_runs.ok = True
    assert cli.main(["run", job_id, "--root", str(root)]) == 0
    _, resumed = seen_runs[-1]
    assert [s["status"] for s in resumed["steps"][:4]] == ["done", "done", "done", "pending"]
    assert songs(root) == [job_id]  # 同じ曲を続ける


def test_run_after_add_uses_the_same_folder(tmp_path, capsys, fake_probe, seen_runs):
    root = tmp_path / "work"
    assert cli.main(["add", str(video(tmp_path)), "--root", str(root)]) == 0
    (job_id,) = songs(root)
    assert cli.main(["run", job_id, "--root", str(root)]) == 0
    assert seen_runs[0][0] == (root / job_id).resolve() and songs(root) == [job_id]


@pytest.mark.parametrize("root_arg", [None, "custom/place"])
def test_stopped_run_prints_resume_command_that_continues_the_same_song(tmp_path, monkeypatch, capsys, fake_probe,
                                                                       seen_runs, root_arg):  # fmt: skip
    monkeypatch.chdir(tmp_path)
    seen_runs.ok = False
    extra = ["--root", root_arg] if root_arg else []
    assert cli.main(["run", str(video(tmp_path)), *extra]) == 1
    out = capsys.readouterr().out
    args = printed_command(out, "続きは ")
    root = (tmp_path / (root_arg or "work")).resolve()
    (job_id,) = songs(root)
    if root_arg:
        assert args == ["run", job_id, "--root", str(root)]
    else:
        assert args == ["run", job_id]  # 既定の work/ なら --root を付けない
    seen_runs.ok = True
    assert cli.main(args) == 0
    assert seen_runs[-1][0] == root / job_id and songs(root) == [job_id]


def test_run_refuses_unknown_target(tmp_path):
    with pytest.raises(SystemExit, match="^動画ファイルか、既存の作業フォルダの ID を指定してください$"):
        cli.main(["run", "nothing-here", "--root", str(tmp_path / "work")])
    with pytest.raises(SystemExit, match="動画ファイルか"):
        cli.main(["run", "../escape", "--root", str(tmp_path / "work")])


def test_run_new_file_with_step_is_an_error(tmp_path, fake_probe, seen_runs):
    with pytest.raises(SystemExit, match="--step"):
        cli.main(["run", str(video(tmp_path)), "--root", str(tmp_path / "work"), "--step", "read"])
    assert songs(tmp_path / "work") == [] and fake_probe == [] and seen_runs == []


@pytest.mark.parametrize("flag", ["--title", "--creator", "--source-url"])
def test_run_existing_id_with_video_fields_is_an_error(tmp_path, fake_probe, seen_runs, flag):
    root = tmp_path / "work"
    assert cli.main(["add", str(video(tmp_path)), "--root", str(root)]) == 0
    (job_id,) = songs(root)
    before = (root / job_id / "meta.json").read_bytes()
    with pytest.raises(SystemExit, match=flag):
        cli.main(["run", job_id, "--root", str(root), flag, "https://example.com/x"])
    assert (root / job_id / "meta.json").read_bytes() == before and seen_runs == []
    assert not (root / job_id / "job.json").exists()


def test_run_checks_choice_before_adding(tmp_path, fake_probe, seen_runs):
    with pytest.raises(SystemExit, match="モデルと推論の強さ"):
        cli.main(["run", str(video(tmp_path)), "--root", str(tmp_path / "work"), "--effort", "nope"])
    assert songs(tmp_path / "work") == [] and fake_probe == []


def test_run_refuses_non_video_with_message(tmp_path, seen_runs, monkeypatch):
    from videotab import add

    def broken(path):
        raise add.NotVideo("映像の入った動画として読めません")

    monkeypatch.setattr(add, "probe", broken)
    with pytest.raises(SystemExit, match="^映像の入った動画として読めません$"):
        cli.main(["run", str(video(tmp_path)), "--root", str(tmp_path / "work")])
    assert songs(tmp_path / "work") == [] and seen_runs == []


def test_step_choices_start_with_add(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "x", "--step", "nope"])
    assert "choose from add, frames," in capsys.readouterr().err.replace("'", "")


def test_version_option_prints_package_version(capsys):
    import videotab

    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"videotab {videotab.__version__}"
    assert videotab.__version__ != "0.0.0"  # pyproject.toml の version を読めている
