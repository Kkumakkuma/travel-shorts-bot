# 스크립트형 자체점검(대기열 갈래 29항목). pytest 수집 대상이 아니라 직접 실행한다: python tests/queue_selfcheck.py
#!/usr/bin/env python3
"""대기열(queue/) 수명주기 회귀 테스트 — 중복 발행 0 방어선.

여기서 검증하는 것(전부 실제 파일 이동으로):
  1. 업로드 성공 → pending → done + video_id/published_at 기록
  2. done 동명 충돌 시 기존 발행 기록 보존(+ .dup-<시각>.json 으로 새 파일 보존)
  3. url 이 videoId 형태가 아니면 대기열을 소비하지 않음
  4. done 이동 실패 시 pending 원본에 video_id 스탬프(재발행 차단 최후 방어)
  5. video_id 가 찍힌 pending 은 소재 수집조차 하지 않고 done 으로 정리
  6. 스키마 위반 대본은 failed/ 로 이동 + .reason.txt

실행: python tests/test_queue.py   (실 리포 queue/ 는 건드리지 않는다 — 임시 디렉터리 사용)
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import main  # noqa: E402

SRC = os.path.join(ROOT, "queue", "pending", "001_lisbon_portugal.json")
if not os.path.exists(SRC):
    # pending 이 비어 있어도(전부 발행됨) 테스트는 돌아야 한다 → done 에서 아무거나 집는다.
    cands = [os.path.join(ROOT, "queue", d, f)
             for d in ("done", "pending", "failed")
             for f in sorted(os.listdir(os.path.join(ROOT, "queue", d)) if
                             os.path.isdir(os.path.join(ROOT, "queue", d)) else [])
             if f.endswith(".json")]
    if not cands:
        print("SKIP: 대기열 샘플 대본이 없어 테스트를 건너뜁니다")
        sys.exit(0)
    SRC = cands[0]

TMP = tempfile.mkdtemp(prefix="qtest_")
main.QUEUE_DIR = TMP
main.QUEUE_PENDING_DIR = os.path.join(TMP, "pending")
main.QUEUE_DONE_DIR = os.path.join(TMP, "done")
main.QUEUE_FAILED_DIR = os.path.join(TMP, "failed")
for d in ("pending", "done", "failed"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)

fails = []
_SLUG = os.path.splitext(os.path.basename(SRC))[0]


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")
    if not cond:
        fails.append(name)


def fresh(name=None, **over):
    name = name or os.path.basename(SRC)
    p = os.path.join(main.QUEUE_PENDING_DIR, name)
    d = json.load(open(SRC, encoding="utf-8"))
    for k in ("video_id", "video_url", "published_at"):
        d.pop(k, None)
    d.update(over)
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return p


def _fake_fetch(meta, wd, **kw):
    _fake_fetch.calls += 1
    return [[("x.mp4", "1")]], ["1"]


_fake_fetch.calls = 0

print("== 1) 정상 소비: pending → done + 발행정보 ==")
p = fresh()
ok = main.queue_mark_done(p, "abc12345XYZ", "https://youtube.com/shorts/abc12345XYZ")
dest = os.path.join(main.QUEUE_DONE_DIR, os.path.basename(SRC))
d = json.load(open(dest, encoding="utf-8"))
check("반환 True", ok is True)
check("pending 에서 사라짐", not os.path.exists(p))
check("done 에 video_id 기록", d.get("video_id") == "abc12345XYZ")
check("published_at 기록", bool(d.get("published_at")))

print("== 2) done 동명 충돌: 기존 발행기록 보존 + dup 파일 ==")
p = fresh()
ok = main.queue_mark_done(p, "second999ID", "https://youtube.com/shorts/second999ID")
d0 = json.load(open(dest, encoding="utf-8"))
dups = [f for f in os.listdir(main.QUEUE_DONE_DIR) if ".dup-" in f]
check("반환 True", ok is True)
check("기존 done 파일 그대로", d0.get("video_id") == "abc12345XYZ")
check("dup 파일 생성", len(dups) == 1, str(dups))

print("== 3) url 형태 검사 ==")
p = fresh("002_dummy_city.json", animal_key="dummy_city")
check("url None → 미소비/False", main._consume_queue_if_uploaded(p, None) is False and os.path.exists(p))
check("이상한 url → 미소비/False", main._consume_queue_if_uploaded(p, "OK") is False and os.path.exists(p))
check("queue_path None → True", main._consume_queue_if_uploaded(None, "https://youtube.com/shorts/x1234567") is True)
check("정상 url → 소비/True",
      main._consume_queue_if_uploaded(p, "https://youtube.com/shorts/z9y8x7w6v") is True and not os.path.exists(p))

print("== 4) 이동 실패 폴백: pending 에 video_id 스탬프 ==")
p = fresh("003_dummy_town.json", animal_key="dummy_town")
_real = os.replace
os.replace = lambda a, b: (_ for _ in ()).throw(OSError("simulated move failure"))
try:
    ok = main.queue_mark_done(p, "vid12345678", "https://youtube.com/shorts/vid12345678")
finally:
    os.replace = _real
check("반환 False", ok is False)
check("pending 원본 유지", os.path.exists(p))
check("video_id 스탬프", json.load(open(p, encoding="utf-8")).get("video_id") == "vid12345678")

print("== 5) 이미 발행된 pending 은 재발행 안 함 ==")
_real_fetch = main.fetch_verified_backgrounds
main.fetch_verified_backgrounds = _fake_fetch
try:
    res = main.try_queue_script([], [], [3])
finally:
    main.fetch_verified_backgrounds = _real_fetch
check("소재 수집 호출 0회", _fake_fetch.calls == 0, f"({_fake_fetch.calls}회)")
check("결과 None", res is None)
check("done 으로 정리", os.path.exists(os.path.join(main.QUEUE_DONE_DIR, "003_dummy_town.json")))
check("pending 비었음", os.listdir(main.QUEUE_PENDING_DIR) == [])

print("== 6) 스키마 위반 → failed + reason ==")
fresh("004_bad_script.json", animal_key="bad_script", author="gpt")
main.fetch_verified_backgrounds = _fake_fetch
try:
    main.try_queue_script([], [], [3])
finally:
    main.fetch_verified_backgrounds = _real_fetch
check("failed 이동", os.path.exists(os.path.join(main.QUEUE_FAILED_DIR, "004_bad_script.json")))
check("reason 파일", os.path.exists(os.path.join(main.QUEUE_FAILED_DIR, "004_bad_script.reason.txt")))

print("== 7) 스키마 검증 규칙 ==")
good = json.load(open(SRC, encoding="utf-8"))
good_name = os.path.basename(SRC)
check("정상 대본 통과", main.validate_queue_meta(good, good_name) == [],
      str(main.validate_queue_meta(good, good_name)))
for label, bad in (
        ("author", dict(good, author="gpt")),
        ("facts 4개", dict(good, facts=good["facts"][:4])),
        ("fact_keywords 4개", dict(good, fact_keywords=good["fact_keywords"][:4])),
        ("country 공백", dict(good, country="  ")),
        ("landmark_lock 누락", {k: v for k, v in good.items() if k != "landmark_lock"}),
        ("confusion_pair 타입", dict(good, confusion_pair="Porto")),
        ("슬러그 불일치", dict(good, animal_key="other_city")),
        ("title_ko 빈값", dict(good, title_ko="")),
):
    check(f"위반 감지: {label}", bool(main.validate_queue_meta(bad, good_name)))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n=== 결과: {'ALL PASS' if not fails else str(len(fails)) + ' FAIL: ' + str(fails)} ===")
sys.exit(1 if fails else 0)
