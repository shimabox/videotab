"""小節ごとの alphaTex を読み、長さ・タイ・記法を調べる。

読み取り結果は {"小節番号": "小節の中身（末尾の | なし）"} の形で持つ。ここではその
「小節の中身」を解釈する。alphaTab の文法の全部ではなく、書き起こしで使う範囲:

- 小節の先頭のメタデータ: \\tempo N、\\ts N M、\\ro、\\rc N、\\ae (1 2) / \\ae N、\\section "..." など
- 拍: (音 音 ...).長さ {拍の効果}、休符 r.長さ、括弧なしの単音 7.3.8、:N（以後の既定の長さ）、*N（同じ拍の繰り返し）
- 音: フレット.弦 {音の効果}。フレットは数字、x（ブラッシング）、-（タイの続き）
- 拍の効果: {d}（付点）、{dd}、{tu 3}（連符）、{gr}（装飾音）、{tempo N}（拍の途中のテンポ変化）、{ch "Am"} など
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

# 拍子を変えない効果・変える効果を区別する必要はないが、知らない効果は注意として出す
KNOWN_BEAT_EFFECTS = {
    "d", "dd", "tu", "gr", "tempo", "ch", "f", "fo", "vs", "v", "vw", "s", "p", "tt", "txt", "lyrics",
    "su", "sd", "cre", "dec", "spd", "sph", "spu", "sd", "ad", "au", "dy", "tb", "tbe", "bu", "bd",
    "cb", "rasg", "ot", "legatoorigin", "timer", "beam", "slashed", "ds", "fermata", "pm", "lr", "h",
    "sl", "v", "st", "x",
}  # fmt: skip
KNOWN_NOTE_EFFECTS = {
    "pm", "h", "sl", "ss", "sib", "sia", "sou", "sod", "psu", "psd", "b", "be", "v", "vw", "nh", "ah",
    "th", "ph", "sh", "fh", "lr", "x", "t", "tr", "st", "ac", "hac", "ten", "g", "lf", "rf", "string",
    "hide", "slur", "turn", "iturn", "umordent", "lmordent", "tp", "ds",
}  # fmt: skip
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
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, str] | None:
        j = self.i + k
        return self.items[j] if j < len(self.items) else None

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


def _parse_effects(toks: _Tokens) -> dict[str, list]:
    """{ ... } の中身。名前のあとに続く数字・文字列・( ) をその引数とする。"""
    effects: dict[str, list] = {}
    current: str | None = None
    while True:
        kind, val = toks.next()
        if kind == "rbrace":
            return effects
        if kind == "word" and not re.fullmatch(r"-?\d+(\.\d+)?", val):
            current = val
            effects[current] = []
        elif current is None:
            raise ParseError(f"効果の名前の前に {val!r} があります")
        elif kind == "lparen":
            effects[current].append(_parse_group(toks))
        elif kind == "str":
            effects[current].append(val[1:-1])
        elif kind == "word":
            effects[current].append(_value(val))
        else:
            raise ParseError(f"{{ }} の中に {val!r} があります")


NOTE_WORD = re.compile(r"^(\d+|x|X|-)\.(\d+)$")
NOTE_DUR_WORD = re.compile(r"^(\d+|x|X|-)\.(\d+)\.(\d+)$")
REST_WORD = re.compile(r"^r(?:\.(\d+))?$")
DUR_WORD = re.compile(r"^\.(\d+)$")


def _parse_note(word: str, toks: _Tokens) -> Note:
    m = NOTE_WORD.match(word)
    if not m:
        raise ParseError(f"音の書き方が違います: {word!r}（フレット.弦）")
    fret = m.group(1).lower()
    note = Note(fret, int(m.group(2)))
    if (t := toks.peek()) and t[0] == "lbrace":
        toks.next()
        note.effects = _parse_effects(toks)
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
            notes.append(Note(m.group(1).lower(), int(m.group(2))))
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
        if (t := toks.peek()) and t[0] == "lbrace":
            toks.next()
            effects = _parse_effects(toks)
        repeat = 1
        if (t := toks.peek()) and t[0] == "star":
            toks.next()
            repeat = int(t[1][1:])
        duration = beat_duration
        for _ in range(repeat):
            beats.append(Beat(notes, rest, beat_duration, effects, bare))

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
                if e not in KNOWN_BEAT_EFFECTS:
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
                    if e not in KNOWN_NOTE_EFFECTS:
                        issues.append(Issue(n, "warning", f"知らない音の効果 {{{e}}}"))
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
