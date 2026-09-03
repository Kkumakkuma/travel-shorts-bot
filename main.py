#!/usr/bin/env python3
"""Wander 60s — 1분 여행 도시 소개 영상 자동 발행 (우주봇 fork).

흐름:
1. 대본 확보 — ① --city 강제 → ② queue/pending 대기열(사람이 쓴 대본) →
   ③ 대기열 비었고 QUEUE_ONLY=1 이면 QUEUE_EMPTY 찍고 종료 → ④ GPT 동적 픽(구 경로)
2. Pexels 소재 수집(영상 → 사진 Ken Burns → AI 이미지) + vision(verify_city) 검증(fail-closed)
   — 하이라이트당 CLIPS_PER_FACT개까지
3. edge-tts로 영문 내레이션 생성 (intro/하이라이트/outro)
4. 도입샷(도시 전경) 확보 후 MoviePy로 1분 영상 합성
   (배경 하드컷 + 비네트 + BIG HOOK + 자막 시간차 + brand)
5. 도입샷/첫 배경 클립 프레임으로 썸네일 (fail-soft)
6. 영문 메인 + 7개국 localizations (대본에 한국어 메타가 있으면 ko 는 그 값 사용)
7. YouTube Shorts 업로드 (privacy=public, defaultLanguage=en)
8. videoId 를 받은 뒤에만 대기열 파일을 queue/done 으로 이동 + 사용 이력 기록
"""
import argparse
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone

if sys.platform == "win32" and "pytest" not in sys.modules:  # pytest 캡처를 깨지 않는다
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as _cfg
from config import ANIMALS, BASE_DIR, OUTPUT_DIR

# 콘텐츠 이력 설정 (없는 config에도 안전한 기본값)
CONTENT_HISTORY_PATH = getattr(_cfg, "CONTENT_HISTORY", os.path.join(BASE_DIR, "content_history.json"))
CONTENT_WINDOW_DAYS = getattr(_cfg, "CONTENT_WINDOW_DAYS", 60)
HISTORY_MIN_CANDIDATES = getattr(_cfg, "HISTORY_MIN_CANDIDATES", 8)
HISTORY_MIN_WINDOW_DAYS = getattr(_cfg, "HISTORY_MIN_WINDOW_DAYS", 7)
CLIP_DEDUP_DAYS = getattr(_cfg, "CLIP_DEDUP_DAYS", 30)
RECENT_FORMAT_KEEP = getattr(_cfg, "RECENT_FORMAT_KEEP", 2)
USE_NASA_MEDIA = getattr(_cfg, "USE_NASA_MEDIA", False)
USE_PEXELS_PHOTO = getattr(_cfg, "USE_PEXELS_PHOTO", False)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("travelbot")

USED_PATH = os.path.join(BASE_DIR, "used_cities.json")
# 직전 N개 도시 회피 + 코드 레벨 강제 dedup.
# 2026-07-17 fix: 로스터(100)와 같은 값이면 100편 발행 후 후보풀이 0이 되어
# off-roster 픽 유도 + 재발행 + GPT 낭비가 지속됨(시뮬 실측). 로스터보다 작게 둬서
# 상시 후보 여유(≥15)를 확보한다 — 도시 재등장은 85편(약 8.5개월) 뒤부터 순환.
USED_KEEP = 85

# Pexels 풋티지 dedup (2026-05-07 우주봇 사용자 지적 동기화: 최근 영상 풋티지 가타)
USED_PEXELS_PATH = os.path.join(BASE_DIR, "used_pexels.json")
USED_PEXELS_KEEP = 30  # 직전 30개 풋티지 soft 차단 (1편당 ~5클립 × 6편 분량, hard는 content_history 30일창)


def _load_used():
    if not os.path.exists(USED_PATH):
        return []
    try:
        return json.load(open(USED_PATH, encoding="utf-8"))
    except Exception:
        return []


# 2026-07-23: 파일에 최근 USED_LOG_MAX(500)편의 발행 이력을 남긴다 — 로스터 100개의 5회전 분량.
# (회피용 슬라이스는 compute_exclude가 [-USED_KEEP:]로 따로 수행하므로 회피 동작은 불변.)
# 기존처럼 85개로 잘라 저장하면 "한 번이라도 발행한 도시"와 "직전 발행 등급"을 알 수 없어,
# 로스터(100) > USED_KEEP(85) 구간에서 풀려난 도시가 재선택되고 일부 등급은 영영 발행되지 않는다.
USED_LOG_MAX = 500


def _save_used(used):
    json.dump(used[-USED_LOG_MAX:], open(USED_PATH, "w", encoding="utf-8"))


def _load_used_pexels() -> list:
    if not os.path.exists(USED_PEXELS_PATH):
        return []
    try:
        return [int(i) for i in json.load(open(USED_PEXELS_PATH, encoding="utf-8")) if str(i).isdigit() or isinstance(i, int)]
    except Exception:
        return []


def _save_used_pexels(used: list):
    json.dump(used[-USED_PEXELS_KEEP:], open(USED_PEXELS_PATH, "w", encoding="utf-8"))


def _extract_pexels_id(path: str):
    """pexels_<id>.mp4 파일명에서 id 추출 (int). 실패 시 None."""
    import re as _re
    base = os.path.basename(path or "")
    m = _re.match(r"pexels_(\d+)\.mp4$", base)
    return int(m.group(1)) if m else None


def _extract_clip_id(path: str):
    """pexels_<id>.mp4 파일명에서 pexels id 추출 (str digit). 실패 시 None."""
    import re as _re
    base = os.path.basename(path or "")
    m = _re.match(r"pexels_(\d+)\.mp4$", base)
    return m.group(1) if m else None


def _extract_photo_id(path: str):
    """pexels_photo_<id>.png 파일명에서 photo id 추출 (str digit). 실패 시 None."""
    import re as _re
    base = os.path.basename(path or "")
    m = _re.match(r"pexels_photo_(\d+)\.png$", base)
    return m.group(1) if m else None


def _collect_used_clip_ids(bg_clip_ids, intro_clip):
    """발행 기록용 클립 id 목록 — 배경 슬롯 id + 도입샷 id, 중복 제거(순서 보존).
    run_once 7단계와 publish-prepared 발행 기록이 같은 계산을 쓰도록 공용화(2026-07-20)."""
    ids = []
    seen = set()
    for c in (bg_clip_ids or []):
        if c and str(c) not in seen:
            ids.append(c)
            seen.add(str(c))
    if intro_clip:
        import re as _re
        m = _re.search(r"pexels_(\d+)", os.path.basename(intro_clip))
        if m and m.group(1) not in seen:
            ids.append(m.group(1))
    return ids


# === 대본 대기열 (Claude 가 쓴 대본을 그대로 발행) =================================
# GPT 동적 픽/후처리를 타지 않는 독립 갈래다. pending 에 넣어두면 cron 이 파일명 순으로
# 집어가고, **실제 업로드가 videoId 를 받은 뒤에만** done 으로 옮긴다.
QUEUE_DIR = os.path.join(BASE_DIR, "queue")
QUEUE_PENDING_DIR = os.path.join(QUEUE_DIR, "pending")
QUEUE_DONE_DIR = os.path.join(QUEUE_DIR, "done")
QUEUE_FAILED_DIR = os.path.join(QUEUE_DIR, "failed")
QUEUE_MAX_TRIES = 3
QUEUE_EMPTY_MARKER = "QUEUE_EMPTY"  # 워크플로 알림이 grep 하는 ASCII 마커

# 여행봇 대본 스키마: 도시 메타(country/landmark_lock)까지 필수. 하이라이트는 5개 고정.
QUEUE_FACTS_N = 5
_QUEUE_KEY_RE = re.compile(r"^[a-z0-9_]+$")
_QUEUE_STR_FIELDS = (
    "animal_key", "display", "single", "country", "category", "angle", "format",
    "landmark_lock", "title_en", "description_en", "hook", "intro", "outro", "comment",
)


def queue_only_mode() -> bool:
    """env QUEUE_ONLY 가 켜져 있으면 대기열이 빌 때 GPT 갈래로 흘리지 않는다."""
    return (os.environ.get("QUEUE_ONLY", "") or "").strip().lower() not in ("", "0", "false", "no")


def queue_pending_files() -> list:
    if not os.path.isdir(QUEUE_PENDING_DIR):
        return []
    return [os.path.join(QUEUE_PENDING_DIR, f)
            for f in sorted(os.listdir(QUEUE_PENDING_DIR)) if f.endswith(".json")]


QUEUE_UNLOCKED_PREFIX = "[unlocked] "  # NO_OPENAI 잠금 정보(clips/localizations)만 아직 없는 대본 — failed 가 아니라 pending 유지


def validate_queue_meta(data, filename: str) -> list:
    """대기열 대본 스키마 검증. 문제 사유 리스트를 돌려준다(빈 리스트 = 통과).

    garbage 발행 0 이 최우선이라 여기서 막히면 렌더까지 가지 않고 failed 로 보낸다.
    """
    r = []
    if not isinstance(data, dict):
        return ["최상위가 JSON object 가 아님"]
    if (data.get("author") or "") != "claude":
        r.append('author 가 "claude" 가 아님')

    for k in _QUEUE_STR_FIELDS:
        v = data.get(k)
        if not isinstance(v, str) or not v.strip():
            r.append(f"{k}: 비어있거나 문자열이 아님")

    key = data.get("animal_key") if isinstance(data.get("animal_key"), str) else ""
    if key and not _QUEUE_KEY_RE.match(key):
        r.append(f"animal_key 형식 위반(^[a-z0-9_]+$): {key!r}")
    stem = os.path.splitext(os.path.basename(filename))[0]
    slug = stem.split("_", 1)[1] if "_" in stem and stem.split("_", 1)[0].isdigit() else stem
    if key and slug != key:
        r.append(f"파일명 슬러그({slug!r}) != animal_key({key!r})")

    title = data.get("title_en")
    if isinstance(title, str) and len(title) > 95:
        r.append(f"title_en 이 95자 초과({len(title)}자)")

    facts = data.get("facts")
    if not isinstance(facts, list) or len(facts) != QUEUE_FACTS_N or not all(
            isinstance(f, str) and f.strip() for f in facts):
        r.append(f"facts 가 비어있지 않은 문자열 {QUEUE_FACTS_N}개가 아님")

    fkw = data.get("fact_keywords")
    if not isinstance(fkw, list) or len(fkw) != QUEUE_FACTS_N or not all(
            isinstance(f, str) and f.strip() for f in fkw):
        r.append(f"fact_keywords 가 비어있지 않은 문자열 {QUEUE_FACTS_N}개가 아님")

    pkw = data.get("pexels_keywords")
    if not isinstance(pkw, list) or len(pkw) < 5 or not all(
            isinstance(f, str) and f.strip() for f in pkw):
        r.append("pexels_keywords 가 비어있지 않은 문자열 5개 이상이 아님")

    tags = data.get("tags")
    if not isinstance(tags, list) or not tags or not all(
            isinstance(t, str) and t.strip() for t in tags):
        r.append("tags 가 비어있지 않은 문자열 배열이 아님")

    cp = data.get("confusion_pair")  # 선택 키 — 있으면 문자열 배열이어야 한다
    if cp is not None and (not isinstance(cp, list) or not all(
            isinstance(c, str) and c.strip() for c in cp)):
        r.append("confusion_pair 가 문자열 배열이 아님")

    for k in ("title_ko", "description_ko", "hook_line"):  # 선택 키 — 있으면 문자열이어야 한다
        v = data.get(k)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            r.append(f"{k}: 값이 있으나 문자열이 아니거나 비어있음")

    # 2026-09-03 NO_OPENAI: clips(잠금 Pexels id)·localizations(7개국) 는 NO_OPENAI 면 필수, 아니면 선택.
    n_facts = len(facts) if isinstance(facts, list) and facts else QUEUE_FACTS_N
    clips = data.get("clips")
    if clips is not None or _cfg.no_openai_mode():
        ok = (isinstance(clips, list) and len(clips) == n_facts and all(
            isinstance(sl, list) and 1 <= len(sl) <= 2
            and all(isinstance(c, int) and not isinstance(c, bool) and c > 0 for c in sl)
            for sl in clips))
        if not ok:
            r.append((QUEUE_UNLOCKED_PREFIX if clips is None else "")
                     + f"clips 가 facts 수({n_facts})만큼의 [양의 정수 id 1~2개] 슬롯이 아님")
        else:
            flat = [c for sl in clips for c in sl]
            if len(flat) != len(set(flat)):
                r.append("clips 에 같은 id 가 두 번 들어감(영상 내 중복)")
    locs = data.get("localizations")
    if locs is not None or _cfg.no_openai_mode():
        need = ("ko", "ja", "es", "pt", "de", "fr", "hi")
        good = isinstance(locs, dict) and all(
            isinstance(locs.get(c), dict)
            and isinstance(locs[c].get("title"), str) and locs[c]["title"].strip()
            and len(locs[c]["title"]) <= 95
            and isinstance(locs[c].get("description"), str) and locs[c]["description"].strip()
            for c in need)
        if not good:
            r.append((QUEUE_UNLOCKED_PREFIX if locs is None else "")
                     + "localizations 가 7개 언어(ko/ja/es/pt/de/fr/hi) 모두 title(≤95자)·description 문자열을 갖지 않음")
    caps = data.get("captions")
    if caps is not None:
        n_lines = n_facts + 2  # TTS 라인 = intro + facts + outro
        good = isinstance(caps, dict) and caps and all(
            isinstance(v, list) and len(v) == n_lines and all(isinstance(t, str) and t.strip() for t in v)
            for v in caps.values())
        if not good:
            r.append(f"captions 가 언어별 {n_lines}줄(intro+facts+outro) 비어있지 않은 문자열 배열이 아님")
    return r


def _queue_enrich(meta: dict) -> dict:
    """로스터(cities.json)에 있는 도시면 confusion_pair 를 대본에 채워 넣는다.

    동음이의 도시(Lisbon↔Porto 등) 오매칭 차단은 pexels slug 필터와 vision 이 함께 쓰는
    값이라, 대본에 없으면 로스터 값을 쓴다(대본이 명시했으면 대본 우선).
    """
    key = meta.get("animal_key") or ""
    cfg = ANIMALS.get(key) or {}
    if not meta.get("confusion_pair") and cfg.get("confusion_pair"):
        meta["confusion_pair"] = list(cfg["confusion_pair"])
        log.info(f"[queue] confusion_pair 로스터 보강: {meta['confusion_pair']}")
    if not cfg:
        log.warning(f"[queue] 로스터(cities.json)에 없는 도시 키: {key} — 대본 값만 사용")
    return meta


def _queue_move_failed(path: str, reasons: list):
    """검증 실패 파일을 failed/ 로 옮기고 같은 이름 .reason.txt 에 사유를 남긴다."""
    os.makedirs(QUEUE_FAILED_DIR, exist_ok=True)
    name = os.path.basename(path)
    dest = os.path.join(QUEUE_FAILED_DIR, name)
    try:
        if os.path.exists(dest):
            os.remove(dest)
        shutil.move(path, dest)
    except Exception as e:
        log.warning(f"[queue] failed 이동 실패 {name}: {e}")
        return
    reason_path = os.path.join(QUEUE_FAILED_DIR, os.path.splitext(name)[0] + ".reason.txt")
    try:
        with open(reason_path, "w", encoding="utf-8") as f:
            print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), file=f)
            for r in reasons:
                print(f"- {r}", file=f)
    except Exception as e:
        log.warning(f"[queue] reason 파일 기록 실패 {name}: {e}")
    log.error(f"[queue] 스키마 검증 실패 → failed/{name}: {reasons}")


def _stamp_published(path: str, video_id: str, video_url: str) -> bool:
    """파일에 발행 정보(video_id)를 덧쓴다. done 이동이 실패했을 때의 최후 방어 —
    pending 에 남더라도 video_id 가 찍혀 있으면 try_queue_script 가 재발행하지 않는다."""
    try:
        data = json.load(open(path, encoding="utf-8"))
        data["published_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["video_id"] = video_id
        data["video_url"] = video_url
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            print(file=f)
        return True
    except Exception as e:
        log.warning(f"[queue] 발행정보 기록 실패 {os.path.basename(path)}: {e}")
        return False


def queue_mark_done(path: str, video_id: str, video_url: str) -> bool:
    """업로드가 videoId 를 받은 뒤에만 호출. pending → done 이동 후 발행 정보를 덧붙인다.

    순서가 중요하다(codex 지적): '쓰고 나서 지우기'로 하면 삭제가 실패했을 때 pending 에
    원본이 남아 **다음 cron 이 같은 대본을 또 발행**한다. 먼저 옮겨(os.replace = 원자적
    rename) pending 에서 사라지게 한 뒤, 옮긴 파일에 메타를 덧쓴다. 덧쓰기가 실패해도
    최악이 '발행 정보 없는 done 파일'이라 중복 발행은 일어나지 않는다.
    반환: 이동 성공 여부(호출부가 실패를 알아야 중복 발행 위험을 알린다).
    """
    os.makedirs(QUEUE_DONE_DIR, exist_ok=True)
    name = os.path.basename(path)
    dest = os.path.join(QUEUE_DONE_DIR, name)
    if os.path.exists(dest):
        # 같은 이름이 이미 done 에 있다 = 과거 발행분. 덮어쓰면 그 발행 기록이 사라지므로
        # 새 파일을 다른 이름으로 보존하고 크게 남긴다(중복 발행 조사용).
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = os.path.join(QUEUE_DONE_DIR, f"{os.path.splitext(name)[0]}.dup-{stamp}.json")
        log.error(f"[queue] done/{name} 이 이미 있음 — 중복 발행 의심. 새 파일은 {os.path.basename(dest)} 로 보존")
    try:
        os.replace(path, dest)  # 같은 볼륨 내 원자적 이동
    except Exception as e:
        log.error(f"[queue] done 이동 실패 — pending 에 남음(중복 발행 위험!) {name}: {e}")
        # 최후 방어: pending 원본에 video_id 를 찍어 다음 실행이 재발행하지 않게 한다.
        _stamp_published(path, video_id, video_url)
        return False
    try:
        data = json.load(open(dest, encoding="utf-8"))
        data["published_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["video_id"] = video_id
        data["video_url"] = video_url
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            print(file=f)
        log.info(f"[queue] done/{os.path.basename(dest)} 이동 완료 (video_id={video_id})")
    except Exception as e:
        log.warning(f"[queue] done/{name} 발행정보 기록 실패(이동 자체는 완료): {e}")
    return True


def _consume_queue_if_uploaded(queue_path: str, url: str) -> bool:
    """업로드 결과 url 이 진짜 videoId 를 담고 있을 때만 대기열을 소비한다.
    반환: 대기열 파일이 done 으로 정리됐는지(대기열 대본이 아니면 True)."""
    if not queue_path:
        return True
    if not url:
        log.info("[queue] 업로드 videoId 없음 → pending 유지 (다음 실행에서 재시도)")
        return False
    vid = str(url).rstrip("/").split("/")[-1].split("?")[0]
    # videoId 모양이 아니면 소비하지 않는다 — 업로더가 이상한 truthy 값을 돌려줘도
    # 대본을 done 으로 태워버리지 않게(codex 지적).
    if str(url).startswith("http") and re.fullmatch(r"[A-Za-z0-9_-]{8,20}", vid or ""):
        return queue_mark_done(queue_path, vid, url)
    log.error(f"[queue] 업로드 반환값이 videoId 형태가 아님 → pending 유지: {url!r}")
    return False


def try_queue_script(used_pexels: list, recent_clips, budget: list = None):
    """대기열에서 발행 가능한 대본 1건 확보.

    파일명 순으로 최대 QUEUE_MAX_TRIES 개까지 시도한다.
    - 스키마 위반 → failed/ 이동(시도 횟수에 안 셈)
    - 소재 매칭 실패(RuntimeError) → pending 에 그대로 두고 다음 파일
    - 그 외 예외(vision 오류·네트워크)는 그대로 전파 → 실행 실패 → 다음 cron 재시도

    budget: [남은 시도 횟수] 형태의 1칸 리스트. 호출부(run_once)가 '내레이션 길이 초과'로
    재호출할 때도 같은 리스트를 넘겨 **한 실행의 총 시도 횟수를 3개로 묶는다**.
    반환: (meta, city_key, work_dir, bg_groups, bg_clip_ids, queue_path) 또는 None
    """
    if budget is None:
        budget = [QUEUE_MAX_TRIES]
    files = queue_pending_files()
    if not files:
        return None
    log.info(f"[queue] pending {len(files)}건 — 남은 시도 {budget[0]}건")
    for path in files:
        if budget[0] <= 0:
            log.warning(f"[queue] 시도 횟수({QUEUE_MAX_TRIES}) 소진 — 중단")
            break
        name = os.path.basename(path)
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            _queue_move_failed(path, [f"JSON 파싱 실패: {e}"])
            continue
        if data.get("video_id"):
            # 발행은 됐는데 done 이동이 실패해 pending 에 남은 파일 — 절대 재발행하지 않는다.
            log.error(f"[queue] {name} 은 이미 발행됨(video_id={data['video_id']}) → done 으로 정리하고 건너뜀")
            queue_mark_done(path, str(data["video_id"]), data.get("video_url", ""))
            continue
        reasons = validate_queue_meta(data, name)
        if reasons and all(x.startswith(QUEUE_UNLOCKED_PREFIX) for x in reasons):
            # 아직 clips/localizations 를 못 채운 대본 — 고장이 아니라 미완성. pending 에 두고 다음 파일로.
            log.warning(f"[queue] {name}: 잠금 정보 없음(NO_OPENAI) → pending 유지, 다음 대본 시도")
            continue
        if reasons:
            _queue_move_failed(path, reasons)
            continue

        budget[0] -= 1
        meta = _queue_enrich(dict(data))
        key = meta["animal_key"]
        meta["use_nasa"] = USE_NASA_MEDIA
        wd = os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y%m%d"), key)
        os.makedirs(wd, exist_ok=True)
        log.info(f"[queue {QUEUE_MAX_TRIES - budget[0]}/{QUEUE_MAX_TRIES}] {name} — "
                 f"{meta['display']}, {meta.get('country', '')} ({meta['category']})")
        try:
            groups, cids = fetch_verified_backgrounds(
                meta, wd, exclude_pexels_ids=used_pexels, exclude_clip_ids=recent_clips)
        except RuntimeError as e:
            log.warning(f"[queue] 소재 매칭 실패 — pending 유지하고 다음 파일: {name}: {e}")
            continue
        return meta, key, wd, groups, cids, path
    return None


def _fetch_locked_backgrounds(meta: dict, work_dir: str, locked: list, n: int,
                              exclude_pexels_ids=None, exclude_clip_ids=None):
    """대본이 잠근 Pexels id 로만 배경을 채운다 — vision·NASA·AI 폴백 전부 건너뜀(2026-09-03 NO_OPENAI).

    id 는 사람이 포스터를 보고 고른 것이라 최근 사용 dedup(used_pexels/recent_clips)보다 우선한다(경고만).
    기존 안전선은 유지: 슬롯 min(n,3)개 이상 채워져야 하고, 빈 슬롯은 이웃 슬롯 클립을 빌린다.
    반환 (bg_groups, clip_ids) — fetch_verified_backgrounds 와 동일 형식.
    """
    import time as _time
    from pipeline import pexels
    _t0 = _time.monotonic()
    if not isinstance(locked, list) or len(locked) != n:
        got = len(locked) if isinstance(locked, list) else "?"
        raise RuntimeError(f"clips 슬롯 수({got}) != facts({n})")
    soft = {str(c) for c in (exclude_pexels_ids or [])}
    hard = {str(c) for c in (exclude_clip_ids or set())}
    want = max(1, int(getattr(_cfg, "CLIPS_PER_FACT", 1)))
    slots = [[] for _ in range(n)]
    seen = set()
    for i, ids in enumerate(locked):
        for cid in list(ids or [])[:want]:
            cid = str(cid)
            if cid in seen:
                log.warning(f"clips fact[{i}] id={cid} 영상 내 중복 → 건너뜀")
                continue
            if cid in hard or cid in soft:
                log.warning(f"clips fact[{i}] id={cid} 는 최근 사용 풋티지 — 대본 잠금이 우선(허용)")
            path = pexels.fetch_by_id(cid, work_dir)
            if path:
                slots[i].append((path, cid))
                seen.add(cid)
    filled_idx = [i for i in range(n) if slots[i]]
    min_pass = min(n, 3)
    if len(filled_idx) < min_pass:
        for i in filled_idx:
            for path, _ in slots[i]:
                try:
                    os.remove(path)
                except Exception:
                    pass
        raise RuntimeError(f"잠금 클립 다운로드 부족 ({len(filled_idx)}/{n}, min={min_pass})")
    bg_groups = []
    for i in range(n):
        if slots[i]:
            bg_groups.append(list(slots[i]))
            continue
        j = min(filled_idx, key=lambda x: (abs(x - i), x))
        log.warning(f"fact[{i}] 잠금 클립 없음 → fact[{j}] 클립 재사용 (fact↔영상 정렬 유지)")
        bg_groups.append([slots[j][0]])
    ids = list(dict.fromkeys(cid for i in filled_idx for _, cid in slots[i]))
    log.info(f"배경 확보(잠금): fact {len(filled_idx)}/{n} 슬롯, 컷 {sum(len(g) for g in bg_groups)}개 "
             f"(슬롯별 {[len(g) for g in bg_groups]}, vision 0회)")
    log.info(f"[timing] media={_time.monotonic() - _t0:.1f} (locked)")
    return bg_groups, ids


def _meta_localizations(meta: dict):
    """대본이 준 localizations({lang: {title, description}}) 를 YouTube 형식으로 정리. 없으면 None."""
    raw = meta.get("localizations")
    if not isinstance(raw, dict) or not raw:
        return None
    out = {}
    for code, e in raw.items():
        if not isinstance(e, dict):
            continue
        title = (e.get("title") or "").strip()
        desc = (e.get("description") or "").strip()
        if title:
            out[str(code)] = {"title": title[:95], "description": desc[:4900]}
    return out or None


def fetch_verified_backgrounds(meta: dict, work_dir: str,
                                oversample: int = 2,
                                exclude_pexels_ids: list = None,
                                exclude_clip_ids=None):
    """비전 검증(fail-closed) 통과 배경을 **fact 슬롯 단위**로 확보 (배경 수 = fact 수 지향).

    각 slot i(=fact i)에 대해 소스 순서대로 시도 → 슬롯 인덱스 보존(fact와 영상 밀림 방지):
      우주봇(use_nasa): NASA 영상 → Pexels → NASA 이미지 Ken Burns
      동물봇: Pexels
    미충족 슬롯은 AI 이미지 폴백으로 채우되 **AI도 vision 검증 필수**(fail-closed 우회 금지).
    모든 클립은 서로 다름(영상 내 중복 없음). 최종 검증 통과분 < min_pass면 RuntimeError.

    2026-09-03 컷 페이싱: 슬롯마다 config.CLIPS_PER_FACT개까지 모은다(모자라면 1개 폴백).
    정지 이미지 tier(NASA 이미지·Pexels 사진 Ken Burns·AI 이미지)는 슬롯이 아예 비었을 때만 쓴다.

    clip id 태깅(L): NASA는 "nasa:<id>", Pexels 사진은 "pexphoto:<id>", Pexels 영상은 숫자
    문자열, AI는 "".
    반환: (bg_groups, clip_ids).
      bg_groups = [[(path, clip_id), ...], ...]  — 바깥 리스트가 하이라이트 순서, 안쪽이 그 컷들
      clip_ids  = 위 그룹을 평탄화한 clip id 리스트(사용 기록/dedup용)
    """
    import time as _time
    from pipeline import pexels, verify_visual

    _t_media0 = _time.monotonic()

    single = meta.get("single", "")      # 도시명
    display = meta.get("display", "")     # 도시명
    category = meta.get("category", "")
    country = meta.get("country", "")
    landmark_lock = meta.get("landmark_lock", "")
    confusion_pair = meta.get("confusion_pair") or []
    angle = (meta.get("angle") or "").strip().lower()
    facts = meta.get("facts") or []
    n = max(1, len(facts))  # 배경 수 = 하이라이트 수

    # 대기열 대본(author=claude)이 clips 로 Pexels id 를 잠갔으면 vision 없이 그 id 만 쓴다(NO_OPENAI).
    if meta.get("author") == "claude" and meta.get("clips"):
        return _fetch_locked_backgrounds(meta, work_dir, meta.get("clips"), n,
                                         exclude_pexels_ids=exclude_pexels_ids,
                                         exclude_clip_ids=exclude_clip_ids)

    ordered_kw = [k for k in (meta.get("fact_keywords") or []) if isinstance(k, str) and k.strip()]
    generic_kw = [g for g in (meta.get("pexels_keywords") or []) if isinstance(g, str) and g.strip()]
    # 폴백은 '정원 랜드마크'(generic)보다 '도시/거리 일반'을 먼저 시도한다 — 음식·추상 fact가
    # 그 주제 영상을 못 찾을 때 엉뚱한 정원(+꽃)으로 튀지 않게 (쿠마님 2026-07-19 A+C 조합).
    _single_s = (single or "").strip()
    _neutral_kw = ([f"{_single_s} old town street", f"{_single_s} downtown", f"{_single_s} city street scene"]
                   if _single_s else [])
    while len(ordered_kw) < n:
        ordered_kw.append(generic_kw[len(ordered_kw) % len(generic_kw)] if generic_kw else (single or "city"))

    # exclude 분류(L): "nasa:"/"pexphoto:" 접두어 → hard, 숫자 → Pexels video hard. exclude_pexels_ids는 soft.
    excl = {str(c) for c in (exclude_clip_ids or set())}
    hard_nasa = {c[len("nasa:"):] for c in excl if c.startswith("nasa:")}
    hard_photo = {c[len("pexphoto:"):] for c in excl if c.startswith("pexphoto:")}
    hard_pexels = [int(c) for c in excl if c.isdigit()]
    soft_pexels = [int(c) for c in (exclude_pexels_ids or []) if str(c).isdigit()]

    want = max(1, int(getattr(_cfg, "CLIPS_PER_FACT", 1)))  # 슬롯당 목표 클립 수(부족하면 1개 폴백)
    slots = [[] for _ in range(n)]          # slots[i] = [(path, clip_id), ...]
    rejected = []
    used_clip_set = set()  # 이번 영상 내 중복 방지 (태깅된 id)
    verify_count = [0]

    nasa = None
    if meta.get("use_nasa"):
        try:
            from pipeline import nasa_media as nasa
        except Exception as e:
            log.warning(f"nasa_media 임포트 실패 — NASA 소스 skip: {e}")
            nasa = None

    def _verify(path, strict=False, subject=""):
        if not path or not os.path.exists(path):
            return False
        if verify_count[0] > 0:
            _time.sleep(0.5)  # vision TPM burst 회피
        verify_count[0] += 1
        return verify_visual.verify_city(path, single, country=country,
                                         landmark_lock=landmark_lock, strict=strict,
                                         confusion_pair=confusion_pair, subject=subject)

    used_path_set = set()  # clip id 를 못 뽑는 후보(AI 이미지 등)까지 커버하는 경로 dedup

    def _assign(i, path, cid):
        slots[i].append((path, cid))
        used_path_set.add(os.path.realpath(path))
        if cid:
            used_clip_set.add(cid)

    def _slot_paths(i):
        return [p for p, _ in slots[i]]

    def _used_nasa():
        return hard_nasa | {c[len("nasa:"):] for c in used_clip_set if c.startswith("nasa:")}

    def _used_pexels():
        return [int(c) for c in used_clip_set if c.isdigit()]

    def _used_photos():
        return hard_photo | {c[len("pexphoto:"):] for c in used_clip_set if c.startswith("pexphoto:")}

    # === 슬롯별 채움 (NASA영상 → Pexels영상 → NASA이미지 → Pexels사진 KenBurns) — 슬롯 인덱스 = 하이라이트 인덱스 ===
    for i in range(n):
        kw = ordered_kw[i]
        # 폴백 순서: [주제 키워드] → [도시/거리 중립] → [정원 등 랜드마크]. (C) 주제 영상 실패 시
        # 정원으로 직행하지 않고 도시/거리를 먼저 시도.
        _fb = ([g for g in _neutral_kw if g != kw]
               + [g for g in generic_kw if g != kw and g not in _neutral_kw])
        slot_kw = [kw] + _fb
        # 아이콘 랜드마크 슬롯(첫 슬롯 + iconic_landmarks 앵글)은 엄격 검증, 나머지는 느슨.
        slot_strict = (i == 0 and angle == "iconic_landmarks")

        # 1) NASA 영상 (우주봇 전용; 여행봇은 nasa None → skip). 슬롯당 1개까지.
        if nasa is not None and len(slots[i]) < want:
            p, nid = nasa.fetch_video(kw, work_dir, exclude_ids=_used_nasa())
            if p:
                if _verify(p, strict=slot_strict, subject=kw):
                    _assign(i, p, f"nasa:{nid}")
                else:
                    rejected.append(p)

        # 2) Pexels 영상 — 남은 수만큼 (slot 키워드 순서, hard/soft exclude 분리, 동음이의 country 필터)
        need = want - len(slots[i])
        if need > 0:
            try:
                cands = pexels.fetch_backgrounds(
                    slot_kw, work_dir, n=oversample + need,
                    animal_single=single, country=country, ordered=True,
                    exclude_pexels_ids=soft_pexels + _used_pexels(),
                    hard_exclude_ids=hard_pexels + _used_pexels(),
                    confusion_pair=confusion_pair,
                )
            except RuntimeError:
                cands = []
            for cand in cands:
                if len(slots[i]) >= want:
                    break
                cid = _extract_clip_id(cand)
                if (cid and cid in used_clip_set) or os.path.realpath(cand) in used_path_set:
                    rejected.append(cand)
                    continue
                if _verify(cand, strict=slot_strict, subject=kw):
                    _assign(i, cand, cid or "")
                else:
                    rejected.append(cand)
            # 이 슬롯에서 못 쓴 나머지 후보는 정리 대상
            taken = set(_slot_paths(i))
            for other in cands:
                if other not in taken and other not in rejected:
                    rejected.append(other)
        if slots[i]:
            continue

        # 3) NASA 이미지 → Ken Burns (파일명 nasa_img_* — 영상과 분리, H)
        #    슬롯이 아예 비었을 때만 — 정지 이미지는 컷 페이싱 소재로 쓰지 않는다.
        if nasa is not None:
            png, nid = nasa.fetch_image(kw, work_dir, exclude_ids=_used_nasa())
            if png:
                from pipeline import image_to_video
                safe = ("".join(ch if ch.isalnum() else "_" for ch in str(nid))[:40]) if nid else f"kb_{i}"
                mp4 = os.path.join(work_dir, f"nasa_img_{safe}.mp4")
                try:
                    image_to_video.png_to_video(png, mp4, duration=10.0)
                except Exception as e:
                    log.warning(f"NASA 이미지 Ken Burns 실패: {e}")
                    mp4 = None
                finally:
                    try:
                        os.remove(png)
                    except Exception:
                        pass
                if mp4 and _verify(mp4, strict=slot_strict, subject=kw):
                    _assign(i, mp4, f"nasa:{nid}")
                    continue
                if mp4:
                    rejected.append(mp4)

        # 3.5) Pexels 사진 → Ken Burns (여행봇 tier: portrait 영상 부족 슬롯을 사진으로 채움)
        if USE_PEXELS_PHOTO:
            try:
                photos = pexels.fetch_photos(
                    slot_kw, work_dir, n=oversample + 1,
                    animal_single=single, country=country, ordered=True,
                    exclude_ids=list(_used_photos()),
                    confusion_pair=confusion_pair,
                )
            except Exception as e:
                log.warning(f"pexels 사진 fetch 실패: {e}")
                photos = []
            photo_assigned = False
            for png in photos:
                pid = _extract_photo_id(png)
                if pid and f"pexphoto:{pid}" in used_clip_set:
                    rejected.append(png)
                    continue
                from pipeline import image_to_video
                mp4 = os.path.join(work_dir, f"pexphoto_{pid or i}.mp4")
                try:
                    image_to_video.png_to_video(png, mp4, duration=10.0)
                except Exception as e:
                    log.warning(f"Pexels 사진 Ken Burns 실패: {e}")
                    # 부분 생성된 mp4 정리 (누수 방지)
                    try:
                        if os.path.exists(mp4):
                            os.remove(mp4)
                    except Exception:
                        pass
                    mp4 = None
                finally:
                    try:
                        os.remove(png)
                    except Exception:
                        pass
                if mp4 and _verify(mp4, strict=slot_strict, subject=kw):
                    _assign(i, mp4, f"pexphoto:{pid}" if pid else "")
                    photo_assigned = True
                    break
                if mp4:
                    rejected.append(mp4)
            # break로 남은 미처리 사진 png 정리 (처리된 png는 finally에서 이미 삭제)
            for leftover in photos:
                if os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except Exception:
                        pass
            if photo_assigned:
                continue
        # slot 미충족 → 빈 리스트 유지

    # === AI 이미지 폴백 (K: 반드시 vision 검증) — 완전히 빈 슬롯만 채움 ===
    unfilled = [i for i in range(n) if not slots[i]]
    if unfilled:
        log.warning(f"미충족 슬롯 {len(unfilled)}개 → AI 이미지 폴백 (vision 검증 필수)")
        try:
            from pipeline import image_fallback, image_to_video
            pngs = image_fallback.generate_fallback_images(meta, len(unfilled), work_dir)
            mp4s = image_to_video.pngs_to_videos(pngs, work_dir, duration=10.0) if pngs else []
            mi = 0
            for i in unfilled:
                if mi >= len(mp4s):
                    break
                cand = mp4s[mi]
                mi += 1
                # 슬롯별 키워드/strict 로 검증 — 루프 잔존 kw(마지막 슬롯 값)를 쓰면
                # 음식 슬롯 AI 이미지가 엉뚱한 힌트로 판정되는 오염 발생 (2026-07-20 fix)
                if _verify(cand, subject=ordered_kw[i],
                           strict=(i == 0 and angle == "iconic_landmarks")):
                    _assign(i, cand, "")
                else:
                    rejected.append(cand)
        except Exception as e:
            log.warning(f"AI 이미지 폴백 실패: {e}")

    # === 결과 정리 — 부분통과여도 하이라이트-배경 정렬 보존 (2026-07-20 fix) ===
    # 미충족 슬롯을 그냥 빼버리면 리스트가 앞으로 당겨져 **하이라이트와 영상이 밀린다**.
    # 그래서 슬롯 개수(n)는 유지하고, 빈 슬롯은 가장 가까운 채워진 슬롯의 클립 1개를 빌려 채운다.
    filled_idx = [i for i in range(n) if slots[i]]
    accepted = {p for i in filled_idx for p, _ in slots[i]}
    for r in rejected:
        if r in accepted:  # H: 채택된 경로는 삭제 skip
            continue
        try:
            os.remove(r)
        except Exception:
            pass

    min_pass = min(n, 3)  # garbage 0: 최소 '고유' 통과 배경 수(패딩 제외 — 기존 바 유지)
    if len(filled_idx) < min_pass:
        for p in accepted:
            try:
                os.remove(p)
            except Exception:
                pass
        raise RuntimeError(
            f"비전 검증 통과 배경 부족 ({len(filled_idx)}/{n}, min={min_pass}) — 다른 토픽 픽 필요"
        )

    bg_groups = []
    for i in range(n):
        if slots[i]:
            bg_groups.append(list(slots[i]))
            continue
        j = min(filled_idx, key=lambda x: (abs(x - i), x))
        log.warning(f"highlight[{i}] 소재 없음 → highlight[{j}] 클립 재사용 (하이라이트↔영상 정렬 유지)")
        bg_groups.append([slots[j][0]])
    # 사용 기록용 id 는 실제 검증분만(빌려온 중복 제외), 순서 유지 dedup
    filled_ids = list(dict.fromkeys(cid for i in filled_idx for _, cid in slots[i]))
    n_clips = sum(len(g) for g in bg_groups)
    log.info(f"배경 확보: 하이라이트 {len(filled_idx)}/{n} 슬롯 검증 통과, 컷 {n_clips}개 "
             f"(슬롯당 목표 {want}개, 슬롯별 {[len(g) for g in bg_groups]})")
    log.info(f"[timing] media={_time.monotonic() - _t_media0:.1f} (vision {verify_count[0]}회)")
    return bg_groups, filled_ids


def run_once(forced_animal: str = None, dry_run: bool = False, no_upload: bool = False) -> dict:
    from pipeline import animals, pexels, tts, composer, translator
    from pipeline import history as hist_mod

    # 2026-07-17: 필수 API 키 fail-fast — 시크릿 누락/오타 상태로 vision 0회 검증 발행이
    # 조용히 성공하는 것을 차단(verify_visual도 무키 reject하지만 여기서 시작 자체를 막아
    # GPT/Pexels 호출 낭비 없이 명확한 에러로 실패시킨다).
    _required = ("OPENAI_API_KEY", "PEXELS_API_KEY")
    if _cfg.no_openai_mode():
        # 2026-09-03: OpenAI 를 한 번도 부르지 않는 모드 — 대본이 clips/localizations/captions 를 제공한다.
        # GPT 갈래가 열려 있으면 안 되므로 QUEUE_ONLY 가 같이 켜져 있어야 한다.
        if not queue_only_mode():
            raise RuntimeError("NO_OPENAI=1 인데 QUEUE_ONLY 가 아님 — GPT 갈래 차단을 위해 QUEUE_ONLY=1 필요")
        log.info("NO_OPENAI=1 — OpenAI 무호출 모드(클립 잠금·localizations·captions 는 대본 제공)")
        _required = ("PEXELS_API_KEY",)
    _missing = [k for k in _required if not (getattr(_cfg, k, "") or "").strip()]
    if _missing:
        raise RuntimeError(f"필수 API 키 누락: {', '.join(_missing)} — fail-fast (무검증 발행 차단)")

    used = _load_used()
    used_pexels = _load_used_pexels()

    # 콘텐츠 이력 로드. 없을 때만 used_animals.json에서 마이그레이션 —
    # 단, .corrupt 백업이 있으면(손상 감지) 재마이그레이션 금지(이력 전량 소실 방지, A).
    hist = hist_mod.load_history(CONTENT_HISTORY_PATH)
    if not hist and used:
        if os.path.exists(CONTENT_HISTORY_PATH + hist_mod.CORRUPT_SUFFIX):
            log.warning("content_history 손상 백업(.corrupt) 존재 → 재마이그레이션 skip (이력 소실 방지)")
        else:
            hist = hist_mod.migrate_from_used(used, CONTENT_WINDOW_DAYS)
            hist_mod.save_history(CONTENT_HISTORY_PATH, hist)
            log.info(f"content_history.json 없음 → used_cities.json {len(used)}건 마이그레이션")

    # (D) 동적 GPT 픽/hard-block용 exclude = 완화 없는 시간창 ∪ used[-KEEP:] 전체
    dynamic_exclude, _ = hist_mod.compute_exclude(
        hist, used, USED_KEEP, CONTENT_WINDOW_DAYS, pool=None)
    dynamic_exclude_list = sorted(dynamic_exclude)
    # (D) 정적 풀 fallback 사전 픽용 exclude = 후보 8개 미만이면 창 완화
    fallback_exclude, eff_window = hist_mod.compute_exclude(
        hist, used, USED_KEEP, CONTENT_WINDOW_DAYS,
        pool=list(ANIMALS.keys()), min_candidates=HISTORY_MIN_CANDIDATES,
        min_window=HISTORY_MIN_WINDOW_DAYS)
    avoid_formats = hist_mod.recent_formats(hist, RECENT_FORMAT_KEEP)
    avoid_angles = hist_mod.recent_angles(hist, RECENT_FORMAT_KEEP)  # 전역 angle 연속 방지 (2026-07-17 배선)
    recent_clips = hist_mod.recent_clip_ids(hist, CLIP_DEDUP_DAYS)  # 태깅된 id (nasa:/숫자)
    log.info(f"dynamic_exclude {len(dynamic_exclude_list)}개 / fallback_exclude {len(fallback_exclude)}개 (window={eff_window:.0f}d) / avoid_formats={avoid_formats} / avoid_angles={avoid_angles} / recent_clips={len(recent_clips)}")

    # 대본 확보 → TTS → 합성. 대기열 갈래는 '내레이션이 길어 음성이 잘리는' 대본을 만나면
    # 그 대본을 queue/failed 로 빼고 다음 pending 으로 재시도한다(총 시도는 queue_budget 로 3건 제한).
    queue_budget = [QUEUE_MAX_TRIES]
    while True:
        bg_clip_ids = []
        queue_path = None  # 대기열 대본을 쓴 경우 그 pending 파일 경로 (업로드 성공 후 done 이동)

        # 우선순위: ① CLI 강제(--city) → ② 대기열 → ③ QUEUE_ONLY 이면 건너뜀 → ④ GPT 갈래
        queue_pick = (None if forced_animal
                      else try_queue_script(used_pexels, recent_clips, queue_budget))

        if forced_animal:
            # 2026-07-17: 오타 키가 조용히 로스터 첫 도시(Paris)로 대체되던 문제 → 사전 검증.
            # 키는 lower_snake_case — 'Kyoto'/'kyoto japan' 같은 입력도 정규화 후 해석.
            forced_animal = forced_animal.strip().lower().replace(" ", "_")
            resolved = animals._resolve_roster_key(forced_animal)
            if resolved not in ANIMALS:
                low = forced_animal.lower().replace(" ", "_")
                close = [k for k in ANIMALS if low in k or k.startswith(low[:4])][:5]
                raise RuntimeError(
                    f"--city '{forced_animal}' 는 로스터에 없는 키입니다. "
                    f"비슷한 키: {close or '없음'} (정확한 키는 cities.json의 city_key 참조)")
            if resolved != forced_animal:
                log.info(f"--city 키 정규화: {forced_animal} → {resolved}")
            forced_animal = resolved
            meta = animals._legacy_meta(forced_animal)
            try:
                meta.update(animals.expand_legacy_to_rich(meta))
            except Exception as e:
                log.warning(f"forced expand 실패 → 템플릿 유지: {e}")
            animal_key = meta["animal_key"]
            work_dir = os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y%m%d"), animal_key)
            os.makedirs(work_dir, exist_ok=True)
            meta["use_nasa"] = USE_NASA_MEDIA
            bg_groups, bg_clip_ids = fetch_verified_backgrounds(
                meta, work_dir, exclude_pexels_ids=used_pexels, exclude_clip_ids=recent_clips)
        elif queue_pick:
            # 대기열 대본은 GPT 후처리(reground/factcheck/retitle/differentiate/전역 novelty)를
            # 타지 않는다. dynamic_exclude(GPT 중복 회피) 검사도 우회 — 대본 관리는 사람이 한다.
            meta, animal_key, work_dir, bg_groups, bg_clip_ids, queue_path = queue_pick
            log.info(f"[queue] 대기열 대본 채택: {os.path.basename(queue_path)} "
                     f"(GPT 후처리·중복검사 우회)")
        elif queue_only_mode():
            if queue_pending_files():
                # 대본은 있는데 소재를 못 구한 상태 — GPT 갈래로 흘리지 않고 실패시켜 다음 cron 재시도.
                raise RuntimeError(
                    "QUEUE_ONLY=1 — 대기열 대본은 있으나 소재/비전 검증에 모두 실패 (다음 cron 재시도)")
            log.warning("대기열(queue/pending)이 비어 있음 — QUEUE_ONLY=1 이라 발행을 건너뜀")
            print(QUEUE_EMPTY_MARKER, flush=True)
            return {"queue_empty": True, "animal": None, "video": None, "url": None, "locales": []}
        else:
            # GPT 동적 픽 + Pexels 매칭 + 비전 검증 재시도 (최대 5번)
            meta = None
            bg_groups = None
            retry_used = list(dynamic_exclude_list)  # (D) 완화 없는 전체 exclude
            for attempt in range(5):
                try:  # (G) pick 자체 예외도 다음 시도로
                    if _cfg.no_openai_mode():
                        raise RuntimeError("NO_OPENAI 모드에서 GPT 대본 갈래 도달 — 대기열이 비었거나 QUEUE_ONLY 누락")
                    try_meta = animals.pick_random_animal(
                        exclude_keys=retry_used, history=hist, avoid_formats=avoid_formats,
                        avoid_angles=avoid_angles, ever_used=used)
                except Exception as e:
                    log.warning(f"[try {attempt+1}] pick_random_animal 예외 → 재시도: {e}")
                    continue
                ak = try_meta["animal_key"]
                log.info(f"[try {attempt+1}/5] picked animal: {ak} ({try_meta['display']})")
                # 강제 중복 차단: 회피는 GPT 프롬프트 의존이라 무시될 때가 있음 → 최근 사용
                # 동물을 다시 픽하면 코드가 확실히 거르고 재픽한다 (쿠마님 중복 신고 대응).
                if ak in retry_used:
                    log.warning(f"[try {attempt+1}] 중복 토픽 {ak} (제외 목록에 있음) → 재픽")
                    retry_used.append(ak)
                    continue
                wd = os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y%m%d"), ak)
                os.makedirs(wd, exist_ok=True)
                try_meta["use_nasa"] = USE_NASA_MEDIA
                try:
                    bgs, cids = fetch_verified_backgrounds(
                        try_meta, wd, exclude_pexels_ids=used_pexels, exclude_clip_ids=recent_clips)
                except RuntimeError as e:
                    log.warning(f"[try {attempt+1}] 검증 실패 ({try_meta['display']}): {e} → GPT 재픽")
                    retry_used.append(ak)
                    continue
                meta = try_meta
                animal_key = ak
                work_dir = wd
                bg_groups = bgs
                bg_clip_ids = cids
                break

            if meta is None or bg_groups is None:
                # 5번 재시도 실패 → legacy 사전 fallback (모두 strict mode).
                # 2026-05-06 fix (우주봇 Saturn 케이크 사고 동기화): 이전엔 min_pass=1 SOFT mode +
                # 마지막엔 검증 없이 강제 발행이 있어서 garbage 영상이 그대로 발행되는 사고가 있었음.
                # 이제 strict only — 5개 fallback도 모두 vision 통과 못하면 워크플로 실패시킴.
                # (F) 정적 풀 fallback은 expand_legacy_to_rich를 먼저 → fact_keywords 확정 → 배경 fetch.
                log.warning("5번 재시도 실패 → legacy 사전 fallback 시작 (strict only)")
                legacy_tries = sorted(fallback_exclude)  # (D) 완화 적용된 exclude
                for legacy_attempt in range(5):
                    legacy_key = animals.pick_animal(exclude=legacy_tries, ever_used=used)
                    legacy_meta = animals._legacy_meta(legacy_key)
                    wd = os.path.join(OUTPUT_DIR, datetime.now().strftime("%Y%m%d"), legacy_key)
                    os.makedirs(wd, exist_ok=True)
                    log.info(f"[legacy try {legacy_attempt+1}/5] {legacy_key} ({legacy_meta['display']})")
                    # (F) 먼저 expand → 최종 facts/fact_keywords 확정 후 배경 fetch
                    try:
                        legacy_meta.update(animals.expand_legacy_to_rich(legacy_meta))
                        log.info(f"legacy → GPT-rich content 변환 완료 ({len(legacy_meta['facts'])} facts)")
                    except Exception as e:
                        log.warning(f"legacy GPT-rich 실패 → 기본 템플릿 유지: {e}")
                    legacy_meta["use_nasa"] = USE_NASA_MEDIA
                    try:
                        bgs, cids = fetch_verified_backgrounds(
                            legacy_meta, wd, exclude_pexels_ids=used_pexels, exclude_clip_ids=recent_clips)
                    except RuntimeError as e:
                        log.warning(f"[legacy try {legacy_attempt+1}] vision 부족 ({legacy_meta['display']}): {e} → 다음 fallback")
                        legacy_tries.append(legacy_key)
                        continue
                    meta = legacy_meta
                    animal_key = legacy_key
                    work_dir = wd
                    bg_groups = bgs
                    bg_clip_ids = cids
                    break

                if meta is None or bg_groups is None:
                    # garbage 발행 차단 — 워크플로 실패시킴
                    raise RuntimeError(
                        "10번 토픽 시도 모두 vision 검증 실패 — 발행 중단 "
                        "(Pexels 일시 이슈 가능성, 다음 cron에서 재시도)"
                    )

        log.info(f"=== 도시: {animal_key} ({meta['display']}, {meta.get('country','')}, {meta['category']}) ===")
        log.info(f"title_en: {meta['title_en']}")
        log.info(f"hook: {meta['hook']}")
        log.info(f"angle: {meta.get('angle','')} / landmark_lock: {meta.get('landmark_lock','')}")
        for i, f in enumerate(meta["facts"]):
            log.info(f"highlight[{i}]: {f}")

        # 1) TTS (intro + 하이라이트 전체 + outro)
        _t = time.monotonic()
        tts_texts = [meta["intro"]] + list(meta["facts"]) + [meta["outro"]]
        tts_lines = tts.synth_lines(tts_texts, work_dir, prefix="tts")
        log.info(f"[timing] tts={time.monotonic() - _t:.1f}")

        # 3) BGM — 음악쇼츠 GDrive 수노 풀 전체에서 미사용곡 랜덤 (read-only, 2026-07-17 재설계)
        from pipeline import bgm as bgm_mod
        bgm_path, bgm_file_id = bgm_mod.fetch_bgm_random(work_dir)
        if bgm_path:
            log.info(f"bgm: {os.path.basename(bgm_path)}")
        else:
            log.info("bgm: 없음 — voice only 진행")

        # 4) 영상 합성
        out_video = os.path.join(
            work_dir, f"{animal_key}_{datetime.now().strftime('%H%M%S')}.mp4"
        )

        # 4-0) 도입샷 — 첫 화면을 '도시 전경'으로(쿠마님 2026-07-18: 첫 화면이 꽃이 아니라 도시/국가여야 함).
        #      cityscape/skyline/aerial 키워드로 vision 통과분 1개. 실패 시 None → 기존 첫 하이라이트 클립이 첫 화면.
        intro_clip = None
        try:
            from pipeline import pexels as _px, verify_visual as _vv
            _s = (meta.get("single") or "").strip()
            _c = (meta.get("country") or "").strip()
            if _s:
                # hard = 이번 영상 클립(같은 영상 내 중복 절대 금지),
                # soft = 최근 사용 풋티지(used_pexels + 30일 content_history) — 풀 부족 시만 완화.
                # 도입샷은 첫 화면(가장 노출 큰 프레임)이라 직전 영상과 같은 전경 반복 방지.
                _hard = [int(x) for x in (bg_clip_ids or []) if str(x).isdigit()]
                _soft = _hard + list(used_pexels) + [int(c) for c in recent_clips if str(c).isdigit()]
                _kws = [f"{_s} {_c} cityscape".strip(), f"{_s} skyline aerial",
                        f"{_s} {_c} city view".strip(), f"{_s} old town streets", f"{_s} downtown"]
                _cands = _px.fetch_backgrounds(_kws, work_dir, n=4, animal_single=_s, country=_c,
                                               ordered=True, exclude_pexels_ids=_soft, hard_exclude_ids=_hard,
                                               confusion_pair=meta.get("confusion_pair"))
                for _cand in _cands:
                    try:
                        if _vv.verify_city(_cand, _s, country=_c,
                                           landmark_lock=meta.get("landmark_lock", ""),
                                           confusion_pair=meta.get("confusion_pair"),
                                           strict=False):
                            intro_clip = _cand
                            log.info(f"도입샷(도시 전경) 확보: {os.path.basename(_cand)}")
                            break
                    except Exception:
                        continue
                if intro_clip is None:
                    log.info("도입샷 검증 통과 0 → 도입샷 없이 진행(첫 하이라이트 클립이 첫 화면)")
                # 미채택 후보 mp4 정리 — 선택 클립 외 나머지가 work_dir에 누적되는 것 방지
                for _cand in _cands:
                    if _cand is not intro_clip:
                        try:
                            os.remove(_cand)
                        except OSError:
                            pass
        except Exception as e:
            log.warning(f"도입샷 처리 실패 → 도입샷 없이 진행: {e}")

        try:
            composer.compose(bg_groups, tts_lines, meta["hook"], out_video, bgm_path=bgm_path,
                             title=meta.get("title_en"), country=meta.get("country"),
                             intro_clip=intro_clip, city=meta.get("single"))
        except composer.NarrationTooLongError as e:
            # fail-closed: 음성이 잘리는 대본은 발행하지 않는다. 대기열 대본이면 failed 로
            # 빼고 다음 pending 을 시도(시도 예산은 소재 매칭과 공유해 총 3건).
            if not queue_path:
                raise
            _queue_move_failed(queue_path, [f"narration_too_long {e.cut_sec:.2f}"])
            log.warning(f"[queue] 내레이션 길이 초과({e.cut_sec:.2f}s 잘림) → failed 이동, "
                        f"다음 대본 시도 (남은 시도 {queue_budget[0]}건)")
            queue_path = None
            if queue_budget[0] > 0:
                continue
            raise RuntimeError(
                "대기열 대본이 모두 내레이션 길이 초과/소재 실패 — 발행 중단 (대본 보충 필요)")
        log.info(f"video: {out_video} ({os.path.getsize(out_video) / 1024 / 1024:.1f}MB)")
        break

    # 4-1) 썸네일 — 도입샷(없으면 첫 배경 클립) 1.0초 프레임 + 상단 후크(대문자).
    #      소재 클립을 쓰는 이유: 완성본 프레임에는 자막·워터마크가 이미 얹혀 있어 글자가 겹친다.
    #      (fail-soft: 실패해도 발행 진행)
    thumbnail_path = None
    if getattr(_cfg, "ENABLE_THUMBNAIL", False):
        _t = time.monotonic()
        try:
            from pipeline.thumbnail import generate_thumbnail
            src = intro_clip if (intro_clip and os.path.exists(intro_clip)) else None
            if not src and bg_groups and bg_groups[0]:
                first = bg_groups[0][0]
                src = first[0] if isinstance(first, (list, tuple)) else first
            city_txt = (meta.get("single") or "").strip().upper()
            hook_txt = (meta.get("hook") or "").strip()
            # 라벨/문장형 훅("Lisbon in 60s")은 썸네일 문구로 부적합 → 도시명 대문자
            punch = ("" if composer.is_legacy_hook_label(hook_txt) else hook_txt.upper()) or city_txt
            if not punch:
                # 도시명도 없으면 제목 앞 4단어 대문자
                punch = " ".join((meta.get("title_en") or "").split()[:4]).upper()
            tp = os.path.join(work_dir, "thumbnail.jpg")
            if src and punch and generate_thumbnail(src, tp, text=punch, frame_time=1.0):
                thumbnail_path = tp
        except Exception as e:
            log.warning(f"썸네일 생성 실패(영상은 정상 진행): {e}")
        log.info(f"[timing] thumbnail={time.monotonic() - _t:.1f}")

    # 5) 7개국 localizations — 대기열 대본에 한국어 메타가 있으면 번역 대신 그 값을 쓴다.
    locs = _meta_localizations(meta)
    if locs is None:
        if _cfg.no_openai_mode():
            log.warning("NO_OPENAI 인데 대본 localizations 없음 → 번역 없이 발행")
            locs = {}
        else:
            locs = translator.translate_for_youtube(meta["title_en"], meta["description_en"])
    else:
        log.info(f"localizations 를 대본 값으로 사용(번역 호출 없음): {','.join(locs.keys())}")
    ko_title = (meta.get("title_ko") or "").strip()
    ko_desc = (meta.get("description_ko") or "").strip()
    if ko_title or ko_desc:
        ko = dict(locs.get("ko") or {})
        if ko_title:
            ko["title"] = ko_title[:95]
        if ko_desc:
            ko["description"] = ko_desc[:4900]
        if ko.get("title"):  # 제목 없는 localization 은 YouTube 가 거부한다
            ko.setdefault("description", meta.get("description_en", ""))
            locs["ko"] = ko
            log.info("locs['ko'] 를 대본의 한국어 메타로 덮어씀 (번역 무시)")

    if dry_run:
        log.info("--dry-run → 업로드 스킵 (대기열 pending 소비 안 함)")
        # 검증→발행 분리: 이 mp4를 나중에 '그대로' 업로드할 수 있게 meta 저장.
        # --upload 재렌더는 대본/클립이 매번 랜덤이라 검증본과 달라짐(꽃 재발 사고 방지).
        try:
            import json as _json
            _pub = {
                "animal_key": animal_key, "video": out_video,
                # video 와 동일하게 절대경로 저장 — publish-prepared 가 존재 확인 후 전달(fail-soft)
                "thumbnail": thumbnail_path,
                "title_en": meta["title_en"], "description_en": meta["description_en"],
                "tags": meta.get("tags", []), "locs": locs,
                "comment": meta.get("comment"), "default_language": "en",
                # 발행 시 사용 이력 기록 재료(2026-07-20 — publish-prepared 미기록 봉합):
                # 이게 없으면 발행돼도 used_cities/used_pexels/content_history/used_bgm 에
                # 안 남아 다음 cron 이 같은 도시·클립·BGM 을 재발행할 수 있다.
                "city": meta.get("single", ""), "country": meta.get("country", ""),
                "angle": meta.get("angle", ""), "format": meta.get("format", ""),
                "facts": meta.get("facts", []),
                "clip_ids": _collect_used_clip_ids(bg_clip_ids, intro_clip),
                "bgm_file_id": bgm_file_id,
                # 대기열 대본이면 그 pending 경로 — publish-prepared 가 업로드 성공 후 done 으로
                # 옮긴다(그 전까지 pending 유지 = 이 실행은 대기열을 소비하지 않는다).
                "queue_path": queue_path,
            }
            # CC 자막 SRT 를 재료(tts_lines·번역)가 살아있는 지금 미리 생성 —
            # publish-prepared 는 업로드만 한다(발행본 CC 미등록 봉합).
            try:
                import time as _t2
                from pipeline import captions as _cap
                try:
                    from moviepy.editor import VideoFileClip as _VFC2
                    with _VFC2(out_video) as _vc2:
                        _adur = float(_vc2.duration)
                except Exception:
                    _adur = composer.final_video_duration()
                _pub["cc_srts"] = _cap.generate_all(
                    tts_lines, work_dir, total_dur=_adur, deadline=_t2.monotonic() + 180.0)
                log.info(f"CC SRT 사전 생성: {len(_pub['cc_srts'])}개 언어 (publish-prepared 용)")
            except Exception as _ce:
                _pub["cc_srts"] = {}
                log.warning(f"CC SRT 사전 생성 실패 (publish-prepared 발행 시 자막 없음): {_ce}")
            _pp = os.path.join(work_dir, "publish_meta.json")
            with open(_pp, "w", encoding="utf-8") as _f:
                _json.dump(_pub, _f, ensure_ascii=False, indent=2)
            log.info(f"publish_meta 저장(검증→발행 분리용): {_pp}")
        except Exception as _e:
            log.warning(f"publish_meta 저장 실패: {_e}")
        return {
            "animal": animal_key,
            "video": out_video,
            "url": None,
            "locales": list(locs.keys()),
            "thumbnail": thumbnail_path,
        }

    # 6) 업로드 (동물별 맞춤 댓글 자동 포함)
    url = None
    if not no_upload:
        from pipeline import youtube_upload
        _t = time.monotonic()
        url = youtube_upload.upload(
            out_video,
            title=meta["title_en"],
            description=meta["description_en"],
            tags=meta["tags"],
            thumbnail_path=thumbnail_path,
            localizations=locs,
            default_language="en",
            custom_comment=meta.get("comment"),
        )
        log.info(f"[timing] upload={time.monotonic() - _t:.1f}")

    # 6-1) 대기열 소비 — **실제 업로드가 videoId 를 받은 뒤에만** done 으로 옮긴다.
    #      --dry-run/--no-upload 는 여기까지 오지 않거나 url 이 None 이라 pending 이 그대로 남는다.
    queue_consumed = _consume_queue_if_uploaded(queue_path, url)

    # 업로드를 요청했는데 URL 이 없으면 '발행됨'으로 기록하지 않는다(codex 지적).
    # 이력만 남고 영상은 없는 상태가 되면 그 도시·대본이 조용히 소모된다.
    if not no_upload and not url:
        raise RuntimeError("업로드가 URL(videoId)을 반환하지 않음 — 발행 실패로 처리(이력 미기록)")

    # 7) 사용 기록 — 실제로 발행된 경우에만. (2026-07-17 fix: --no-upload 테스트 런이
    #    used_cities/used_pexels/content_history를 '발행됨'으로 오염시키던 문제)
    #    도입샷 id 포함·중복 제거 — publish-prepared 기록과 동일 계산(_collect_used_clip_ids)
    used_clip_ids = _collect_used_clip_ids(bg_clip_ids, intro_clip)
    if queue_path and not no_upload and not queue_consumed:
        # 발행은 됐는데 대기열 정리가 실패 — 다음 cron 이 같은 대본을 다시 집을 수 있다.
        # (--no-upload 는 애초에 소비하지 않는 게 정상이라 여기 해당 없음)
        log.error("[queue] 발행 후 대기열 정리 실패 — 다음 실행 전 queue/pending 을 손으로 확인하세요")
    if no_upload:
        log.info("--no-upload → 사용 기록(used_cities/used_pexels/content_history) 저장 스킵 (상태 오염 방지)")
    else:
        used.append(animal_key)
        _save_used(used)

        # 7-1) Pexels 풋티지 사용 기록 (다음 영상부터 같은 풋티지 안 뜨도록)
        new_pexels_ids = [int(c) for c in used_clip_ids if str(c).isdigit()]
        if new_pexels_ids:
            used_pexels.extend(new_pexels_ids)
            _save_used_pexels(used_pexels)
            log.info(f"used_pexels.json 갱신: +{new_pexels_ids} (전체 {len(used_pexels)}개 추적)")

        # 7-2) content_history.json 기록 (시간 기반 재등장 차단 + 클립 dedup + angle/format 로테이션)
        try:
            entry = hist_mod.build_entry(
                animal_key,
                angle=meta.get("angle", ""),
                fmt=meta.get("format", ""),
                facts=meta.get("facts", []),
                title=meta.get("title_en", ""),
                clip_ids=used_clip_ids,
            )
            hist_mod.add_entry(hist, animal_key, entry)
            hist_mod.save_history(CONTENT_HISTORY_PATH, hist)
            log.info(f"content_history.json 기록: {animal_key} (angle={entry['angle']}, format={entry['format']}, clips={len(used_clip_ids)})")
        except Exception as e:
            log.warning(f"content_history 기록 실패: {e}")

        # 7-3) BGM 사용 기록 — 풀 1회전 전 중복 방지 (발행 성공 시에만, 실패해도 발행은 유효)
        if bgm_file_id:
            try:
                bgm_mod.mark_bgm_used(bgm_file_id)
                log.info(f"used_bgm.json 갱신: +{bgm_file_id}")
            except Exception as e:
                log.warning(f"used_bgm 기록 실패 (영상은 정상 발행됨): {e}")

    # 8) CC 자막 트랙 (en + 7개국) — 기록(used/history) 저장을 먼저 끝낸 뒤 수행.
    #    **fail-soft**: 자막 실패가 영상 발행/기록을 실패시키지 않음. CC 전체 예산 180s.
    cap_ok = {}
    if url and not no_upload:
        _t_cc = time.monotonic()
        try:
            import time as _time
            from pipeline import captions
            cc_deadline = _time.monotonic() + 180.0  # CC 단계 전체 예산(초)
            video_id = url.rstrip("/").split("/")[-1].split("?")[0]
            # 적응형 길이(composer v4)라 실제 렌더 길이를 실측해 자막 총길이로 사용
            try:
                from moviepy.editor import VideoFileClip as _VFC
                with _VFC(out_video) as _vc:
                    _actual_dur = float(_vc.duration)
            except Exception:
                _actual_dur = composer.final_video_duration()
            line_tr = meta.get("captions") if isinstance(meta.get("captions"), dict) else None
            if line_tr or _cfg.no_openai_mode():
                # 대본이 준 라인별 번역으로 CC 생성 — OpenAI 무호출(없는 언어는 en 만)
                srts = captions.generate_from_lines(
                    tts_lines, work_dir, line_tr or {}, total_dur=_actual_dur)
            else:
                srts = captions.generate_all(
                    tts_lines, work_dir,
                    total_dur=_actual_dur, deadline=cc_deadline,
                )
            cap_ok = captions.upload_captions(video_id, srts, deadline=cc_deadline)
            n_ok = sum(1 for v in cap_ok.values() if v)
            log.info(f"CC 자막: {n_ok}/{captions.EXPECTED_CC_LANGS} 언어 등록")
        except Exception as e:
            log.warning(f"CC 자막 전체 실패 (영상 정상 발행됨): {e}")
        log.info(f"[timing] captions={time.monotonic() - _t_cc:.1f}")

    return {
        "animal": animal_key,
        "video": out_video,
        "url": url,
        "locales": list(locs.keys()),
    }


def _publish_prepared(meta_json_path: str):
    """dry-run이 저장한 publish_meta.json을 읽어 '검증된 그 mp4'를 그대로 업로드한다.
    --upload(=기본) 재렌더는 매번 대본/클립이 랜덤이라(꽃 재발 등) 검증본과 달라지므로,
    dry-run으로 눈으로 확인한 영상을 재렌더 없이 발행하기 위한 경로(쿠마님 2026-07-18)."""
    import json
    from pipeline import youtube_upload
    with open(meta_json_path, encoding="utf-8") as f:
        pub = json.load(f)
    video = pub["video"]
    if not os.path.exists(video):
        raise FileNotFoundError(f"발행할 영상 없음: {video}")
    log.info(f"검증본 발행 시작: {os.path.basename(video)}")
    # 썸네일 — dry-run 이 만들어 둔 파일이 있으면 그대로 전달 (없으면 썸네일 없이, fail-soft)
    thumbnail = pub.get("thumbnail")
    if thumbnail and not os.path.exists(thumbnail):
        log.warning(f"publish_meta 썸네일 파일 없음 → 썸네일 없이 발행: {thumbnail}")
        thumbnail = None
    url = youtube_upload.upload(
        video,
        title=pub["title_en"],
        description=pub["description_en"],
        tags=pub.get("tags", []),
        thumbnail_path=thumbnail,
        localizations=pub.get("locs", {}),
        default_language=pub.get("default_language", "en"),
        custom_comment=pub.get("comment"),
    )
    log.info(f"검증본 발행 완료: {url}")

    # 대기열 대본이었다면 업로드 성공(videoId 확인) 후에만 pending → done.
    # 이게 없으면 dry-run 으로 검증→발행한 대본이 pending 에 남아 다음 cron 이 중복 발행한다.
    _qp = pub.get("queue_path")
    if _qp and os.path.exists(_qp):
        _consume_queue_if_uploaded(_qp, url)
    elif _qp:
        log.warning(f"publish_meta 의 대기열 파일이 이미 없음(다른 실행이 소비?): {_qp}")

    # 사용 이력 기록 — run_once 7단계와 동일(2026-07-20 봉합). 기록이 빠지면 다음 cron 이
    # 같은 도시/클립/BGM 을 다시 뽑아 중복 재발행된다. 구버전 meta(기록 필드 없음)는 경고.
    city_key = pub.get("animal_key") or ""
    clip_ids = [c for c in (pub.get("clip_ids") or []) if c]
    legacy_meta = not clip_ids and not pub.get("bgm_file_id")
    if legacy_meta:
        log.warning("publish_meta 에 기록 필드(clip_ids/bgm_file_id) 없음 — 구버전 meta. "
                    "도시 기록만 남기고 클립/BGM/컨텐츠 이력은 스킵")
    # 기록 4종은 각각 독립 try — 하나가 실패해도 나머지 기록은 진행(codex P2 반영).
    try:
        if city_key:
            # 2026-07-23: 재방문(이미 목록에 있는 도시)도 append 한다 — run_once 경로와 동일.
            # 기록을 건너뛰면 '직전 발행 등급'이 갱신되지 않아 fame 순환이 제자리걸음 한다.
            used = _load_used()
            used.append(city_key)
            _save_used(used)
            log.info(f"used_cities.json 갱신: +{city_key}")
    except Exception as e:
        log.warning(f"used_cities 기록 실패 (영상은 정상 발행됨): {e}")
    try:
        new_pexels_ids = [int(str(c)) for c in clip_ids if str(c).isdigit()]
        if new_pexels_ids:
            up = _load_used_pexels()
            added = []
            for i in new_pexels_ids:  # meta 내 중복까지 방어(한 번만 기록)
                if i not in up:
                    up.append(i)
                    added.append(i)
            if added:
                _save_used_pexels(up)
            log.info(f"used_pexels.json 갱신: +{added}")
    except Exception as e:
        log.warning(f"used_pexels 기록 실패 (영상은 정상 발행됨): {e}")
    try:
        # 구버전 meta 는 빈 angle/clip_ids 엔트리를 남기지 않는다(부정확한 이력 방지)
        if city_key and not legacy_meta:
            from pipeline import history as hist_mod
            hist = hist_mod.load_history(CONTENT_HISTORY_PATH)
            entry = hist_mod.build_entry(
                city_key,
                angle=pub.get("angle", ""), fmt=pub.get("format", ""),
                facts=pub.get("facts", []), title=pub.get("title_en", ""),
                clip_ids=[str(c) for c in clip_ids],
            )
            hist_mod.add_entry(hist, city_key, entry)
            hist_mod.save_history(CONTENT_HISTORY_PATH, hist)
            log.info(f"content_history.json 기록: {city_key}")
    except Exception as e:
        log.warning(f"content_history 기록 실패 (영상은 정상 발행됨): {e}")
    try:
        if pub.get("bgm_file_id"):
            from pipeline import bgm as bgm_mod
            bgm_mod.mark_bgm_used(pub["bgm_file_id"])
            log.info(f"used_bgm.json 갱신: +{pub['bgm_file_id']}")
    except Exception as e:
        log.warning(f"used_bgm 기록 실패 (영상은 정상 발행됨): {e}")

    # CC 자막 — dry-run 이 사전 생성해 둔 SRT 업로드(fail-soft, 발행본 CC 미등록 봉합)
    try:
        srts = {k: v for k, v in (pub.get("cc_srts") or {}).items() if v and os.path.exists(v)}
        if srts:
            import time as _time
            from pipeline import captions
            video_id = url.rstrip("/").split("/")[-1].split("?")[0]
            cap_ok = captions.upload_captions(video_id, srts, deadline=_time.monotonic() + 180.0)
            n_ok = sum(1 for v in cap_ok.values() if v)
            log.info(f"CC 자막: {n_ok}/{len(srts)} 언어 등록")
        else:
            log.warning("publish_meta 에 cc_srts 없음/파일 소실 — CC 자막 스킵(구버전 meta)")
    except Exception as e:
        log.warning(f"CC 자막 실패 (영상 정상 발행됨): {e}")

    print({"video": video, "url": url})
    return url


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--animal", "--city", dest="animal",
                   help="강제 도시 키 (예: kyoto_japan)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--publish-prepared", dest="publish_prepared", default=None,
                   help="dry-run이 저장한 publish_meta.json으로 검증된 mp4를 그대로 업로드")
    args = p.parse_args()

    if args.publish_prepared:
        try:
            _publish_prepared(args.publish_prepared)
        except Exception as e:
            log.exception(f"검증본 발행 실패: {e}")
            sys.exit(1)
        return

    try:
        result = run_once(
            forced_animal=args.animal,
            dry_run=args.dry_run,
            no_upload=args.no_upload,
        )
        if result.get("queue_empty"):
            # 대기열이 비어 정상 종료(실패 아님). 마커는 run_once 가 이미 stdout 에 찍었다.
            sys.exit(0)
        print(result)
    except Exception as e:
        log.exception(f"실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
