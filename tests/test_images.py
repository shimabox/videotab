import numpy as np
import pytest
from synth import BAND, TAB_LINES, frame, video_noise, write_frames

from videotab import pages, strip
from videotab.workdir import list_frames


def detect_setup(workdir):
    frames = list_frames(workdir)
    r = strip.detect(frames)
    return frames, r, pages.Setup(r.band, r.polarity, r.background, [s.lines for s in r.staves])


@pytest.mark.parametrize("dark", [False, True])
def test_strip_finds_faint_tab_lines_and_skips_staff(tmp_path, dark):
    write_frames(tmp_path, [frame(k, dark=dark, rng_seed=k) for k in range(6)])
    _, r, _ = detect_setup(tmp_path)
    assert r.polarity == (1 if dark else -1)
    assert len(r.staves) == 1  # 五線（5 本）はタブにしない
    assert np.allclose(r.staves[0].lines, TAB_LINES, atol=1.0)
    y0, y1 = r.band
    assert y0 <= BAND[0] + 3 and y1 >= BAND[1] - 3
    assert y0 >= BAND[0] - 6 and y1 <= BAND[1] + 6


def test_strip_band_option_limits_search(tmp_path):
    write_frames(tmp_path, [frame(k, rng_seed=k) for k in range(4)])
    r = strip.detect(list_frames(tmp_path), band=(200, 340))
    assert r.band == (200, 340)
    with pytest.raises(SystemExit) as e:
        strip.detect(list_frames(tmp_path), band=(0, 100))
    assert not isinstance(e.value, strip.NoTabFound)  # 手で決めた帯の外れは「タブ譜なし」にしない


def test_strip_without_tab_raises_no_tab_found(tmp_path):
    rng = np.random.default_rng(0)
    write_frames(tmp_path, [video_noise(rng).astype(np.uint8) for _ in range(4)])
    with pytest.raises(strip.NoTabFound, match="タブ譜が写っていないかもしれません"):
        strip.detect(list_frames(tmp_path))


def starts(det):
    return [p.frames[0].index for p in det.pages]


def test_pages_ignore_cursor_and_split_page_flips(tmp_path):
    seq = []
    for page in range(3):
        for k in range(5):
            seq.append(frame(page, cursor_x=40 + 110 * k, rng_seed=len(seq)))
    write_frames(tmp_path, seq)
    frames, _, setup = detect_setup(tmp_path)
    det = pages.detect_pages(frames, setup)
    assert starts(det) == [1, 6, 11]
    assert det.pages[0].pick.index == 3


def test_pages_split_small_horizontal_scroll(tmp_path):
    seq = [frame(7, shift=0, cursor_x=60 + 100 * k, rng_seed=k) for k in range(4)]
    seq += [frame(7, shift=90, cursor_x=60 + 100 * k, rng_seed=10 + k) for k in range(4)]
    write_frames(tmp_path, seq)
    frames, _, setup = detect_setup(tmp_path)
    det = pages.detect_pages(frames, setup)
    assert starts(det) == [1, 5]


def test_pages_crossfade_is_not_its_own_page_but_single_scroll_frame_is(tmp_path):
    a = [frame(1, rng_seed=k) for k in range(3)]
    b = [frame(2, rng_seed=10 + k) for k in range(3)]
    # 前後の中身のインクを両方残した「切り替え途中」の画面
    blend = a[0].copy()
    both = (a[0] < 128) | (b[0] < 128)
    blend[BAND[0] : BAND[1]][both[BAND[0] : BAND[1]]] = 20
    scrolled = frame(2, shift=150, rng_seed=30)  # 1 枚だけ出た横スクロールの途中の画面
    c = [frame(3, rng_seed=20 + k) for k in range(3)]
    write_frames(tmp_path, a + [blend] + b + [scrolled] + c)
    frames, _, setup = detect_setup(tmp_path)
    det = pages.detect_pages(frames, setup)
    assert starts(det) == [1, 5, 8, 9]


def test_is_blend():
    rng = np.random.default_rng(0)
    before = rng.random((40, 200)) < 0.1
    after = rng.random((40, 200)) < 0.1
    assert pages._is_blend(before | after, before, after)
    other = rng.random((40, 200)) < 0.1
    assert not pages._is_blend(other, before, after)
    # 前のページから音が減っただけの本物のページは重ね合わせではない
    fewer = before & (rng.random((40, 200)) < 0.7)
    assert not pages._is_blend(fewer, before, after)


def test_pages_skip_frames_without_tab(tmp_path):
    rng = np.random.default_rng(0)
    intro = [(rng.integers(0, 255, size=(360, 640, 3))).astype(np.uint8) for _ in range(2)]
    seq = [frame(1, rng_seed=k) for k in range(6)]
    write_frames(tmp_path, seq)
    frames, _, setup = detect_setup(tmp_path)
    write_frames(tmp_path, intro + seq)  # 帯の検出はタブのある画面で済ませておく
    det = pages.detect_pages(list_frames(tmp_path), setup)
    assert [f.index for f in det.no_tab] == [1, 2]
    assert starts(det) == [3]


def test_write_pages_outputs_zoomed_images_with_line_positions(tmp_path):
    write_frames(tmp_path, [frame(0, rng_seed=k) for k in range(3)] + [frame(1, rng_seed=9 + k) for k in range(3)])
    frames, _, setup = detect_setup(tmp_path)
    det = pages.detect_pages(frames, setup)
    out = pages.write_pages(tmp_path, frames, setup, det)
    data = __import__("json").loads((out / "pages.json").read_text(encoding="utf-8"))
    assert len(data["pages"]) == 2
    scale = data["scale"]
    assert 4.0 <= scale <= 4.5  # 間隔 7px → 約 30px
    spacing = np.diff(data["lines"][0])
    assert np.allclose(spacing, 7 * scale, atol=1.0)
    from PIL import Image

    im = Image.open(out / data["pages"][0]["images"][0])
    assert im.height == round((setup.band[1] - setup.band[0]) * scale)
    assert "p001_a.png" in (out / "index.md").read_text(encoding="utf-8")


def test_auto_threshold_sits_in_gap():
    noise = np.full(50, 0.004)
    flips = np.array([0.08, 0.09, 0.1])
    thr = pages.auto_threshold(np.concatenate([noise, flips]))
    assert 0.012 <= thr < 0.08
