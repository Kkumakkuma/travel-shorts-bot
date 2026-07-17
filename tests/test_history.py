"""pipeline/history.py 순수 단위테스트 (API 호출 없음).

검증 항목:
- 시간창 제외 (recent_topics)
- 완화 (compute_exclude: 후보 8개 미만이면 창 0.75배씩 완화, 최소 7일)
- 마이그레이션 (migrate_from_used: 날짜 미상 → 오늘-창간격/2)
- 클립 30일 제외 (recent_clip_ids)
- angle/format 로테이션 (recent_formats, topic_entries)
- 내용 겹침 (facts_too_similar)

실행: py -3 tests/test_history.py  (동물봇 폴더에서)
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_HIST_PY = os.path.join(_HERE, "..", "pipeline", "history.py")
_spec = importlib.util.spec_from_file_location("history", _HIST_PY)
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

NOW = datetime(2026, 7, 5)
_passed = 0
_failed = 0


def check(name, cond, extra=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name} {extra}")
    else:
        _failed += 1
        print(f"  FAIL  {name} {extra}")


def d(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def entry(date, angle="", fmt="", facts=None, clips=None, title=""):
    return {"date": date, "angle": angle, "format": fmt,
            "facts": facts or [], "title": title, "clip_ids": clips or []}


print("== 1. recent_topics 시간창 제외 ==")
hist = {
    "otter": [entry(d(10))],      # 10일 전
    "fox": [entry(d(100))],       # 100일 전
    "owl": [entry(d(50))],        # 50일 전
}
r60 = H.recent_topics(hist, 60, now=NOW)
check("60일 창: otter/owl 포함, fox 제외", r60 == {"otter", "owl"})
r30 = H.recent_topics(hist, 30, now=NOW)
check("30일 창: otter만", r30 == {"otter"})

print("== 2-a. compute_exclude 완화 성공 (창 줄이면 후보 확보) ==")
pool = [f"t{i}" for i in range(10)]
hist2 = {f"t{i}": [entry(d(20))] for i in range(8)}  # t0..t7 20일 전 사용
excl, eff = H.compute_exclude(hist2, used=[], keep=45, window_days=60,
                              pool=pool, min_candidates=8, min_window=7, now=NOW)
free = [p for p in pool if p not in excl]
check("완화 후 후보 >= 8 확보", len(free) >= 8)
check("완화된 창이 20일 미만이라 20일전 항목 해제됨", eff < 20)
check("eff_window가 min_window(7) 이상", eff >= 7)

print("== 2-b. compute_exclude 완화 하한(7일) 고정, 전체 리셋 안 함 ==")
hist3 = {f"t{i}": [entry(d(5))] for i in range(8)}   # t0..t7 5일 전 (7일 창에도 걸림)
excl3, eff3 = H.compute_exclude(hist3, used=[], keep=45, window_days=60,
                                pool=pool, min_candidates=8, min_window=7, now=NOW)
check("하한 7일에서 멈춤", abs(eff3 - 7) < 1e-9)
check("5일전 항목은 7일 창에 여전히 제외(전체 리셋 아님)", len({f"t{i}" for i in range(8)} & excl3) == 8)

print("== 2-c. compute_exclude 개수 기반 + 시간 기반 합집합 ==")
hist4 = {"a": [entry(d(10))]}   # 시간 기반 a
excl4, _ = H.compute_exclude(hist4, used=["x", "y", "z"], keep=2, window_days=60,
                             pool=None, now=NOW)
# keep=2 → used 마지막 2개 (y,z) + 시간기반 a
check("합집합 = {a,y,z} (used 마지막 2개 + 시간)", excl4 == {"a", "y", "z"})

print("== 3. migrate_from_used (날짜 미상 → 오늘-창/2) ==")
mig = H.migrate_from_used(["Otter", "fox", "owl"], window_days=60, now=NOW)
check("3개 topic 이관", set(mig.keys()) == {"otter", "fox", "owl"})
check("60일 창에 다 포함 (30일 전 기록)", H.recent_topics(mig, 60, now=NOW) == {"otter", "fox", "owl"})
check("20일 창엔 없음 (30일 전이라 벗어남)", H.recent_topics(mig, 20, now=NOW) == set())

print("== 4. recent_clip_ids 30일 제외 ==")
hist5 = {
    "otter": [entry(d(10), clips=["111", "222"])],
    "fox": [entry(d(40), clips=["333"])],           # 40일 전 → 제외
    "owl": [entry(d(29), clips=["444"])],           # 29일 전 → 포함
}
c30 = H.recent_clip_ids(hist5, 30, now=NOW)
check("30일 내 클립 = 111/222/444", c30 == {"111", "222", "444"})
check("40일 전 클립 333 제외", "333" not in c30)

print("== 5. recent_formats / topic_entries 로테이션 ==")
hist6 = {
    "a": [entry(d(9), fmt="classic_trivia")],
    "b": [entry(d(6), fmt="myth_vs_fact")],
    "c": [entry(d(3), fmt="top_records")],
}
rf = H.recent_formats(hist6, 2)
check("직전 2편 format = [myth_vs_fact, top_records]", rf == ["myth_vs_fact", "top_records"])
hist6b = {"a": [entry(d(9), angle="hunting_diet"), entry(d(3), angle="records_extremes")]}
te = H.topic_entries(hist6b, "a")
check("topic_entries a = 2건", len(te) == 2)
check("최신 angle records_extremes 조회 가능", te[-1]["angle"] == "records_extremes")

print("== 6. facts_too_similar (내용 겹침 휴리스틱) ==")
past = [
    "Octopuses have three hearts and blue blood pumped by copper-based hemocyanin instead of iron.",
    "An octopus can taste with its suckers, each arm acting like an independent brain.",
]
similar = ["An octopus has three hearts and blue copper-based blood, not iron-based like humans."]
distinct = ["Octopuses can edit their own RNA to adapt neurons to cold water within minutes."]
check("과거와 유사한 fact → too_similar True", H.facts_too_similar(similar, past) is True)
check("완전히 다른 fact → too_similar False", H.facts_too_similar(distinct, past) is False)

print("== 7. build_entry / summarize_fact ==")
e = H.build_entry("otter", "hunting_diet", "classic_trivia",
                  ["word " * 30], "Title Here", [123, "nasa_x"])
check("build_entry facts 요약됨(…)", e["facts"][0].endswith("…"))
check("build_entry clip_ids 문자열화", e["clip_ids"] == ["123", "nasa_x"])
check("build_entry date 오늘", e["date"] == datetime.now().strftime("%Y-%m-%d"))

print("== 8. 원자 저장 round-trip + leftover .tmp 없음 (A) ==")
import tempfile, glob
_tmpdir = tempfile.mkdtemp()
_hp = os.path.join(_tmpdir, "content_history.json")
H.save_history(_hp, {"otter": [entry(d(1), fmt="classic_trivia")]})
check("저장 후 파일 존재", os.path.exists(_hp))
check("load 라운드트립 일치", H.load_history(_hp).get("otter", [{}])[0]["format"] == "classic_trivia")
check("leftover .hist_*.tmp 없음", len(glob.glob(os.path.join(_tmpdir, ".hist_*.tmp"))) == 0)

print("== 9. 손상 파일 → .corrupt 백업 + 빈 이력 (A) ==")
open(_hp, "w", encoding="utf-8").write("{ this is not valid json ]]")
r = H.load_history(_hp)
check("손상 시 빈 dict 반환", r == {})
check(".corrupt 백업 생성", os.path.exists(_hp + ".corrupt"))
check("원본은 사라짐(백업으로 이동)", not os.path.exists(_hp))
# dict 아닌 최상위(JSON 배열)도 손상 취급
open(_hp, "w", encoding="utf-8").write("[1,2,3]")
check("최상위 비-dict도 빈 이력", H.load_history(_hp) == {})

print("== 10. compute_exclude 무한루프 방지 (B): min_window=0 + 전량 차단 ==")
pool_b = [f"t{i}" for i in range(10)]
hist_b = {f"t{i}": [entry(d(0))] for i in range(10)}  # 전부 오늘(어떤 창에도 걸림)
import time as _t
_start = _t.time()
excl_b, eff_b = H.compute_exclude(hist_b, used=[], keep=45, window_days=60,
                                  pool=pool_b, min_candidates=8, min_window=0, now=NOW)
_elapsed = _t.time() - _start
check("무한루프 없이 즉시 반환(<2s)", _elapsed < 2.0, f"[{_elapsed:.3f}s]")
check("min_window 0 클램프(>=0.001)", eff_b >= 0.001)
check("전량 차단이라 union=10", len(excl_b) == 10)

print(f"\n=== 결과: {_passed} PASS / {_failed} FAIL ===")
if __name__ == "__main__":
    # 모듈 최상위 sys.exit는 unittest discover/pytest의 import 단계에서 SystemExit을 일으켜
    # 전부 통과인데도 FAILED(errors)로 집계됐다(2026-07-17 fix). 직접 실행 시에만 종료코드 전달.
    sys.exit(1 if _failed else 0)
elif _failed:
    # discover/pytest로 import된 경우에도 실패가 초록으로 묻히지 않게 에러로 표면화
    raise AssertionError(f"test_history: {_failed}개 체크 실패 (직접 실행: py -3 tests/test_history.py)")
