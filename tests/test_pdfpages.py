"""PDF のページを画像にする子プロセス（pdfpages）。PDF はテストの中で合成する。"""

import io
import subprocess
import sys

import pytest
from PIL import Image

from videotab import pdfpages
from pdfs import image_pdf, locked_pdf, page_pdf

PNG = io.BytesIO()
Image.new("RGB", (30, 40), "white").save(PNG, "PNG")


def render(tmp_path, data, max_pages=200, long_edge=2400):
    src = tmp_path / "source.pdf"
    src.write_bytes(data)
    out = tmp_path / "out"
    out.mkdir()
    return pdfpages.render(src, out, max_pages, long_edge), out


def names(out):
    return sorted(p.name for p in out.iterdir())


def test_pages_are_written_in_order_as_rgb_at_the_long_edge(tmp_path):
    code, out = render(tmp_path, image_pdf([1, 2, 10]))
    assert code == pdfpages.OK
    assert names(out) == ["page-1.png", "page-2.png", "page-3.png"]
    ims = [Image.open(out / f"page-{n}.png") for n in (1, 2, 3)]
    assert [(im.mode, im.size) for im in ims] == [("RGB", (1800, 2400))] * 3
    assert [im.getpixel((900, 1200)) for im in ims] == [(1, 1, 1), (2, 2, 2), (10, 10, 10)]


def test_long_edge_is_exact_when_the_scale_does_not_divide_evenly(tmp_path):
    # 2400 / 577 * 577 は誤差で 2400 をわずかに超える。切り上げで 2401 にならないこと。
    code, out = render(tmp_path, page_pdf([(577, 433, 0)]))
    assert code == pdfpages.OK
    im = Image.open(out / "page-1.png")
    assert im.width == 2400 and abs(im.height - 433 / 577 * 2400) <= 1
    # 向きはそのまま: 左半分が黒、右半分が白。
    assert im.getpixel((600, 900)) == (0, 0, 0) and im.getpixel((1800, 900)) == (255, 255, 255)


def test_rotated_page_is_rendered_upright_at_the_long_edge(tmp_path):
    code, out = render(tmp_path, page_pdf([(577, 433, 90)]))
    assert code == pdfpages.OK
    im = Image.open(out / "page-1.png")
    assert im.height == 2400 and abs(im.width - 433 / 577 * 2400) <= 1
    # 時計回りに 90 度回るので、回す前の左半分（黒）が上に来る。
    assert im.getpixel((900, 600)) == (0, 0, 0) and im.getpixel((900, 1800)) == (255, 255, 255)


def test_each_page_gets_its_own_scale(tmp_path):
    code, out = render(tmp_path, page_pdf([(30, 40, 0), (30000, 10000, 0), (2, 1, 0)]), long_edge=600)
    assert code == pdfpages.OK
    assert [Image.open(out / f"page-{n}.png").size for n in (1, 2, 3)] == [(450, 600), (600, 200), (600, 300)]


def test_no_pages_is_unreadable(tmp_path):
    code, out = render(tmp_path, page_pdf([]))
    assert code == pdfpages.UNREADABLE and names(out) == []


def test_too_many_pages_are_refused_without_rendering(tmp_path):
    code, out = render(tmp_path, image_pdf([1, 2, 10]), max_pages=2)
    assert code == pdfpages.TOO_MANY and names(out) == []
    assert render(tmp_path / "out", image_pdf([1, 2, 10]), max_pages=3)[0] == pdfpages.OK


@pytest.mark.parametrize("data", [b"%PDF-1.4 garbage", PNG.getvalue(), b""], ids=["garbage", "png", "empty"])
def test_broken_bytes_are_unreadable(tmp_path, data):
    code, out = render(tmp_path, data)
    assert code == pdfpages.UNREADABLE and names(out) == []


def test_missing_file_is_unreadable(tmp_path):
    assert pdfpages.render(tmp_path / "none.pdf", tmp_path, 200, 2400) == pdfpages.UNREADABLE


def test_password_protected_pdf_is_reported_as_encrypted(tmp_path):
    code, out = render(tmp_path, locked_pdf())
    assert code == pdfpages.ENCRYPTED and names(out) == []


@pytest.mark.parametrize("size", [(0.0, 40.0), (30.0, -1.0), (float("nan"), 40.0), (30.0, float("inf"))])
def test_page_with_abnormal_size_is_not_rendered(size):
    class Page:
        def get_size(self):
            return size

        def render(self, **kwargs):
            raise AssertionError("描画しない")

    assert pdfpages._page_image(Page(), 2400) is None


@pytest.mark.parametrize("size, expected", [((2401, 1802), (2400, 1801)), ((1802, 2401), (1801, 2400)),
                                            ((1200, 900), (2400, 1800)), ((2400, 1), (2400, 1)),
                                            ((4800, 1), (2400, 1))])
def test_fit_makes_the_long_edge_exact(size, expected):
    assert pdfpages._fit(Image.new("RGB", size), 2400).size == expected


def child(*args):
    return subprocess.run([sys.executable, "-m", "videotab.pdfpages", *map(str, args)],
                          capture_output=True, stdin=subprocess.DEVNULL, timeout=60)


def test_module_runs_as_a_child_process_and_reports_by_exit_code(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for name, data in (("ok.pdf", image_pdf([1])), ("locked.pdf", locked_pdf()), ("bad.pdf", b"%PDF-1.4 garbage")):
        (tmp_path / name).write_bytes(data)
    assert child(tmp_path / "locked.pdf", out, 200, 2400).returncode == pdfpages.ENCRYPTED
    assert child(tmp_path / "bad.pdf", out, 200, 2400).returncode == pdfpages.UNREADABLE
    assert child(tmp_path / "ok.pdf", out, 0, 2400).returncode == pdfpages.TOO_MANY
    assert names(out) == []
    done = child(tmp_path / "ok.pdf", out, 200, 2400)
    assert done.returncode == pdfpages.OK and done.stdout == b""
    assert names(out) == ["page-1.png"] and Image.open(out / "page-1.png").size == (1800, 2400)
    # 引数が足りないなどの予期しない失敗は、決めた終了コードのどれにもならない。
    assert child(tmp_path / "ok.pdf").returncode not in (pdfpages.OK, pdfpages.ENCRYPTED, pdfpages.UNREADABLE,
                                                         pdfpages.TOO_MANY)


def test_importing_the_module_does_not_load_pdfium():
    code = "import sys, videotab.pdfpages; sys.exit('pypdfium2' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60).returncode == 0
