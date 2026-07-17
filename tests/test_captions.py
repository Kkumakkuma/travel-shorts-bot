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

print(f"\n=== 결과: {_passed} PASS / {_failed} FAIL ===")
if __name__ == "__main__":
    # 모듈 최상위 sys.exit는 unittest discover/pytest의 import 단계에서 SystemExit을 일으켜
    # 전부 통과인데도 FAILED(errors)로 집계됐다(2026-07-17 fix). 직접 실행 시에만 종료코드 전달.
    sys.exit(1 if _failed else 0)
elif _failed:
    # discover/pytest로 import된 경우에도 실패가 초록으로 묻히지 않게 에러로 표면화
    raise AssertionError(f"test_captions: {_failed}개 체크 실패 (직접 실행: py -3 tests/test_captions.py)")
