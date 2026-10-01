"""alphaTex を、alphaTab を埋め込んだ 1 枚の HTML にする。

alphaTab 本体と Bravura フォントは HTML に埋め込むので、表示はオフラインでもできる。
再生だけは音源データ（soundfont）を CDN から読む。同梱物の出どころとライセンスは
templates/vendor/VENDOR.md を参照。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from importlib import resources

from jinja2 import Environment, PackageLoader, select_autoescape

# templates/vendor/VENDOR.md と合わせる
ALPHATAB_VERSION = "1.8.4"
SOUNDFONT_URL = f"https://cdn.jsdelivr.net/npm/@coderline/alphatab@{ALPHATAB_VERSION}/dist/soundfont/sonivox.sf3"

_env = Environment(loader=PackageLoader("videotab", "templates"), autoescape=select_autoescape(("html", "j2")))


def _vendor(name: str) -> bytes:
    return resources.files("videotab").joinpath("templates", "vendor", name).read_bytes()


def _js_string_literal(value: str) -> str:
    """<script> の中に置いても安全な JS の文字列リテラル。

    json.dumps だけでは、文字列に "</script" が含まれると HTML の解析で script 要素が
    閉じてしまう。< > & を \\uXXXX に置き換えて、HTML として読める文字を残さない。
    """
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def tuning_label(tuning: str) -> str:
    """"e4 b3 g3 d3 a2 e2"（1 弦から）→ "E A D G B E"（6 弦から）。"""
    notes = [t.rstrip("0123456789-") for t in tuning.split()]
    return " ".join(n[:1].upper() + n[1:] for n in reversed(notes))


def file_stem(title: str) -> str:
    """ダウンロードするファイルの名前（拡張子なし）。ファイル名に使えない文字を除く。"""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title).strip(" ._")
    return stem[:120] or "tab"


def render_html(
    tex: str,
    *,
    title: str,
    tempo: float,
    tuning: str,
    capo: int = 0,
    bar_count: int = 0,
    source_url: str | None = None,
    source_title: str | None = None,
    source_creator: str | None = None,
    generated_at: str | None = None,
) -> str:
    font = base64.b64encode(_vendor("Bravura.woff2")).decode("ascii")
    return _env.get_template("viewer.html.j2").render(
        title=title,
        tempo=f"{tempo:g}",
        tuning_label=tuning_label(tuning),
        capo=capo,
        bar_count=bar_count,
        source_url=source_url,
        source_title=source_title,
        source_creator=source_creator,
        generated_at=generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        alphatab_version=ALPHATAB_VERSION,
        alphatab_js=_vendor("alphatab.min.js").decode("utf-8"),
        tex_js_literal=_js_string_literal(tex),
        bravura_font_data_uri_js_literal=_js_string_literal(f"data:font/woff2;base64,{font}"),
        soundfont_url_js_literal=_js_string_literal(SOUNDFONT_URL),
        file_stem_js_literal=_js_string_literal(file_stem(title)),
    )
