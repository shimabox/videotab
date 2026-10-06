import json

import numpy as np
import pytest
from synth import BAND, TAB_LINES, digit_frame, frame, video_noise, write_frames

from PIL import Image, ImageDraw, UnidentifiedImageError

from videotab import cli, pages, strip
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


def test_pages_do_not_keep_every_frame_image(tmp_path, monkeypatch):
    # 長い高解像度の動画でメモリが足りなくならないよう、読んだ画像はフレームごとに手放す
    import gc
    import weakref

    seq = [frame(page, cursor_x=40 + 110 * k, rng_seed=page * 5 + k) for page in range(2) for k in range(5)]
    write_frames(tmp_path, seq)
    frames, _, setup = detect_setup(tmp_path)
    alive = []
    held = []
    original = pages.band_rgb

    def tracked(frame, setup):
        gc.collect()
        held.append(sum(r() is not None for r in alive))
        rgb = original(frame, setup)
        alive.append(weakref.ref(rgb.base if rgb.base is not None else rgb))
        return rgb

    monkeypatch.setattr(pages, "band_rgb", tracked)
    det = pages.detect_pages(frames, setup)
    assert starts(det) == [1, 6]
    assert max(held) <= 1  # 次のフレームを読む時点で残っているのは、直前の 1 枚まで


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


def digit_setup(dark=False, scale=1):
    return pages.Setup(
        tuple(y * scale for y in BAND), 1 if dark else -1,
        (60, 60, 60) if dark else (252, 252, 252),
        [[y * scale for y in TAB_LINES]],
    )


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scale", [1, 2])
def test_pages_split_same_layout_digit_changes(tmp_path, dark, scale):
    seq = [digit_frame(digit, dark=dark) for digit in "333355558888"]
    if scale != 1:
        seq = [np.asarray(Image.fromarray(im).resize((640 * scale, 360 * scale), Image.Resampling.NEAREST)) for im in seq]
    write_frames(tmp_path, seq)
    setup = digit_setup(dark, scale)
    frames = list_frames(tmp_path)
    det = pages.detect_pages(frames, setup)
    # Issue #10: 帯全体の差は約 0.005 で、全体しきい値も横スクロールの下限も下回る。
    assert 0 < det.diffs[4] < 0.008 < det.threshold
    assert starts(det) == [1, 5, 9]
    assert [p.pick.index for p in det.pages] == [3, 7, 11]
    # 手動指定はこれまでと同じ意味のままにする。
    assert starts(pages.detect_pages(frames, setup, threshold=0.02)) == [1]


@pytest.mark.parametrize("cursor_width", [1, 2])
def test_pages_ignore_cursor_but_keep_digit_changes(tmp_path, cursor_width):
    seq = [
        digit_frame(digit, cursor_x=40 + 110 * (i % 4), cursor_width=cursor_width)
        for i, digit in enumerate("333355558888")
    ]
    write_frames(tmp_path, seq)
    assert starts(pages.detect_pages(list_frames(tmp_path), digit_setup())) == [1, 5, 9]


def test_pages_split_when_only_one_fret_digit_changes(tmp_path):
    a = digit_frame("3")
    b = a.copy()
    b[270:282, 34:46] = digit_frame("5")[270:282, 34:46]
    write_frames(tmp_path, [a] * 4 + [b] * 4)
    det = pages.detect_pages(list_frames(tmp_path), digit_setup())
    assert 0 < det.diffs[4] < 0.001
    assert starts(det) == [1, 5]


@pytest.mark.parametrize("dark", [False, True])
def test_pages_split_thin_stroke_digit_changes(tmp_path, dark):
    write_frames(tmp_path, [digit_frame(digit, dark=dark) for digit in "66668888"])
    assert starts(pages.detect_pages(list_frames(tmp_path), digit_setup(dark))) == [1, 5]


@pytest.mark.parametrize("dark", [False, True])
def test_pages_split_digits_with_gaps_in_dark_staff_lines(tmp_path, dark):
    seq = []
    for digit in "33335555":
        orig = digit_frame(digit, dark=dark)
        im = orig.copy()
        for y in TAB_LINES:
            im[y, 10:-10] = 235 if dark else 20
        # 濃い弦線を数字の背景だけ白抜きする。弦線の各端点をカーソルの角と混同しない。
        for x in range(40, 620, 45):
            for y in TAB_LINES[::2]:
                im[y - 6 : y + 6, x - 6 : x + 6] = orig[y - 6 : y + 6, x - 6 : x + 6]
        seq.append(im)
    write_frames(tmp_path, seq)
    assert starts(pages.detect_pages(list_frames(tmp_path), digit_setup(dark))) == [1, 5]


@pytest.mark.parametrize("rounded,width,height", [(False, 60, 48), (True, 60, 48), (True, 30, 32)])
def test_pages_ignore_cursor_pauses_corners_and_small_gaps(tmp_path, rounded, width, height):
    seq = []
    for i in range(12):
        base = digit_frame("3")
        im = Image.fromarray(base)
        draw = ImageDraw.Draw(im)
        x = 50 + 110 * (i // 4)
        if rounded:
            draw.rounded_rectangle((x, 270, x + width, 270 + height), radius=8, outline=(20,) * 3)
        else:
            draw.rectangle((x, 270, x + width, 270 + height), outline=(20,) * 3)
        arr = np.array(im)
        if not rounded:
            for y in range(275, 315, 10):
                arr[y, [x, x + width]] = base[y, [x, x + width]]
        seq.append(arr)
    write_frames(tmp_path, seq)
    assert starts(pages.detect_pages(list_frames(tmp_path), digit_setup())) == [1]


def test_pages_ignore_scattered_ink_noise_and_note_highlights(tmp_path):
    rng = np.random.default_rng(10)
    base = digit_frame("3")
    seq = []
    for _ in range(12):
        im = base.copy()
        # 二値化の境界をまたぐ散在ノイズと、演奏中の数字の色替え。
        noisy = rng.random(im[BAND[0] : BAND[1]].shape[:2]) < 0.004
        im[BAND[0] : BAND[1]][noisy] = 180
        ink = (im[:, :, 0] < 100) & (np.indices(im.shape[:2])[0] >= BAND[0])
        im[ink] = (180, 20, 20)
        seq.append(im)
    write_frames(tmp_path, seq)
    assert starts(pages.detect_pages(list_frames(tmp_path), digit_setup())) == [1]


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("digit,color", [
    ("3", (180, 20, 20)), ("8", (180, 20, 20)),
    ("9", (255, 200, 0)), ("3", (255, 0, 0)),
])
def test_pages_ignore_single_note_highlight(tmp_path, dark, scale, digit, color):
    base = digit_frame(digit, dark=dark)
    highlighted = digit_frame(digit, dark=dark, highlight_color=color)
    seq = [base] * 4 + [highlighted] * 4 + [base] * 4
    if scale != 1:
        seq = [np.asarray(Image.fromarray(im).resize((640 * scale, 360 * scale), Image.Resampling.NEAREST)) for im in seq]
    write_frames(tmp_path, seq)
    frames, _, setup = detect_setup(tmp_path)
    det = pages.detect_pages(frames, setup)
    assert starts(det) == [1]
    assert det.no_tab == []
    assert det.dropped == []


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("starts_colored", [False, True])
@pytest.mark.parametrize("scale", [1, 2])
def test_pages_ignore_highlight_when_no_other_digits_remain(tmp_path, dark, starts_colored, scale):
    normal = digit_frame("3", dark=dark, sparse=True)
    colored = digit_frame("3", dark=dark, sparse=True, highlight_color=(180, 20, 20))
    a, b = (colored, normal) if starts_colored else (normal, colored)
    seq = [a] * 4 + [b] * 4 + [a] * 4
    if scale != 1:
        seq = [np.asarray(Image.fromarray(im).resize((640 * scale, 360 * scale), Image.Resampling.NEAREST)) for im in seq]
    write_frames(tmp_path, seq)
    frames, _, setup = detect_setup(tmp_path)
    det = pages.detect_pages(frames, setup)
    assert starts(det) == [1]
    assert det.dropped == []


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("first_fade", [False, True])
def test_pages_ignore_highlight_on_later_sparse_page(tmp_path, dark, scale, first_fade):
    a = digit_frame("3", dark=dark, sparse=True)
    b = digit_frame("8", dark=dark, sparse=True)
    colored_b = digit_frame("8", dark=dark, sparse=True, highlight_color=(180, 20, 20))
    seq = [a] * 4 + [b] * 4 + [colored_b] * 4 + [b] * 4
    if first_fade:
        bg = 60 if dark else 252
        seq.insert(0, ((a.astype(float) + bg) / 2).astype(np.uint8))
    if scale != 1:
        seq = [np.asarray(Image.fromarray(im).resize((640 * scale, 360 * scale), Image.Resampling.NEAREST)) for im in seq]
    write_frames(tmp_path, seq)
    frames, _, setup = detect_setup(tmp_path)
    det = pages.detect_pages(frames, setup)
    # 先頭の薄い字画の扱いによらず、後のページは色替え前から最後まで 1 ページにする。
    assert det.pages[-1].frames[0].index == (6 if first_fade else 5)
    assert det.pages[-1].frames[-1].index == len(seq)
    if not first_fade:
        assert starts(det) == [1, 5]
    assert det.dropped == []


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("scale", [1, 2])
@pytest.mark.parametrize("digits", [("3", "5"), ("6", "8")])
@pytest.mark.parametrize("color", [(180, 20, 20), (255, 200, 0)])
def test_pages_split_single_digit_change_while_highlighted(tmp_path, dark, scale, digits, color):
    first, second = digits
    a = digit_frame(first, dark=dark)
    b = a.copy()
    b[270:282, 34:46] = digit_frame(second, dark=dark)[270:282, 34:46]
    colored_a = digit_frame(first, dark=dark, highlight_color=color)
    colored_b = b.copy()
    colored_b[270:282, 34:46] = digit_frame(second, dark=dark, highlight_color=color)[270:282, 34:46]
    seq = [colored_a] * 4 + [colored_b] * 4 + [b] * 4 + [colored_b] * 4
    if scale != 1:
        seq = [np.asarray(Image.fromarray(im).resize((640 * scale, 360 * scale), Image.Resampling.NEAREST)) for im in seq]
    write_frames(tmp_path, seq)
    det = pages.detect_pages(list_frames(tmp_path), digit_setup(dark, scale))
    assert starts(det) == [1, 5]
    assert det.dropped == []


def test_pages_ignore_noise_at_ink_threshold(tmp_path):
    rng = np.random.default_rng(20)
    base = digit_frame("3").astype(float)
    base[BAND[0] : BAND[1]][base[BAND[0] : BAND[1]] < 100] = 192
    seq = [np.clip(base + rng.normal(0, 2, base.shape), 0, 255).astype(np.uint8) for _ in range(12)]
    write_frames(tmp_path, seq)
    assert starts(pages.detect_pages(list_frames(tmp_path), digit_setup())) == [1]


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("threshold", [None, 0.001])
def test_pages_keep_one_frame_digit_change_and_last_page(tmp_path, dark, threshold):
    write_frames(tmp_path, [digit_frame(digit, dark=dark) for digit in "3333588889"])
    det = pages.detect_pages(list_frames(tmp_path), digit_setup(dark), threshold=threshold)
    assert starts(det) == [1, 5, 6, 10]
    assert det.dropped == []


@pytest.mark.parametrize("noise", [0, 0.5, 1.5])
@pytest.mark.parametrize("dark", [False, True])
def test_pages_drop_crossfade_between_similar_digits(tmp_path, noise, dark):
    a, b = digit_frame("3", dark=dark), digit_frame("5", dark=dark)
    blend = np.maximum(a, b) if dark else np.minimum(a, b)
    if noise:
        rng = np.random.default_rng(4)
        blend = np.clip(blend.astype(float) + rng.normal(0, noise, blend.shape), 0, 255).astype(np.uint8)
    write_frames(tmp_path, [a] * 4 + [blend] + [b] * 4)
    det = pages.detect_pages(list_frames(tmp_path), digit_setup(dark))
    assert starts(det) == [1, 6]
    assert [f.index for f in det.dropped] == [5]


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("weight", [0.25, 0.5, 0.75])
@pytest.mark.parametrize("noise", [0, 1.5])
def test_pages_drop_weighted_crossfade_between_similar_digits(tmp_path, dark, weight, noise):
    a, b = digit_frame("3", dark=dark), digit_frame("5", dark=dark)
    blend = a.astype(float) * (1 - weight) + b.astype(float) * weight
    if noise:
        blend += np.random.default_rng(4).normal(0, noise, blend.shape)
    blend = np.clip(blend, 0, 255).astype(np.uint8)
    write_frames(tmp_path, [a] * 4 + [blend] + [b] * 4)
    det = pages.detect_pages(list_frames(tmp_path), digit_setup(dark))
    assert starts(det) == [1, 6]
    assert [f.index for f in det.dropped] == [5]


# --- ページごとに段が上下に動く動画


def shifted_video(dys, per=5, staff_moves=False, cursor=False, **kw):
    """ページごとに dy だけ段がずれる画面の列と、フレームごとの正解の dy（タブのない画面は None）。

    dys の要素が None なら、そのページはタブのない映像だけの画面にする。staff_moves なら五線も
    一緒に動く。cursor なら演奏位置の枠がページの中で右へ進む。
    """
    seq, truth = [], []
    for page, dy in enumerate(dys):
        for k in range(per):
            n = len(seq)
            if dy is None:
                seq.append(video_noise(np.random.default_rng(100 + n)).astype(np.uint8))
            else:
                extra = {"cursor_x": 40 + 150 * k} if cursor else {}
                seq.append(frame(page, dy=dy, staff_dy=dy if staff_moves else 0, rng_seed=n, **extra, **kw))
            truth.append(dy)
    return seq, truth


def shift_at(shifts, index):
    """フレーム番号 index の dy（shifts がなければ 0）。"""
    return next((d for a, b, d in shifts if a <= index <= b), 0.0)


def assert_positions(r, truth, lines=(TAB_LINES,)):
    """基準 + dy が、タブの見えるフレームごとの正解の線と ±1px で合う。区間は全フレームを隙間なく覆う。"""
    shifts = getattr(r, "shifts", [])
    assert len(r.staves) == len(lines)
    for i, dy in enumerate(truth, start=1):
        if dy is None:
            continue
        got = shift_at(shifts, i)
        for st, want in zip(r.staves, lines):
            assert np.allclose(np.array(st.lines) + got, np.array(want) + dy, atol=1.0), (i, dy, got)
    assert shifts[0][0] == 1 and shifts[-1][1] == len(truth)
    assert all(b + 1 == a for (_, b, _), (a, _, _) in zip(shifts, shifts[1:]))
    # 基準は観測された位置のひとつ
    seen = {dy for dy in truth if dy is not None}
    assert any(np.allclose(r.staves[0].lines, np.array(lines[0]) + dy, atol=1.0) for dy in seen)


def assert_band_holds_lines(r, truth, lines=(TAB_LINES,)):
    dys = [dy for dy in truth if dy is not None]
    y0, y1 = r.band
    assert y0 <= min(lines[0]) + min(dys) and y1 > max(lines[-1]) + max(dys)


def stacked_staves(seq, polarity=-1):
    """変更前の detect と同じく、全フレームの線の強さを重ねて探した組。"""
    grays = [strip.to_gray(a.astype(np.float32)) for a in seq]
    strengths = np.stack([strip.line_strength(g, polarity) for g in grays])
    return strip.find_staves(strip.find_peaks(np.quantile(strengths, 0.67, axis=0)), seq[0].shape[0])


@pytest.mark.parametrize("dark", [False, True])
def test_strip_follows_staff_moving_page_by_page(tmp_path, dark):
    # どの位置も全体の 1/3 未満。ちょうど 1 間隔（7px）のずれと端数のずれを含む
    seq, truth = shifted_video([0, 7, 3, 18, 11], dark=dark)
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert r.polarity == (1 if dark else -1)
    assert_positions(r, truth)
    assert_band_holds_lines(r, truth)
    assert len(r.shifts) == 5
    assert r.frames_with_tab == r.frames_used


def test_strip_moving_staff_when_stacking_makes_a_false_staff(tmp_path):
    seq, truth = shifted_video([0, 3, 7, 10], per=6)
    # 前提: 重ねると、間隔 7px でない偽の組ができる
    assert not any(abs(st.spacing - 7) < 0.7 for st in stacked_staves(seq))
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert_positions(r, truth)
    assert np.isclose(r.staves[0].spacing, 7, atol=0.5)


def test_strip_moving_staff_when_one_frame_shows_an_extra_staff(tmp_path):
    seq, truth = shifted_video([0, 3, 7, 10], per=6)
    # 1 枚だけ、同じ間隔の 6 本組がもう 1 組見つかり、そのフレームだけ 2 段になる
    seq[2] = frame(0, second=-120, rng_seed=2)
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert r.shifts
    assert_positions(r, truth)
    assert np.isclose(r.staves[0].spacing, 7, atol=0.5)


def test_strip_moving_staff_when_stacking_makes_two_staves(tmp_path):
    # タブだけの画面で、段が 60px 動く。重ねると 1 段の動画が 2 段に見える
    seq, truth = shifted_video([0, -60, 0, -60], per=6, staff=False)
    assert len(stacked_staves(seq)) == 2
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert_positions(r, truth)
    assert_band_holds_lines(r, truth)


def test_strip_moving_staff_and_staff_notation_by_60px(tmp_path):
    # 五線も一緒に 60px 動く。重ねると、線を寄せ集めた間隔の広い偽の組ができる
    seq, truth = shifted_video([0, -60, 0, -60], per=6, staff_moves=True)
    assert not any(abs(st.spacing - 7) < 0.7 for st in stacked_staves(seq))
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert_positions(r, truth)
    assert_band_holds_lines(r, truth)


def test_strip_moving_staff_with_one_dominant_position(tmp_path):
    # 0px の位置が半分を占める（重ねても見つかる）が、ほかのページで動く
    seq, truth = shifted_video([0, 4, 0, 10], per=6)
    stacked = stacked_staves(seq)
    assert len(stacked) == 1 and np.allclose(stacked[0].lines, TAB_LINES, atol=1.0)
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert_positions(r, truth)
    assert [(a, b) for a, b, _ in r.shifts] == [(1, 6), (7, 12), (13, 18), (19, 24)]
    assert np.allclose([d for _, _, d in r.shifts], [0, 4, 0, 10], atol=1.0)


def test_strip_moving_staff_with_intro_outro_and_cursor(tmp_path):
    seq, truth = shifted_video([None, 0, 7, 14, None], per=4, cursor=True)
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert_positions(r, truth)
    assert r.frames_with_tab == 12  # 演奏位置の枠があるフレームも位置が出る。前奏と終わりは出ない
    assert r.shifts[0][0] == 1 and r.shifts[-1][1] == 20


@pytest.mark.parametrize("staff_moves", [False, True])
def test_strip_moving_tab_does_not_take_the_staff(tmp_path, staff_moves):
    seq, truth = shifted_video([0, 4, 9, 15], staff_moves=staff_moves)
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert_positions(r, truth)
    assert np.isclose(r.staves[0].spacing, 7, atol=0.5)


def test_strip_moving_staff_with_band_option(tmp_path):
    seq, truth = shifted_video([0, 7, 3, 18, 11])
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path), band=(240, 345))
    assert r.band == (240, 345)
    assert_positions(r, truth)


@pytest.mark.parametrize("dark", [False, True])
def test_strip_fixed_staff_has_no_shifts(tmp_path, dark):
    write_frames(tmp_path, [frame(k // 3, dark=dark, rng_seed=k) for k in range(30)])
    r = strip.detect(list_frames(tmp_path))
    assert r.shifts == []
    assert np.allclose(r.staves[0].lines, TAB_LINES, atol=1.0)
    assert r.frames_with_tab == r.frames_used == 24


def test_strip_two_staves_fixed_and_moving_together(tmp_path):
    second = [y - 120 for y in TAB_LINES]
    write_frames(tmp_path, [frame(k // 3, second=-120, rng_seed=k) for k in range(9)])
    r = strip.detect(list_frames(tmp_path))
    assert len(r.staves) == 2 and r.shifts == []
    assert np.allclose(r.staves[0].lines, second, atol=1.0)
    assert np.allclose(r.staves[1].lines, TAB_LINES, atol=1.0)

    moving = tmp_path / "moving"
    seq, truth = shifted_video([0, 5, 11, 16], per=5, second=-120)
    write_frames(moving, seq)
    r = strip.detect(list_frames(moving))
    assert_positions(r, truth, lines=(second, TAB_LINES))
    assert_band_holds_lines(r, truth, lines=(second, TAB_LINES))


def test_strip_video_only_with_many_frames_raises_no_tab_found(tmp_path):
    rng = np.random.default_rng(1)
    write_frames(tmp_path, [video_noise(rng).astype(np.uint8) for _ in range(30)])
    with pytest.raises(strip.NoTabFound):
        strip.detect(list_frames(tmp_path))


def test_segments_absorb_only_runs_of_one_frame():
    frames = list(range(1, 10))
    # 位置の分からないフレームは直前の区間に入る。フレーム 3〜5 の 3 枚の区間は吸収しない
    assert strip._segments(frames, [0, 0, 4, None, None, 0, 0, 10, 10], 1.05) == [
        (1, 2, 0.0),
        (3, 5, 4.0),
        (6, 7, 0.0),
        (8, 9, 10.0),
    ]
    # 同じ位置に挟まれた 1 フレームだけの区間は吸収する
    located = [0, 0, 4, 0, 0, 10, 10]
    assert strip._segments(frames[:7], located, 1.05) == [(1, 5, 0.0), (6, 7, 10.0)]
    # そのフレームが両隣の位置より自分の位置によく当てはまるなら残す
    seen = []
    keep = strip._segments(frames[:7], located, 1.05, lambda pos, d: seen.append((pos, d)) or True)
    assert keep == [(1, 2, 0.0), (3, 3, 4.0), (4, 5, 0.0), (6, 7, 10.0)]
    assert seen == [(2, 0.0)]


def test_strip_keeps_one_frame_page_at_its_own_position(tmp_path):
    # 繰り返しの 1 番かっこへ飛ぶ所などで、1 フレームだけ別のページが別の位置に出る
    truth = [0] * 4 + [11] + [0] * 4
    seeds = [0] * 4 + [1] + [2] * 4
    seq = [frame(s, dy=dy, cursor_x=40 + 60 * n, rng_seed=n) for n, (s, dy) in enumerate(zip(seeds, truth))]
    write_frames(tmp_path, seq)
    frames, r, setup = moving_setup(tmp_path)
    assert_positions(r, truth)
    assert [(a, b) for a, b, _ in r.shifts] == [(1, 4), (5, 5), (6, 9)]
    det = pages.detect_pages(frames, setup)
    assert det.no_tab == []
    assert starts(det) == [1, 5, 6]


def test_strip_moving_staff_with_extra_line_and_wider_spacing(tmp_path):
    # あるページだけ、タブの線の間隔が基準より 2% ほど広く、1 弦の 1 間隔上に同じ間隔の線がもう 1 本ある。
    # 基準の間隔で当てると、外側の線（下の 6 弦）が予測の位置から許容差より離れ、1 本上にずれた組が勝ちやすい
    spacing = 7 * 1.022
    wide = [276.6 + spacing * m for m in range(6)]
    seq, truth = [], []
    for page, (dy, lines) in enumerate([(0, TAB_LINES), (7, TAB_LINES), (4, wide), (12, TAB_LINES)]):
        extra = (wide[0] - spacing,) if lines is wide else ()
        for k in range(4):
            n = len(seq)
            seq.append(frame(page, dy=dy, tab_lines=lines, extra_lines=extra, staff=False, rng_seed=n))
            truth.append([y + dy for y in lines])
    write_frames(tmp_path, seq)
    r = strip.detect(list_frames(tmp_path))
    assert len(r.staves) == 1 and np.isclose(r.staves[0].spacing, 7, atol=0.5)
    for i, want in enumerate(truth, start=1):
        got = np.array(r.staves[0].lines) + shift_at(r.shifts, i)
        assert np.allclose(got, want, atol=1.0), (i, got, want)


# --- 段が動く動画の pages・zoom・cmd_strip


def moving_workdir(tmp_path, intro=2, name="moving"):
    """前奏 intro 枚・4 ページ（中身が同じで位置だけ違うページを含む）・終わり 2 枚の動画。

    戻り値は作業フォルダ、フレームごとの正解の dy（タブのない画面は None）。
    """
    wd = tmp_path / name
    seeds, dys = [0, 1, 1, 2], [0, 7, 3, 18]
    seq = [video_noise(np.random.default_rng(50 + k)).astype(np.uint8) for k in range(intro)]
    truth = [None] * intro
    for seed, dy in zip(seeds, dys):
        for k in range(4):
            seq.append(frame(seed, dy=dy, cursor_x=40 + 150 * k, rng_seed=len(seq)))
            truth.append(dy)
    seq += [video_noise(np.random.default_rng(60 + k)).astype(np.uint8) for k in range(2)]
    truth += [None, None]
    write_frames(wd, seq)
    return wd, truth


def moving_setup(workdir):
    frames = list_frames(workdir)
    r = strip.detect(frames)
    shifts = [list(s) for s in r.shifts]
    return frames, r, pages.Setup(r.band, r.polarity, r.background, [s.lines for s in r.staves], shifts)


def marks(im, x, colors):
    """画像の x 列で、印の色が続く行の真ん中を上から順に返す。"""
    px = np.asarray(im.convert("RGB"))[:, x]
    on = np.array([tuple(int(v) for v in p) in colors for p in px])
    padded = np.concatenate([[False], on, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    return [(a + b - 1) / 2 for a, b in zip(edges[::2], edges[1::2])]


PAGE_MARKS = {(255, 210, 0), (0, 220, 255)}
CHECK_MARKS = {(0, 200, 0), (0, 120, 255)}


def zoomed(y, band, scale):
    return round((y - band[0] + 0.5) * scale - 0.5, 1)


def test_pages_follow_moving_staff(tmp_path):
    wd, truth = moving_workdir(tmp_path)
    frames, r, setup = moving_setup(wd)
    assert setup.shifts
    det = pages.detect_pages(frames, setup)
    assert [f.index for f in det.no_tab] == [1, 2, 19, 20]  # タブのあるフレームは no_tab に入らない
    assert starts(det) == [3, 7, 11, 15]  # 中身が同じで位置だけ違うページ（7 と 11）も区切る
    # 差がしきい値以下でも、位置の変わる所では必ず区切る
    assert starts(pages.detect_pages(frames, setup, threshold=1.0)) == [3, 7, 11, 15]


def test_write_pages_marks_each_page_at_its_own_lines(tmp_path):
    from PIL import Image

    wd, truth = moving_workdir(tmp_path)
    frames, r, setup = moving_setup(wd)
    det = pages.detect_pages(frames, setup)
    out = pages.write_pages(wd, frames, setup, det)
    data = json.loads((out / "pages.json").read_text(encoding="utf-8"))
    scale, band = data["scale"], data["band"]
    assert data["lines"] == pages.layout(setup, 640).lines  # 最上位は基準の位置のまま
    for p in data["pages"]:
        dy = truth[p["pick"] - 1]
        want = [zoomed(y + dy, band, scale) for y in TAB_LINES]
        assert np.allclose(p["lines"][0], want, atol=scale), (p["page"], p["lines"], want)
        im = Image.open(out / p["images"][0])
        assert np.allclose(marks(im, pages.MARGIN - 5, PAGE_MARKS), want, atol=scale), p["page"]
    index = (out / "index.md").read_text(encoding="utf-8")
    assert "| ページ | 時刻 | フレーム | 画像 | 弦の線（1〜6 弦の y） | 注意 |" in index
    assert "弦の線の位置はページによって違う" in index
    row = next(line for line in index.splitlines() if line.startswith("| 4 |"))
    assert ", ".join(f"{y:g}" for y in data["pages"][3]["lines"][0]) in row

    # 動かない動画では、各ページの lines は基準と同じで、一覧は今までどおり
    fixed = tmp_path / "fixed"
    write_frames(fixed, [frame(k // 3, rng_seed=k) for k in range(6)])
    frames, _, setup = detect_setup(fixed)
    out = pages.write_pages(fixed, frames, setup, pages.detect_pages(frames, setup))
    data = json.loads((out / "pages.json").read_text(encoding="utf-8"))
    assert all(p["lines"] == data["lines"] for p in data["pages"])
    index = (out / "index.md").read_text(encoding="utf-8")
    assert "| ページ | 時刻 | フレーム | 画像 | 注意 |" in index and "弦の線（" not in index


def test_zoom_frame_marks_lines_of_that_frame(tmp_path):
    from PIL import Image

    wd, truth = moving_workdir(tmp_path)
    frames, r, setup = moving_setup(wd)
    scale = pages.layout(setup, 640).scale
    for f in (frames[2], frames[16]):  # 位置 0 と 18 のフレーム
        paths = pages.zoom_frame(wd, f, setup)
        want = [zoomed(y + truth[f.index - 1], setup.band, scale) for y in TAB_LINES]
        assert np.allclose(marks(Image.open(paths[0]), pages.MARGIN - 5, PAGE_MARKS), want, atol=scale), f.index


def test_setup_from_meta_shifts(tmp_path):
    strip_meta = {"band": [240, 350], "polarity": -1, "background": [252, 252, 252], "staves": [TAB_LINES]}
    setup = pages.Setup.from_meta({"strip": strip_meta})
    assert setup.shifts == [] and setup.dy(5) == 0.0

    write_frames(tmp_path, [frame(0, rng_seed=k) for k in range(6)])
    frames = list_frames(tmp_path)
    moving = dict(strip_meta, shifts=[[1, 3, 0.0], [4, 6, 7.0]])
    setup = pages.Setup.from_meta({"strip": moving}, frames)
    assert [setup.dy(i) for i in range(1, 7)] == [0, 0, 0, 7, 7, 7]
    # 画像を作り直してフレーム数が変わったら、strip のやり直しを求める
    write_frames(tmp_path, [frame(0, rng_seed=k) for k in range(8)])
    with pytest.raises(SystemExit, match="strip をやり直してください"):
        pages.Setup.from_meta({"strip": moving}, list_frames(tmp_path))
    (tmp_path / "meta.json").write_text(json.dumps({"id": "x", "strip": moving}), encoding="utf-8")
    for argv in (["pages", str(tmp_path)], ["zoom", str(tmp_path), "1"]):
        with pytest.raises(SystemExit, match="strip をやり直してください"):
            cli.main(argv)


def test_cmd_strip_writes_shifts_only_for_moving_staff(tmp_path, capsys):
    from PIL import Image

    wd, truth = moving_workdir(tmp_path)
    assert cli.main(["strip", str(wd)]) == 0
    printed = capsys.readouterr().out
    meta = json.loads((wd / "meta.json").read_text(encoding="utf-8"))["strip"]
    shifts = meta["shifts"]
    assert shifts[0][0] == 1 and shifts[-1][1] == len(truth)
    assert list(meta) == ["band", "polarity", "background", "staves", "shifts"]
    assert "線の位置はページによって上下に動きます: 1 弦 y=" in printed and "タブ 1 段目（基準）" in printed
    # 確認用の画像は、いちばん上と下の位置のフレームを含み、印はその画像のフレームの線の上にある
    checks = sorted((wd / "strip").glob("check_*.png"))
    picked = [int(p.stem.split("_")[1]) for p in checks]
    assert {truth[i - 1] for i in picked} >= {0, 18}
    for p, i in zip(checks, picked):
        want = [y + truth[i - 1] for y in TAB_LINES]
        assert np.allclose(marks(Image.open(p), 2, CHECK_MARKS), want, atol=1.0), p.name
    assert cli.main(["pages", str(wd)]) == 0 and cli.main(["zoom", str(wd), "17"]) == 0

    # 前奏が長く、前奏が最初の区間の大半を占めても、確認用の画像はタブの見えるフレームから選ぶ
    long_intro, truth = moving_workdir(tmp_path, intro=8, name="long_intro")
    assert cli.main(["strip", str(long_intro)]) == 0
    picked = [int(p.stem.split("_")[1]) for p in sorted((long_intro / "strip").glob("check_*.png"))]
    assert all(truth[i - 1] is not None for i in picked)

    fixed = tmp_path / "fixed"
    write_frames(fixed, [frame(k // 3, rng_seed=k) for k in range(6)])
    capsys.readouterr()
    assert cli.main(["strip", str(fixed)]) == 0
    printed = capsys.readouterr().out
    assert "shifts" not in json.loads((fixed / "meta.json").read_text(encoding="utf-8"))["strip"]
    assert "動きます" not in printed and "基準" not in printed


def test_frames_are_opened_only_as_png_or_jpeg(tmp_path):
    # 名前が .png でも、中身が別の形式（EPS など）のファイルは開かない
    eps = tmp_path / "0001_00m00s000.png"
    eps.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n%%EndComments\nshowpage\n")
    assert Image.open(eps).format == "EPS"  # 形式を限らなければ、Pillow は EPS として開く
    with pytest.raises(UnidentifiedImageError):
        strip.load_rgb(eps)
    for fmt, name in (("PNG", "a.png"), ("JPEG", "b.jpg")):
        Image.new("RGB", (4, 3), (10, 20, 30)).save(tmp_path / name, format=fmt)
        assert strip.load_rgb(tmp_path / name).shape == (3, 4, 3)
