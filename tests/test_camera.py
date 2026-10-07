import json

import numpy as np
import pytest
from PIL import Image, ImageDraw
from synth import TAB_LINES, camera_frame, frame, video_noise, write_frames

from videotab import camera, cli, pages, strip
from videotab.workdir import list_frames


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("slope", [-0.013, 0, 0.009])
def test_faint_camera_lines_with_uneven_lighting(tmp_path, dark, slope):
    write_frames(tmp_path, [camera_frame(slope=slope, dy=d, dark=dark, rng_seed=i)
                            for i, d in enumerate([0, 2, -2])])
    result = strip.detect(list_frames(tmp_path))
    assert result.polarity == (1 if dark else -1)
    assert len(result.staves) == 1
    assert len(result.corrections) == 3
    assert all(row[3] for row in result.corrections)
    for (index, dy, found_slope, _, scale), actual_dy in zip(result.corrections, [0, 2, -2]):
        assert abs(found_slope - slope) < 0.002
        assert np.allclose(np.array(result.staves[0].lines) * scale + dy, np.array(TAB_LINES) + actual_dy, atol=1)


def test_camera_pages_and_zoom_use_the_same_correction(tmp_path, capsys):
    seq = [camera_frame(i // 4, dy=d, slope=s, rng_seed=i)
           for i, (d, s) in enumerate(zip([0, 2, -2, 1, 3, -1, 2, 0],
                                         [0.009, 0.011, 0.008, 0.01] * 2))]
    write_frames(tmp_path, seq)
    assert cli.main(["strip", str(tmp_path)]) == 0
    assert "画面録画" in capsys.readouterr().out
    meta = json.loads((tmp_path / "meta.json").read_text())
    frames = list_frames(tmp_path)
    setup = pages.Setup.from_meta(meta, frames)
    det = pages.detect_pages(frames, setup)
    assert not det.no_tab
    assert [p.frames[0].index for p in det.pages] == [1, 5]
    out = pages.write_pages(tmp_path, frames, setup, det)
    data = json.loads((out / "pages.json").read_text())
    assert data["camera_corrected"]
    assert "画面録画" in (out / "index.md").read_text()
    for p in det.pages:
        paths = pages.zoom_frame(tmp_path, p.pick, setup)
        for zoom, name in zip(paths, p.images):
            assert np.array_equal(np.asarray(Image.open(zoom)), np.asarray(Image.open(out / name)))
        # 拡大画像の印が、その横片の両端の線の位置と一致する。
        rgb = setup.correct(strip.load_rgb(p.pick.path), p.pick.index)
        for x in [80, 560]:
            profile = camera.strength(strip.to_gray(rgb[:, x-30:x+30]), setup.polarity)
            for y in setup.staves[0]:
                row = int(round(y))
                assert profile[row-1:row+2].max() > 1.5


def test_camera_meta_rejects_changed_frames(tmp_path):
    write_frames(tmp_path, [camera_frame(rng_seed=i) for i in range(2)])
    cli.main(["strip", str(tmp_path)])
    meta = json.loads((tmp_path / "meta.json").read_text())
    write_frames(tmp_path, [camera_frame(rng_seed=i) for i in range(3)])
    with pytest.raises(SystemExit, match="strip をやり直してください"):
        pages.Setup.from_meta(meta, list_frames(tmp_path))


def test_camera_fallback_rejects_noise_and_five_lines(tmp_path):
    rng = np.random.default_rng(4)
    five = Image.new("RGB", (640, 360), (240,)*3)
    draw = ImageDraw.Draw(five)
    for y in TAB_LINES[:5]:
        draw.line((10, y - 3, 630, y + 3), fill=(234,)*3)
    for seq in ([video_noise(rng).astype(np.uint8) for _ in range(2)], [np.asarray(five)] * 2):
        write_frames(tmp_path, seq)
        with pytest.raises(strip.NoTabFound):
            strip.detect(list_frames(tmp_path))


def test_normal_recording_keeps_existing_metadata(tmp_path):
    write_frames(tmp_path, [frame(0, rng_seed=i) for i in range(3)])
    result = strip.detect(list_frames(tmp_path))
    assert not result.corrections


def test_camera_720p_and_manual_band(tmp_path):
    seq = [camera_frame(slope=0.009, dy=d, rng_seed=i) for i, d in enumerate([0, 3])]
    seq = [np.asarray(Image.fromarray(a).resize((1280, 720), Image.Resampling.BILINEAR)) for a in seq]
    write_frames(tmp_path, seq)
    result = strip.detect(list_frames(tmp_path), band=(470, 710))
    assert result.band == (470, 710)
    assert len(result.corrections) == 2
    for row, d in zip(result.corrections, [0, 3]):
        assert row[3] and abs(row[2] - 0.009) < 0.002
        assert np.allclose(np.array(result.staves[0].lines) * row[4] + row[1],
                           np.array(TAB_LINES) * 2 + 0.5 + d * 2, atol=1)
    with pytest.raises(SystemExit, match="帯の指定を見直してください"):
        strip.detect(list_frames(tmp_path), band=(0, 100))


def test_camera_tracks_unsampled_frames_and_skips_intro(tmp_path):
    rng = np.random.default_rng(15)
    noise = video_noise(rng).astype(np.uint8)
    write_frames(tmp_path, [noise] + [camera_frame(dy=d, slope=s, rng_seed=i)
                                    for i, (d, s) in enumerate(zip([0, 3, -3, 2],
                                                                  [0.01, -0.009, 0.02, 0.008]))] + [noise])
    frames = list_frames(tmp_path)
    result = strip.detect(frames, n_samples=4)
    assert [row[3] for row in result.corrections] == [False, True, True, True, True, False]
    setup = pages.Setup(result.band, result.polarity, result.background, [s.lines for s in result.staves],
                        corrections={i: (d, s, v, scale) for i, d, s, v, scale in result.corrections})
    det = pages.detect_pages(frames, setup)
    assert [f.index for f in det.no_tab] == [1, 6]
    assert len(det.pages) == 1


def test_camera_distance_changes_keep_all_bars_and_line_marks(tmp_path):
    scales = [1, 1.17, 0.88, 1.1, 1, 0.9, 1.15, 1]
    write_frames(tmp_path, [camera_frame(i // 4, scale=s, dy=i % 3, rng_seed=i)
                            for i, s in enumerate(scales)])
    cli.main(["strip", str(tmp_path)])
    frames = list_frames(tmp_path)
    setup = pages.Setup.from_meta(json.loads((tmp_path / "meta.json").read_text()), frames)
    assert all(row[2] for row in setup.corrections.values())
    assert max(row[3] for row in setup.corrections.values()) - min(row[3] for row in setup.corrections.values()) > 0.2
    det = pages.detect_pages(frames, setup)
    assert not det.no_tab
    assert any(p.frames[0].index == 5 for p in det.pages)
    for f in frames:
        rgb = setup.correct(strip.load_rgb(f.path), f.index)
        profile = camera.strength(strip.to_gray(rgb), setup.polarity)
        for y in setup.staves[0]:
            row = int(round(y))
            assert profile[row-1:row+2].max() > 1.5


def test_legacy_camera_metadata_defaults_to_unit_scale(tmp_path):
    write_frames(tmp_path, [camera_frame(rng_seed=i) for i in range(2)])
    cli.main(["strip", str(tmp_path)])
    meta = json.loads((tmp_path / "meta.json").read_text())
    for row in meta["strip"]["corrections"]:
        del row[4:]
    setup = pages.Setup.from_meta(meta, list_frames(tmp_path))
    assert all(row[3] == 1 for row in setup.corrections.values())


def test_camera_prefers_tab_over_staff_and_beam_at_another_slope():
    im = Image.new("RGB", (640, 360), (240,) * 3)
    draw = ImageDraw.Draw(im)
    # 五線と長い連桁が濃い 6 本の候補になる。遠近の歪みでタブとは傾きが異なる。
    for y in [240, 244, 248, 252, 256]:
        draw.line((10, y - 3, 630, y + 3), fill=(190,) * 3)
    draw.line((60, 257, 580, 263), fill=(190,) * 3)
    for y in TAB_LINES:
        draw.line((10, y - 8, 630, y + 8), fill=(234,) * 3)
    result = camera.find_frame(np.asarray(im).astype(np.float32))
    assert result is not None and result[0] == -1
    assert len(result[1]) == 1
    assert np.allclose(result[1][0].lines, TAB_LINES, atol=1)
    assert abs(result[2] - 16 / 620) < 0.003
