"""小節ごとの alphaTex を読み、長さ・タイ・記法を調べる。

読み取り結果は {"小節番号": "小節の中身（末尾の | なし）"} の形で持つ。ここではその
「小節の中身」を解釈する。alphaTab の文法の全部ではなく、書き起こしで使う範囲:

- 小節の先頭のメタデータ: \\tempo N、\\ts N M、\\ro、\\rc N、\\ae (1 2) / \\ae N、\\section "..." など
- 拍: (音 音 ...).長さ {拍の効果}、休符 r.長さ、括弧なしの単音 7.3.8、:N（以後の既定の長さ）、*N（同じ拍の繰り返し）
- 音: フレット.弦 {音の効果}。フレットは数字、x（ブラッシング）、-（タイの続き）
- 拍の効果: {d}（付点）、{dd}、{tu 3}（連符）、{gr}（装飾音）、{tempo N}（拍の途中のテンポ変化）、{ch "Am"} など

{pm} のような音の効果を拍の後ろ（.8 のあと）に書くと、alphaTab は楽譜全体を読み込めない。
検査では注意として知らせ、組み立てでは move_note_effects でその拍の音に付け直す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

# 効果の引数の決まり。同梱の alphaTab 1.8.4（templates/vendor/alphatab.min.js）の static beatProperties /
# noteProperties の表を写したもの（tests/test_alphatex.py で表と同じことを確かめる）。
# 名前 → None（引数なし）か、書き方の候補の並び。候補は引数の並びで、引数は（型, 読み方, 許す値）。
# 型は n が数字、s が文字列、i が識別子（引用符のない単語）。許す値は文字列と識別子にだけ効く。
# 拍の後ろの { } は beatProperties だけを、音の後ろの { } は noteProperties と beatProperties を受け付け、
# ほかの名前があると楽譜全体を読めない。
REQUIRED, OPTIONAL, REQUIRED_FLOAT, OPTIONAL_FLOAT, VALUE_LIST, LIST_NO_PAREN = range(6)  # 読み方
Param = tuple[str, int, tuple[str, ...] | None]
Signatures = list[list[Param]] | None
# { } の中の効果 1 つ: （名前, 引数, 小節の中身の中の名前から最後の引数までの位置）
EffectItem = tuple[str, list, tuple[int, int]]


def _bend(types: tuple[str, ...]) -> Signatures:
    styles = ("default", "gradual", "fast")
    values: Param = ("n", LIST_NO_PAREN, None)
    return [
        [values],
        [("is", REQUIRED, types), values],
        [("is", REQUIRED, styles), values],
        [("is", REQUIRED, types), ("is", REQUIRED, styles), values],
    ]


_DYNAMICS = (
    "ppp", "pp", "p", "mp", "mf", "f", "ff", "fff", "pppp", "ppppp", "pppppp", "ffff", "fffff", "ffffff", "sf",
    "sfp", "sfpp", "fp", "rf", "rfz", "sfz", "sffz", "fz", "n", "pf", "sfzp",
)  # fmt: skip
_RASGUEADO = (
    "ii", "mi", "miitriplet", "miianapaest", "pmptriplet", "pmpanapaest", "peitriplet", "peianapaest",
    "paitriplet", "paianapaest", "amitriplet", "amianapaest", "ppp", "amii", "amip", "eami", "eamii", "peami",
)  # fmt: skip
_ACCIDENTALS = (
    "default", "forcenone", "forcenatural", "forcesharp", "forcedoublesharp", "forceflat", "forcedoubleflat",
    "d", "-", "n", "#", "##", "x", "b", "bb",
)  # fmt: skip
_NUMBER: Signatures = [[("n", REQUIRED, None)]]
_OPTIONAL_NUMBER: Signatures = [[("n", OPTIONAL, None)]]
_TEXT: Signatures = [[("si", REQUIRED, None)]]

BEAT_EFFECT_ARGS: dict[str, Signatures] = {
    **dict.fromkeys(
        ["f", "fo", "vs", "v", "vw", "s", "p", "tt", "d", "dd", "su", "sd", "cre", "dec", "spd", "sph", "spu",
         "spe", "slashed", "ds", "glpf", "glpt", "waho", "wahc", "legatoorigin", "timer"]
    ),  # fmt: skip
    "tu": [[("n", REQUIRED, ("3", "5", "6", "7", "9", "10", "12"))], [("n", REQUIRED, None), ("n", REQUIRED, None)]],
    "txt": _TEXT,
    "lyrics": [[("s", REQUIRED, None)], [("n", REQUIRED, None), ("s", REQUIRED, None)]],
    "tb": _bend(("custom", "dive", "dip", "hold", "predive", "predivedive")),
    "tbe": _bend(("custom", "dive", "dip", "hold", "predive", "predivedive")),
    **dict.fromkeys(["bu", "bd", "au", "ad"], _OPTIONAL_NUMBER),
    "ch": _TEXT,
    "gr": [[("is", OPTIONAL, ("onbeat", "beforebeat", "bendgrace", "ob", "bb", "b"))]],
    "dy": [[("is", REQUIRED, _DYNAMICS)]],
    "tempo": [
        [("n", REQUIRED, None), ("i", OPTIONAL, ("hide",))],
        [("n", REQUIRED, None), ("s", REQUIRED, None), ("i", OPTIONAL, ("hide",))],
    ],
    "volume": _NUMBER,
    "balance": _NUMBER,
    "tp": [[("n", REQUIRED, None), ("is", OPTIONAL, ("default", "buzzroll"))]],
    "barre": [[("n", REQUIRED, None), ("is", OPTIONAL, ("full", "half"))]],
    "rasg": [[("is", REQUIRED, _RASGUEADO)]],
    "ot": [[("is", REQUIRED, ("15ma", "8va", "regular", "8vb", "15mb"))]],
    "instrument": [[("n", REQUIRED, None)], [("si", REQUIRED, None)], [("i", REQUIRED, ("percussion",))]],
    "bank": _NUMBER,
    "fermata": [[("is", REQUIRED, ("short", "medium", "long")), ("n", OPTIONAL_FLOAT, None)]],
    "beam": [[("is", REQUIRED, ("invert", "up", "down", "auto", "split", "merge", "splitsecondary"))]],
}
NOTE_EFFECT_ARGS: dict[str, Signatures] = {
    "nh": None,
    **dict.fromkeys(["ah", "th", "ph", "sh", "fh"], _OPTIONAL_NUMBER),
    **dict.fromkeys(
        ["v", "vw", "sl", "ss", "sib", "sia", "sou", "sod", "psu", "psd", "h", "lht", "g", "ac", "hac", "ten"]
    ),
    "tr": [[("n", REQUIRED, None), ("n", OPTIONAL, ("16", "32", "64"))]],
    **dict.fromkeys(["pm", "st", "lr", "x", "t", "turn", "iturn", "umordent", "lmordent", "string", "hide"]),
    "b": _bend(("custom", "bend", "release", "bendrelease", "hold", "prebend", "prebendbend", "prebendrelease")),
    "be": _bend(("custom", "bend", "release", "bendrelease", "hold", "prebend", "prebendbend", "prebendrelease")),
    "lf": [[("n", REQUIRED, ("1", "2", "3", "4", "5"))]],
    "rf": [[("n", REQUIRED, ("1", "2", "3", "4", "5"))]],
    "acc": [[("is", REQUIRED, _ACCIDENTALS)]],
    "slur": [[("s", REQUIRED, None)], [("i", REQUIRED, None)]],
    "-": None,
}
KNOWN_BEAT_EFFECTS = set(BEAT_EFFECT_ARGS)
KNOWN_NOTE_EFFECTS = set(NOTE_EFFECT_ARGS)
# 音にしか付けられない効果（拍の後ろに書くと alphaTab が読めない）
NOTE_ONLY_EFFECTS = KNOWN_NOTE_EFFECTS - KNOWN_BEAT_EFFECTS
# 小節の先頭で使うメタデータと、その引数の数（None は数字と文字列を続く限り取る）
BAR_META_ARITY = {"ro": 0, "rc": 1, "ae": 1, "tempo": None, "ts": None, "section": None, "ks": 1, "clef": 1,
                  "jump": 1, "ft": 0, "simile": 1, "tf": 1, "accidentals": 1}  # fmt: skip
META_ORDER = ["tempo", "ts", "ro", "rc", "ae"]
VALID_DURATIONS = {1, 2, 4, 8, 16, 32, 64}
DEFAULT_TUPLET_DENOM = {3: 2, 5: 4, 6: 4, 7: 4, 9: 8, 10: 8, 11: 8, 12: 8}

TOKEN = re.compile(
    r"""\s*(?:
      (?P<meta>\\[A-Za-z]+)
    | (?P<str>"(?:[^"\\]|\\.)*")
    | (?P<lbrace>\{) | (?P<rbrace>\}) | (?P<lparen>\() | (?P<rparen>\))
    | (?P<colon>:\d+)
    | (?P<star>\*\d+)
    | (?P<word>[A-Za-z0-9#\-\.]+)
    | (?P<other>\S)
    )""",
    re.VERBOSE,
)


class ParseError(ValueError):
    pass


@dataclass
class Note:
    fret: str  # 数字・"x"・"-"
    string: int
    effects: dict[str, list] = field(default_factory=dict)
    # 書き直すときの位置: 効果を {..} で足す位置（「フレット.弦」の直後）と、音の効果の } の位置
    pos: int | None = field(default=None, compare=False, repr=False)
    close: int | None = field(default=None, compare=False, repr=False)
    # 書いた順の効果（同じ名前が何度あってもすべて）。effects は同じ名前なら最後のもの
    effect_items: list[EffectItem] = field(default_factory=list, compare=False, repr=False)
    # 引数の決まりどおりに読めなかった効果（知らない名前を含む）
    unread: set[str] = field(default_factory=set, compare=False, repr=False)

    @property
    def is_tie(self) -> bool:
        return self.fret == "-"


@dataclass
class Beat:
    notes: list[Note]
    rest: bool
    duration: int
    effects: dict[str, list] = field(default_factory=dict)
    bare: bool = False  # 括弧で囲んでいない単音
    # 書き直すときの位置: 拍の効果の { から } までと、書いた順の効果（同じ名前が何度あってもすべて）
    effects_span: tuple[int, int] | None = field(default=None, compare=False, repr=False)
    effect_items: list[EffectItem] = field(default_factory=list, compare=False, repr=False)
    # 引数の決まりどおりに読めなかった拍の効果（知らない名前を含む）
    unread: set[str] = field(default_factory=set, compare=False, repr=False)

    @property
    def is_grace(self) -> bool:
        return "gr" in self.effects

    def length(self) -> Fraction:
        """4 分音符を 1 とした長さ。装飾音は 0。"""
        if self.is_grace:
            return Fraction(0)
        v = Fraction(4, self.duration)
        if "d" in self.effects:
            v *= Fraction(3, 2)
        elif "dd" in self.effects:
            v *= Fraction(7, 4)
        if "tu" in self.effects:
            args = [int(a) for a in self.effects["tu"] if isinstance(a, int)]
            if args:
                num = args[0]
                den = args[1] if len(args) > 1 else DEFAULT_TUPLET_DENOM.get(num, 2)
                v *= Fraction(den, num)
        return v


@dataclass
class Bar:
    meta: list[tuple[str, list]]
    beats: list[Beat]
    warnings: list[str] = field(default_factory=list)

    def meta_value(self, name: str):
        for n, args in self.meta:
            if n == name:
                return args
        return None

    @property
    def repeat_open(self) -> bool:
        return self.meta_value("ro") is not None

    @property
    def repeat_close(self) -> int:
        args = self.meta_value("rc")
        return int(args[0]) if args else 0

    @property
    def alternate_endings(self) -> set[int] | None:
        args = self.meta_value("ae")
        if args is None:
            return None
        out: set[int] = set()
        for a in args:
            out.update(a if isinstance(a, list) else [a])
        return {int(x) for x in out}

    @property
    def tempo(self) -> float | None:
        args = self.meta_value("tempo")
        return float(args[0]) if args else None

    @property
    def time_signature(self) -> tuple[int, int] | None:
        args = self.meta_value("ts")
        if not args:
            return None
        if args[0] == "common":
            return (4, 4)
        return int(args[0]), int(args[1])

    @property
    def is_full_bar_rest(self) -> bool:
        """休符 r.1 だけの小節。alphaTab と同じく、拍子にかかわらず 1 小節まるごとの休符とみなす。"""
        return (
            len(self.beats) == 1
            and self.beats[0].rest
            and self.beats[0].duration == 1
            and not ({"d", "dd", "tu"} & set(self.beats[0].effects))
        )

    def length(self, time_signature: tuple[int, int] | None = None) -> Fraction:
        """4 分音符を 1 とした小節の長さ。拍子を渡すと、全休符の小節はその拍子ぶんとする。"""
        if time_signature and self.is_full_bar_rest:
            return Fraction(time_signature[0] * 4, time_signature[1])
        return sum((b.length() for b in self.beats), Fraction(0))


def _value(tok: str):
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if re.fullmatch(r"-?\d+\.\d+", tok):
        return float(tok)
    return tok


class _Tokens:
    def __init__(self, text: str):
        self.items: list[tuple[str, str]] = []
        self.spans: list[tuple[int, int]] = []  # items と同じ順の、text の中の位置
        pos = 0
        while pos < len(text):
            m = TOKEN.match(text, pos)
            if not m or m.end() == pos:
                break
            pos = m.end()
            kind = m.lastgroup
            if kind is None:
                continue
            self.items.append((kind, m.group(kind)))
            self.spans.append(m.span(kind))
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, str] | None:
        j = self.i + k
        return self.items[j] if j < len(self.items) else None

    def last_span(self) -> tuple[int, int]:
        """最後に読んだ字句の位置。"""
        return self.spans[self.i - 1]

    def next(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ParseError("途中で終わっています")
        self.i += 1
        return tok


def _parse_group(toks: _Tokens) -> list:
    """( ... ) の中の値の列。入れ子も可。"""
    out: list = []
    while True:
        kind, val = toks.next()
        if kind == "rparen":
            return out
        if kind == "lparen":
            out.append(_parse_group(toks))
        elif kind == "str":
            out.append(val[1:-1])
        elif kind == "word":
            out.append(_value(val))
        else:
            raise ParseError(f"( ) の中に {val!r} があります")


def _arg_type(tok: tuple[str, str] | None) -> str | None:
    """{ } の中の字句の型。n は数字、s は文字列、i は識別子、( は ( ) の始まり。ほかは None。"""
    if tok is None:
        return None
    kind, val = tok
    if kind == "word":
        return "n" if re.fullmatch(r"-?\d+(\.\d+)?", val) else "i"
    return {"str": "s", "lparen": "("}.get(kind)


def _take_arg(toks: _Tokens):
    kind, val = toks.next()
    if kind == "lparen":
        return _parse_group(toks)
    return val[1:-1] if kind == "str" else _value(val)


def _read_args(toks: _Tokens, signatures: Signatures) -> list | None:
    """名前に続く引数を、alphaTab と同じ手順で読む（候補の書き方を、字句を 1 つずつ照らして絞る）。

    決まりに合わなければ None を返す。そのとき toks は途中まで進んでいる。
    """
    args: list = []
    if signatures is not None:
        at = dict.fromkeys(range(len(signatures)), 0)  # 候補ごとの、次に照らす引数の位置
        while len(at) > 1 or (at and all(at[k] < len(signatures[k]) for k in at)):
            tok = toks.peek()
            kind = _arg_type(tok)
            if kind is None:
                break
            matched = False
            drop = set()
            for k in at:
                while at[k] < len(signatures[k]):
                    types, mode, allowed = signatures[k][at[k]]
                    fits = kind in types or (kind == "(" and mode in (VALUE_LIST, LIST_NO_PAREN))
                    if kind in "si" and allowed is not None:
                        fits = fits and (tok[1][1:-1] if kind == "s" else tok[1]).lower() in allowed
                    if fits:
                        matched = True
                        if mode != LIST_NO_PAREN:
                            at[k] += 1
                        break
                    if mode in (REQUIRED, REQUIRED_FLOAT):
                        drop.add(k)
                        break
                    at[k] += 1
                else:
                    # 引数を読み終えた候補は、次の名前（識別子）の前でだけ残る
                    if kind != "i":
                        drop.add(k)
            if not matched:
                break
            args.append(_take_arg(toks))
            for k in drop:
                del at[k]
        # 必須の引数が残っている候補は外す
        for k in [k for k in at if any(p[1] in (REQUIRED, REQUIRED_FLOAT) for p in signatures[k][at[k] :])]:
            del at[k]
        if not at:
            return None
    # 引数のあとは、次の名前か } でなければならない
    tok = toks.peek()
    if tok is None or (tok[0] != "rbrace" and _arg_type(tok) != "i"):
        return None
    return args


def _parse_effects(
    toks: _Tokens, tables: list[dict[str, Signatures]], items: list[EffectItem] | None = None
) -> tuple[dict[str, list], set[str]]:
    """{ ... } の中身を、効果の名前とその引数に分ける。

    引数は、tables のうち名前が最初に載っている表の決まりに従って読む。tempo のあとの hide や、dy のあとの rf の
    ような識別子の引数も、決まりどおりなら引数とする。表にない名前や、決まりに合わない並びは、続く数字・
    文字列・( ) を引数とみなして読み進める。戻り値は（効果, 決まりどおりに読めなかった名前）で、効果は
    同じ名前なら最後のもの。items を渡すと、書いた順の効果（同じ名前もすべて）を入れる。
    """
    effects: dict[str, list] = {}
    unread: set[str] = set()
    while True:
        kind, val = toks.next()
        if kind == "rbrace":
            return effects, unread
        if _arg_type((kind, val)) != "i":
            raise ParseError(f"効果の名前の前に {val!r} があります")
        start = toks.last_span()[0]
        after_name = toks.i
        table = next((t for t in tables if val.lower() in t), None)
        args = _read_args(toks, table[val.lower()]) if table is not None else None
        if args is None:
            unread.add(val)
            toks.i = after_name
            args = []
            while _arg_type(toks.peek()) in ("n", "s", "("):
                args.append(_take_arg(toks))
            if (t := toks.peek()) and t[0] != "rbrace" and _arg_type(t) != "i":
                raise ParseError(f"{{ }} の中に {t[1]!r} があります")
        effects[val] = args
        if items is not None:
            items.append((val, args, (start, toks.last_span()[1])))


NOTE_WORD = re.compile(r"^(\d+|x|X|-)\.(\d+)$")
NOTE_DUR_WORD = re.compile(r"^(\d+|x|X|-)\.(\d+)\.(\d+)$")
REST_WORD = re.compile(r"^r(?:\.(\d+))?$")
DUR_WORD = re.compile(r"^\.(\d+)$")


def _parse_note(word: str, toks: _Tokens) -> Note:
    m = NOTE_WORD.match(word)
    if not m:
        raise ParseError(f"音の書き方が違います: {word!r}（フレット.弦）")
    fret = m.group(1).lower()
    note = Note(fret, int(m.group(2)), pos=toks.last_span()[1])
    if (t := toks.peek()) and t[0] == "lbrace":
        toks.next()
        note.effects, note.unread = _parse_effects(toks, [NOTE_EFFECT_ARGS, BEAT_EFFECT_ARGS], note.effect_items)
        note.close = toks.last_span()[0]
    return note


def parse_bar(text: str, default_duration: int = 4) -> tuple[Bar, int]:
    """小節の中身を読む。戻り値は（小節, 次の小節に引き継ぐ既定の長さ）。"""
    toks = _Tokens(text)
    meta: list[tuple[str, list]] = []
    beats: list[Beat] = []
    warnings: list[str] = []
    duration = default_duration

    while toks.peek() is not None:
        kind, val = toks.next()
        if kind == "meta":
            name = val[1:]
            if beats:
                raise ParseError(f"\\{name} が拍のあとにあります（メタデータは小節の先頭に書く）")
            arity = BAR_META_ARITY.get(name, None)
            args: list = []
            if name == "ae":
                t = toks.next()
                args = [_parse_group(toks)] if t[0] == "lparen" else [_value(t[1])]
            elif arity is None:
                while (t := toks.peek()) and t[0] in ("word", "str") and not _looks_like_beat(t[1]):
                    toks.next()
                    args.append(t[1][1:-1] if t[0] == "str" else _value(t[1]))
            else:
                for _ in range(arity):
                    t = toks.next()
                    args.append(_value(t[1]) if t[0] == "word" else t[1].strip('"'))
            if name not in BAR_META_ARITY:
                warnings.append(f"知らないメタデータ \\{name}")
            _validate_meta(name, args)
            meta.append((name, args))
            continue
        if kind == "colon":
            duration = int(val[1:])
            continue

        # 拍
        notes: list[Note] = []
        rest = False
        bare = False
        beat_duration = duration
        if kind == "lparen":
            while (t := toks.peek()) and t[0] != "rparen":
                toks.next()
                if t[0] != "word":
                    raise ParseError(f"( ) の中に {t[1]!r} があります")
                notes.append(_parse_note(t[1], toks))
            toks.next()
            if not notes:
                raise ParseError("音のない ( ) があります")
        elif kind == "word" and REST_WORD.match(val):
            rest = True
            if REST_WORD.match(val).group(1):
                beat_duration = int(REST_WORD.match(val).group(1))
        elif kind == "word" and NOTE_DUR_WORD.match(val):
            m = NOTE_DUR_WORD.match(val)
            bare = True
            notes.append(Note(m.group(1).lower(), int(m.group(2)), pos=toks.last_span()[0] + m.end(2)))
            beat_duration = int(m.group(3))
        elif kind == "word" and NOTE_WORD.match(val):
            bare = True
            notes.append(_parse_note(val, toks))
        else:
            raise ParseError(f"読めない記号 {val!r}")

        # .長さ（括弧のあと・単音の効果のあと）
        if not rest and (t := toks.peek()) and t[0] == "word" and DUR_WORD.match(t[1]):
            toks.next()
            beat_duration = int(DUR_WORD.match(t[1]).group(1))
        effects: dict[str, list] = {}
        unread: set[str] = set()
        effects_span: tuple[int, int] | None = None
        effect_items: list[EffectItem] = []
        if (t := toks.peek()) and t[0] == "lbrace":
            toks.next()
            open_at = toks.last_span()[0]
            # 拍の後ろに書いた音の効果も、付け直せるように音の効果の決まりで引数を読む
            effects, unread = _parse_effects(toks, [BEAT_EFFECT_ARGS, NOTE_EFFECT_ARGS], effect_items)
            effects_span = (open_at, toks.last_span()[1])
        repeat = 1
        if (t := toks.peek()) and t[0] == "star":
            toks.next()
            repeat = int(t[1][1:])
        duration = beat_duration
        for _ in range(repeat):
            beats.append(Beat(notes, rest, beat_duration, effects, bare, effects_span, effect_items, unread))

    return Bar(meta, beats, warnings), duration


def _validate_meta(name: str, args: list) -> None:
    def numbers(k: int) -> bool:
        return len(args) >= k and all(isinstance(a, (int, float)) for a in args[:k])

    if name == "tempo" and not (numbers(1) and args[0] > 0):
        raise ParseError("\\tempo のあとにテンポの数字がありません（\\tempo 147）")
    if name == "ts" and not (args[:1] == ["common"] or (numbers(2) and args[0] > 0 and args[1] in VALID_DURATIONS)):
        raise ParseError("\\ts のあとに拍子の数字 2 つがありません（\\ts 3 4）")
    if name == "rc" and not (numbers(1) and isinstance(args[0], int) and args[0] >= 1):
        raise ParseError("\\rc のあとに演奏回数がありません（\\rc 2）")
    if name == "ae":
        flat = args[0] if args and isinstance(args[0], list) else args
        if not flat or not all(isinstance(a, int) and a >= 1 for a in flat):
            raise ParseError("\\ae のあとに何番かっこかの数字がありません（\\ae (1) / \\ae (1 2)）")


def _looks_like_beat(word: str) -> bool:
    return bool(NOTE_WORD.match(word) or NOTE_DUR_WORD.match(word) or REST_WORD.match(word))


@dataclass
class Issue:
    bar: int
    level: str  # "error" / "warning"
    message: str

    def __str__(self) -> str:
        mark = "誤り" if self.level == "error" else "注意"
        return f"{self.bar} 小節: [{mark}] {self.message}"


def fmt_len(v: Fraction) -> str:
    return str(v.numerator) if v.denominator == 1 else f"{v.numerator}/{v.denominator}"


def check_bars(
    bars: dict[int, str], time_signature: tuple[int, int] = (4, 4), strings: int = 6
) -> tuple[list[Issue], dict[int, Bar]]:
    """小節ごとの長さ・タイのつながり・記法を調べる。bars は小節番号順に並べて調べる。"""
    issues: list[Issue] = []
    parsed: dict[int, Bar] = {}
    ts = time_signature
    duration = 4
    prev_beat: Beat | None = None
    # 直前の小節が手元にない（分担したファイルの先頭・抜け・読めない小節のあと）ときは
    # タイのつながり先を調べられないので、次の拍が来るまで調べない
    prev_unknown = False
    prev_number: int | None = None
    open_repeat = False

    for n in sorted(bars):
        if (prev_number is None and n > 1) or (prev_number is not None and n != prev_number + 1):
            prev_beat, prev_unknown = None, True
        prev_number = n
        try:
            bar, duration = parse_bar(bars[n], duration)
        except ParseError as e:
            issues.append(Issue(n, "error", f"読めません: {e}"))
            prev_beat, prev_unknown = None, True
            continue
        parsed[n] = bar
        issues.extend(Issue(n, "warning", w) for w in bar.warnings)

        if not bar.beats:
            issues.append(Issue(n, "error", "拍がありません（全休符なら r.1）"))
        if bar.time_signature:
            ts = bar.time_signature
        want = Fraction(ts[0] * 4, ts[1])
        got = bar.length(ts)
        if got != want:
            issues.append(Issue(n, "error", f"長さが {fmt_len(got)} 拍（{ts[0]}/{ts[1]} なら {fmt_len(want)} 拍）"))

        names = [m for m, _ in bar.meta if m in META_ORDER]
        if names != sorted(names, key=META_ORDER.index):
            issues.append(Issue(n, "warning", "メタデータの順は \\tempo → \\ts → \\ro → \\rc → \\ae"))
        if bar.repeat_open:
            open_repeat = True
        if bar.repeat_close:
            if not open_repeat:
                issues.append(Issue(n, "warning", "\\rc の前に \\ro がありません（曲の頭から繰り返す扱い）"))
            open_repeat = False

        for beat in bar.beats:
            if beat.duration not in VALID_DURATIONS:
                issues.append(Issue(n, "error", f"長さ .{beat.duration} は使えません"))
            if beat.bare:
                issues.append(Issue(n, "warning", "括弧のない単音があります（(7.3).8 のように括弧で囲む）"))
            for e in beat.effects:
                if e in KNOWN_BEAT_EFFECTS:
                    if e in beat.unread:
                        issues.append(Issue(n, "warning", f"{{{e}}} の引数の書き方が alphaTab の決まりに合いません"))
                    continue
                if e in NOTE_ONLY_EFFECTS:
                    written = next((bars[n][s:t] for name, _, (s, t) in beat.effect_items if name == e), e)
                    if beat.rest:
                        message = f"{{{e}}} は音の効果です。休符には付けられません"
                    else:
                        message = f"{{{e}}} は音の効果です。(7.5{{{written}}}).8 のように音の中に書きます"
                    if beat.unread or any(note.unread for note in beat.notes):
                        message += "（{ } の中に読めない書き方があるため、組み立てでは直しません）"
                    elif _fix_beat(bars[n], beat) is None:
                        message += "（音の中の効果と合わせると引数の区切りが変わるため、組み立てでは直しません）"
                    elif beat.rest:
                        message += "（組み立てでは外します）"
                    issues.append(Issue(n, "warning", message))
                else:
                    issues.append(Issue(n, "warning", f"知らない拍の効果 {{{e}}}"))
            seen = set()
            for note in beat.notes:
                if not 1 <= note.string <= strings:
                    issues.append(Issue(n, "error", f"{note.string} 弦はありません（1〜{strings}）"))
                if note.string in seen:
                    issues.append(Issue(n, "error", f"同じ拍に {note.string} 弦が 2 回あります"))
                seen.add(note.string)
                if note.fret.isdigit() and int(note.fret) > 24:
                    issues.append(Issue(n, "warning", f"フレット {note.fret} は大きすぎます"))
                for e in note.effects:
                    # 音に書いた拍の効果は、alphaTab がその拍に付ける
                    if e not in KNOWN_NOTE_EFFECTS and e not in KNOWN_BEAT_EFFECTS:
                        issues.append(Issue(n, "warning", f"知らない音の効果 {{{e}}}"))
                    elif e in note.unread:
                        issues.append(Issue(n, "warning", f"{{{e}}} の引数の書き方が alphaTab の決まりに合いません"))
                if note.is_tie and not prev_unknown:
                    origin = prev_beat.notes if prev_beat else []
                    if not any(o.string == note.string for o in origin):
                        issues.append(Issue(n, "error", f"{note.string} 弦のタイのつながり先（直前の拍の同じ弦の音）がありません"))
            if not beat.is_grace:
                prev_beat, prev_unknown = beat, False
    # 同じ小節の同じ指摘は 1 つにまとめる
    unique: list[Issue] = []
    seen_keys = set()
    for issue in issues:
        key = (issue.bar, issue.level, issue.message)
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(issue)
    return unique, parsed


@dataclass
class MovedEffects:
    moved: int = 0  # 拍の後ろから音へ付け直した効果の数（拍ごと）
    dropped: int = 0  # 休符の拍から外した効果の数


def move_note_effects(bars: dict[int, str]) -> tuple[dict[int, str], MovedEffects]:
    """拍の後ろに書かれた音の効果を、その拍のすべての音に付け直した小節の中身を返す。

    (7.5).8 {pm tempo 143} → (7.5{pm}).8 {tempo 143}、(5.5 0.6).8 {pm} → (5.5{pm} 0.6{pm}).8。
    同じ効果がすでに付いている音には重ねない。休符の拍に付いたものは外す。拍の効果（tempo・tu など）は
    引数（{tempo 143 hide} の hide など）ごと、書いた順と重なり（lyrics 0 "a" lyrics 1 "b" など）も
    そのまま拍に残し、ほかの書き方もそのまま残す。読めない小節と、
    { } の中に引数の決まりどおりに読めない効果がある拍、付け直すと音の中の効果と引数の区切りが
    変わってしまう拍は書き換えない。bars は書き換えない。
    """
    out: dict[int, str] = {}
    count = MovedEffects()
    duration = 4
    for n in sorted(bars):
        text = bars[n]
        try:
            bar, duration = parse_bar(text, duration)
        except ParseError:
            out[n] = text
            continue
        edits: list[tuple[int, int, str]] = []  # (始まり, 終わり, 置き換える文字)
        done: set[tuple[int, int]] = set()  # *N で繰り返した拍は 1 回だけ直す
        for beat in bar.beats:
            span = beat.effects_span
            wrong = [e for e in beat.effects if e in NOTE_ONLY_EFFECTS]
            if span is None or span in done or not wrong:
                continue
            done.add(span)
            fix = _fix_beat(text, beat)
            if fix is None:
                continue
            edits.extend(fix)
            if beat.rest or not beat.notes:
                count.dropped += len(wrong)
            else:
                count.moved += len(wrong)
        for start, end, new in sorted(edits, reverse=True):
            text = text[:start] + new + text[end:]
        out[n] = text
    return out, count


def _named(items: list[EffectItem]) -> list[tuple[str, list]]:
    return [(name, args) for name, args, _ in items]


def _reread_effects(
    inner: str, tables: list[dict[str, Signatures]]
) -> tuple[list[tuple[str, list]], set[str]] | None:
    """{ } の中身を、組み立て前と同じ _parse_effects で読み直す。戻り値は（書いた順の（名前, 引数）, 決まり
    どおりに読めなかった名前）。読めなければ None。"""
    toks = _Tokens(inner + "}")
    items: list[EffectItem] = []
    try:
        _, unread = _parse_effects(toks, tables, items)
    except ParseError:
        return None
    return (_named(items), unread) if toks.peek() is None else None


def _fix_beat(text: str, beat: Beat) -> list[tuple[int, int, str]] | None:
    """拍の後ろの音の効果を音へ付け直す書き換え（始まり, 終わり, 置き換える文字）の並び。

    拍の { } からは移す効果だけを取り除き、残す効果は書いたまま（順序と重なりも）残す。付け直したあとの
    { } を読み直し、効果と引数の並びが意図どおりのときだけ返す。音の { } の末尾に足すと前の効果の引数に
    読まれる（{tempo 143} に hide を足すと tempo の引数になる）ときは先頭に足す。どちらでも意図どおりに
    読めなければ None（その拍は書き換えない）。
    """
    if beat.unread or beat.effects_span is None:
        return None
    wrong = [item for item in beat.effect_items if item[0] in NOTE_ONLY_EFFECTS]
    kept = [(name, args) for name, args, _ in beat.effect_items if name not in NOTE_ONLY_EFFECTS]
    start, end = beat.effects_span
    edits: list[tuple[int, int, str]] = []
    if kept:
        block = text[start:end]
        for _, _, (s, e) in reversed(wrong):
            s, e = s - start, e - start
            # 前の空白ごと取り除く。{ の直後なら後ろの空白ごと取り除く
            if block[s - 1].isspace():
                while block[s - 1].isspace():
                    s -= 1
            else:
                while block[e].isspace():
                    e += 1
            block = block[:s] + block[e:]
        if _reread_effects(block[1:-1], [BEAT_EFFECT_ARGS, NOTE_EFFECT_ARGS]) != (kept, set()):
            return None
        edits.append((start, end, block))
    else:
        while start > 0 and text[start - 1].isspace():
            start -= 1
        edits.append((start, end, ""))
    if beat.rest:
        return edits
    # 拍に同じ音の効果が何度あっても、音には重ねず最後の指定だけを足す
    last = {item[0]: item for item in wrong}
    for note in beat.notes:
        moving = [item for item in wrong if last[item[0]] is item and item[0] not in note.effects]
        if not moving:
            continue
        add = " ".join(text[s:e] for _, _, (s, e) in moving)
        adding, current = _named(moving), _named(note.effect_items)
        if note.pos is None:
            return None
        if note.close is None:
            where, tries = (note.pos, note.pos), [(add, adding)]
        else:
            open_at = text.index("{", note.pos) + 1
            inner = text[open_at : note.close].strip()
            where = (open_at, note.close)
            tries = [(f"{inner} {add}", current + adding), (f"{add} {inner}", adding + current)]
        tables = [NOTE_EFFECT_ARGS, BEAT_EFFECT_ARGS]
        fit = next((t for t, want in tries if _reread_effects(t, tables) == (want, set())), None)
        if fit is None:
            return None
        edits.append((*where, fit if note.close is not None else "{" + fit + "}"))
    return edits
