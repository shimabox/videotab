"""PDF のページを画像にする。取り込みの本体とは別のプロセスで動かす。

    python -m videotab.pdfpages <pdf> <出力フォルダ> <最大ページ数> <長辺px>

壊れた PDF で PDFium が落ちたり固まったりしても本体を巻き込まないよう、PDFium を読み込むのは
このプロセスだけにする。結果は終了コードで伝え、ページは出力フォルダの page-<n>.png に書く。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image

OK = 0
ENCRYPTED = 2  # 開くのにパスワードが要る
UNREADABLE = 3  # 壊れている・PDF でない・ページの寸法が異常
TOO_MANY = 4  # ページ数が上限を超えている


def _fit(im: Image.Image, long_edge: int) -> Image.Image:
    """長辺がちょうど long_edge になるよう、縦横比を保って大きさを直す。"""
    w, h = im.size
    if max(w, h) == long_edge:
        return im
    if w >= h:
        size = (long_edge, max(1, round(h * long_edge / w)))
    else:
        size = (max(1, round(w * long_edge / h)), long_edge)
    return im.resize(size, Image.LANCZOS)


def _page_image(page, long_edge: int) -> Image.Image | None:
    """1 ページを RGB の画像にする。寸法が異常なページは None。"""
    # /Rotate は寸法と描画の両方に反映されるので、長辺は回転に左右されない。
    size = page.get_size()
    if not all(math.isfinite(v) and v > 0 for v in size):
        return None
    # 描画の寸法は切り上げられる。誤差で長辺が 1px 増えないよう、倍率をわずかに小さくする。
    bitmap = page.render(scale=long_edge / max(size) * (1 - 1e-9))
    try:
        return _fit(bitmap.to_pil().convert("RGB"), long_edge)
    finally:
        bitmap.close()


def render(pdf: Path, out: Path, max_pages: int, long_edge: int) -> int:
    # PDFium はここで初めて読み込む（終了コードの定数だけを使う本体には読み込ませない）。
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    try:
        doc = pdfium.PdfDocument(pdf)
    except pdfium.PdfiumError as e:
        return ENCRYPTED if e.err_code == pdfium_c.FPDF_ERR_PASSWORD else UNREADABLE
    except OSError:
        return UNREADABLE
    try:
        try:
            # フォームの見た目を描くには、ページ数やページを取る前に用意しておく。
            doc.init_forms()
            count = len(doc)
        except pdfium.PdfiumError:
            return UNREADABLE
        if count < 1:
            return UNREADABLE
        if count > max_pages:
            return TOO_MANY
        # 1 ページずつ開いて描き、閉じてから次へ進む（全ページのビットマップを同時に持たない）。
        for n in range(count):
            try:
                page = doc[n]
                try:
                    im = _page_image(page, long_edge)
                finally:
                    page.close()
            except Exception:
                return UNREADABLE
            if im is None:
                return UNREADABLE
            im.save(out / f"page-{n + 1}.png")
    finally:
        doc.close()
    return OK


def main() -> None:
    pdf, out, max_pages, long_edge = sys.argv[1:]
    sys.exit(render(Path(pdf), Path(out), int(max_pages), int(long_edge)))


if __name__ == "__main__":
    main()
