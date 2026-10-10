"""テスト用の合成 PDF。画像のページは Pillow で、寸法・回転・暗号化を決めたいページは手で組む。"""

from __future__ import annotations

import io

from PIL import Image


def image_pdf(values, size=(30, 40)) -> bytes:
    """values の濃さで塗った画像を 1 ページずつ並べた PDF（1px = 1pt）。"""
    ims = [Image.new("RGB", size, (v, v, v)) for v in values]
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:], resolution=72)
    return buf.getvalue()


def page_pdf(pages, trailer="") -> bytes:
    """pages は (幅pt, 高さpt, /Rotate) の並び。回す前の左半分を黒く塗る。trailer は trailer 辞書に足す中身。"""
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>"]
    for i, (w, h, rotate) in enumerate(pages):
        content = f"0 g 0 0 {w / 2} {h} re f"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] /Rotate {rotate}"
                    f" /Contents {4 + 2 * i} 0 R >>")
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R {trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def locked_pdf() -> bytes:
    """開くのにパスワードが要る PDF。/O と /U が空のパスワードに合わないので、中身は暗号化していない。"""
    encrypt = (f"/Encrypt << /Filter /Standard /V 1 /R 2 /Length 40 /P -1 /O <{'11' * 32}> /U <{'22' * 32}> >>"
               f" /ID [<{'33' * 16}> <{'33' * 16}>]")
    return page_pdf([(30, 40, 0)], encrypt)
