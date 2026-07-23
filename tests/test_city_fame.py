"""도시 유명도(fame) 게이팅 회귀 테스트 (API 호출 없음).

배경: 채널 첫 편이 Kanazawa로 나가 조회수가 안 붙었다(2026-07-23 쿠마님 지적).
원인은 로스터 100개를 그냥 셔플해 GPT에 던지고 프롬프트가 "fresh한 도시 선호"였던 것.

검증 항목:
- cities.json fame(1~4) 무결성 (전 도시 부여, 분포)
- _roster_candidates: 미발행 우선 → 그중 최상위 fame 등급만 후보
- 등급 소진 시 1 → 2 → 3 → 4 자동 하강, 전량 소진 시 재방문 모드
- 100편 시뮬레이션: 유명도 오름차순으로 로스터 전량 소진(하위 등급 기아 없음)
- pick_animal(legacy fallback)도 동일 게이팅
- GPT가 tier 밖 도시를 픽하면 코드가 거절 (프롬프트 무시 방어)
- fame 결측/이상값 → 3 폴백

실행: py -3 tests/test_city_fame.py  (travel-shorts-bot 폴더에서)
"""
import io
import json
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
_out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config                      # noqa: E402
from pipeline import animals       # noqa: E402

A = config.ANIMALS
BY_FAME = {}
for _k, _c in A.items():
    BY_FAME.setdefault(_c["fame"], []).append(_k)

_passed = 0


def ok(msg):
    global _passed
    _passed += 1
    print(f"[OK] {msg}", file=_out)


def keys_of(block):
    return [ln.split("(key: ")[1].rstrip(")") for ln in block.splitlines() if ln.strip()]


# --- 1. 로스터 무결성 -------------------------------------------------------
raw = json.load(open(os.path.join(_ROOT, "cities.json"), encoding="utf-8"))
assert len(raw) == len(A) == 100, f"로스터 개수 {len(raw)}/{len(A)}"
assert all("fame" in c for c in raw), "fame 필드 누락 도시 존재"
assert all(1 <= c["fame"] <= 4 for c in raw), "fame 범위 이탈"
assert len(BY_FAME[1]) >= 10, "fame=1(최상위) 도시가 너무 적다"
ok(f"cities.json 100개 fame 부여 완료 (분포 {{f: len(v) for ...}} = "
   f"{ {f: len(v) for f, v in sorted(BY_FAME.items())} })")

for must in ("paris_france", "tokyo_japan", "new_york_usa", "rome_italy", "london_uk"):
    assert A[must]["fame"] == 1, f"{must} 는 fame=1 이어야 함"
assert A["kanazawa_japan"]["fame"] == 4, "kanazawa 는 니치 등급(4)이어야 함"
ok("파리/도쿄/뉴욕/로마/런던 = fame1, 카나자와 = fame4")

# --- 2. tier 게이팅 ---------------------------------------------------------
assert set(keys_of(animals._roster_lines([]))) == set(BY_FAME[1])
ok(f"초기 후보 = fame1 {len(BY_FAME[1])}개만 (무명 도시 노출 없음)")

for lo, hi in ((1, 2), (2, 3), (3, 4)):
    used = [k for f in range(1, lo + 1) for k in BY_FAME[f]]
    assert set(keys_of(animals._roster_lines(used))) == set(BY_FAME[hi]), f"{lo}→{hi} 하강 실패"
ok("fame 소진 시 1 → 2 → 3 → 4 자동 하강")

assert animals._roster_lines(list(A.keys())) == "", "전량 exclude 시 빈 블록이어야 함"
ok("전량 exclude → 빈 후보(GPT 경로는 예외 → legacy fallback 위임)")

assert set(keys_of(animals._roster_lines([k.upper() for k in BY_FAME[1]]))) == set(BY_FAME[2])
ok("exclude 대소문자 정규화 정상")

# --- 3. 미발행 우선 (ever_used) --------------------------------------------
# exclude는 비었지만 fame1을 이미 전부 발행한 상태 → 재방문 대신 fame2로 내려가야 한다.
cands = keys_of(animals._roster_lines([], ever_used=BY_FAME[1]))
assert set(cands) == set(BY_FAME[2]), "발행 이력이 있으면 재방문보다 미발행 도시가 우선"
ok("ever_used 반영: 이미 발행한 fame1 대신 미발행 fame2 제시")

all_used = list(A.keys())
recent = all_used[-85:]                      # compute_exclude 가 넘기는 것과 같은 슬라이스
cands = keys_of(animals._roster_lines(recent, ever_used=all_used))
free = [k for k in all_used if k not in recent]
top = min(A[k]["fame"] for k in free)
assert set(cands) == {k for k in free if A[k]["fame"] == top}, "재방문 모드 tier 계산 오류"
ok("로스터 1순회 완료 후 재방문 모드 진입(유명도 순 유지)")

# --- 4. 100편 시뮬레이션: 기아 없이 전량 소진 --------------------------------
USED_KEEP = 85
used, fames = [], []
for _ in range(100):
    pick = animals.pick_animal(exclude=used[-USED_KEEP:], ever_used=used)
    used.append(pick)
    fames.append(A[pick]["fame"])
assert len(set(used)) == 100, f"100편 중 중복 발생 ({100 - len(set(used))}건)"
assert fames == sorted(fames), "유명도 순서가 뒤섞임 (오름차순이어야 함)"
assert fames[:12] == [1] * 12 and fames[-37:] == [4] * 37
ok("100편 시뮬: 중복 0, fame 1→4 오름차순으로 로스터 전량 소진(하위 등급 기아 없음)")

picks = {animals.pick_animal(exclude=[]) for _ in range(300)}
assert picks <= set(BY_FAME[1]), "legacy fallback 이 tier 밖 도시 픽"
ok(f"pick_animal 300회 전부 fame1 ({len(picks)}종)")

picks = {animals.pick_animal(exclude=list(A.keys())) for _ in range(50)}
assert picks <= set(BY_FAME[1]), "전량 exclude 시에도 fame1 재사용이어야 함"
ok("전량 exclude(IndexError 회귀 케이스) → fame1 재사용")

# --- 5. fame 결측/이상값 방어 ----------------------------------------------
assert animals._fame({}) == 3 and animals._fame({"fame": "x"}) == 3
assert animals._fame({"fame": 9}) == 3 and animals._fame(None) == 3
assert config._fame_of({"fame": "2"}) == 2 and config._fame_of({}) == 3
ok("fame 결측·이상값 → 3 폴백 (무명 취급으로 영영 미발행되는 일 없음)")

# --- 6. 프롬프트 회귀 -------------------------------------------------------
src = open(os.path.join(_ROOT, "pipeline", "animals.py"), encoding="utf-8").read()
assert "Prefer cities lower in the list" not in src, "옛 'fresh 선호' 유도 문구 잔존"
assert "pre-filtered to the most famous cities" in src
ok("프롬프트에서 'fresh 도시 선호' 유도 제거 확인")

# --- 7. GPT가 tier 밖을 픽하면 코드가 거절 ----------------------------------
_calls = {"n": 0}


class _FakeCompletions:
    def create(self, **kw):
        _calls["n"] += 1
        body = json.dumps({
            "animal_key": "kanazawa_japan", "display": "Kanazawa", "single": "Kanazawa",
            "country": "Japan", "category": "asia_culture", "angle": "hidden_gem",
            "format": "listicle", "title_en": "x", "description_en": "x",
            "hook": "x", "intro": "x", "facts": ["a", "b", "c", "d"], "outro": "x",
            "tags": ["x"], "comment": "x",
        })
        msg = types.SimpleNamespace(content=body)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class _FakeClient:
    def __init__(self, *a, **kw):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions())


sys.modules["openai"] = types.SimpleNamespace(OpenAI=_FakeClient)
animals.OPENAI_API_KEY = "test-key"
animals.fetch_source_for = lambda *a, **kw: "SOURCE TEXT"
animals.expand_legacy_to_rich = lambda m: {}
animals._enforce_global_fact_novelty = lambda meta, *a, **kw: meta
animals._title_needs_fix = lambda *a, **kw: False

meta = animals.pick_random_animal(exclude_keys=[], ever_used=[])
assert _calls["n"] == 4, f"tier 밖 픽인데 재요청이 4회가 아님 ({_calls['n']}회)"
assert meta["animal_key"] != "kanazawa_japan", "tier 밖 GPT 픽이 그대로 통과됨"
assert A[meta["animal_key"]]["fame"] == 1, f"fallback 이 fame1 이 아님 ({meta['animal_key']})"
ok(f"GPT가 tier 밖(kanazawa) 4회 픽 → 전부 거절 후 fame1 fallback ({meta['animal_key']})")

print(f"\n{_passed} passed — ALL PASS", file=_out)
