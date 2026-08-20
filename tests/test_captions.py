"""pipeline/captions.py 순수 단위테스트 (네트워크/OpenAI 호출 없음).

검증 항목:
- SRT 겹침 불변식 (_enforce_srt_invariants): 다음 cue 이전 종료(end <= next.start - eps),
  단조 증가, 최소지속 시도, 공간 없으면 merge. 구 버그(min-dur bump가 겹침 생성) 회귀 방지.
- total_dur clamp/drop (build_cues): 최종 영상 길이 뒤로 자막이 삐져나오지 않음.
- word 타임스탬프 cue 트랙 무결성 + EXPECTED_CC_LANGS 고정(8) + SRT 라운드트립.

동물봇/우주봇 공통(byte-identical). 실행: py -3 tests/test_captions.py  (각 봇 폴더에서)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline import captions as C

_passed = 0
_failed = 0
EPS = C.CUE_EPS
TOL = 1e-6


def check(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name} {extra}")
    else:
        _failed += 1
        print(f"  FAIL  {name} {extra}")


def gap_ok(cues):
    """불변식: 각 cue.end <= 다음 cue.start - eps (다음 cue 이전 종료)."""
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"] - EPS + TOL:
            return False
    return True


def monotonic(cues):
    for i in range(len(cues) - 1):
        if cues[i]["start"] > cues[i + 1]["start"] + TOL:
            return False
    return True


def all_positive(cues):
    return all(c["end"] > c["start"] for c in cues)


print("== 1. 정상 cue: 겹침 없음/단조/양수 유지 ==")
c1 = C._enforce_srt_invariants([
    {"start": 0.0, "end": 1.0, "text": "a"},
    {"start": 1.2, "end": 2.0, "text": "b"},
    {"start": 2.5, "end": 3.4, "text": "c"},
])
check("겹침 없음(불변식)", gap_ok(c1))
check("단조 증가", monotonic(c1))
check("모든 cue 양수 길이", all_positive(c1))
check("3개 유지", len(c1) == 3)

print("== 2. 구 버그 회귀: 0-길이 cue의 min-dur bump가 다음 cue와 겹치면 안 됨 ==")
# 구 코드: end<=start면 end=start+0.4 → 다음 start(0.1)를 넘겨 겹침 발생했음.
c2 = C._enforce_srt_invariants([
    {"start": 0.0, "end": 0.0, "text": "x"},   # 0-길이
    {"start": 0.1, "end": 0.5, "text": "y"},
    {"start": 0.6, "end": 1.0, "text": "z"},
])
check("0-길이 cue 있어도 겹침 없음", gap_ok(c2))
check("첫 cue가 다음 cue 이전에 종료", c2[0]["end"] <= c2[1]["start"] - EPS + TOL)
check("모든 cue 양수", all_positive(c2))

print("== 3. 최소지속: 공간 넉넉하면 0.4s 확보 ==")
c3 = C._enforce_srt_invariants([
    {"start": 0.0, "end": 0.05, "text": "a"},
    {"start": 3.0, "end": 3.5, "text": "b"},
])
check("첫 cue >= 0.4s (여유 있음)", (c3[0]["end"] - c3[0]["start"]) >= 0.4 - TOL)

print("== 4. 공간 없음 → merge (겹침 대신 병합) ==")
c4 = C._enforce_srt_invariants([
    {"start": 0.0, "end": 2.0, "text": "hello"},
    {"start": 0.0005, "end": 2.0, "text": "world"},   # 사실상 동시 시점
    {"start": 3.0, "end": 3.5, "text": "end"},
])
check("겹침 없음(merge 후)", gap_ok(c4))
check("cue 수 감소(merge 발생)", len(c4) == 2, f"[{len(c4)}]")
check("텍스트 병합(hello+world)", "hello" in c4[0]["text"] and "world" in c4[0]["text"])

print("== 5. build_cues total_dur clamp/drop ==")
lines = [
    {"text": "one", "duration": 0.8, "words": [{"text": "one", "start": 0.0, "end": 0.8}]},
    {"text": "two", "duration": 0.8, "words": [{"text": "two", "start": 0.0, "end": 0.8}]},
    {"text": "three", "duration": 0.8, "words": [{"text": "three", "start": 0.0, "end": 0.8}]},
]
cues_full = C.build_cues(lines, pad_between=0.18)
check("total_dur 없으면 3 cue 전부", len(cues_full) == 3, f"[{len(cues_full)}]")
cues_clamped = C.build_cues(lines, pad_between=0.18, total_dur=1.5)
check("total_dur=1.5 → 마지막 cue drop", len(cues_clamped) == 2, f"[{len(cues_clamped)}]")
check("모든 end <= total_dur", all(c["end"] <= 1.5 + TOL for c in cues_clamped))
check("겹침 없음(clamp 후)", gap_ok(cues_clamped))

print("== 6. build_cues: word 타임스탬프 트랙 무결성 ==")
lines2 = [
    {"text": "aa bb cc dd ee ff gg hh", "duration": 4.0, "words": [
        {"text": w, "start": i * 0.5, "end": i * 0.5 + 0.5}
        for i, w in enumerate(["aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh"])
    ]},
]
c6 = C.build_cues(lines2, pad_between=0.18)
check("word cue 겹침 없음", gap_ok(c6))
check("word cue 단조", monotonic(c6))
check("word cue 양수 길이", all_positive(c6))

print("== 7. EXPECTED_CC_LANGS 고정 (en+7=8) ==")
check("EXPECTED_CC_LANGS == 8", C.EXPECTED_CC_LANGS == 8, f"[{C.EXPECTED_CC_LANGS}]")
check("CAPTION_LANGS 7개", len(C.CAPTION_LANGS) == 7)

print("== 8. cues_to_srt ↔ parse_srt 라운드트립 ==")
srt = C.cues_to_srt([{"start": 0.0, "end": 1.0, "text": "hi"},
                     {"start": 1.2, "end": 2.0, "text": "there"}])
parsed = C.parse_srt(srt)
check("parse 2블록", len(parsed) == 2)
check("텍스트 보존", parsed[0]["text"] == "hi" and parsed[1]["text"] == "there")


# == 9. 번역 조각내기 (_tr_groups / _tr_chunk) — 가짜 클라이언트, 네트워크 없음 ==
print("== 9. 번역 조각내기 ==")

_G = C._tr_groups(["A a", "b c.", "D d", "e f.", "G g."])
check("문장 끝에서 끊음", _G == [[0, 1], [2, 3], [4]], str(_G))
check("문장부호 없어도 마지막 조각 살림", C._tr_groups(["x", "y"]) == [[0, 1]])
_G2 = C._tr_groups(["w%d" % i for i in range(13)])
check("마침표 없는 대본도 줄 수 상한으로 쪼갬",
      _G2 == [[0, 1, 2, 3, 4, 5], [6, 7, 8, 9, 10, 11], [12]], str(_G2))
check("모든 인덱스가 한 번씩만 들어감",
      sorted(i for g in _G2 for i in g) == list(range(13)))


class _FakeClient:
    """요청받은 줄 수에 대해 정해진 개수만 돌려주는 가짜 OpenAI 클라이언트."""

    def __init__(self, plan):
        self.plan = plan          # {요청줄수: 반환리스트}
        self.calls = []

    class _Msg:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})

    def _make(self, **kw):
        prompt = kw["messages"][0]["content"]
        import re as _re
        asked = len(_re.findall(r"^\d+\. ", prompt, flags=_re.M))
        self.calls.append(asked)
        out = self.plan.get(asked, [])
        return type("R", (), {"choices": [_FakeClient._Msg(
            __import__("json").dumps({"lines": out}))]})

    @property
    def chat(self):
        return type("C2", (), {"completions": type("C3", (), {"create": self._make})})


def _run(plan, texts):
    st = {"picked": {}, "calls": 0, "until": C.time.monotonic() + 30}
    cl = _FakeClient(plan)
    C._tr_chunk(cl, "ko", texts, list(range(len(texts))), 5, st)
    return st, cl


_TX = ["one.", "two.", "three.", "four."]

st, cl = _run({4: ["1", "2", "3", "4"]}, _TX)
check("개수 맞으면 1회 호출", cl.calls == [4] and len(st["picked"]) == 4)
check("순서대로 배정", [st["picked"][i] for i in range(4)] == ["1", "2", "3", "4"])

# 4줄 요청에 3줄만 → 2+2로 쪼개 다시 물어봄
st, cl = _run({4: ["a", "b", "c"], 2: ["x", "y"]}, _TX)
check("개수 어긋나면 반으로 쪼갬", cl.calls == [4, 2, 2], str(cl.calls))
check("쪼갠 결과로 전부 채움", len(st["picked"]) == 4)

# 끝까지 어긋나면 1줄씩 — 1줄 요청의 답은 무엇이든 그 줄의 번역
st, cl = _run({4: ["a"], 2: ["b"], 1: ["only", "extra"]}, _TX)
check("1줄까지 내려가 채움", len(st["picked"]) == 4, str(st["picked"]))
check("1줄 답이 쪼개져 오면 합침", st["picked"][0] == "only extra")

# 개수는 맞는데 빈 칸이 섞인 경우 → 그 줄만 재요청
st, cl = _run({4: ["a", "", "c", "d"], 1: ["B"]}, _TX)
check("빈 칸만 재요청", cl.calls == [4, 1] and st["picked"][1] == "B", str(cl.calls))

# 호출 상한을 넘기면 멈춘다(폭주 방지)
st = {"picked": {}, "calls": C._TR_MAX_CALLS, "until": C.time.monotonic() + 30}
cl = _FakeClient({4: ["a", "b", "c", "d"]})
C._tr_chunk(cl, "ko", _TX, [0, 1, 2, 3], 5, st)
check("호출 상한에서 중단", cl.calls == [] and st["picked"] == {})

print(f"\n=== 결과: {_passed} PASS / {_failed} FAIL ===")
if __name__ == "__main__":
    # 모듈 최상위 sys.exit는 unittest discover/pytest의 import 단계에서 SystemExit을 일으켜
    # 전부 통과인데도 FAILED(errors)로 집계됐다(2026-07-17 fix). 직접 실행 시에만 종료코드 전달.
    sys.exit(1 if _failed else 0)
elif _failed:
    # discover/pytest로 import된 경우에도 실패가 초록으로 묻히지 않게 에러로 표면화
    raise AssertionError(f"test_captions: {_failed}개 체크 실패 (직접 실행: py -3 tests/test_captions.py)")
