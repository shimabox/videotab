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


# --- 楽譜の種類の記録（取り込むとき）

USER_PAPER = {"mode": "paper", "by": "user"}


def meta_of(root):
    (job_id,) = songs(root)
    return json.loads((root / job_id / "meta.json").read_text(encoding="utf-8"))


def test_video_without_paper_flag_records_no_source_choice(tmp_path, fake_probe, seen_runs):
    for n, command in enumerate(("add", "run")):
        root = tmp_path / f"work{n}"
        assert cli.main([command, str(video(tmp_path)), "--root", str(root)]) == 0
        meta = meta_of(root)
        assert "source_choice" not in meta and "paper" not in meta  # strip の段が自動で見分ける


@pytest.mark.parametrize("command", ["add", "run"])
@pytest.mark.parametrize("options, paper", [
    ([], {"part": None, "strings": None}),
    (["--strings", "4"], {"part": None, "strings": 4}),
    (["--part", "Guitar I"], {"part": "Guitar I", "strings": 6}),
])  # fmt: skip
def test_paper_flag_records_that_user_chose_paper(tmp_path, fake_probe, seen_runs, command, options, paper):
    root = tmp_path / "work"
    assert cli.main([command, str(video(tmp_path)), "--root", str(root), "--paper", *options]) == 0
    meta = meta_of(root)
    assert meta["paper"] == paper and meta["source_choice"] == USER_PAPER
    assert "strip" not in meta


@pytest.mark.parametrize("command", ["add", "run"])
def test_documents_are_recorded_as_paper_chosen_by_user(tmp_path, seen_runs, command):
    from PIL import Image

    photo = tmp_path / "photo.png"
    Image.new("RGB", (40, 30), "white").save(photo)
    root = tmp_path / "work"
    assert cli.main([command, str(photo), "--root", str(root)]) == 0
    meta = meta_of(root)
    assert meta["source_kind"] == "document" and meta["source_choice"] == USER_PAPER
    assert meta["paper"] == {"part": None, "strings": None}


def test_paper_command_records_user_choice_only_when_song_becomes_paper(tmp_path, monkeypatch, fake_probe):
    from videotab import paper

    monkeypatch.setattr(paper, "prepare", lambda _: [])
    root = tmp_path / "work"
    assert cli.main(["add", str(video(tmp_path)), "--root", str(root)]) == 0
    (job_id,) = songs(root)
    assert cli.main(["paper", str(root / job_id), "--part", "Bass", "--strings", "4"]) == 0
    assert (meta_of(root)["paper"], meta_of(root)["source_choice"]) == ({"part": "Bass", "strings": 4}, USER_PAPER)
    # 自動で紙と見分けた曲のパートを決めても、見分けた記録は書き換えない
    auto = {"mode": "paper", "by": "auto", "tab_frames": [7, 24], "located_frames": [25, 97]}
    path = root / job_id / "meta.json"
    path.write_text(json.dumps({**meta_of(root), "paper": {"part": None, "strings": None}, "source_choice": auto}))
    assert cli.main(["paper", str(root / job_id), "--part", "Guitar II"]) == 0
    assert (meta_of(root)["paper"], meta_of(root)["source_choice"]) == ({"part": "Guitar II", "strings": 6}, auto)
    # source_choice の無い、これまでの紙の曲には足さない
    meta = meta_of(root)
    del meta["source_choice"]
    path.write_text(json.dumps(meta))
    assert cli.main(["paper", str(root / job_id), "--part", "Guitar I"]) == 0
    assert "source_choice" not in meta_of(root)


# --- 楽譜の種類の切り替え（取り込み済みの曲）

USER_SCREEN = {"mode": "video", "by": "user"}
AUTO_SCREEN = {"mode": "video", "by": "auto", "tab_frames": [22, 24]}
AUTO_PAPER = {"mode": "paper", "by": "auto", "tab_frames": [7, 24], "located_frames": [25, 97]}


def added_song(tmp_path, *options, **meta):
    """取り込み済みで、全段が済んだ曲の作業フォルダ。meta の項目を meta.json に足す。"""
    root = tmp_path / "work"
    assert cli.main(["add", str(video(tmp_path)), "--root", str(root), *options]) == 0
    (job_id,) = songs(root)
    wd = root / job_id
    (wd / "meta.json").write_text(json.dumps({**meta_of(root), **meta}), encoding="utf-8")
    job = pipeline.Job.create(wd)
    data = job.load()
    for s in data["steps"]:
        s["status"] = "done"
    data["status"] = "done"
    job.save(data)
    return wd


def run_song(wd, *options):
    return cli.main(["run", wd.name, "--root", str(wd.parent), *options])


def statuses(data):
    return {s["name"]: s["status"] for s in data["steps"]}


def test_paper_and_screen_flags_cannot_be_combined(tmp_path, fake_probe, seen_runs):
    wd = added_song(tmp_path)
    before = (wd / "meta.json").read_bytes()
    with pytest.raises(SystemExit, match="--paper と --screen は同時に指定できません"):
        run_song(wd, "--step", "strip", "--paper", "--screen")
    for options in (["--part", "Guitar I"], ["--strings", "4"]):
        with pytest.raises(SystemExit, match="--screen と一緒には指定できません"):
            run_song(wd, "--step", "strip", "--screen", *options)
    assert (wd / "meta.json").read_bytes() == before and seen_runs == []


def test_screen_flag_is_refused_for_a_new_file(tmp_path, fake_probe, seen_runs):
    with pytest.raises(SystemExit, match="--screen は既存の作業フォルダの ID"):
        cli.main(["run", str(video(tmp_path)), "--root", str(tmp_path / "work"), "--screen"])
    assert songs(tmp_path / "work") == [] and fake_probe == [] and seen_runs == []


@pytest.mark.parametrize("step", [["--step", "strip"], []])
def test_paper_flag_switches_screen_song_to_paper_and_restarts_from_strip(tmp_path, fake_probe, seen_runs, step):
    wd = added_song(tmp_path, strip={"band": [240, 350]}, source_choice=AUTO_SCREEN)
    (wd / "strip").mkdir()
    (wd / "strip" / "check_0001.png").write_bytes(b"old")
    before = (wd / "meta.json").read_bytes()
    with pytest.raises(SystemExit, match="楽譜の種類を変えるときは --step strip からやり直してください"):
        run_song(wd, "--step", "read", "--paper")
    assert (wd / "meta.json").read_bytes() == before and (wd / "strip").exists() and seen_runs == []

    assert run_song(wd, *step, "--paper") == 0
    meta = meta_of(wd.parent)
    assert meta["paper"] == {"part": None, "strings": None} and meta["source_choice"] == USER_PAPER
    assert "strip" not in meta and not (wd / "strip").exists()
    assert statuses(seen_runs[-1][1]) == {"add": "done", "frames": "done", "strip": "pending", "pages": "pending",
                                          "read": "pending", "build": "pending", "verify": "pending"}  # fmt: skip


@pytest.mark.parametrize("choice", [AUTO_PAPER, USER_PAPER, None])
def test_screen_flag_switches_paper_song_to_screen_and_restarts_from_strip(tmp_path, fake_probe, seen_runs, choice):
    wd = added_song(tmp_path, paper={"part": None, "strings": None}, **({"source_choice": choice} if choice else {}))
    (wd / "paper").mkdir()
    (wd / "paper" / "parts.json").write_text("{}")
    assert pipeline.Job(wd).load()["source_mode"] == "paper"
    before = (wd / "meta.json").read_bytes()
    with pytest.raises(SystemExit, match="楽譜の種類を変えるときは --step strip からやり直してください"):
        run_song(wd, "--step", "pages", "--screen")
    with pytest.raises(SystemExit, match="推論の強さ"):
        run_song(wd, "--screen", "--effort", "minimal")  # 使えない指定では、切り替える前に断る
    assert (wd / "meta.json").read_bytes() == before and seen_runs == []

    assert run_song(wd, "--screen") == 0  # --step を省いても、帯と線の検出からやり直す
    meta = meta_of(wd.parent)
    assert "paper" not in meta and meta["source_choice"] == USER_SCREEN
    assert (wd / "paper" / "parts.json").exists()  # 紙の楽譜に戻したとき、洗い出しをやり直すので残してよい
    data = seen_runs[-1][1]
    assert "source_mode" not in data and statuses(data)["frames"] == "done"
    assert [statuses(data)[n] for n in ("strip", "pages", "read", "build", "verify")] == ["pending"] * 5


def test_same_kind_flag_changes_nothing(tmp_path, fake_probe, seen_runs, capsys):
    wd = added_song(tmp_path, strip={"band": [240, 350]}, source_choice=AUTO_SCREEN)
    before = (wd / "meta.json").read_bytes()
    assert run_song(wd, "--screen") == 0  # すでに画面のタブ譜
    assert "できあがっています" in capsys.readouterr().out
    assert (wd / "meta.json").read_bytes() == before and seen_runs == []
    assert run_song(wd, "--step", "read", "--screen") == 0  # やり直し自体は、指定の段から行う
    assert (wd / "meta.json").read_bytes() == before
    assert statuses(seen_runs[-1][1])["strip"] == "done" and statuses(seen_runs[-1][1])["read"] == "pending"

    (wd / "meta.json").write_text(json.dumps({"id": wd.name, "paper": {"part": None, "strings": None},
                                              "source_choice": AUTO_PAPER}), encoding="utf-8")  # fmt: skip
    before = (wd / "meta.json").read_bytes()
    assert run_song(wd, "--step", "strip", "--paper") == 0  # すでに紙の楽譜
    assert (wd / "meta.json").read_bytes() == before  # 見分けた記録のまま


def test_screen_flag_is_refused_for_documents(tmp_path, seen_runs):
    from PIL import Image

    photo = tmp_path / "photo.png"
    Image.new("RGB", (40, 30), "white").save(photo)
    root = tmp_path / "work"
    assert cli.main(["add", str(photo), "--root", str(root)]) == 0
    (job_id,) = songs(root)
    before = (root / job_id / "meta.json").read_bytes()
    with pytest.raises(SystemExit, match="写真・PDF・ZIP の曲は、紙の楽譜としてだけ扱えます"):
        cli.main(["run", job_id, "--root", str(root), "--step", "strip", "--screen"])
    assert (root / job_id / "meta.json").read_bytes() == before and seen_runs == []


def test_screen_flag_resets_tuning_of_the_chosen_paper_part(tmp_path, fake_probe, seen_runs):
    wd = added_song(tmp_path, paper={"part": "Bass", "strings": 4}, source_choice=USER_PAPER)
    score = {"title": "曲", "tempo": 96, "time_signature": [3, 4], "tuning": "g2 d2 a1 e1", "capo": 2}
    (wd / "score.json").write_text(json.dumps(score), encoding="utf-8")
    assert run_song(wd, "--step", "strip", "--screen") == 0
    new = json.loads((wd / "score.json").read_text(encoding="utf-8"))
    assert new == {**score, "tuning": "e4 b3 g3 d3 a2 e2", "capo": 0}  # ベースのチューニングを持ち越さない
    (saved,) = (wd / "history").glob("part-*/score.json")
    assert json.loads(saved.read_text(encoding="utf-8")) == score
    assert json.loads((saved.parent / "paper.json").read_text(encoding="utf-8")) == {"part": "Bass", "strings": 4}


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
