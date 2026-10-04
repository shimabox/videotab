"""check / build: 読み取り結果を検査し、つないで alphaTex と HTML にする。

読み取り結果は parts/ の *.json（{"小節番号": "小節の中身"}）。分担して読んだときは
担当ごとに 1 ファイル。境目を 2 人で重ねて読んだ小節は、内容が一致すれば 1 つにし、
違えば食い違いとして一覧にして止める。解いた内容は resolve.json に書く。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from videotab import add, confine, inside
from videotab.alphatex import Issue, check_bars, move_note_effects
from videotab.render import render_html
from videotab.workdir import create_json, load_meta, read_json, write_json

DEFAULT_TUNING = "e4 b3 g3 d3 a2 e2"
MAX_BAR = 9999  # 小節番号の上限。大きな番号が 1 つあると、抜けている小節の一覧がその数だけ膨らむ


def norm(tex: str) -> str:
    return " ".join(tex.split())


@dataclass
class Conflict:
    bar: int
    readings: list[tuple[str, str]]  # (ファイル名, 小節の中身)


@dataclass
class Merged:
    bars: dict[int, str] = field(default_factory=dict)
    source: dict[int, str] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    resolved: list[int] = field(default_factory=list)

    @property
    def missing(self) -> list[int]:
        if not self.bars:
            return []
        return [n for n in range(1, max(self.bars) + 1) if n not in self.bars]


def load_part(path: Path) -> dict[int, str]:
    data = read_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: {{\"小節番号\": \"alphaTex\"}} の形ではありません")
    bars = {int(k): str(v) for k, v in data.items()}
    if bars and max(bars) > MAX_BAR:
        raise SystemExit(f"{path}: 小節番号 {max(bars)} が大きすぎます（{MAX_BAR} まで）")
    return bars


def part_files(workdir: Path) -> list[Path]:
    return sorted(confine.guard(workdir / "parts").glob("*.json"))


def merge(workdir: Path) -> Merged:
    files = part_files(workdir)
    if not files:
        raise SystemExit(f"{workdir / 'parts'} に読み取り結果（*.json）がありません")
    resolve_path = workdir / "resolve.json"
    overrides = load_part(resolve_path) if resolve_path.exists() else {}

    readings: dict[int, list[tuple[str, str]]] = {}
    for path in files:
        for n, tex in load_part(path).items():
            readings.setdefault(n, []).append((path.name, tex))

    merged = Merged()
    for n in sorted(set(readings) | set(overrides)):
        if n in overrides:
            merged.bars[n] = overrides[n]
            merged.source[n] = "resolve.json"
            if len({norm(t) for _, t in readings.get(n, [])}) > 1:
                merged.resolved.append(n)
            continue
        variants = {norm(t) for _, t in readings[n]}
        if len(variants) > 1:
            merged.conflicts.append(Conflict(n, readings[n]))
            continue
        merged.bars[n] = readings[n][0][1]
        merged.source[n] = readings[n][0][0]
    return merged


TS_META = re.compile(r"\\ts\s+(\d+)\s+(\d+)")


def time_signature_before(workdir: Path, bar: int, default: tuple[int, int]) -> tuple[int, int]:
    """bar より前の小節（ほかの担当の読み取り結果も含む）で最後に出た拍子。

    途中の小節から始まる担当のファイルを 1 つだけ検査するときに、それまでの拍子の変化を
    引き継ぐため。読めないファイル（閉じ込めがあるときの、外を指すリンクを含む）は飛ばす。
    """
    earlier: dict[int, str] = {}
    for path in part_files(workdir):
        try:
            for n, tex in load_part(path).items():
                if n < bar:
                    earlier.setdefault(n, tex)
        except (SystemExit, ValueError):
            continue
    ts = default
    for n in sorted(earlier):
        m = TS_META.search(earlier[n])
        if m:
            ts = (int(m.group(1)), int(m.group(2)))
    return ts


def load_score(workdir: Path) -> dict:
    """score.json（見出し）を読む。なければ雛形を作る。

    雛形は無いときだけ新しく作る。score.json が作業フォルダの外を指すリンクなら、先が無くても作らずに断る。
    """
    path = confine.guard(workdir / "score.json")
    meta = load_meta(workdir)
    creator = video_creator(meta)
    if not path.exists():
        template = {
            "title": meta.get("title") or workdir.name,
            "subtitle": default_subtitle(creator),
            "tab_by": creator,
            "tempo": None,
            "time_signature": [4, 4],
            "tuning": DEFAULT_TUNING,
            "capo": 0,
        }
        try:
            create_json(path, template, root=confine.root_of(workdir))
        except FileExistsError:
            pass  # 確かめたあとに作られた。そのまま読む
    score = read_json(path)
    score.setdefault("tuning", DEFAULT_TUNING)
    score.setdefault("time_signature", [4, 4])
    score.setdefault("capo", 0)
    score.setdefault("title", meta.get("title") or workdir.name)
    score.setdefault("tab_by", creator)  # 欄を作る前の score.json でも、動画の作成者を楽譜に残す
    return score


def default_subtitle(creator: str | None) -> str:
    """score.json の雛形の副題。動画の作成者がいれば名前を入れる。"""
    return f"{creator} さんの動画のタブ譜から書き起こし" if creator else "動画のタブ譜から書き起こし"


def retitle_score(score: dict, *, title: str, creator: str | None, old_meta: dict) -> dict:
    """曲の情報を書き換えたあとの score.json（見出し）。score は書き換えない。

    title と tab_by（作成者。無ければ None）を置き換える。subtitle は、いまの値が雛形の形の
    ときだけ新しい作成者の雛形にし、手で直した文言は残す。雛形の形とは、欄が無い・空、または
    書き換える前の meta.json の作成者・いまの tab_by・新しい作成者・作成者なしのどれかから作る
    雛形と同じこと（score.json だけ書けて meta.json を書けなかったあとに、同じ内容で保存し直しても
    揃うよう、新しい作成者も含める）。ほかの欄（tempo など）はそのまま。
    """
    out = dict(score)
    old_tab_by = str(score.get("tab_by") or "").strip() or None
    defaults = {default_subtitle(c) for c in (video_creator(old_meta), old_tab_by, creator, None)}
    subtitle = score.get("subtitle")
    if not str(subtitle or "").strip() or (isinstance(subtitle, str) and subtitle in defaults):
        out["subtitle"] = default_subtitle(creator)
    out["title"] = title
    out["tab_by"] = creator
    return out


def video_creator(meta: dict) -> str | None:
    """動画の作成者（取り込むときに利用者が入れた名前）。入れていなければ None。"""
    return str(meta.get("creator") or "").strip() or None


def source_link(meta: dict) -> str | None:
    """元動画のページ。取り込み時と同じ検査（add.check_link）に通るリンクだけを使う。

    https:// で始まり、空白と制御文字を含まず、長すぎないもの。meta.json は読み手も書けるので、
    取り込み時の検査に頼らず、使うたびに確かめる。
    """
    try:
        return add.check_link(str(meta.get("source_url") or ""))
    except ValueError:
        return None


def print_issues(issues: list[Issue]) -> int:
    errors = [i for i in issues if i.level == "error"]
    for i in issues:
        print(f"  {i}")
    return len(errors)


WORKDIR_SEARCH_DEPTH = 4  # ファイルから meta.json を探して上る階層の数


def workdir_of(path: Path) -> Path:
    """検査するファイルの作業フォルダ（見出しの score.json を読むフォルダ）。

    閉じ込めがあればその作業フォルダ、なければファイルから 4 階層上までで meta.json を持つ
    いちばん近いフォルダ。どちらも無ければ、parts/ の中なら parts/ の親、ほかはファイルのあるフォルダ。
    """
    root = confine.base()
    if root is not None:
        return root
    for folder in path.parents[:WORKDIR_SEARCH_DEPTH]:
        if (folder / "meta.json").exists():
            return folder
    return path.parent.parent if path.parent.name == "parts" else path.parent


def earlier_parts_folder(path: Path, workdir: Path) -> Path:
    """前の小節の拍子を探すフォルダ（parts/ を持つフォルダ）。

    X/parts/ の中のファイルなら X、すぐ隣に parts/ があればファイルのあるフォルダ
    （history/<日時>/resolve.json はその履歴の拍子を引き継ぐ）、どちらでもなければ作業フォルダ。
    閉じ込めがあるときは、決まったフォルダも作業フォルダの中かを確かめる（外なら断る）。
    """
    if path.parent.name == "parts":
        return confine.guard(path.parent.parent)
    if confine.guard(path.parent / "parts").is_dir():
        return confine.guard(path.parent)
    return workdir


def run_check(target: Path) -> int:
    """part_*.json 1 つか、作業フォルダ（parts/ の全部）を検査する。誤りがあれば 1 を返す。"""
    if target.is_file():
        files = [target]
        workdir = workdir_of(target)
        earlier = earlier_parts_folder(target, workdir)
    else:
        workdir = target
        files = part_files(workdir)
        if not files:
            raise SystemExit(f"{workdir / 'parts'} に読み取り結果（*.json）がありません")
    has_meta = confine.guard(workdir / "meta.json").exists()
    score = load_score(workdir) if has_meta else {"time_signature": [4, 4], "tuning": DEFAULT_TUNING}
    strings = len(score["tuning"].split())
    total_errors = 0
    for path in files:
        bars = load_part(path)
        ts = tuple(score["time_signature"])
        if bars and target.is_file():
            ts = time_signature_before(earlier, min(bars), ts)
        issues, _ = check_bars(bars, ts, strings)
        errors = sum(1 for i in issues if i.level == "error")
        total_errors += errors
        print(f"{path.name}: {len(bars)} 小節（{min(bars)}〜{max(bars)}）、誤り {errors}、注意 {len(issues) - errors}")
        print_issues(issues)
    return 1 if total_errors else 0


def tex_string(text: str) -> str:
    """alphaTex の "..." に入れる文字列。区切りと紛れる " と \\ は全角に置き換える。"""
    return '"' + str(text).replace("\\", "＼").replace('"', "＂").replace("\n", " ") + '"'


def alphatex_document(bars: dict[int, str], score: dict) -> str:
    ts = score["time_signature"]
    head = [f"\\title {tex_string(score['title'])}"]
    if score.get("subtitle"):
        head.append(f"\\subtitle {tex_string(score['subtitle'])}")
    if score.get("tab_by"):
        head.append(f"\\tab {tex_string(score['tab_by'])}")  # 楽譜には描かれず、Guitar Pro の Tab 欄に入る
    head += [f"\\tempo {score['tempo']:g}", ".", f"\\tuning {score['tuning']}"]
    if score.get("capo"):
        head.append(f"\\capo {score['capo']}")
    head.append(f"\\ts {ts[0]} {ts[1]}")
    return "\n".join(head + [" |\n".join(bars[n] for n in sorted(bars))]) + "\n"


HEAD_LINES = 8  # alphatex_document が書く見出しの行数の上限（title・subtitle・tab・tempo・.・tuning・capo・ts）
HEAD_END = re.compile(r"\\ts \d+ \d+")


def document_head(tex: str) -> tuple[dict[str, str], str]:
    """alphatex_document が書いた文書を、見出し（title・tempo・tuning・capo の値）と小節の並びに分ける。

    見出しは先頭から \\ts の行まで。その形でなければ、見出しは空にして、全体を小節の並びとして返す。
    title は、tex_string が全角に置き換えた " と \\ を元に戻す。
    """
    lines = tex.split("\n")
    end = next((i for i, line in enumerate(lines[:HEAD_LINES]) if HEAD_END.fullmatch(line)), None)
    if end is None:
        return {}, tex
    head: dict[str, str] = {}
    for line in lines[:end]:
        key, _, value = line.partition(" ")
        if key in ("\\title", "\\tempo", "\\tuning", "\\capo"):
            head.setdefault(key[1:], value)
    title = head.get("title")
    if title is not None:
        if len(title) >= 2 and title[0] == title[-1] == '"':
            title = title[1:-1]
        head["title"] = title.replace("＂", '"').replace("＼", "\\")
    return head, "\n".join(lines[end + 1 :])


def count_bars(body: str) -> int:
    """小節の並びにある小節の数（"..." の外の | で区切った数）。"""
    if not body.strip():
        return 0
    bars, quoted = 1, False
    for ch in body:
        if ch == '"':
            quoted = not quoted
        elif ch == "|" and not quoted:
            bars += 1
    return bars


def _number(text: str | None, kind=float):
    try:
        return kind(float(text))
    except (TypeError, ValueError, OverflowError):
        return kind(0)


def page_html(tex: str, meta: dict, *, name: str, generated_at: str | None = None) -> str:
    """alphaTex の文書 tex から作るタブ譜のページ。

    build が書く <ID>.html と、画面が /files/ で返すページの両方をこれで作る（画面は保存された HTML を
    返さず、<ID>.alphatex から要求のたびに組み立てる）。題名・テンポ・チューニング・カポ・小節の数は
    tex の見出しから、元動画の情報は meta（meta.json）から取る。tex も meta も読み手が書けるので、
    読めない値は既定の値にする。name は見出しに題名が無いときの題名（作業フォルダの名前）。
    """
    head, body = document_head(tex)
    return render_html(
        tex,
        title=head.get("title") or name,
        tempo=_number(head.get("tempo")),
        tuning=head.get("tuning") or DEFAULT_TUNING,
        capo=_number(head.get("capo"), int),
        bar_count=count_bars(body),
        source_url=source_link(meta),
        source_title=meta.get("title"),
        source_creator=video_creator(meta),
        generated_at=generated_at,
    )


def run_build(workdir: Path, allow_check_errors: bool = False) -> int:
    merged = merge(workdir)
    score = load_score(workdir)
    stop = False
    root = confine.root_of(workdir)  # 出力は、リンクをたどらずに作業フォルダの中へ書く（inside）

    if merged.conflicts:
        stop = True
        print(f"食い違い {len(merged.conflicts)} 小節（画像を見直し、正しい方を resolve.json に書く）:")
        for c in merged.conflicts:
            print(f"  {c.bar} 小節:")
            for name, tex in c.readings:
                print(f"    {name}: {norm(tex)}")
        write_json(
            workdir / "conflicts.json",
            {str(c.bar): {name: tex for name, tex in c.readings} for c in merged.conflicts},
            root=root,
        )
    else:
        with inside.open_dir(root) as top:
            inside.remove(top, "conflicts.json")  # リンクならリンクだけを消す
    if merged.missing:
        stop = True
        print(f"抜けている小節: {', '.join(map(str, merged.missing))}")

    issues, _ = check_bars(merged.bars, tuple(score["time_signature"]), len(score["tuning"].split()))
    errors = sum(1 for i in issues if i.level == "error")
    if issues:
        print(f"検査: 誤り {errors}、注意 {len(issues) - errors}")
        print_issues(issues)
    if errors and not allow_check_errors:
        stop = True
    if not score.get("tempo"):
        stop = True
        print(f"{workdir / 'score.json'} に tempo（最初のテンポ）を書いてください")

    print(f"小節 {len(merged.bars)}（1〜{max(merged.bars) if merged.bars else 0}）、読み取り結果 {len(part_files(workdir))} ファイル"
          + (f"、resolve.json で解いた食い違い {len(merged.resolved)}" if merged.resolved else ""))
    if stop:
        print("出力を止めました。上を直してからもう一度 videotab build してください。")
        return 1

    # 拍の後ろに書かれた音の効果があると alphaTab が楽譜を読めないので、出力だけ音の中へ移す
    bars, fixed = move_note_effects(merged.bars)
    if fixed.moved or fixed.dropped:
        print(f"拍の後ろに書かれた音の効果を音の中へ付け直しました: {fixed.moved} 件"
              + (f"（休符の拍から外したもの {fixed.dropped} 件）" if fixed.dropped else ""))
    tex = alphatex_document(bars, score)
    name = workdir.name
    html = page_html(tex, load_meta(workdir), name=name)
    out = workdir / f"{name}.html"
    with inside.open_dir(root) as top:
        inside.write_text(top, f"{name}.alphatex", tex, notify=print)
        inside.write_text(top, out.name, html, notify=print)
    print(f"出力: {out}")
    print(f"      {workdir / f'{name}.alphatex'}")
    return 0

