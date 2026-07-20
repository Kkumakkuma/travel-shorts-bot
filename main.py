#!/usr/bin/env python3
"""Wander 60s — 1분 여행 도시 소개 영상 자동 발행 (우주봇 fork).

흐름:
1. 최근 사용 회피하면서 로스터(cities.json)에서 도시 1곳 랜덤 선택
2. GPT가 앵글 + hook + intro + 하이라이트 4~5개 + outro + 영문 title/description 생성
3. edge-tts로 영문 내레이션 생성 (intro/하이라이트/outro)
4. Pexels에서 도시 배경(영상 → 사진 Ken Burns → AI 이미지) 확보 + vision(verify_city) 검증
5. MoviePy로 1분 영상 합성 (배경 concat + 비네트 + hook 상단 + 자막 시간차 + brand 하단)
6. 영문 메인 + 7개국 localizations 번역
7. YouTube Shorts 업로드 (privacy=public, defaultLanguage=en)
8. 사용 도시 기록 (used_cities.json) — 같은 cron에서 연속 회피
"""
import argparse
import io
import json
import logging
import os
import random
import sys
from datetime import datetime

if sys.platform == "win32":
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


def _save_used(used):
    json.dump(used[-USED_KEEP:], open(USED_PATH, "w", encoding="utf-8"))


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

    clip id 태깅(L): NASA는 "nasa:<id>", Pexels는 숫자 문자열, AI는 "".
    반환: (paths, clip_ids). paths는 슬롯 순서(=fact 순서), clip_ids 병렬.
    """
    import time as _time
    from pipeline import pexels, verify_visual

    single = meta.get("single", "")      # 도시명
    display = meta.get("display", "")     # 도시명
    category = meta.get("category", "")
    country = meta.get("country", "")
    landmark_lock = meta.get("landmark_lock", "")
    confusion_pair = meta.get("confusion_pair") or []
    angle = (meta.get("angle") or "").strip().lower()
    facts = meta.get("facts") or []
    n = max(1, len(facts))  # 배경 수 = 하이라이트 수

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

    slots_path = [None] * n
    slots_id = [None] * n
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

    def _assign(i, path, cid):
        slots_path[i] = path
        slots_id[i] = cid
        if cid:
            used_clip_set.add(cid)

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

        # 1) NASA 영상 (우주봇 전용; 여행봇은 nasa None → skip)
        if nasa is not None:
            p, nid = nasa.fetch_video(kw, work_dir, exclude_ids=_used_nasa())
            if p:
                if _verify(p, strict=slot_strict, subject=kw):
                    _assign(i, p, f"nasa:{nid}")
                    continue
                rejected.append(p)

        # 2) Pexels 영상 (slot 키워드 순서, hard/soft exclude 분리, 동음이의 도시 country 필터)
        try:
            cands = pexels.fetch_backgrounds(
                slot_kw, work_dir, n=oversample + 1,
                animal_single=single, country=country, ordered=True,
                exclude_pexels_ids=soft_pexels + _used_pexels(),
                hard_exclude_ids=hard_pexels + _used_pexels(),
                confusion_pair=confusion_pair,
            )
        except RuntimeError:
            cands = []
        assigned = False
        for cand in cands:
            cid = _extract_clip_id(cand)
            if cid and cid in used_clip_set:
                rejected.append(cand)
                continue
            if _verify(cand, strict=slot_strict, subject=kw):
                _assign(i, cand, cid or "")
                assigned = True
                break
            rejected.append(cand)
        # 이 슬롯에서 못 쓴 나머지 후보는 정리 대상
        for other in cands:
            if other is not slots_path[i] and other not in rejected:
                rejected.append(other)
        if assigned:
            continue

        # 3) NASA 이미지 → Ken Burns (파일명 nasa_img_* — 영상과 분리, H)
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
                    animal_single=single, country=country,
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
        # slot 미충족 → None 유지

    # === AI 이미지 폴백 (K: 반드시 vision 검증) — 미충족 슬롯 채움 ===
    unfilled = [i for i in range(n) if slots_path[i] is None]
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

    # === 결과 정리 — 부분통과여도 fact-배경 정렬 보존 (2026-07-20 fix) ===
    # 기존: 미충족 슬롯 drop 후 압축 → composer 가 fact 순서 1:1 매핑이라 통과 클립이
    # 다른 fact 자막 밑에 깔림(정렬 붕괴). 수정: 미충족 슬롯을 인접 통과 클립으로 채워
    # 길이 n 을 유지(같은 클립 반복은 composer 자체 폴백과 동일한 수용 가능한 절충).
    filled_slots = [i for i in range(n) if slots_path[i]]
    if filled_slots and len(filled_slots) < n:
        for i in range(n):
            if slots_path[i] is None:
                donor = min(filled_slots, key=lambda j: (abs(j - i), j))
                slots_path[i] = slots_path[donor]
                slots_id[i] = slots_id[donor]
        log.info(f"미충족 슬롯 {n - len(filled_slots)}개를 인접 통과 클립으로 채움 (fact-배경 정렬 보존)")
    filled_paths = [slots_path[i] for i in range(n) if slots_path[i]]
    filled_ids = [slots_id[i] for i in range(n) if slots_path[i]]
    accepted = set(filled_paths)
    for r in rejected:
        if r in accepted:  # H: 채택된 경로는 삭제 skip
            continue
        try:
            os.remove(r)
        except Exception:
            pass

    min_pass = min(n, 3)  # garbage 0: 최소 '고유' 통과 배경 수(패딩 제외 — 기존 바 유지)
    if len(filled_slots) >= min_pass:
        return filled_paths, filled_ids

    for p in set(filled_paths):
        try:
            os.remove(p)
        except Exception:
            pass
    raise RuntimeError(
        f"비전 검증 통과 배경 부족 ({len(filled_slots)}/{n}, min={min_pass}) — 다른 토픽 픽 필요"
    )


def run_once(forced_animal: str = None, dry_run: bool = False, no_upload: bool = False) -> dict:
    from pipeline import animals, pexels, tts, composer, translator
    from pipeline import history as hist_mod

    # 2026-07-17: 필수 API 키 fail-fast — 시크릿 누락/오타 상태로 vision 0회 검증 발행이
    # 조용히 성공하는 것을 차단(verify_visual도 무키 reject하지만 여기서 시작 자체를 막아
    # GPT/Pexels 호출 낭비 없이 명확한 에러로 실패시킨다).
    _missing = [k for k in ("OPENAI_API_KEY", "PEXELS_API_KEY")
                if not (getattr(_cfg, k, "") or "").strip()]
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

    bg_clip_ids = []

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
        bg_paths, bg_clip_ids = fetch_verified_backgrounds(
            meta, work_dir, exclude_pexels_ids=used_pexels, exclude_clip_ids=recent_clips)
    else:
        # GPT 동적 픽 + Pexels 매칭 + 비전 검증 재시도 (최대 5번)
        meta = None
        bg_paths = None
        retry_used = list(dynamic_exclude_list)  # (D) 완화 없는 전체 exclude
        for attempt in range(5):
            try:  # (G) pick 자체 예외도 다음 시도로
                try_meta = animals.pick_random_animal(
                    exclude_keys=retry_used, history=hist, avoid_formats=avoid_formats,
                    avoid_angles=avoid_angles)
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
            bg_paths = bgs
            bg_clip_ids = cids
            break

        if meta is None or bg_paths is None:
            # 5번 재시도 실패 → legacy 사전 fallback (모두 strict mode).
            # 2026-05-06 fix (우주봇 Saturn 케이크 사고 동기화): 이전엔 min_pass=1 SOFT mode +
            # 마지막엔 검증 없이 강제 발행이 있어서 garbage 영상이 그대로 발행되는 사고가 있었음.
            # 이제 strict only — 5개 fallback도 모두 vision 통과 못하면 워크플로 실패시킴.
            # (F) 정적 풀 fallback은 expand_legacy_to_rich를 먼저 → fact_keywords 확정 → 배경 fetch.
            log.warning("5번 재시도 실패 → legacy 사전 fallback 시작 (strict only)")
            legacy_tries = sorted(fallback_exclude)  # (D) 완화 적용된 exclude
            for legacy_attempt in range(5):
                legacy_key = animals.pick_animal(exclude=legacy_tries)
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
                bg_paths = bgs
                bg_clip_ids = cids
                break

            if meta is None or bg_paths is None:
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
    tts_texts = [meta["intro"]] + list(meta["facts"]) + [meta["outro"]]
    tts_lines = tts.synth_lines(tts_texts, work_dir, prefix="tts")

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
            _excl = [int(x) for x in (bg_clip_ids or []) if str(x).isdigit()]
            _kws = [f"{_s} {_c} cityscape".strip(), f"{_s} skyline aerial",
                    f"{_s} {_c} city view".strip(), f"{_s} old town streets", f"{_s} downtown"]
            _cands = _px.fetch_backgrounds(_kws, work_dir, n=4, animal_single=_s, country=_c,
                                           ordered=True, exclude_pexels_ids=_excl, hard_exclude_ids=_excl,
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
    except Exception as e:
        log.warning(f"도입샷 처리 실패 → 도입샷 없이 진행: {e}")

    composer.compose(bg_paths, tts_lines, meta["hook"], out_video, bgm_path=bgm_path,
                     title=meta.get("title_en"), country=meta.get("country"),
                     intro_clip=intro_clip, city=meta.get("single"))
    log.info(f"video: {out_video} ({os.path.getsize(out_video) / 1024 / 1024:.1f}MB)")

    # 5) 7개국 localizations
    locs = translator.translate_for_youtube(meta["title_en"], meta["description_en"])

    if dry_run:
        log.info("--dry-run → 업로드 스킵")
        # 검증→발행 분리: 이 mp4를 나중에 '그대로' 업로드할 수 있게 meta 저장.
        # --upload 재렌더는 대본/클립이 매번 랜덤이라 검증본과 달라짐(꽃 재발 사고 방지).
        try:
            import json as _json
            _pub = {
                "animal_key": animal_key, "video": out_video,
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
        }

    # 6) 업로드 (동물별 맞춤 댓글 자동 포함)
    url = None
    if not no_upload:
        from pipeline import youtube_upload
        url = youtube_upload.upload(
            out_video,
            title=meta["title_en"],
            description=meta["description_en"],
            tags=meta["tags"],
            localizations=locs,
            default_language="en",
            custom_comment=meta.get("comment"),
        )

    # 7) 사용 기록 — 실제로 발행된 경우에만. (2026-07-17 fix: --no-upload 테스트 런이
    #    used_cities/used_pexels/content_history를 '발행됨'으로 오염시키던 문제)
    #    도입샷 id 포함·중복 제거 — publish-prepared 기록과 동일 계산(_collect_used_clip_ids)
    used_clip_ids = _collect_used_clip_ids(bg_clip_ids, intro_clip)
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
            srts = captions.generate_all(
                tts_lines, work_dir,
                total_dur=_actual_dur, deadline=cc_deadline,
            )
            cap_ok = captions.upload_captions(video_id, srts, deadline=cc_deadline)
            n_ok = sum(1 for v in cap_ok.values() if v)
            log.info(f"CC 자막: {n_ok}/{captions.EXPECTED_CC_LANGS} 언어 등록")
        except Exception as e:
            log.warning(f"CC 자막 전체 실패 (영상 정상 발행됨): {e}")

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
    url = youtube_upload.upload(
        video,
        title=pub["title_en"],
        description=pub["description_en"],
        tags=pub.get("tags", []),
        localizations=pub.get("locs", {}),
        default_language=pub.get("default_language", "en"),
        custom_comment=pub.get("comment"),
    )
    log.info(f"검증본 발행 완료: {url}")

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
            used = _load_used()
            if city_key not in used:
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
        print(result)
    except Exception as e:
        log.exception(f"실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
