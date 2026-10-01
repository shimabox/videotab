"""読み取りのテストで使う、偽のエージェントと合成の作業フォルダ。"""

import io
import json
import re

from synth import frame, write_frames

from videotab.agent import AgentResult


class FakeAgent:
    """読み手・まとめ役の代わり。プロンプトの中の出力先に、決まった結果を書く。

    担当 A と B は重ねて読んだ 2 小節目を違う内容で書き、まとめ役がそれを解く。
    """

    def __init__(self, actual=None):
        self.prompts = []
        self.settings = []  # 起動ごとの（担当, 受け取った読み取りの設定）
        self.actual = actual  # 担当 → on_actual に渡す本文（実際のモデルが分かった起動の代わり）

    def __call__(self, prompt, *, engine, workdir, writable, log, label, settings=None, on_actual=None, cancel=None):
        self.prompts.append((label, prompt, writable))
        self.settings.append((label, settings))
        if self.actual and label in self.actual and on_actual is not None:
            on_actual(self.actual[label])
        wd = workdir.resolve()
        if label in ("A", "B"):
            bars = {"1": "r.1", "2": "(0.6).1"} if label == "A" else {"2": "(0.5).1", "3": "\\rc 2 (3.5).1"}
            (wd / "parts" / f"part_{label}.json").write_text(json.dumps(bars), encoding="utf-8")
            pages = [int(p) for p in re.findall(r"^\| (\d+) \|", prompt, re.M)]
            (wd / "readers" / f"pagebars_{label}.json").write_text(
                json.dumps({str(p): (1 if label == "A" else 2) for p in pages}), encoding="utf-8"
            )
            if label == "A":
                score = json.loads((wd / "score.json").read_text(encoding="utf-8"))
                score["tempo"] = 120
                (wd / "score.json").write_text(json.dumps(score, ensure_ascii=False), encoding="utf-8")
        elif label == "まとめ役":
            assert "2 小節の食い違い" in prompt
            (wd / "resolve.json").write_text(json.dumps({"2": "(0.6).1"}), encoding="utf-8")
        log(f"[{label}] fake done")
        return AgentResult(True, f"{label} の報告", 0.1)

def send(app, job_id, engine="claude"):
    """画面のアップロードの代わりに、App.receive へ動画を渡す（plain_ids なら job_id が ID になる）。"""
    return app.receive(io.BytesIO(b"video"), 5, f"{job_id}.mp4", engine)


def make_work(tmp_path, n_pages=10):
    wd = tmp_path / "work" / "abcdefghijk"
    seq = []
    for page in range(n_pages):
        seq += [frame(page, rng_seed=10 * page + k) for k in range(3)]
    write_frames(wd, seq)
    (wd / "meta.json").write_text(json.dumps({"id": wd.name, "title": "テスト曲", "source_url": "https://example.com/abcdefghijk", "frames_from": "test"}), encoding="utf-8")
    return wd
