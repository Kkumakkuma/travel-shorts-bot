"""콘텐츠 이력(content_history.json) — 시간 기반 중복 방지 + 클립 dedup + 로테이션.

동물봇/우주봇 공통 모듈 (동일 코드). 도메인 텍스트 분기 없음 — 순수 로직만.

목적:
- 같은 topic이 창(window) 내에 다시 발행되지 않게 시간 기반으로 차단 (동물 60일 / 우주 45일).
  기존 used_animals.json의 개수 기반 KEEP과 병행 — 더 엄격한 쪽(합집합) 적용.
- 제외 후 후보가 min_candidates 미만이면 창을 0.75배씩 완화(최소 min_window일).
  한 번에 전체 리셋하지 않는다.
- 클립(Pexels video id / NASA nasa_id)을 기록해 30일 내 재사용 차단.
- angle / format 로테이션: 같은 topic 직전 angle, 전역 직전 N편 format 조회.
- content_history.json 없으면 used_animals.json에서 초기 이력 마이그레이션.

데이터 구조 (content_history.json):
{
  "<topic_key>": [
    {"date":"YYYY-MM-DD", "angle": str, "format": str,
     "facts": [<fact 요약 1줄> x4], "title": str, "clip_ids": [str, ...]},
    ...
  ],
  ...
}
파일 크기는 걱정 수준이 아니라 무제한 누적한다.
"""
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta

log = logging.getLogger("shortsbot.history")

CORRUPT_SUFFIX = ".corrupt"

DATE_FMT = "%Y-%m-%d"

# 겹침 휴리스틱용 불용어 (내용 없는 흔한 단어)
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "that", "this", "these", "those", "than", "then", "into", "from",
    "have", "has", "had", "are", "is", "was", "were", "be", "been", "being",
    "its", "their", "they", "them", "it", "as", "by", "can", "could", "would",
    "will", "not", "no", "so", "up", "out", "one", "two", "about", "over",
    "more", "most", "some", "any", "each", "which", "who", "what", "when",
    "where", "how", "why", "you", "your", "our", "we", "his", "her", "she",
    "he", "if", "just", "also", "even", "very", "much", "many", "like", "make",
    "makes", "made", "them", "there", "here", "own", "per", "only",
}


def _today() -> datetime:
    return datetime.now()


def normalize_key(topic_key: str) -> str:
    """topic_key 정규화 (소문자 + snake_case)."""
    return (topic_key or "").strip().lower().replace(" ", "_")


def load_history(path: str) -> dict:
    """content_history.json 로드. 없으면 {}.

    손상(깨진 JSON / dict 아님)이면 **빈 이력으로 덮지 않고** 원본을 `<path>.corrupt`로
    백업한 뒤 {} 반환. 호출 측(main.py)은 .corrupt 백업이 있으면 used_animals.json에서
    재마이그레이션하지 않는다(이력 전량 소실 방지).
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
        raise ValueError("content_history 최상위가 dict 아님")
    except Exception as e:
        try:
            os.replace(path, path + CORRUPT_SUFFIX)
            log.warning(f"content_history.json 손상 → {path}{CORRUPT_SUFFIX} 백업, 빈 이력 반환: {e}")
        except Exception as e2:
            log.warning(f"content_history.json 손상(백업 실패 {e2}): {e}")
        return {}


def save_history(path: str, hist: dict):
    """content_history.json 원자 저장 (temp write → fsync → os.replace)."""
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".hist_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)  # 원자적 교체
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"content_history.json 저장 실패: {e}")


def _parse_date(s: str):
    try:
        return datetime.strptime(str(s)[:10], DATE_FMT)
    except Exception:
        return None


def summarize_fact(fact: str, max_words: int = 16) -> str:
    """fact를 이력 저장/주입용 1줄 요약으로 (앞 max_words 단어)."""
    words = (fact or "").split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + "…"


def add_entry(hist: dict, topic_key: str, entry: dict) -> dict:
    """이력에 발행 1건 추가. hist를 in-place 갱신하고 반환."""
    k = normalize_key(topic_key)
    hist.setdefault(k, []).append(entry)
    return hist


def build_entry(topic_key: str, angle: str, fmt: str, facts: list,
                title: str, clip_ids: list, date: str = None) -> dict:
    """발행 이력 엔트리 생성 (facts는 자동 요약)."""
    return {
        "date": date or _today().strftime(DATE_FMT),
        "angle": (angle or "").strip(),
        "format": (fmt or "").strip(),
        "facts": [summarize_fact(f) for f in (facts or [])],
        "title": (title or "").strip(),
        "clip_ids": [str(c) for c in (clip_ids or []) if c is not None],
    }


def topic_entries(hist: dict, topic_key: str) -> list:
    """특정 topic의 과거 발행 엔트리 리스트 (없으면 빈 리스트)."""
    return hist.get(normalize_key(topic_key), [])


def recent_topics(hist: dict, window_days: float, now: datetime = None) -> set:
    """window_days 내에 발행된 topic_key 집합."""
    now = now or _today()
    cutoff = now - timedelta(days=float(window_days))
    out = set()
    for k, entries in (hist or {}).items():
        for e in entries or []:
            d = _parse_date(e.get("date", ""))
            if d and d >= cutoff:
                out.add(k)
                break
    return out


def recent_clip_ids(hist: dict, window_days: float, now: datetime = None) -> set:
    """window_days 내에 사용된 클립 id 집합 (문자열)."""
    now = now or _today()
    cutoff = now - timedelta(days=float(window_days))
    out = set()
    for entries in (hist or {}).values():
        for e in entries or []:
            d = _parse_date(e.get("date", ""))
            if d and d >= cutoff:
                for c in e.get("clip_ids", []) or []:
                    out.add(str(c))
    return out


def recent_facts(hist: dict, window_days: float, limit: int = 40,
                 now: datetime = None) -> list:
    """window_days 내에 **전 토픽에서** 사용된 fact 요약 리스트 (최신 우선, 최대 limit개).

    기존 _differentiate_content는 같은 topic의 과거 facts만 봤다. 그래서 topic이 달라도
    같은 사실을 반복하는 것(예: 목성 편과 토성 편이 둘 다 '고리 소멸')을 못 막았다.
    """
    now = now or _today()
    cutoff = now - timedelta(days=float(window_days))
    dated = []
    for entries in (hist or {}).values():
        for e in entries or []:
            d = _parse_date(e.get("date", ""))
            if d and d >= cutoff:
                for f in e.get("facts", []) or []:
                    if f:
                        dated.append((d, f))
    dated.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in dated[:limit]]


def recent_titles(hist: dict, count: int = 12, now: datetime = None) -> list:
    """전역 직전 count편의 제목 (최신 우선). 제목 템플릿 반복 차단용."""
    entries = _sorted_entries(hist)
    titles = [e.get("title", "") for e in entries if e.get("title")]
    return list(reversed(titles[-count:])) if count > 0 else []


def recent_titled_topics(hist: dict, count: int = 12) -> list:
    """전역 직전 count편의 (title, topic_key) 쌍 (최신 우선).

    지문을 계산할 때 그 제목의 주제어를 빼야 '대상만 갈아끼운' 템플릿이 같은 지문이 된다.
    """
    rows = []
    for topic, entries in (hist or {}).items():
        for e in entries or []:
            d = _parse_date(e.get("date", ""))
            if d and e.get("title"):
                rows.append((d, e["title"], topic))
    rows.sort(key=lambda x: x[0], reverse=True)
    return [(t, k) for _, t, k in rows[:count]]


_TEMPLATE_STOP = {"a", "an", "the"}


def _stem(w: str) -> str:
    """아주 가벼운 복수→단수 어간화. fox/foxes, comet/comets, city/cities를 같은 어간으로."""
    w = (w or "").lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es"):
        return w[:-2]
    if len(w) > 2 and w.endswith("s"):
        return w[:-1]
    return w


def _subject_stems(*subjects) -> set:
    """주제어 어간 집합 (topic_key / display / single 에서 추출)."""
    out = set()
    for s in subjects:
        for w in _WORD_RE.findall(str(s or "").replace("_", " ")):
            out.add(_stem(w))
    return out


def title_template_sig(title: str, n: int = 3, subjects=()) -> str:
    """제목의 템플릿 지문 = 주제어를 제거한 뒤 앞 n개 의미 단어(어간, 소문자).

    "You Won't Believe What Foxes Do at Night" / "...What Comets Do at Night" 처럼
    대상만 갈아끼운 양산 제목을 같은 지문으로 묶어 잡아낸다. 유튜브 2025-07
    inauthentic content 정책(템플릿 양산)이 실제 수익화 리스크라 코드로 차단한다.

    주제어를 빼는 이유(codex 지적, 2026-07-10): 앞 3단어만 보면
    "What Foxes Actually Do" 와 "What Comets Actually Do" 의 지문이 달라져 미탐이 난다.
    어간으로 비교해 fox/foxes 같은 불규칙 복수까지 주제어로 제거한다.
    """
    subj = _subject_stems(*subjects) if subjects else set()
    sig = []
    for w in _WORD_RE.findall(title or ""):
        st = _stem(w)
        if st in _TEMPLATE_STOP or st in subj:
            continue
        sig.append(st)
    return " ".join(sig[:n])


def title_template_repeats(title: str, prev_titles: list, n: int = 3,
                           subjects=(), prev_subjects=None) -> bool:
    """title의 템플릿 지문이 최근 제목 중 하나와 같으면 True.

    prev_titles가 (title, topic_key) 튜플 리스트면 각 제목의 주제어를 자동으로 뺀다.
    문자열 리스트면 prev_subjects(같은 길이)로 주제어를 넘길 수 있다.
    """
    s = title_template_sig(title, n, subjects)
    if not s:
        return False
    for i, t in enumerate(prev_titles or []):
        if isinstance(t, (tuple, list)):
            pt, psub = t[0], (t[1],)
        else:
            pt = t
            psub = (prev_subjects[i],) if prev_subjects and i < len(prev_subjects) else ()
        if title_template_sig(pt, n, psub) == s:
            return True
    return False


def facts_repeat_globally(new_facts: list, prev_facts: list,
                          threshold: float = 0.45, shared_min: int = 4) -> list:
    """새 facts 중 과거 전역 facts와 겹치는 인덱스 목록.

    프롬프트로 "반복하지 마라"만 넣으면 모델이 무시할 때 막을 방법이 없다(codex 지적).
    최종 facts를 코드로 대조해 겹치는 것을 잡아낸다.
    """
    hits = []
    for i, nf in enumerate(new_facts or []):
        if facts_too_similar([nf], prev_facts, threshold=threshold, shared_min=shared_min):
            hits.append(i)
    return hits


def _sorted_entries(hist: dict) -> list:
    """모든 엔트리를 날짜 오름차순으로 (date 파싱 가능한 것만)."""
    all_e = []
    for entries in (hist or {}).values():
        for e in entries or []:
            d = _parse_date(e.get("date", ""))
            if d:
                all_e.append((d, e))
    all_e.sort(key=lambda x: x[0])
    return [e for _, e in all_e]


def recent_formats(hist: dict, count: int) -> list:
    """전역 직전 count편의 format 리스트 (오래된→최신 순)."""
    entries = _sorted_entries(hist)
    fmts = [e.get("format", "") for e in entries if e.get("format")]
    return fmts[-count:] if count > 0 else []


def compute_exclude(hist: dict, used: list, keep: int, window_days: float,
                    pool: list = None, min_candidates: int = 8,
                    min_window: float = 7.0, now: datetime = None):
    """제외할 topic 집합 계산 (시간창 + 개수 기반 합집합) + 완화.

    - count_blocked: used 리스트 마지막 keep개 (개수 기반, 기존 로직)
    - time_blocked: window_days 내 발행 topic (시간 기반, 신규)
    - 합집합이 exclude. pool(유한 후보 참조)이 주어지면 pool - exclude < min_candidates일 때
      window_days를 0.75배씩 줄여(min_window까지) 완화. 한 번에 전체 리셋하지 않는다.

    반환: (exclude_set, effective_window_days)
    """
    now = now or _today()
    count_blocked = {normalize_key(k) for k in (used or [])[-keep:] if k}
    pool_norm = {normalize_key(p) for p in (pool or [])}
    min_window = max(0.001, float(min_window))  # denormal 무한루프 방지 클램프
    w = float(window_days)
    union = count_blocked | recent_topics(hist, w, now)
    # 반복 상한(64) — min_window=0 등 극단값에서도 무한루프 불가
    for _ in range(64):
        time_blocked = recent_topics(hist, w, now)
        union = count_blocked | time_blocked
        if not pool_norm:
            return union, w
        free = [p for p in pool_norm if p not in union]
        if len(free) >= min_candidates or w <= min_window:
            return union, w
        w = max(min_window, w * 0.75)
    return union, w


def migrate_from_used(used: list, window_days: float, now: datetime = None) -> dict:
    """used_animals.json 리스트 → 초기 content_history.

    날짜 미상이므로 '오늘 - 창간격 절반'으로 기록 (재등장 창 계산 시 절반 정도 소진된 것으로 취급).
    """
    now = now or _today()
    d = (now - timedelta(days=float(window_days) / 2.0)).strftime(DATE_FMT)
    hist = {}
    for k in (used or []):
        nk = normalize_key(k)
        if not nk:
            continue
        hist.setdefault(nk, []).append({
            "date": d, "angle": "", "format": "",
            "facts": [], "title": "", "clip_ids": [],
        })
    return hist


# === 재등장 시 내용 겹침 검사 (핵심 키워드 휴리스틱) ===

_NUM_RE = re.compile(r"\d[\d,\.]*")
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]+")


def significant_tokens(text: str) -> set:
    """내용 키워드 토큰 집합: 숫자(전부) + 길이 4+ 비불용어 단어(소문자)."""
    text = text or ""
    nums = {n.replace(",", "") for n in _NUM_RE.findall(text)}
    words = {w.lower() for w in _WORD_RE.findall(text)
             if len(w) >= 4 and w.lower() not in _STOPWORDS}
    return nums | words


def fact_overlap_ratio(new_fact: str, prev_fact: str) -> float:
    """두 fact의 유의미 토큰 Jaccard 유사도 (0~1)."""
    a = significant_tokens(new_fact)
    b = significant_tokens(prev_fact)
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


def facts_too_similar(new_facts: list, prev_facts: list,
                      threshold: float = 0.45, shared_min: int = 4) -> bool:
    """새 facts가 과거 facts와 지나치게 겹치는지.

    True 조건 (둘 중 하나라도):
    - 어떤 new_fact가 어떤 prev_fact와 Jaccard >= threshold, 또는
    - 유의미 토큰 shared_min개 이상 공유(숫자 포함 시 강한 신호).
    """
    for nf in (new_facts or []):
        na = significant_tokens(nf)
        for pf in (prev_facts or []):
            pb = significant_tokens(pf)
            if not na or not pb:
                continue
            inter = na & pb
            union = na | pb
            jac = len(inter) / len(union) if union else 0.0
            if jac >= threshold or len(inter) >= shared_min:
                return True
    return False
