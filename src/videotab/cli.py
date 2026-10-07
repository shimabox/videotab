"""videotab のコマンドライン。

  videotab serve                     動画ファイルを送るとタブ譜まで作る画面を開く
  videotab run FILE|ID               取り込みからタブ譜の出力までを通しで実行（画面なし）
  videotab add FILE                  動画ファイルを新しい作業フォルダ work/<ID>/ に取り込む
  videotab frames ID [--from DIR]    1 秒 1 枚の画像にする（既存の画像フォルダや ZIP も取り込める）
  videotab strip ID                  タブの帯と弦の線の位置を検出
  videotab pages ID                  ページに分けて拡大画像を作る
  videotab zoom ID FRAME             1 フレームを拡大する
  videotab check ID|FILE             読み取り結果（小節ごとの alphaTex）を検査
  videotab build ID                  読み取り結果をつないで alphaTex と HTML を出力
  videotab verify ID                 繰り返し・テンポを展開した時刻を動画と比べる
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from videotab import __version__
from videotab.workdir import WORK_ROOT, load_meta, resolve_target, save_meta


def cmd_serve(args) -> int:
    from videotab.server import serve

    serve(Path(args.root).resolve(), port=args.port, open_browser=not args.no_open)
    return 0


USUAL = "usual"  # --model / --effort で「普段の設定に戻す」ときの書き方


def _run_choice(args, data: dict):
    """videotab run で使うエンジンと選択を決める。検査に通らなければ SystemExit。

    既存の曲では、--engine を付けないか保存済みと同じなら、保存された選択を引き継ぎ、
    付けた項目だけを上書きする。違うエンジンを付けたら、保存された選択は引き継がない。
    """
    from videotab import agent_settings
    from videotab.agent import DEFAULT_ENGINE

    saved_engine = (data.get("engine") or DEFAULT_ENGINE) if data else None
    engine = args.engine or saved_engine or DEFAULT_ENGINE
    stored = data.get("choice") if data and engine == saved_engine else None
    stored = stored if isinstance(stored, dict) else {}
    model, effort = stored.get("model"), stored.get("effort")
    if args.model is not None:
        model = None if args.model == USUAL else args.model
    if args.effort is not None:
        effort = None if args.effort == USUAL else args.effort
    try:
        return engine, agent_settings.normalize_choice(engine, model, effort)
    except ValueError as e:
        name = agent_settings.ENGINE_NAMES[engine]
        raise SystemExit(f"{name} の読み取りのモデルと推論の強さの指定が使えません: {e}") from None


def _add_video(src: Path, args) -> Path:
    """動画ファイル src を、args.root の下の新しい作業フォルダに取り込む。"""
    from videotab import add

    try:
        return add.add(src, Path(args.root), title=args.title, creator=args.creator, source_url=args.source_url)
    except add.NotVideo as e:
        raise SystemExit(str(e)) from None


def _resume_command(job_id: str, root: Path) -> str:
    """途中で止まった曲を同じ作業フォルダで続けるコマンド。既定の置き場（work/）なら --root を付けない。"""
    words = ["videotab", "run", job_id]
    if root != WORK_ROOT.resolve():
        words += ["--root", str(root)]
    return shlex.join(words)


def cmd_run(args) -> int:
    from contextlib import ExitStack

    from videotab.pipeline import STEP_NAMES, Busy, Job, has_steps
    from videotab.workdir import ID_PATTERN

    root = Path(args.root).resolve()
    existing = root / args.target if ID_PATTERN.fullmatch(args.target) else None
    known = existing is not None and ((existing / "meta.json").exists() or (existing / "job.json").exists())
    if known:
        given = [opt for opt, v in (("--title", args.title), ("--creator", args.creator),
                                    ("--source-url", args.source_url)) if v is not None]  # fmt: skip
        if given:
            raise SystemExit(f"{'・'.join(given)} は動画ファイルを新しく取り込むときだけ指定できます"
                             f"（{args.target} は取り込み済みの曲です）")  # fmt: skip
        workdir = existing
    elif Path(args.target).is_file():
        if args.step:
            raise SystemExit("--step は既存の作業フォルダの ID と一緒に指定してください（新しい動画は最初の段から実行します）")
        # 指定の誤りで取り込み済みのフォルダを残さないよう、取り込む前に検査する
        engine, choice = _run_choice(args, {})
        workdir = _add_video(Path(args.target), args)
        print(f"取り込みました: {workdir}（ID: {workdir.name}）", flush=True)
    else:
        raise SystemExit("動画ファイルか、既存の作業フォルダの ID を指定してください")
    job = Job(workdir)
    with ExitStack() as stack:
        # 準備（job の読み込み・作り直し・やり直し）から実行の終わりまで job.lock を握る。
        # 画面の実行や削除と重ならない
        try:
            stack.enter_context(job.hold())
        except Busy:
            raise SystemExit(f"{workdir.name} は別の videotab（画面など）が実行中です") from None
        if not known:
            Job.create(workdir, engine, choice=choice)
        else:
            data = job.load()
            if data and not args.step and data.get("status") == "done":
                print(f"できあがっています: {workdir / (workdir.name + '.html')}（作り直すなら --step で段を指定）")
                return 0
            engine, choice = _run_choice(args, data)  # job を作る・書き換える前に検査する
            if args.step and has_steps(data):
                # 画面の「やり直す」と同じく、指定した段から後だけを未実行に戻す（前の段の記録は残す）。
                # 前の段で済んでいないものは、済んでいるものとする
                job.reset_from(args.step, engine, choice)
                for st in job.load()["steps"][: STEP_NAMES.index(args.step)]:
                    if st.get("status") != "done":
                        job.update_step(st["name"], status="done", started=None, ended=None, seconds=None,
                                        message="済み")  # fmt: skip
            elif args.step or not data:
                Job.create(workdir, engine, choice=choice)
                if args.step:
                    for name in STEP_NAMES[: STEP_NAMES.index(args.step)]:
                        job.update_step(name, status="done", message="済み")
            else:
                pending = next((st["name"] for st in data["steps"] if st["status"] != "done"), None)
                if pending:
                    job.reset_from(pending, engine, choice)
        # ログを端末にも出す
        original = job.log

        def log_both(message: str) -> None:
            original(message)
            print(message, flush=True)

        job.log = log_both
        ok = job.run()  # 握ったまま実行する（握り直さない）
    if ok:
        print(f"できあがり: {workdir / (workdir.name + '.html')}")
        return 0
    print(f"途中で止まりました（{job.path} と {job.log_path}）")
    # 動画ファイルをもう一度渡すと別の曲になるので、続きは ID で指す
    print(f"続きは {_resume_command(workdir.name, root)}")
    return 1


def cmd_add(args) -> int:
    workdir = _add_video(Path(args.file), args)
    meta = load_meta(workdir)
    print(f"{workdir}  {meta.get('title', '')}")
    print(f"ID: {workdir.name}")
    # 作業フォルダのパスで指せば、--root を付けなくても同じ作業フォルダで続けられる
    print(f"次: videotab frames {shlex.quote(str(workdir))}")
    return 0


def cmd_frames(args) -> int:
    from videotab import frames

    workdir = resolve_target(args.target)
    if args.source:
        n = frames.import_folder(workdir, Path(args.source), fps=args.fps, force=args.force)
    else:
        n = frames.extract(workdir, fps=args.fps, force=args.force)
    print(f"{workdir / 'frames'}: {n} 枚")
    print(f"次: videotab strip {workdir}")
    return 0


def _reader_target(target: str) -> Path | None:
    """読み取りのエージェントから実行されたとき（VIDEOTAB_CONFINE あり）の対象のパス。無ければ None。

    引数はパスとしてだけ扱い（ID の読み替えはしない）、ファイルシステムに触れる前に作業フォルダの
    中かを確かめる。以降は、確かめたあとのパス（作業フォルダの中の実体）だけを使う。
    """
    from videotab import confine

    if confine.base() is None:
        return None
    return confine.guard(target, given=True)


def _frames_or_exit(workdir: Path):
    from videotab.workdir import list_frames

    frames = list_frames(workdir)
    if not frames:
        raise SystemExit(f"{workdir / 'frames'} に画像がありません（先に videotab frames）")
    return frames


def _check_frames(frames, result):
    """確認用の画像にするフレーム。

    位置が動かなければ動画全体から間引いた 3 枚。動くときは、いちばん上の位置の区間・いちばん下の
    位置の区間・フレームのいちばん多い区間から、線の位置が見つかったフレームの真ん中を 1 枚ずつ選ぶ
    （前奏などタブのない画面も前後の区間に入っているので、それを避ける）。
    """
    from videotab import strip

    if result.corrections:
        found = [f for f in frames if f.index in result.located]
        return strip.sample_frames(found, 3)
    if not result.shifts:
        return strip.sample_frames(frames, 3)
    segs = result.shifts
    chosen = {
        min(segs, key=lambda s: s[2]),
        max(segs, key=lambda s: s[2]),
        max(segs, key=lambda s: s[1] - s[0]),
    }
    picks = []
    for a, b, _ in chosen:
        inner = [f for f in frames if a <= f.index <= b]
        inner = [f for f in inner if f.index in result.located] or inner
        picks.append(inner[len(inner) // 2])
    return sorted(picks, key=lambda f: f.index)


def cmd_strip(args) -> int:
    from videotab import confine, inside, strip
    from videotab.workdir import fmt_time

    workdir = resolve_target(args.target)
    frames = _frames_or_exit(workdir)
    try:
        result = strip.detect(frames, band=tuple(args.band) if args.band else None)
    except strip.NoTabFound as e:
        print(e, file=sys.stderr)
        return strip.NO_TAB_EXIT  # 通しの実行はこの終了コードで動画と画像を消す
    meta = load_meta(workdir)
    meta["strip"] = {
        "band": list(result.band),
        "polarity": result.polarity,
        "background": list(result.background),
        "staves": [[round(y, 1) for y in st.lines] for st in result.staves],
    }
    if result.shifts:  # ページによって段が上下に動く動画だけ。フレームの区間ごとの基準からのずれ
        meta["strip"]["shifts"] = [[a, b, d] for a, b, d in result.shifts]
    if result.corrections:
        meta["strip"]["corrections"] = [list(row) for row in result.corrections]
    if args.band:
        meta["strip"]["band_given"] = True  # 通しの実行でやり直しても、手で決めた帯を使う
    save_meta(workdir, meta)

    out = workdir / "strip"
    picks = _check_frames(frames, result)
    # strip/ が作業フォルダの外を指すリンクなら作り直し、リンクをたどらずに書く
    with inside.open_dir(confine.root_of(workdir), "strip", remake=True, notify=print) as folder:
        for f in picks:
            image = strip.debug_image(strip.load_rgb(f.path), result, dy=result.dy_at(f.index),
                                      slope=result.slope_at(f.index), scale=result.scale_at(f.index))
            inside.write_image(folder, f"check_{f.index:04d}.png", image, notify=print)
    kind = "白地に濃い線" if result.polarity < 0 else "暗い地に明るい線"
    print(f"帯: y={result.band[0]}〜{result.band[1]}（{kind}、地の色 {result.background}）")
    if result.corrections:
        print("カメラ撮影の傾き・上下の揺れ・弦の間隔の伸縮をフレームごとに補正します。")
        print("ぼけや映り込みで数字を読み違えることがあります。可能なら画面録画の動画を使ってください。")
    if result.shifts:
        top = result.staves[0].lines[0]
        ds = [d for _, _, d in result.shifts]
        print(
            f"線の位置はページによって上下に動きます: 1 弦 y={top + min(ds):.1f}〜{top + max(ds):.1f}"
            f"（区間 {len(result.shifts)} 個）"
        )
    mark = "（基準）" if result.shifts else ""
    for k, st in enumerate(result.staves, start=1):
        ys = ", ".join(f"{y:.1f}" for y in st.lines)
        print(f"タブ {k} 段目{mark}: 1〜6 弦の線 y={ys}（間隔 {st.spacing:.2f}px）")
    print(f"タブが見えたフレーム: {result.frames_with_tab}/{result.frames_used}（間引いて調べた枚数）")
    print(f"確認用の画像: {out}/check_*.png（" + ", ".join(fmt_time(f.time) for f in picks) + "）")
    print(f"次: videotab pages {workdir}")
    return 0


def cmd_pages(args) -> int:
    from videotab import pages

    workdir = resolve_target(args.target)
    frames = _frames_or_exit(workdir)
    setup = pages.Setup.from_meta(load_meta(workdir), frames)
    det = pages.detect_pages(frames, setup, threshold=args.threshold)
    out = pages.write_pages(workdir, frames, setup, det)
    longs = [p.number for p in det.pages if p.duration > pages.LONG_PAGE]
    print(f"{len(det.pages)} ページ（しきい値 {det.threshold:.3f}）→ {out}/index.md")
    if det.no_tab:
        print(f"タブの見えないフレーム {len(det.no_tab)} 枚（前奏・終わりなど）は除きました")
    if longs:
        print(f"長く同じページが続く所: ページ {', '.join(map(str, longs))}（切り替えの取りこぼしに注意）")
    return 0


def cmd_zoom(args) -> int:
    from videotab import confine, pages
    from videotab.workdir import list_frames

    with confine.short_errors():
        workdir = _reader_target(args.target) or resolve_target(args.target)
        frames = list_frames(workdir)
        setup = pages.Setup.from_meta(load_meta(workdir), frames)
        frame = next((f for f in frames if f.index == args.frame), None)
        if frame is None:
            raise SystemExit(f"フレーム {args.frame} がありません")
        for p in pages.zoom_frame(workdir, frame, setup):
            print(p)
    return 0


def cmd_check(args) -> int:
    from videotab import build, confine

    with confine.short_errors():
        target = _reader_target(args.target)
        if target is None:
            target = Path(args.target) if Path(args.target).is_file() else resolve_target(args.target)
        return build.run_check(target)


def cmd_build(args) -> int:
    from videotab import build

    workdir = resolve_target(args.target)
    return build.run_build(workdir, allow_check_errors=args.allow_check_errors)


def cmd_verify(args) -> int:
    from videotab import verify

    workdir = resolve_target(args.target)
    return verify.run_verify(workdir, marks=args.mark, tolerance=args.tolerance)


def main(argv: list[str] | None = None) -> int:
    from videotab.add import VIDEO_EXTS

    p = argparse.ArgumentParser(
        prog="videotab", description="手元の動画ファイルから、画面に写るタブ譜を alphaTab の HTML＋alphaTex に書き起こす"
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="動画ファイルを送るとタブ譜まで作る画面を開く")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--root", default="work", help="作業フォルダを置く場所（既定 work/）")
    s.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    s.set_defaults(func=cmd_serve)

    def video_fields(s) -> None:
        s.add_argument("--title", help="題名（既定はファイル名）")
        s.add_argument("--creator", help="動画の作成者（楽譜とできあがりのページに出す）")
        s.add_argument("--source-url", help="元動画のページ（https:// で始まるもの。できあがりのページからリンクする）")

    s = sub.add_parser("run", help="取り込みからタブ譜の出力までを通しで実行（画面なし）")
    s.add_argument("target", help="動画ファイルか、既存の作業フォルダの ID")
    video_fields(s)
    # 省略と明示を区別する（省略すると、新しい曲は Claude Code、既存の曲は保存済みのエンジン）
    s.add_argument("--engine", choices=["claude", "codex"], default=None,
                   help="読み取りに使うエージェント（既定: 新しい曲は claude、既存の曲は前回のまま）")
    s.add_argument("--model", help="読み取りのモデル（既定: 前回の指定か普段の設定。usual で普段の設定に戻す）")
    s.add_argument("--effort", help="読み取りの推論の強さ（既定: 前回の指定か普段の設定。usual で普段の設定に戻す）")
    s.add_argument("--step", choices=["add", "frames", "strip", "pages", "read", "build", "verify"],
                   help="既存の曲で、この段から実行する（それより前は済んでいるものとする）")
    s.add_argument("--root", default="work", help="作業フォルダを置く場所（既定 work/）")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("add", help="動画ファイルを新しい作業フォルダ work/<ID>/ に取り込む")
    s.add_argument("file", help=f"動画ファイル（{' '.join(VIDEO_EXTS)}）")
    video_fields(s)
    s.add_argument("--root", default="work", help="作業フォルダを作る場所（既定 work/）")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("frames", help="動画を一定間隔の画像にする")
    s.add_argument("target", help="作業フォルダ（work/ 以下の ID かパス）")
    s.add_argument("--from", dest="source", help="動画の代わりに取り込む画像フォルダか ZIP（komadori の書き出しなど）")
    s.add_argument("--fps", type=float, default=1.0, help="1 秒あたりの枚数（既定 1）")
    s.add_argument("--force", action="store_true", help="既存の画像を消して作り直す")
    s.set_defaults(func=cmd_frames)

    s = sub.add_parser("strip", help="タブの帯と弦の線の位置を検出")
    s.add_argument("target")
    s.add_argument("--band", type=int, nargs=2, metavar=("Y0", "Y1"), help="この y 範囲の中だけで探す")
    s.set_defaults(func=cmd_strip)

    s = sub.add_parser("pages", help="ページに分けて拡大画像を作る")
    s.add_argument("target")
    s.add_argument("--threshold", type=float, help="ページ切り替えのしきい値（既定は自動）")
    s.set_defaults(func=cmd_pages)

    s = sub.add_parser("zoom", help="1 フレームをページと同じ倍率で拡大する")
    s.add_argument("target")
    s.add_argument("frame", type=int, help="フレーム番号（ファイル名の先頭 4 桁）")
    s.set_defaults(func=cmd_zoom)

    s = sub.add_parser("check", help="読み取り結果を検査（作業フォルダなら parts/ の全部）")
    s.add_argument("target", help="作業フォルダか part_*.json")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("build", help="読み取り結果をつないで alphaTex と HTML を出力")
    s.add_argument("target")
    s.add_argument("--allow-check-errors", action="store_true", help="検査の誤りがあっても出力する")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("verify", help="繰り返し・テンポを展開した時刻を動画と比べる")
    s.add_argument("target")
    s.add_argument("--mark", action="append", default=[], metavar="BAR=SEC", help="動画で見た小節の開始時刻")
    s.add_argument("--tolerance", type=float, default=2.0, help="ずれの許容（秒、既定 2）")
    s.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
