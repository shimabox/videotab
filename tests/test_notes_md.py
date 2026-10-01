"""読み手の報告（Markdown）の解析。"""

import time

import pytest

from videotab.notes_md import MAX_CHARS, MAX_DEPTH, parse


def text(s):
    return {"t": "text", "text": s}


def code(s):
    return {"t": "code", "text": s}


def p(*inline):
    return {"t": "p", "inline": list(inline)}


def item(*inline, children=()):
    return {"inline": list(inline), "children": list(children)}


def ul(*items):
    return {"t": "list", "ordered": False, "start": 1, "items": list(items)}


def ol(*items, start=1):
    return {"t": "list", "ordered": True, "start": start, "items": list(items)}


def test_headings():
    assert parse("# 一\n## 二\n###### 六") == [
        {"t": "h", "level": 1, "inline": [text("一")]},
        {"t": "h", "level": 2, "inline": [text("二")]},
        {"t": "h", "level": 6, "inline": [text("六")]},
    ]
    assert parse("####### 七") == [p(text("####### 七"))]  # 7 つは見出しにしない
    assert parse("#見出しではない") == [p(text("#見出しではない"))]


def test_bold_and_inline_code():
    assert parse("**12 小節**の `3.2` を直した") == [
        p({"t": "b", "inline": [text("12 小節")]}, text("の "), code("3.2"), text(" を直した"))
    ]
    # 太字の中のインラインコードは有効（コードの中の ** では閉じない）
    assert parse("**直した: `(0.6).1 **x`**") == [
        p({"t": "b", "inline": [text("直した: "), code("(0.6).1 **x")]})
    ]
    assert parse("`**太字ではない**`") == [p(code("**太字ではない**"))]


def test_unclosed_marks_stay_as_text():
    assert parse("**閉じない") == [p(text("**閉じない"))]
    assert parse("`閉じない") == [p(text("`閉じない"))]
    assert parse("空の `` と ****") == [p(text("空の `` と ****"))]


def test_paragraphs_keep_line_breaks():
    assert parse("1 行目\n2 行目\n\n\n次の段落") == [p(text("1 行目\n2 行目")), p(text("次の段落"))]


def test_nested_lists():
    md = "- 構成\n  - 繰り返し: 5〜8 小節\n    1. 1 回目\n    2. 2 回目\n- テンポ\n3. 番号付き\n4. 続き\n"
    assert parse(md) == [
        ul(
            item(text("構成"), children=[
                ul(item(text("繰り返し: 5〜8 小節"), children=[ol(item(text("1 回目")), item(text("2 回目")))]))
            ]),
            item(text("テンポ")),
        ),
        ol(item(text("番号付き")), item(text("続き")), start=3),
    ]  # fmt: skip
    assert parse("* a\n\t* b") == [ul(item(text("a"), children=[ul(item(text("b")))]))]  # タブは 4 文字
    assert parse("- a\n続きの行\n\n- b\n\n段落") == [ul(item(text("a\n続きの行")), item(text("b"))), p(text("段落"))]


def test_list_depth_is_limited():
    md = "\n".join("  " * k + f"- {k}" for k in range(MAX_DEPTH + 3))
    depth, lst = 0, parse(md)[0]
    while True:
        depth += 1
        last = lst["items"][-1]
        if not last["children"]:
            break
        lst = last["children"][0]
    assert depth == MAX_DEPTH
    assert [i["inline"][0]["text"] for i in lst["items"]] == [str(k) for k in range(MAX_DEPTH - 1, MAX_DEPTH + 3)]


def test_code_blocks():
    assert parse("前\n```alphatex\n1.3 **x** `y`\n\n# z\n```\n後") == [
        p(text("前")), code("1.3 **x** `y`\n\n# z"), p(text("後"))
    ]
    assert parse("```\n閉じない\n- a\n") == [code("閉じない\n- a")]


def test_empty_trailing_newline_and_crlf():
    assert parse("") == []
    assert parse("\n\n") == []
    assert parse("報告\n") == [p(text("報告"))]
    assert parse("# 見出し\r\n- a\r\n- b\r\n\r\n本文\r\n") == [
        {"t": "h", "level": 1, "inline": [text("見出し")]}, ul(item(text("a")), item(text("b"))), p(text("本文"))
    ]


@pytest.mark.parametrize("raw", [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "[リンク](javascript:alert(1)) ![画像](x.png)",
    "| a | b |\n|---|---|\n| 1 | 2 |",
    "*斜体* と _斜体_ と __太字ではない__",
    "> 引用",
])  # fmt: skip
def test_html_links_tables_and_italics_are_text(raw):
    assert parse(raw) == [p(text(raw))]


def test_english_report_has_same_shape():
    md = "## Summary\n- Bars **1-8** written\n  - unsure: bar `5`\n\nTempo ~120."
    assert parse(md) == [
        {"t": "h", "level": 2, "inline": [text("Summary")]},
        ul(item(text("Bars "), {"t": "b", "inline": [text("1-8")]}, text(" written"), children=[
            ul(item(text("unsure: bar "), code("5")))
        ])),
        p(text("Tempo ~120.")),
    ]  # fmt: skip


def test_long_report_is_one_paragraph():
    raw = "- a **b**\n" * (MAX_CHARS // 10 + 1)
    assert parse(raw) == [p(text(raw))]


@pytest.mark.parametrize("unit", ["**", "`", "** `", "*a", "- ", "  - x\n", "```\n"])
def test_parse_is_fast_on_repeated_marks(unit):
    raw = (unit * (MAX_CHARS // len(unit)))[:MAX_CHARS]
    start = time.monotonic()
    parse(raw)
    assert time.monotonic() - start < 2
