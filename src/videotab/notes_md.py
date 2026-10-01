"""読み手の報告（Markdown）を、画面が組み立てやすい木（JSON）にする。

扱うのは見出し・段落・コードブロック・箇条書き・インラインコード・太字だけ。それ以外
（HTML のタグ・リンク・画像・表・引用・斜体など）は文字のまま残す。画面は決まった要素に
文字を入れるだけなので、報告の中の HTML は動かない。

ブロック:
  {"t": "h", "level": 1〜6, "inline": [...]}
  {"t": "p", "inline": [...]}                      段落の中の改行は残す
  {"t": "code", "text": "..."}                     ``` で囲んだ部分（中は解析しない）
  {"t": "list", "ordered": bool, "start": int, "items": [{"inline": [...], "children": [ブロック, ...]}]}
インライン:
  {"t": "text", "text": "..."} / {"t": "code", "text": "..."} / {"t": "b", "inline": [text か code]}

走査は前から順に 1 回ずつ行い、後戻りの多い正規表現は使わない（長い報告でも入力の長さに比例する）。
"""

from __future__ import annotations

MAX_CHARS = 50_000  # これを超える報告は解析せず、原文を 1 つの段落にする
MAX_DEPTH = 8  # 箇条書きの入れ子の深さの上限（それより深い字下げは同じ段に並べる）
TAB_WIDTH = 4


def parse(text: str) -> list[dict]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_CHARS:
        return [{"t": "p", "inline": [{"t": "text", "text": text}]}]
    blocks: list[dict] = []
    para: list[str] = []  # 組み立て中の段落の行
    # 組み立て中の箇条書き: [(字下げの幅, リストのブロック, そのリストを入れた先), ...]（外側から）
    stack: list[tuple[int, dict, list]] = []
    blank = False  # 箇条書きのあとに空行があった
    lines = text.split("\n")
    while lines and not lines[-1]:  # 末尾の改行で、閉じの無いコードブロックに空の行を足さない
        lines.pop()
    i = 0

    def end_para() -> None:
        if para:
            blocks.append({"t": "p", "inline": "\n".join(para)})
            para.clear()

    def end_list() -> None:
        stack.clear()

    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip():
            end_para()
            blank = bool(stack)
            continue
        indent, rest = _indent(line)
        if rest.startswith("```"):
            end_para()
            end_list()
            body = []
            while i < len(lines) and not _indent(lines[i])[1].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # 閉じの行（無ければ最後まで）
            blocks.append({"t": "code", "text": "\n".join(body)})
            continue
        level = _heading_level(rest) if indent < 4 else 0
        if level:
            end_para()
            end_list()
            blocks.append({"t": "h", "level": level, "inline": rest[level:].strip()})
            continue
        item = _list_item(rest)
        if item is not None:
            end_para()
            ordered, start, content = item
            _add_item(blocks, stack, indent, ordered, start, content)
            blank = False
            continue
        if stack and not blank:
            # 箇条書きの項目の続きの行（空行を挟まない）
            stack[-1][1]["items"][-1]["inline"].append(line.strip())
            continue
        end_list()
        blank = False
        para.append(line)
    end_para()
    return [_finish(b) for b in blocks]


def _indent(line: str) -> tuple[int, str]:
    """行頭の字下げの幅（タブは TAB_WIDTH 文字）と、字下げを除いた残り。"""
    width = 0
    for k, ch in enumerate(line):
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += TAB_WIDTH
        else:
            return width, line[k:]
    return width, ""


def _heading_level(rest: str) -> int:
    n = 0
    while n < len(rest) and rest[n] == "#":
        n += 1
    if 1 <= n <= 6 and (n == len(rest) or rest[n] in " \t"):
        return n
    return 0


def _list_item(rest: str) -> tuple[bool, int, str] | None:
    """「- 」「* 」「数字. 」で始まれば（番号付きか, 始まりの番号, 中身）。"""
    if rest[:1] in ("-", "*") and rest[1:2] in (" ", "\t"):
        return False, 1, rest[2:].strip()
    n = 0
    while n < len(rest) and n < 9 and rest[n].isascii() and rest[n].isdigit():
        n += 1
    if n and rest[n : n + 1] == "." and rest[n + 1 : n + 2] in (" ", "\t"):
        return True, int(rest[:n]), rest[n + 2 :].strip()
    return None


def _add_item(blocks: list, stack: list, indent: int, ordered: bool, start: int, content: str) -> None:
    item = {"inline": [content], "children": []}  # 続きの行は後ろに足す
    # 今の項目より浅い字下げなら、その深さのリストまで戻る
    while len(stack) > 1 and indent < stack[-1][0]:
        stack.pop()
    if stack and indent > stack[-1][0] and len(stack) < MAX_DEPTH:
        # 1 つ前の項目の下に入れ子のリストを作る
        container = stack[-1][1]["items"][-1]["children"]
        lst = {"t": "list", "ordered": ordered, "start": start, "items": [item]}
        container.append(lst)
        stack.append((indent, lst, container))
        return
    if stack and stack[-1][1]["ordered"] == ordered:
        stack[-1][1]["items"].append(item)
        return
    # 箇条書きの始まり、または同じ段で種類（番号付きか）が変わった
    container = stack[-1][2] if stack else blocks
    width = stack[-1][0] if stack else indent
    lst = {"t": "list", "ordered": ordered, "start": start, "items": [item]}
    container.append(lst)
    if stack:
        stack[-1] = (width, lst, container)
    else:
        stack.append((width, lst, container))


def _finish(block: dict) -> dict:
    """組み立て中は文字列にしておいたインラインを解析する。"""
    if block["t"] in ("h", "p"):
        block["inline"] = inline(block["inline"])
    elif block["t"] == "list":
        for item in block["items"]:
            item["inline"] = inline("\n".join(item["inline"]))
            item["children"] = [_finish(c) for c in item["children"]]
    return block


def inline(s: str) -> list[dict]:
    """インラインコードと太字を拾う。閉じの無い ` や ** は文字のまま。"""
    out = _Inline()
    i, n = 0, len(s)
    while i < n:
        if s[i] == "`":
            i = _code_span(s, i, out)
            continue
        if s.startswith("**", i):
            j = _bold_end(s, i + 2)
            if j > i + 2:
                inner = _Inline()
                k = i + 2
                while k < j:  # 太字の中はインラインコードだけを拾う
                    if s[k] == "`":
                        k = _code_span(s, k, inner)
                    else:
                        m = s.find("`", k, j)
                        m = j if m < 0 else m
                        inner.text(s[k:m])
                        k = m
                out.node({"t": "b", "inline": inner.done()})
                i = j + 2
                continue
            out.text("**")
            i += 2
            continue
        j = i + 1
        while j < n and s[j] not in "`*":
            j += 1
        out.text(s[i:j])
        i = j
    return out.done()


def _code_span(s: str, i: int, out: "_Inline") -> int:
    """s[i] の ` から始まるインラインコードを out に足し、次に読む位置を返す。

    閉じが無い ` と、中身の無い `` は文字のまま。どこから呼んでも同じ組み合わせで閉じを探す
    （太字の閉じを探すときと、本文を読むときで、コードの範囲が食い違わない）。
    """
    j = s.find("`", i + 1)
    if j < 0:
        out.text("`")
        return i + 1
    if j == i + 1:
        out.text("``")
        return i + 2
    out.node({"t": "code", "text": s[i + 1 : j]})
    return j + 1


def _bold_end(s: str, i: int) -> int:
    """i から探した、太字を閉じる ** の位置（インラインコードの中は飛ばす）。無ければ -1。"""
    skip = _Inline()  # 読み飛ばすだけ（中身は使わない）
    n = len(s)
    while i < n:
        if s[i] == "`":
            i = _code_span(s, i, skip)
            skip.clear()
        elif s.startswith("**", i):
            return i
        else:
            i += 1
    return -1


class _Inline:
    """インラインの節を並べる。続く文字はまとめて 1 つの text の節にする。"""

    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.buf: list[str] = []

    def text(self, s: str) -> None:
        if s:
            self.buf.append(s)

    def node(self, node: dict) -> None:
        self._flush()
        self.nodes.append(node)

    def clear(self) -> None:
        self.nodes.clear()
        self.buf.clear()

    def _flush(self) -> None:
        if self.buf:
            self.nodes.append({"t": "text", "text": "".join(self.buf)})
            self.buf.clear()

    def done(self) -> list[dict]:
        self._flush()
        return self.nodes
