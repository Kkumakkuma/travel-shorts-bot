"""도시 유명도(fame) 게이팅 회귀 테스트 (API 호출 없음).

배경: 채널 첫 편이 Kanazawa로 나가 조회수가 안 붙었다(2026-07-23 쿠마님 지적).
원인은 로스터 100개를 그냥 셔플해 GPT에 던지고 프롬프트가 "fresh한 도시 선호"였던 것.

2차 지시(같은 날): "초반만 관심 가고 나중엔 안 가지 않냐 — 1234 순서대로 12341234"
→ 최상위부터 소진이 아니라 **fame 1→2→3→4 순환**. 4편 중 1편은 항상 파리·도쿄급.

검증 항목:
- cities.json fame(1~4) 무결성 (전 도시 부여, 분포)
- _roster_candidates: 미발행 우선 → 이번 차례의 fame 등급만 후보
- 1→2→3→4 순환, 소진된 등급은 건너뜀, 전량 소진 시 재방문 모드
- 기울어진 이력(4등급만 2편 발행된 현재 상태)에서 순환이 스스로 균형 회복
- 100편 시뮬레이션: 중복 없이 로스터 전량 소진(등급 기아 없음)
- pick_animal(legacy fallback)도 동일 순환
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
ROSTER_N = len(raw)
assert ROSTER_N == len(A) >= 160, f"로스터 개수 {ROSTER_N}/{len(A)}"
assert all("fame" in c for c in raw), "fame 필드 누락 도시 존재"
assert all(1 <= c["fame"] <= 4 for c in raw), "fame 범위 이탈"
assert len({c["city_key"] for c in raw}) == ROSTER_N, "city_key 중복"
assert len({c["city"].lower() for c in raw}) == ROSTER_N, "도시명 중복"
# 등급이 정확히 균등해야 1234 순환이 로스터 끝까지 유지된다 (한 등급이라도 적으면 거기서 깨짐)
assert set(BY_FAME) == {1, 2, 3, 4} and len({len(v) for v in BY_FAME.values()}) == 1, \
    f"등급 불균등 — 순환이 일찍 깨진다: { {f: len(v) for f, v in sorted(BY_FAME.items())} }"
assert ROSTER_N % 4 == 0, f"로스터 개수가 4의 배수가 아님 ({ROSTER_N})"
ok(f"cities.json {ROSTER_N}개 fame 부여·균형 확인 "
   f"(분포 { {f: len(v) for f, v in sorted(BY_FAME.items())} }, 키/도시명 중복 0)")

VALID_CAT = {"europe_city", "asia_culture", "beach_tropical", "desert_middleeast",
             "africa_safari", "latin_america", "north_america", "oceania",
             "nordic_nature", "historic_ancient"}
for c in raw:
    assert c["category"] in VALID_CAT, f"{c['city_key']}: 잘못된 category {c['category']}"
    assert isinstance(c.get("country"), str) and c["country"].strip(), \
        f"{c['city_key']}: country 누락 (grounding 국가 게이트가 무력화됨)"
    assert isinstance(c.get("confusion_pair"), list) and \
        all(isinstance(x, str) and x.strip() for x in c["confusion_pair"]), \
        f"{c['city_key']}: confusion_pair 형식 오류"
    assert c["landmark_lock"], f"{c['city_key']}: landmark_lock 비어있음"
    assert isinstance(c["pexels_keywords"], list) and len(c["pexels_keywords"]) >= 4, \
        f"{c['city_key']}: 키워드 부족"
    tok = "".join(ch for ch in c["city"].split()[0].lower() if ch.isalnum())
    for kw in c["pexels_keywords"]:
        flat = "".join(ch for ch in kw.lower() if ch.isalnum())
        assert tok in flat, f"{c['city_key']}: 키워드에 도시명 없음 → {kw!r}"
ok(f"{ROSTER_N}개 전부 category 유효 + landmark_lock 존재 + 키워드에 도시명 포함")

for must in ("paris_france", "tokyo_japan", "new_york_usa", "rome_italy", "london_uk"):
    assert A[must]["fame"] == 1, f"{must} 는 fame=1 이어야 함"
assert A["kanazawa_japan"]["fame"] == 4, "kanazawa 는 니치 등급(4)이어야 함"
ok("파리/도쿄/뉴욕/로마/런던 = fame1, 카나자와 = fame4")

# --- 2. 등급 순환 -----------------------------------------------------------
assert set(keys_of(animals._roster_lines([]))) == set(BY_FAME[1])
ok(f"첫 편 후보 = fame1 {len(BY_FAME[1])}개만 (무명 도시 노출 없음)")

# 이력이 1편씩 쌓일 때 다음 차례가 2 → 3 → 4 → 1 로 순환하는지
seq, hist_used = [], []
for step in range(9):
    cands = keys_of(animals._roster_lines(hist_used, ever_used=hist_used))
    f = A[cands[0]]["fame"]
    assert {A[k]["fame"] for k in cands} == {f}, "후보에 여러 등급이 섞임"
    seq.append(f)
    hist_used.append(cands[0])
assert seq == [1, 2, 3, 4, 1, 2, 3, 4, 1], f"순환 실패: {seq}"
ok(f"등급 순환 실측 9편: {''.join(map(str, seq))} (1→2→3→4 반복)")

# 소진된 등급은 건너뛴다 (fame1 12개를 다 쓴 뒤에는 2,3,4 만 순환)
used = list(BY_FAME[1]) + BY_FAME[2][:12] + BY_FAME[3][:12] + BY_FAME[4][:12]
seq = []
for step in range(6):
    cands = keys_of(animals._roster_lines(used, ever_used=used))
    seq.append(A[cands[0]]["fame"])
    used.append(cands[0])
assert 1 not in seq and set(seq) == {2, 3, 4}, f"소진 등급 스킵 실패: {seq}"
ok(f"fame1 소진 후 남은 등급만 순환: {''.join(map(str, seq))}")

assert animals._roster_lines(list(A.keys())) == "", "전량 exclude 시 빈 블록이어야 함"
ok("전량 exclude → 빈 후보(GPT 경로는 예외 → legacy fallback 위임)")

# 대문자 exclude 가 정규화되지 않으면 fame1 이 그대로 후보로 남는다 → fame2 면 정규화 정상
assert {A[k]["fame"] for k in keys_of(animals._roster_lines([k.upper() for k in BY_FAME[1]]))} == {2}
ok("exclude 대소문자 정규화 정상(fame1 전량 제외 → 다음 등급으로)")

# --- 3. 미발행 우선 + 기울어진 이력 보정 -------------------------------------
cands = keys_of(animals._roster_lines([], ever_used=BY_FAME[1]))
assert set(cands) <= set(BY_FAME[2]), "fame1 12편 발행 후 차례는 fame2 (재방문 아님)"
ok("ever_used 반영: 이미 발행한 fame1 대신 미발행 fame2 제시")

# 현재 채널 실제 상태 — 직전 발행이 jaipur(fame4)이므로 다음은 1, 그 뒤 2·3·4… 순환
real = ["kanazawa_japan", "jaipur_india"]
seq, hist_used = [], list(real)
for step in range(8):
    cands = keys_of(animals._roster_lines(hist_used, ever_used=hist_used))
    seq.append(A[cands[0]]["fame"])
    hist_used.append(cands[0])
assert seq == [1, 2, 3, 4, 1, 2, 3, 4], f"현재 상태에서 순환 실패: {seq}"
ok(f"현재 상태(카나자와·자이푸르=4등급 선발행) → 이후 8편 {''.join(map(str, seq))}")

# 각 등급이 한 번씩 나간 뒤(초기 진입 예외 off) = 순수 라운드로빈 구간
SEED = ["paris_france", "seville_spain", "colmar_france", "kanazawa_japan"]   # fame 1,2,3,4
assert [A[k]["fame"] for k in SEED] == [1, 2, 3, 4], "테스트 seed 등급 전제 깨짐"
for last_key, want in (("paris_france", 2), ("seville_spain", 3),
                       ("colmar_france", 4), ("kanazawa_japan", 1)):
    got = animals._next_fame_tier({1, 2, 3, 4}, SEED + [last_key])
    assert got == want, f"직전 {last_key}(fame{A[last_key]['fame']}) → {got}, 기대 {want}"
assert animals._next_fame_tier({2, 4}, SEED + ["paris_france"]) == 2, "직전 1 → 2"
assert animals._next_fame_tier({1, 3}, SEED + ["paris_france"]) == 3, "2 소진 → 3으로 건너뜀"
assert animals._next_fame_tier({1}, SEED + ["colmar_france"]) == 1, "4 없으면 한 바퀴 돌아 1"
assert animals._next_fame_tier(set(), SEED) == 1, "후보 등급 없음 → 기본 1"
ok("_next_fame_tier 단위: 직전 등급 다음 칸 + 소진 등급 스킵 (기대값 하드코딩)")

# 초기 진입 예외 — 아직 한 편도 안 나간 등급이 있으면 그중 가장 유명한 등급이 먼저
assert animals._next_fame_tier({1, 2, 3, 4}, []) == 1, "이력 0이면 1부터"
assert animals._next_fame_tier({1, 2, 3, 4}, ["없는_도시"]) == 1, "로스터 밖 키만 있으면 1부터"
assert animals._next_fame_tier({1, 2, 3, 4}, ["kanazawa_japan"]) == 1, "4등급만 나갔으면 다음 1"
assert animals._next_fame_tier({1, 2, 3, 4}, ["kanazawa_japan", "jaipur_india"]) == 1, \
    "3·4등급만 나간 현재 채널 상태에서 다음은 1"
assert animals._next_fame_tier({2, 3, 4}, ["kanazawa_japan"]) == 2, "1등급 후보 없으면 2"
assert animals._next_fame_tier({1, 2, 3, 4}, SEED) == 1, "한 바퀴 다 돌면 예외 off → 직전4 다음 1"
ok("초기 진입 예외: 미발행 등급 우선(하위 등급으로 기운 과거 이력 보정) 후 자동 종료")

# 1순회 완료 후 재방문 모드에서도 순환이 이어지는지 (기대값을 구현식으로 계산하지 않음)
all_used = list(A.keys())
recent = all_used[-85:]                      # compute_exclude 가 넘기는 것과 같은 슬라이스
free = [k for k in all_used if k not in recent]
cands = keys_of(animals._roster_lines(recent, ever_used=all_used))
last_f = A[all_used[-1]]["fame"]
want = next(f for f in ((last_f + i) % 4 + 1 for i in range(4)) if f in {A[k]["fame"] for k in free})
assert {A[k]["fame"] for k in cands} == {want}, "재방문 모드 순환 오류"
assert set(cands) <= set(free), "재방문 모드가 exclude된 도시를 후보로 냄"
ok(f"1순회 완료 후 재방문 모드: 직전 fame{last_f} → fame{want} 유지")

# --- 4. 100편 시뮬레이션: 기아 없이 전량 소진 --------------------------------
USED_KEEP = 85
used, fames = [], []
for _ in range(ROSTER_N + 40):           # 1순회 전량 + 재방문 40편까지 밀어본다
    pick = animals.pick_animal(exclude=used[-USED_KEEP:], ever_used=used)
    used.append(pick)
    fames.append(A[pick]["fame"])
assert len(set(used[:ROSTER_N])) == ROSTER_N, \
    f"1순회에서 중복 발생 ({ROSTER_N - len(set(used[:ROSTER_N]))}건)"
full = 4 * min(len(v) for v in BY_FAME.values())     # 가장 적은 등급이 소진될 때까지는 완전 순환
assert fames[:full] == [1, 2, 3, 4] * (full // 4), f"{full}편까지 1234 완전 순환이 아님"
for f in (1, 2, 3, 4):
    assert fames[:ROSTER_N].count(f) == len(BY_FAME[f]), \
        f"fame{f} 1순회 소진 실패 ({fames[:ROSTER_N].count(f)}/{len(BY_FAME[f])})"
# 재방문 구간: 같은 도시가 연속 재등장하지 않고(=exclude 유효) 계속 후보가 나온다
assert len(set(used[ROSTER_N:])) == len(used[ROSTER_N:]), "재방문 구간에서 같은 도시 중복 선택"
ok(f"{ROSTER_N + 40}편 시뮬: 1순회 {ROSTER_N}개 중복 0 + {full}편까지 1234 완전 순환 + 재방문 정상")

# 재방문 구간에서도 4개 등급 후보가 모두 살아있으면 순환은 그대로 1234 (KEEP 작게 잡아 재현)
revisit, seq2 = list(A.keys()), []
for _ in range(24):
    pick = animals.pick_animal(exclude=revisit[-20:], ever_used=revisit)
    revisit.append(pick)
    seq2.append(A[pick]["fame"])
assert seq2 == [1, 2, 3, 4] * 6, f"재방문 구간 순환 깨짐: {seq2}"
ok(f"재방문(2순회) 24편 순환 실측: {''.join(map(str, seq2))[:12]}… 1234 유지")

# --- 4b. 이력 저장/회피 슬라이스 연동 -----------------------------------------
import importlib                                                        # noqa: E402
import pipeline.history as H                                            # noqa: E402
main_mod = importlib.import_module("main")

long_used = [f"city_{i}" for i in range(600)] + ["paris_france"]
tmp = os.path.join(_HERE, "_tmp_used.json")
_real_path = main_mod.USED_PATH
try:
    main_mod.USED_PATH = tmp
    main_mod._save_used(long_used)
    saved = main_mod._load_used()
finally:
    main_mod.USED_PATH = _real_path
    if os.path.exists(tmp):
        os.remove(tmp)
assert len(saved) == main_mod.USED_LOG_MAX == 500, f"이력 상한 적용 실패 ({len(saved)})"
assert saved[-1] == "paris_france", "최신 발행이 잘려나감 (뒤에서 자르지 않음)"
ok("used_cities 저장: 최근 500편 보존(오래된 쪽부터 절단, 최신 유지)")

# 회피(exclude)는 저장 이력이 길어져도 최근 USED_KEEP개만 — 기존 동작 불변
ex, _w = H.compute_exclude({}, list(A.keys()), 85, 120.0, pool=None)
assert len(ex) == 85 and "paris_france" not in ex, "compute_exclude 가 전량을 차단(회피 슬라이스 붕괴)"
ok("compute_exclude: 이력 100편이어도 회피는 최근 85편만(전량 보존과 무충돌)")

# 재방문 도시를 다시 append 해도(publish-prepared 경로) 순환은 계속 진행된다
dup = ["paris_france", "amsterdam_netherlands", "paris_france"]
assert animals._next_fame_tier({1, 2, 3, 4}, dup) == 2, "중복 append 시 직전 등급 반영 실패"
ok("재방문 중복 기록도 '직전 발행 등급'으로 정상 반영(순환 제자리걸음 없음)")

picks = {animals.pick_animal(exclude=[]) for _ in range(300)}
assert picks <= set(BY_FAME[1]), "발행 이력 0인데 fame1 밖을 픽"
ok(f"pick_animal 300회 전부 fame1 ({len(picks)}종)")

picks = {animals.pick_animal(exclude=list(A.keys())) for _ in range(50)}
assert picks <= set(BY_FAME[1]), "전량 exclude 시에도 이력 0이면 fame1"
ok("전량 exclude(IndexError 회귀 케이스) → fame1 재사용")

assert animals._fame_counts(["paris_france", "kanazawa_japan", "not_a_city"]) == {1: 1, 2: 0, 3: 0, 4: 1}
ok("_fame_counts: 로스터 밖 키 무시하고 등급별 발행 수 집계")

# --- 5. fame 결측/이상값 방어 ----------------------------------------------
assert animals._fame({}) == 3 and animals._fame({"fame": "x"}) == 3
assert animals._fame({"fame": 9}) == 3 and animals._fame(None) == 3
assert config._fame_of({"fame": "2"}) == 2 and config._fame_of({}) == 3
ok("fame 결측·이상값 → 3 폴백 (무명 취급으로 영영 미발행되는 일 없음)")

# --- 6. 프롬프트 회귀 -------------------------------------------------------
src = open(os.path.join(_ROOT, "pipeline", "animals.py"), encoding="utf-8").read()
assert "Prefer cities lower in the list" not in src, "옛 'fresh 선호' 유도 문구 잔존"
assert "pre-filtered this list for you" in src
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
