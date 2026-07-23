"""GPT가 여행 도시 + 앵글 + Pexels 키워드 + 영문 hook/intro/하이라이트/outro 일괄 생성.

ANIMALS 사전(=CITIES 도시 사전)은 fallback용 (legacy). 기본은 GPT 동적 픽(로스터 내).

Note: 변수명/함수명은 animals/animal_key/pick_random_animal 그대로 유지 (main.py 호환).
의미상 animal_key=city_key, single/display=city, facts=하이라이트 1-liner.
콘텐츠 모델: 한 편 = 한 도시 60초 소개. GPT가 앵글 1개를 정하고 하이라이트 4~5개(각 8~20단어)를
생성. 하이라이트 타입 믹스 가능(랜드마크/음식/역사/명소). 사실성 기계장치(grounding/factcheck/
consistency/novelty)는 하이라이트 문장에 그대로 작동(랜드마크·역사·특산물 검증, 변동정보 정성적).
"""
import json
import logging
import random
import re

from config import ANIMALS, META_MODEL, OPENAI_API_KEY

try:  # 모델 3분할 (2026-07-10). 구버전 config 호환을 위해 폴백.
    from config import SCRIPT_MODEL, FACTCHECK_MODEL
except ImportError:  # pragma: no cover
    SCRIPT_MODEL = FACTCHECK_MODEL = META_MODEL

log = logging.getLogger("travelbot.topics")

# === Angle 로테이션 (여행 도메인) — 같은 도시 재등장 시/전역 다양성 확보 ===
# 도시 소개형 앵글: 한 편은 하나의 앵글을 중심으로 하이라이트 4~5개를 구성.
ANGLES = [
    "iconic_landmarks", "best_things_to_do", "food_and_specialty",
    "history_story", "hidden_gem",
]

# 유효 angle이 아니면(GPT near-miss) 코드로 정규화 — verify_city 아이콘 슬롯 strict 판정이
# angle=="iconic_landmarks" 정확 일치에 의존하므로 매핑 실패는 strict 미작동/이력 오염을 낳는다.
# 알 수 없는 값은 landmark가 아닌(=strict 미유발) 안전한 "best_things_to_do"로 폴백한다.
_ANGLE_ALIASES = [
    (("iconic_landmark", "landmark", "sight", "monument", "must_see", "must-see"), "iconic_landmarks"),
    (("food", "cuisine", "eat", "dish", "specialt", "drink"), "food_and_specialty"),
    (("history", "histor", "story", "past", "heritage"), "history_story"),
    (("hidden", "gem", "underrated", "secret", "offbeat", "local"), "hidden_gem"),
    (("thing", "do", "activit", "experience", "attraction"), "best_things_to_do"),
]


def _normalize_angle(angle) -> str:
    """angle 문자열을 유효 ANGLES 값으로 정규화. 매핑 불가 시 'best_things_to_do'(strict 미유발)."""
    a = str(angle or "").strip().lower().replace(" ", "_")
    if a in ANGLES:
        return a
    for needles, canon in _ANGLE_ALIASES:
        if any(nd in a for nd in needles):
            return canon
    return "best_things_to_do"


def _fame(c: dict) -> int:
    """도시 엔트리의 유명도 등급(1=세계 최상위 … 4=니치). 누락/이상값은 3."""
    try:
        v = int((c or {}).get("fame", 3))
    except (TypeError, ValueError):
        return 3
    return v if 1 <= v <= 4 else 3


def pick_animal(rng: random.Random = None, exclude: list = None,
                ever_used: list = None) -> str:
    """legacy fallback (사전에서 픽). pick_random_animal() 우선.

    2026-05-07 fix: exclude가 ANIMALS dict 전체를 덮으면 pool이 빈 리스트가 되어
    random.choice([]) IndexError 발생 → 빈 pool일 때 exclude 무시하고 전체에서 픽.
    2026-07-23: 이 fallback 경로도 GPT 경로와 같은 '미발행 우선 + fame 등급 순환'을 적용
    (여기만 안 걸면 GPT 실패 시 순환이 깨지고 무명 도시가 새어 나감).
    """
    rng = rng or random.Random()
    pool = [k for k in ANIMALS.keys() if not exclude or k not in exclude]
    if not pool:
        pool = list(ANIMALS.keys())
    ever = {(k or "").lower().strip() for k in (ever_used or [])}
    fresh = [k for k in pool if k.lower() not in ever]
    pool = fresh or pool
    fames = {_fame(ANIMALS.get(k)) for k in pool}
    if fames:
        target = _next_fame_tier(fames, ever_used)
        pool = [k for k in pool if _fame(ANIMALS.get(k)) == target]
    return rng.choice(pool)


# 컴필레이션 단어 차단 (단일 도시 영상이므로). " list"는 "bucket list" 오탐이라 제외.
_BAD_TITLE_EN = ["compilation", "best of", " mix"]
# 여행 카테고리(대륙/여행테마) — BGM 라우팅 키와 일치. cities.json category 필드와 동일.
_VALID_CATEGORIES = {
    "europe_city", "asia_culture", "beach_tropical", "desert_middleeast",
    "africa_safari", "latin_america", "north_america", "oceania",
    "nordic_nature", "historic_ancient",
}

# 다른 유명 도시 단어 — comment에서 본 도시 외 다른 도시가 등장하면 off-topic 의심(TOPIC LOCK).
# 소프트 heuristic (경고만, GPT가 최종 판단). 흔한 영단어와 겹치는 도시명(nice 등)은 오탐 회피 위해 제외.
# 국가명은 넣지 않음 (본 도시가 자기 나라를 언급하는 건 정상).
_OTHER_TOPIC_WORDS = {
    "tokyo", "kyoto", "osaka", "paris", "london", "rome", "venice", "florence",
    "barcelona", "madrid", "lisbon", "porto", "amsterdam", "prague", "vienna",
    "berlin", "munich", "budapest", "istanbul", "athens", "cairo", "marrakech",
    "dubai", "bangkok", "hanoi", "singapore", "seoul", "sydney", "melbourne",
    "havana", "cusco", "cartagena", "reykjavik", "edinburgh", "dublin", "krakow",
    "dubrovnik", "santorini", "kathmandu", "petra", "queenstown",
}


def _has_bad(text: str, words) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


# 진부 양산 opener — 과거 이력에 없어도(신규 채널/prev_titles 누락) 무조건 차단.
# title_template_repeats는 '과거 반복'만 보므로 첫 발생·이력 장애 시 이 opener가 샌다(codex 지적 2026-07-11).
_BANNED_OPENERS = ("you won't believe", "you wont believe", "wait until you see",
                   "wait til you see", "did you know", "you'll never guess",
                   "youll never guess", "you won't guess")


def _starts_with_banned_opener(title) -> bool:
    t = (title or "").strip().lstrip("\"'“”‘’ ")
    t = t.replace("’", "'").replace("‘", "'").lower()  # 스마트 아포스트로피 → ASCII 정규화
    return any(t.startswith(b) for b in _BANNED_OPENERS)


def _title_needs_fix(title, prev_titles, subjects) -> bool:
    """제목이 (1) 금지 opener로 시작하거나 (2) 과거 템플릿 지문과 겹치면 True.
    (1)은 prev_titles 유무와 무관 — deterministic 최종 방어."""
    if _starts_with_banned_opener(title):
        return True
    if prev_titles:
        from pipeline import history as _hist
        if _hist.title_template_repeats(title, prev_titles, subjects=subjects):
            return True
    return False


# === 2026-07-05 업그레이드 공통 로직 (동물봇/우주봇 동일 코드) ===
FORMATS = ["highlight_reel", "local_guide", "first_timer", "hidden_side"]

# 하이라이트·intro(내레이션)에서 금지하는 클리셰/과장. title 훅 과장은 허용.
BANNED_PHRASES = [
    "more interesting than people think", "way more interesting", "more than people think",
    "will blow your mind", "blow your mind", "mind-blowing", "mind blowing",
    "you won't believe your eyes", "number will shock you", "you won't believe how",
    "paradise on earth", "best kept secret", "must-see before you die",
    "will take your breath away", "like nowhere else on earth", "hidden paradise",
    # 브로슈어 상투구(2026-07-18 gpt-5.6-sol codex): 프롬프트뿐 아니라 결정적 차단으로 격리
    "rich historical tapestry", "historical tapestry", "tapestry of", "boasts",
    "steeped in history", "nestled", "hidden gem", "melting pot",
    "will steal your heart", "a feast for the senses",
]


def _has_banned(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in BANNED_PHRASES)


# 변동정보(시점의존 수치) 결정적 백스톱 (2026-07-17) — 프롬프트/GPT 팩트체커가 놓쳐도
# 코드가 최종 차단한다. 가격·영업시간·환율·'현재 ~중' 류는 촬영 시점에만 참이라
# 고정 사실로 발행 금지 (fail-closed: 걸리면 안전 하이라이트로 교체).
_VOLATILE_PATTERNS = [
    r"[$€£₩¥]\s?\d",                                                # $25, €10 …
    r"\d+\s?(?:dollars|euros|pounds|won|yen|pesos|baht|dirhams|rupees)\b",
    r"\b\d{1,2}\s?(?:am|pm)\b",                                     # 9am, 10 pm
    r"\b\d{1,2}:\d{2}\s?(?:am|pm)?\b",                              # 9:30, 21:00
    r"\bopen(?:s|ing)?\s(?:daily|from|at|until)\b",
    r"\b(?:entry|entrance|admission)\s?(?:fee|ticket|price)\b",
    r"\bticket(?:s)?\s?(?:cost|price|fee)s?\b",
    r"\bexchange rate\b",
    r"\bcurrently\b",
    r"\bper\s(?:night|person|day)\b",
]
_VOLATILE_RE = re.compile("|".join(_VOLATILE_PATTERNS), re.IGNORECASE)


def _has_volatile(text: str) -> bool:
    return bool(_VOLATILE_RE.search(text or ""))


def _sweep_deterministic(display: str, facts: list, flagged=()) -> list:
    """금지문구/변동정보/최종 false 하이라이트를 안전 템플릿으로 교체하는 결정적 최종 방어."""
    out = list(facts)
    for i in range(len(out)):
        if i in flagged or _has_banned(out[i]) or _has_volatile(out[i]):
            log.warning(f"factcheck: fact{i+1} false/금지문구/변동정보 → 안전 fact 교체: {out[i][:60]!r}")
            out[i] = _safe_fact(display, i)
    return out


def _count_numbers(texts) -> int:
    return sum(len(re.findall(r"\d[\d,\.]*", t or "")) for t in texts)


def _validate_fact_keywords(raw, single: str, generic: list, n: int = 4) -> list:
    """per-fact 배경 키워드 n개 정규화 — 각 키워드에 topic명(single) 포함 보장, 부족분 generic 폴백."""
    single_l = (single or "").lower().strip()
    gk = [g for g in (generic or []) if isinstance(g, str) and g.strip()] or [single or "nature"]
    raw = raw if isinstance(raw, list) else []
    out = []
    for i in range(n):
        kw = raw[i].strip() if i < len(raw) and isinstance(raw[i], str) and raw[i].strip() else ""
        if not kw:
            kw = gk[i % len(gk)]
        if single_l and single_l not in kw.lower():
            kw = f"{single} {kw}".strip()
        out.append(kw[:80])
    return out


def _safe_fact(display: str, idx: int) -> str:
    """팩트체크 최종 실패 시 교체할 안전(보수적·참) 하이라이트 템플릿 (20~30단어, 내레이션 분량 유지)."""
    templates = [
        f"{display} rewards slow wandering — skip the checklist for an afternoon and let its quiet streets, small squares, and everyday corners show you its real character.",
        f"Start your day early in {display}: soft morning light, calmer streets, and the first fresh food of the day make it feel like a different city.",
        f"Half the joy of {display} is getting a little lost — the alleys between the famous sights are where you'll find the moments you actually remember.",
        f"{display} keeps its best moments just off the main square — walk ten extra minutes past the crowds and the city quietly opens up for you.",
    ]
    return templates[idx % len(templates)]


def adversarial_factcheck(display, single, hook, facts, source_text, max_rounds=2):
    """적대적 팩트체크 패스 (생성 → 팩트체크 단계). temp 0 회의적 페르소나로 hook+facts 판정.

    각 항목을 supported / plausible / false_or_exaggerated로 판정하고 SOURCE와 대조.
    false_or_exaggerated 항목만 SOURCE 근거로 교정 재생성(최대 max_rounds회) 후,
    최종 판정 1회로 '그래도 false로 남는' fact만 안전 fact로 교체 → 과잉 교체 방지 + 허위 자막 차단.

    2026-07-10 강화 — 판정 기준을 SOURCE 유무로 분기한다.
    - SOURCE 있음: **SOURCE에 없는 구체 수치·최상급·순위는 false로 판정**한다.
      (기존 "애매하면 plausible"이 환각 수치의 통과 경로였다. 토성 고리 소멸 100M vs 300M년
       모순이 이 구멍으로 나갔다.) false여도 교정 라운드에서 SOURCE 값으로 고쳐 쓰므로
      발행이 막히지 않는다. 끝까지 남은 것만 안전 fact로 교체.
    - SOURCE 없음: 근거가 없으니 과잉 탈락을 피해 기존 보수 모드 유지(확신할 때만 false).
      단 SOURCE 없는 토픽은 애초에 pick 단계에서 재픽되므로 드문 경로다.

    검증 모델은 FACTCHECK_MODEL — 생성(SCRIPT_MODEL)과 다른 계열이라 자기합치 편향이 준다.
    반환: (hook, facts). OPENAI 키 없으면 원본 그대로.
    """
    if not OPENAI_API_KEY:
        return hook, facts
    facts = [str(f).strip() for f in (facts or []) if str(f).strip()]
    if not facts:
        return hook, facts
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    src = (source_text or "").strip()
    src_block = src if src else (
        "(No source text available — rely ONLY on well-established, uncontroversial "
        "travel facts about this city and be extra conservative.)"
    )
    # 판정 기준: SOURCE가 있으면 엄격(근거 없는 수치 = false), 없으면 보수(확신할 때만 false).
    if src:
        rubric = """- "supported": the claim — INCLUDING every number (founding year, height, length, date) — is directly verifiable in SOURCE.
- "plausible": a QUALITATIVE travel claim (no specific number, no superlative, no ranking) that is uncontroversial and is not contradicted by SOURCE (e.g. "famous for its old town", "known for seafood").
- "false_or_exaggerated": mark whenever ANY of these hold:
  (a) the claim contradicts SOURCE (wrong landmark, wrong country/region, wrong historical event);
  (b) it states a SPECIFIC NUMBER, year, height, distance, or date that does NOT appear in SOURCE and that you cannot verify — an unverifiable precise number is the single most common way a channel publishes false claims, so treat it as false, NOT plausible;
  (c) it asserts a superlative or ranking ("the oldest", "the only", "the biggest in the world") that SOURCE does not state;
  (d) it states VOLATILE info as a hard fact — exact ticket prices, opening hours, exchange rates, current population figures — treat any precise volatile figure not in SOURCE as false (a qualitative version like "cheap street food" / "go early" is fine);
  (e) it uses empty hype ("will blow your mind", "paradise on earth", "more interesting than people think").
A believable-sounding number you cannot find in SOURCE is FALSE, not plausible. Do not give it the benefit of the doubt."""
    else:
        rubric = """- "supported": uncontroversial, well-known travel fact about this city.
- "plausible": reasonable mainstream travel claim. USE THIS (not false) whenever a qualitative claim is believable — you have no SOURCE to check against, so do not over-reject.
- "false_or_exaggerated": mark ONLY when you are CONFIDENT the claim is wrong — it names a landmark/dish/event that does not belong to this city, cites a clearly wrong year or figure, states a disputed superlative as settled fact, gives a precise price/hours as if fixed, or uses empty hype. When in doubt here, choose "plausible"."""

    def _judge(cur_hook, cur_facts):
        """GPT 1회: 각 항목 판정 + false 항목 교정본 반환. (verdicts, hook, facts)."""
        item_lines = [f"hook: {cur_hook}"] + [f"highlight{i+1}: {f}" for i, f in enumerate(cur_facts)]
        verdict_keys = '"hook": "...", ' + ", ".join(f'"fact{i+1}": "..."' for i in range(len(cur_facts)))
        facts_slot = ", ".join(['"<highlight 20-30 words>"'] * len(cur_facts))
        prompt = f"""You are a SKEPTICAL travel fact-checker for a 60-second YouTube Shorts about the city {display} ({single}).
Viewers actively fact-check and leave angry comments when a claim is wrong, so catch every FALSE or EXAGGERATED statement before it airs.

SOURCE TEXT (primary reference, from Wikipedia):
{src_block}

Judge EACH item below as exactly one of:
{rubric}

For EVERY item judged "false_or_exaggerated", REWRITE it to be accurate and supported while staying engaging:
- Replace any unverifiable number/year with a value you can point to in SOURCE. If SOURCE has no number for that claim, drop the number and make a true qualitative statement instead.
- For VOLATILE info (ticket prices, opening hours, exchange rates, current population): never state a precise figure as fixed — use a qualitative version ("cheap street food", "go early to beat the crowds").
- Do NOT invent facts you are unsure of; a true qualitative highlight beats a false precise one. Keep the landmark/dish/event correct for THIS city.
- Each highlight MUST stay 20-30 words. The hook MUST be 12 words or fewer, punchy, and NEVER start with "Did you know".
- Keep every "supported"/"plausible" item EXACTLY as written.

ITEMS:
{chr(10).join(item_lines)}

Output STRICT JSON:
{{"verdicts": {{{verdict_keys}}}, "hook": "<corrected or unchanged>", "facts": [{facts_slot}]}}"""
        resp = client.chat.completions.create(
            model=FACTCHECK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1200,
        )
        d = json.loads(resp.choices[0].message.content)
        verdicts = d.get("verdicts") or {}
        nh = (d.get("hook") or "").strip() or cur_hook
        nf = d.get("facts") or []
        if isinstance(nf, list) and len(nf) == len(cur_facts):
            nf = [str(x).strip() or cur_facts[i] for i, x in enumerate(nf)]
        else:
            nf = list(cur_facts)
        return verdicts, nh, nf

    cur_hook = (hook or "").strip()
    cur_facts = list(facts)
    final_flagged = []
    # 교정 라운드 max_rounds회 → 마지막(rnd==max_rounds)은 판정 전용(교정 미반영, 잔존 false 확정)
    for rnd in range(max_rounds + 1):
        try:
            verdicts, nh, nf = _judge(cur_hook, cur_facts)
        except Exception as e:
            log.warning(f"adversarial factcheck 호출 실패 (round {rnd+1}) — 현재 버전 유지: {e}")
            # GPT 판정이 불가해도 결정적 백스톱(금지문구/변동정보)은 적용하고 반환
            return cur_hook, _sweep_deterministic(display, cur_facts)
        flagged = [i for i in range(len(cur_facts))
                   if str(verdicts.get(f"fact{i+1}", "")).lower().startswith("false")]
        hook_flagged = str(verdicts.get("hook", "")).lower().startswith("false")
        log.info(f"factcheck round {rnd+1}: hook_false={hook_flagged} facts_false={flagged or 'none'}")
        if not flagged and not hook_flagged:
            final_flagged = []
            break
        if rnd >= max_rounds:
            # 판정 전용 라운드: 교정 반영 안 하고 잔존 false만 확정 (hook 교정만 반영)
            final_flagged = flagged
            if hook_flagged:
                cur_hook = nh
            break
        # 교정 라운드: 교정본 반영 후 다음 라운드 재판정
        cur_hook, cur_facts = nh, nf
    # 잔존 false + 금지문구 + 변동정보 fact는 안전 fact로 교체 (허위/시점의존 자막 차단)
    cur_facts = _sweep_deterministic(display, cur_facts, final_flagged)
    if _has_volatile(cur_hook) or _has_banned(cur_hook):
        log.warning(f"factcheck: hook에 변동정보/클리셰 → 안전 hook 교체: {cur_hook[:60]!r}")
        cur_hook = f"Here's why {display} belongs on your travel list."
    return cur_hook, cur_facts


# 도메인 맥락 (동음이의 위키 grounding 방지 — config.CONTENT_DOMAIN로 분기)
# 여행은 country를 추가로 붙여(fetch_source_for) 동명 도시(Paris France vs Texas) 오매칭을 줄인다.
_DOMAIN_CONTEXT = {
    "space": "space astronomy planet moon star galaxy cosmos nebula NASA spacecraft universe orbit solar",
    "animal": "animal wildlife species mammal bird fish reptile amphibian insect nature",
    "travel": "city travel tourism tourist destination landmark town",
}


def _content_domain() -> str:
    try:
        from config import CONTENT_DOMAIN
        return (CONTENT_DOMAIN or "").lower().strip()
    except Exception:
        return ""


def _keywords_for_facts(display, single, facts):
    """최종 facts에 맞춘 per-fact 배경 검색 키워드 재생성 (각 topic명 포함). 실패 시 None."""
    if not OPENAI_API_KEY or not facts:
        return None
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    n = len(facts)
    joined = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
    prompt = f"""For each fact below about {display} ({single}), give ONE short stock-video search keyword (2-4 words) describing the best background visual for THAT fact. EVERY keyword MUST contain "{single}". Each keyword should point to a FILMABLE OUTDOOR visual (a landmark / street / district / castle / garden / temple / market / riverside / viewpoint), NOT an abstract concept or indoor exhibit; for a food fact use a market or street-food scene. Keep the SAME order.

{joined}

Output STRICT JSON: {{"fact_keywords": [{', '.join(['"..."'] * n)}]}}"""
    try:
        resp = client.chat.completions.create(
            model=META_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=300,
        )
        kws = json.loads(resp.choices[0].message.content).get("fact_keywords")
        if isinstance(kws, list) and len(kws) >= n:
            return _validate_fact_keywords(kws, single, [], n=n)
    except Exception as e:
        log.warning(f"fact_keywords 재생성 실패 — 기존 유지: {e}")
    return None


def _finalize_fact_keywords(meta: dict, generic: list = None) -> dict:
    """(F) 모든 fact 변경(differentiate/factcheck/consistency) 이후 마지막에 fact_keywords 확정.

    meta['_kw_facts'](키워드 도출 당시 facts 스냅샷)와 현재 facts가 다르면 재도출.
    재도출 실패 시 최소 topic명 포함만 보장. 스냅샷 키는 pop.
    """
    facts = meta.get("facts") or []
    snap = meta.pop("_kw_facts", None)
    single = meta.get("single", "")
    display = meta.get("display", "")
    if snap is not None and list(snap) == list(facts):
        return meta  # 변화 없음 → 기존 키워드 유지
    new_kw = _keywords_for_facts(display, single, facts)
    if new_kw:
        meta["fact_keywords"] = new_kw
        log.info("fact_keywords 확정 (최종 facts 기준 재도출)")
    else:
        meta["fact_keywords"] = _validate_fact_keywords(
            meta.get("fact_keywords"), single, generic or [], n=(len(facts) or 4))
    return meta


def _strip_hook_deterministic(hook, max_words=12):
    """API 없이 결정적으로 hook 정리: 'Did you know (that)' 시작 제거 + 첫 글자 대문자 + 단어수 컷."""
    h = (hook or "").strip()
    h = re.sub(r"^\s*did you know\s*(that\s+)?", "", h, flags=re.I).strip()
    h = re.sub(r"^\s*(wait until you see|watch this|this is why)\b[,:]?\s*", "", h, flags=re.I).strip()
    if h:
        h = h[0].upper() + h[1:]
    words = h.split()
    if len(words) > max_words:
        words = words[:max_words]
    out = " ".join(words)
    return out or (hook or "").strip()


def _enforce_hook_length(display, single, hook, max_words=12):
    """(5) hook(=intro 나레이션) 코드 강제: ①≤max_words ②'Did you know' 시작 금지 — **분리 검사**.
    문제 있으면 GPT rewrite 시도 → 실패/무키 시 결정적 제거(_strip_hook_deterministic)."""
    h = (hook or "").strip()
    too_long = len(h.split()) > max_words
    banned_start = h.lower().startswith("did you know")
    if not too_long and not banned_start:
        return h
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            prompt = (f"Rewrite this opening hook for a 60-second Shorts about {display} to "
                      f"{max_words} words or FEWER. Keep the key number or shocking comparison. "
                      f"It MUST NOT start with \"Did you know\" (or \"Wait until you see\"/\"Watch this\"). "
                      f"Return ONLY the hook line, no quotes.\n\nHook: {h}")
            resp = client.chat.completions.create(
                model=META_MODEL, messages=[{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=40)
            out = (resp.choices[0].message.content or "").strip().strip('"').splitlines()[0].strip()
            if out and len(out.split()) <= max_words and not out.lower().startswith("did you know"):
                log.info(f"hook 교정: len={len(h.split())}→{len(out.split())}, banned_start={banned_start}")
                return out
        except Exception as e:
            log.warning(f"hook GPT 교정 실패 → 결정적 제거: {e}")
    return _strip_hook_deterministic(h, max_words)


def _retitle_unique(display: str, single: str, bad_title: str, prev_titles: list,
                    tries: int = 3) -> str:
    """제목이 최근 템플릿의 복제본일 때 구조가 다른 제목으로 재생성.

    prev_titles는 (title, topic_key) 튜플 리스트다 — 지문 계산 시 각 제목의 주제어를 뺀다.
    GPT가 새 제목도 같은 템플릿을 쓰면 최대 tries회 재시도하고, 끝내 실패하면
    결정적(deterministic) 대체 제목을 만든다 — 절대 양산 제목을 그대로 두지 않는다.
    """
    from pipeline import history as _hist
    subj = (single, display)
    prev_str = [(t[0] if isinstance(t, (tuple, list)) else t) for t in prev_titles]
    # 후보 검사용: 기존 prev + (bad_title, 이번 주제) 를 튜플로
    check_prev = list(prev_titles) + [(bad_title, single)]
    if OPENAI_API_KEY:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        banned = "\n".join("- " + t for t in prev_str[:10])
        for _ in range(tries):
            try:
                r = client.chat.completions.create(
                    model=SCRIPT_MODEL,
                    messages=[{"role": "user", "content": f"""Write ONE YouTube Shorts TRAVEL title for a 60-second city intro about {display} ({single}).

It must NOT reuse the sentence structure of any of these recent titles (only the city was swapped in them — that is exactly what to avoid):
{banned}
Also rejected for reusing a template: "{bad_title}"

Rules: max 70 characters, include "{display}", make it inviting with a curiosity gap or clear travel promise, no emoji, do not start with "You Won't Believe" / "Wait Until You See" / "Did You Know". Use a sentence structure that none of the titles above use.

Output STRICT JSON: {{"title": "..."}}"""}],
                    response_format={"type": "json_object"},
                    temperature=1.0,
                    max_tokens=120,
                )
                t = str(json.loads(r.choices[0].message.content).get("title", "")).strip()[:95]
                if t and not _starts_with_banned_opener(t) and not _hist.title_template_repeats(t, check_prev, subjects=subj):
                    return t
            except Exception as e:
                log.warning(f"제목 재생성 실패: {e}")
                break
    # 결정적 폴백 — 여행 제목 구조 4종. 최근 지문과 안 겹치는 첫 번째 채택.
    for cand in (f"{display} in 60 Seconds",
                 f"Why {display} Should Be Your Next Trip",
                 f"The One Thing You Can't Miss in {display}",
                 f"First Time in {display}? Start Here"):
        if not _starts_with_banned_opener(cand) and not _hist.title_template_repeats(cand, check_prev, subjects=subj):
            return cand[:95]
    return f"{display} in 60 Seconds"[:95]


# 위키 본문이 쓰는 국가명 변형 — must_include 게이트가 정식 명칭만 요구하면
# 'Edinburgh(Scotland)'류 요약을 false-reject하므로 별칭까지 허용한다.
_COUNTRY_ALIASES = {
    "united states": ["united states", "u.s.", "usa"],
    "united kingdom": ["united kingdom", "uk", "england", "scotland", "wales", "northern ireland"],
    "czech republic": ["czech republic", "czechia"],
    "turkey": ["turkey", "türkiye", "turkiye"],
    "south korea": ["south korea", "korea"],
    "united arab emirates": ["united arab emirates", "uae"],
}


def fetch_source_for(display: str, single: str, category: str = "", country: str = "") -> str:
    """토픽(도시)의 위키 SOURCE 확보. 실패 시 "" (호출 측이 재픽 판단).

    country: 여행 도메인의 동명 도시(Tripoli 레바논 vs 리비아) 방어 —
    (1) must_include 필수 토큰으로 강제(제네릭 'city' 키워드는 게이트가 못 됨),
    (2) 검색 쿼리도 코드 슬러그 category('europe_city' 등, 위키 검색어로 무의미) 대신
        country를 사용("Tripoli Lebanon")."""
    context = _DOMAIN_CONTEXT.get(_content_domain(), "")
    must = []
    if country:
        context = (context + " " + country).strip()
        must = _COUNTRY_ALIASES.get(country.strip().lower(), [country.strip()])
    try:
        from pipeline import grounding
        return grounding.fetch_source(display, single, context=context,
                                      category=(country or category), must_include=must)
    except Exception as e:
        log.warning(f"grounding fetch 예외: {e}")
        return ""


def reground_facts(meta: dict, source_text: str) -> dict:
    """SOURCE를 근거로 facts/hook을 다시 쓴다 (생성 → 검증 사이의 새 단계).

    기존 파이프라인은 GPT가 근거 없이 '충격적인 숫자'를 먼저 창작하고 나중에 팩트체크로
    걸러내는 구조라, 팩트체커가 놓치면 환각 수치가 그대로 발행됐다. 여기서 SOURCE를 앞에
    두고 "SOURCE에서 인용 가능한 사실만" 쓰게 해 창작 여지를 없앤다.

    SOURCE가 없거나 호출 실패면 meta 원본 유지(발행 계속) — 뒤의 팩트체크가 여전히 방어한다.
    """
    src = (source_text or "").strip()
    if not OPENAI_API_KEY or not src:
        return meta
    facts = [f for f in (meta.get("facts") or []) if str(f).strip()]
    if not facts:
        return meta
    display, single = meta.get("display", ""), meta.get("single", "")
    n = len(facts)
    draft = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
    prompt = f"""You are writing the script for a 60-second YouTube Shorts introducing the city {display} ({single}).

SOURCE TEXT (Wikipedia — the ONLY place you may take facts and numbers from):
{src}

Below is a DRAFT of city highlights written WITHOUT the source. It may contain invented facts.
DRAFT:
{draft}

Rewrite it into {n} final travel HIGHLIGHTS (each = one punchy destination one-liner) obeying ALL of these:
- Every highlight must be supported by SOURCE. **Every number, year, height, distance and date MUST appear in SOURCE.** If the draft has a fact/number not in SOURCE, replace it with a correct one from SOURCE or state the true qualitative highlight instead. Keep every landmark, dish, or historical event correct for THIS city.
- Do NOT invent superlatives ("the oldest", "the only", "the biggest in the world") unless SOURCE states them in those words.
- For VOLATILE info (ticket prices, opening hours, exchange rates, current population) never give a precise fixed figure — use a qualitative version ("cheap street food", "go early").
- Keep the highlights genuinely appealing: pick the most memorable landmarks / food / activities / history SOURCE actually mentions. Prefer a true vivid highlight over a fabricated shocking one.
- MIX the highlight types — do NOT make all {n} dry history facts. If SOURCE mentions any food/dish/market, include at least ONE food / local-specialty highlight; always include at least ONE present-tense sensory experience (what a visitor sees, tastes, hears, or the best time to go). NEVER invent food or detail not in SOURCE. Lead each highlight with what the traveler EXPERIENCES, not an administrative/technical detail — e.g. never sell a famous garden as an "emergency water reservoir"; sell its seasonal scenery instead. Vary the opening word; do NOT start every highlight with the same verb (Discover/Explore/Visit/Wander/Experience).
- FILMABLE: each highlight must center on a scene with likely travel stock video of {single} — outdoor landmarks, streets, districts, the castle, gardens, temples, markets, riverside, or viewpoints. Avoid museum interiors, indoor exhibits, abstract concepts, or an isolated single dish. Only if SOURCE actually mentions a market, street food, or dining district may you frame food that way; otherwise pick another SOURCE-backed outdoor subject. Never invent a market or scene not in SOURCE.
- **Each highlight MUST be 20-30 words. Count the words before you answer.** Warm, first/second-person travel tone (e.g. "Fushimi Inari's thousands of vermilion gates climb the mountainside — go at sunrise and you'll have whole stretches of the winding trail almost to yourself.").
- All numeric/historical claims across the {n} highlights MUST agree with each other and with SOURCE.
- No hype phrases ("will blow your mind", "paradise on earth", "more interesting than people think") and NO brochure clichés. BANNED words/phrases: "rich historical tapestry", "historical tapestry", "tapestry of", "boasts", "steeped in history", "nestled", "hidden gem", "melting pot", "will steal your heart", "must-see", "a feast for the senses".
- hook: 12 words or fewer, a concrete SOURCE-backed detail about the city (a specific number, food, or signature fact — e.g. gold-leaf output, a famous dish, a record), never starts with "Did you know", and never uses any BANNED cliché above (especially "rich historical tapestry").

Output STRICT JSON: {{"hook": "...", "facts": [{', '.join(['"highlight (20-30 words)"'] * n)}]}}"""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        def _call(extra: str = ""):
            r = client.chat.completions.create(
                model=SCRIPT_MODEL,
                messages=[{"role": "user", "content": prompt + extra}],
                response_format={"type": "json_object"},
                temperature=0.4,  # 근거 재작성이라 창작보다 충실도 우선
                max_tokens=1200,
            )
            d = json.loads(r.choices[0].message.content)
            return d, [str(f).strip() for f in (d.get("facts") or []) if str(f).strip()]

        d, nf = _call()
        # 길이 보정: 하이라이트는 20~30단어(60초 내레이션 채움). 짧으면(< 14단어) SOURCE 범위 안에서 1회 보강.
        if len(nf) == n and any(len(f.split()) < 20 for f in nf):
            short = [i + 1 for i, f in enumerate(nf) if len(f.split()) < 20]
            log.info(f"reground: highlight {short} 단어수 미달 → SOURCE 범위 내 보강 재요청")
            try:
                d2, nf2 = _call(
                    "\n\nYour previous answer had highlights shorter than 20 words: "
                    f"{', '.join('highlight' + str(i) for i in short)}. "
                    "Rewrite ALL highlights so each is 20-30 words. Add the length with SENSORY / EXPERIENTIAL "
                    "detail found in SOURCE (what a visitor sees, tastes, hears, or the best time to go) — NOT dry "
                    "encyclopedic padding, and never the BANNED brochure clichés.")
                if len(nf2) == n and sum(len(f.split()) for f in nf2) > sum(len(f.split()) for f in nf):
                    d, nf = d2, nf2
            except Exception as e:
                log.warning(f"reground 확장 재요청 실패 — 1차 결과 사용: {e}")
        if len(nf) == n:
            meta["facts"] = nf
            if d.get("hook"):
                meta["intro"] = str(d["hook"]).strip()
            log.info(f"reground: SOURCE 기반으로 {n} facts 재작성 완료 "
                     f"(단어수 {[len(f.split()) for f in nf]})")
        else:
            log.warning(f"reground: facts 개수 불일치({len(nf)}/{n}) → 원본 유지")
    except Exception as e:
        log.warning(f"reground 실패 — 원본 유지: {e}")
    return meta


def _enforce_global_fact_novelty(meta: dict, prev_facts: list, source_text: str) -> dict:
    """최종 facts를 과거 전역 facts와 코드로 대조 → 겹치면 SOURCE로 그 fact만 재작성.

    프롬프트에 "반복하지 마라"만 넣던 것을 실제 검사로 승격(codex 지적). 재작성이 또
    겹치거나 SOURCE/키가 없으면 원본 유지(발행 0 방지 — 겹치는 fact가 나가는 게
    발행 스킵보다 낫다). 재작성 성공분만 교체한다.
    """
    if not prev_facts:
        return meta
    from pipeline import history as _hist
    facts = [f for f in (meta.get("facts") or []) if str(f).strip()]
    if not facts:
        return meta
    dup_idx = _hist.facts_repeat_globally(facts, prev_facts)
    if not dup_idx:
        return meta
    log.warning(f"전역 fact 중복 감지: 인덱스 {dup_idx} → SOURCE로 재작성 시도")
    src = (source_text or "").strip()
    if not OPENAI_API_KEY or not src:
        log.info("전역 fact 중복: SOURCE/키 없음 → 원본 유지(발행 우선)")
        return meta
    display, single = meta.get("display", ""), meta.get("single", "")
    avoid = "\n".join("- " + f for f in prev_facts[:20])
    keep = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts) if i not in dup_idx)
    redo = "\n".join(f"{i+1}. {facts[i]}" for i in dup_idx)
    prompt = f"""Some highlights in a {display} ({single}) city script repeat highlights already used on this channel.

SOURCE TEXT (Wikipedia — take all facts/numbers ONLY from here):
{src}

ALREADY-USED highlights on the channel — the rewrites MUST NOT overlap with any of these:
{avoid}

HIGHLIGHTS TO KEEP (do not change, for context):
{keep or '(none)'}

REWRITE ONLY these highlights to cover DIFFERENT true aspects of the city from SOURCE (a different landmark, dish, activity, or piece of history), not overlapping the used ones above:
{redo}

Rules: each rewrite 20-30 words, supported by SOURCE, every number/year must appear in SOURCE, vary openers, warm travel tone, no hype.
Output STRICT JSON with the SAME 1-based indices you were asked to rewrite:
{{"rewrites": {{{', '.join(f'"{i+1}": "..."' for i in dup_idx)}}}}}"""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        r = client.chat.completions.create(
            model=SCRIPT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.5,
            max_tokens=900,
        )
        rw = (json.loads(r.choices[0].message.content) or {}).get("rewrites") or {}
        new_facts = list(facts)
        replaced = 0
        for i in dup_idx:
            cand = str(rw.get(str(i + 1), "")).strip()
            # 재작성이 여전히 겹치면 버린다(원본 유지). 새로 지어낸 중복은 넣지 않는다.
            if cand and not _hist.facts_too_similar([cand], prev_facts):
                new_facts[i] = cand
                replaced += 1
        if replaced:
            meta["facts"] = new_facts
            log.info(f"전역 fact 중복 재작성: {replaced}/{len(dup_idx)}건 교체")
        else:
            log.info("전역 fact 중복 재작성 실패(여전히 겹침) → 원본 유지")
    except Exception as e:
        log.warning(f"전역 fact 중복 재작성 예외 — 원본 유지: {e}")
    return meta


def _apply_grounded_factcheck(meta: dict, source_text: str = None) -> dict:
    """SOURCE(위키, 도메인 맥락+category) 확보 → 적대적 팩트체크 → meta.intro/facts 갱신.
    hook(intro)은 ≤12단어 코드 강제. fact_keywords 확정은 파이프라인 최종 _finalize_fact_keywords에서(F).
    source_text가 주어지면 재fetch하지 않는다(pick 단계에서 이미 확보). 실패 시 원본 유지."""
    if not OPENAI_API_KEY:
        return meta
    display = meta.get("display", "")
    single = meta.get("single", "")
    src = source_text if source_text is not None else fetch_source_for(
        display, single, meta.get("category", ""), country=meta.get("country", ""))
    if not src:
        log.info("grounding: on-topic SOURCE 없음 → 보수 모드(established consensus) 팩트체크 진행")
    hook, facts = adversarial_factcheck(display, single, meta.get("intro", ""),
                                        meta.get("facts", []), src)
    hook = _enforce_hook_length(display, single, hook)  # (5) ≤12단어 강제
    if hook:
        meta["intro"] = hook
    if facts:
        meta["facts"] = facts
    return meta


def _differentiate_content(meta, prev_entries, avoid_formats=None):
    """재등장 topic: 과거 facts/angle 주입해 완전히 새로운 내용으로 재생성 + 겹침 1회 재검사."""
    if not OPENAI_API_KEY or not prev_entries:
        return meta
    from pipeline import history as _hist
    display = meta.get("display", "")
    single = meta.get("single", "")
    past_facts, past_angles = [], []
    for e in prev_entries:
        past_facts.extend([f for f in (e.get("facts") or []) if f])
        if e.get("angle"):
            past_angles.append(e["angle"])
    past_facts = past_facts[-16:]
    if not past_facts:
        return meta
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    def _regen():
        avoid_fmt = ", ".join(avoid_formats or []) or "none"
        n = len(meta.get("facts") or []) or 4
        prompt = f"""This city ({display}) was covered before on the channel. Write a COMPLETELY NEW 60-second city intro that does NOT repeat any previously used highlight or angle.

PREVIOUSLY COVERED — DO NOT REPEAT ANY OF THESE (use entirely different highlights and a different angle):
{chr(10).join('- ' + f for f in past_facts)}
Already-used angles for this city: {', '.join(past_angles) or 'none'}
Recently used formats to avoid: {avoid_fmt}

REQUIREMENTS:
- {n} NEW travel highlights, each 20-30 words, each about a DIFFERENT aspect than the previously covered ones above. Mix highlight types (landmark / food / activity / history / hidden corner). Warm first/second-person travel tone.
- Hook (intro narration): 12 words or fewer, a concrete detail about the city, NEVER start with "Did you know".
- Do NOT use empty hype ("will blow your mind", "paradise on earth", "more interesting than people think").
- Any number/year must be a real, verifiable fact for THIS city; volatile info (prices, hours) stays qualitative.
- fact_keywords: {n} background search keywords, one per highlight IN ORDER, each containing "{single}".
- angle: short label for the fresh angle (different from already-used ones). format: one of {FORMATS}.
- title (<=70 chars, includes "{single}", curiosity gap, no emoji), description (2 sentences + 3-5 hashtags), comment (reply-magnet, 8-20 words, about {single} ONLY).

Output STRICT JSON: {{"intro":"...","facts":[{', '.join(['"..."'] * n)}],"fact_keywords":[{', '.join(['"..."'] * n)}],"angle":"...","format":"...","title_en":"...","description_en":"...","comment":"..."}}"""
        resp = client.chat.completions.create(
            model=SCRIPT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.95,
            max_tokens=1200,
        )
        return json.loads(resp.choices[0].message.content)

    try:
        d = _regen()
        new_facts = [str(f).strip() for f in (d.get("facts") or []) if str(f).strip()]
        if len(new_facts) >= 3 and _hist.facts_too_similar(new_facts, past_facts):
            log.info("재등장 차별화: 1차 결과 과거와 겹침 → 1회 재생성")
            d2 = _regen()
            nf2 = [str(f).strip() for f in (d2.get("facts") or []) if str(f).strip()]
            if len(nf2) >= 3:
                d, new_facts = d2, nf2
        if len(new_facts) >= 3:
            meta["facts"] = new_facts
            if d.get("intro"):
                meta["intro"] = str(d["intro"]).strip()
            if d.get("title_en"):
                meta["title_en"] = str(d["title_en"]).strip()[:95]
            if d.get("comment"):
                meta["comment"] = str(d["comment"]).strip()[:280]
            if d.get("description_en"):
                meta["description_en"] = str(d["description_en"]).strip()[:4900]
            if d.get("angle"):
                meta["angle"] = _normalize_angle(d["angle"])
            if d.get("format"):
                meta["format"] = str(d["format"]).strip()
            meta["fact_keywords"] = _validate_fact_keywords(
                d.get("fact_keywords"), single, meta.get("pexels_keywords") or [], n=len(new_facts))
            meta["_kw_facts"] = list(new_facts)  # (F) 키워드 도출 시점 facts 스냅샷
            log.info(f"재등장 차별화 완료: {len(new_facts)} facts 새로 생성 (angle={meta.get('angle')})")
    except Exception as e:
        log.warning(f"재등장 차별화 실패 — 원본 유지: {e}")
    return meta


def _topic_tokens(text: str) -> set:
    """소문자 단어 토큰 집합 (영문만)."""
    import re as _re
    return {w for w in _re.findall(r"[a-z]+", text.lower()) if len(w) >= 3}


def verify_consistency(meta: dict) -> dict:
    """GPT 1회로 하이라이트/comment 내부 일관성 검증/수정.

    Catches:
    - 하이라이트 내 숫자/연도 모순 (같은 랜드마크 건립연도가 서로 다름 등)
    - comment가 다른 도시 (Kyoto 영상에 Tokyo 댓글 — TOPIC LOCK 위반)
    - title/description/comment 도시 불일치

    실패 시 원본 meta 유지 (검증 실패해도 발행 자체는 진행).
    """
    if not OPENAI_API_KEY:
        return meta

    display = meta.get("display", "").strip()
    single = meta.get("single", "").strip()
    facts = list(meta.get("facts", []) or [])
    comment = meta.get("comment", "").strip()
    title = meta.get("title_en", "").strip()

    if not facts or not display:
        return meta

    # 1) Python 사전 체크: comment에 다른 우주 토픽 단어가 본 토픽 단어 없이 등장 → off-topic 의심
    own_tokens = _topic_tokens(f"{display} {single}")
    comment_tokens = _topic_tokens(comment)
    other_in_comment = (comment_tokens & _OTHER_TOPIC_WORDS) - own_tokens
    has_own_in_comment = bool(comment_tokens & own_tokens) or display.lower() in comment.lower() or single.lower() in comment.lower()
    suspect_offtopic_comment = bool(other_in_comment) or not has_own_in_comment

    country = (meta.get("country") or "").strip()
    landmark_lock = (meta.get("landmark_lock") or "").strip()
    ctx_lines = f"City: {display} ({single})" + (f", Country: {country}" if country else "")
    if landmark_lock:
        ctx_lines += f"\nSignature landmarks (ground truth for what belongs to {display}): {landmark_lock}"

    review_prompt = f"""You are a travel-script consistency editor for YouTube Shorts about the city {display} ({single}).

{ctx_lines}
Treat the Country and Signature landmarks above as ground truth — a highlight naming a landmark
from a different city/country, or implying the wrong country, is a FALSE claim.

Review this city-intro script for three CRITICAL issues:
1. NUMERIC/HISTORICAL CONTRADICTIONS between highlights (e.g. one says a landmark opened in 1889, another says 1900 for the same thing)
2. FACTUALLY FALSE claims — a landmark, dish, or historical event that does NOT belong to {display}, a clearly wrong year, or a precise price/hours stated as fixed fact
3. COMMENT OFF-TOPIC (comment mentions a DIFFERENT city than {display})

Title: {title}
Highlights:
{chr(10).join(f"{i+1}. {f}" for i, f in enumerate(facts))}
Comment: {comment}

INSTRUCTIONS:
- If 2+ highlights give different numbers/years for the SAME thing, pick ONE accurate value and rewrite ALL contradicting highlights to use THAT value.
  Keep highlights at 20-30 words. Only modify the contradicting claim, keep the rest of the line.
- If any highlight is factually FALSE (wrong-city landmark/dish/event, wrong year, fixed price stated as fact), correct it to an accurate, equally
  appealing highlight for {display} (keep 20-30 words). Do NOT invent facts you are not confident about.
- If comment mentions any city other than {display}, rewrite the comment to be about {display} ONLY,
  in 8-20 words, using one of: hot take / forced choice within {display} / what-if / personal challenge / confession bait / fill-in-the-blank.
- If everything is fine, return original highlights and comment unchanged.

Output STRICT JSON — "facts" MUST contain EXACTLY {len(facts)} items in the same order:
{{"facts": [{", ".join(['"..."'] * len(facts))}], "comment": "...", "fixed": ["short list of issues fixed, empty if none"]}}"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=FACTCHECK_MODEL,
            messages=[{"role": "user", "content": review_prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=900,
        )
        d = json.loads(resp.choices[0].message.content)
        new_facts = d.get("facts") or []
        new_comment = (d.get("comment") or "").strip()
        fixed = d.get("fixed") or []
        if isinstance(new_facts, list) and len(new_facts) == len(facts):
            new_facts = [str(f).strip() for f in new_facts if str(f).strip()]
            if len(new_facts) == len(facts):
                meta["facts"] = new_facts
        if new_comment and 5 < len(new_comment) <= 280:
            meta["comment"] = new_comment
        if fixed:
            log.info(f"consistency check fixed {len(fixed)}건: {fixed}")
        elif suspect_offtopic_comment:
            log.warning(f"comment 사전 의심됐지만 GPT는 수정 없다고 판단 (other_in_comment={other_in_comment}, has_own={has_own_in_comment})")
    except Exception as e:
        log.warning(f"consistency check 실패 — 원본 유지: {e}")

    return meta


FAME_CYCLE = (1, 2, 3, 4)   # 발행 등급 순환 순서 (쿠마님 2026-07-23 지시: "12341234 순")


def _fame_counts(ever_used: list = None) -> dict:
    """지금까지 발행한 편수를 fame 등급별로 집계 (로스터에 있는 키만). 로그·점검용."""
    counts = {f: 0 for f in FAME_CYCLE}
    for k in (ever_used or []):
        entry = ANIMALS.get((k or "").lower().strip())
        if entry:
            counts[_fame(entry)] = counts.get(_fame(entry), 0) + 1
    return counts


def _last_fame(ever_used: list = None) -> int:
    """직전에 발행한 도시의 fame 등급. 이력이 없거나 전부 로스터 밖이면 0(=다음이 1)."""
    for k in reversed(ever_used or []):
        entry = ANIMALS.get((k or "").lower().strip())
        if entry:
            return _fame(entry)
    return 0


def _next_fame_tier(available: set, ever_used: list = None) -> int:
    """다음에 쓸 fame 등급 — **직전 발행 등급의 다음 칸**(1→2→3→4→1…).

    엄격한 라운드로빈이다. 별도 카운터 파일 없이 '마지막으로 발행한 도시의 등급'에서
    한 칸 돌린다. 이력이 없으면 1(파리·도쿄급)부터 시작한다.
    후보가 남지 않은 등급은 건너뛰고 그 다음 칸으로 넘어간다(4개 다 없으면 available 최소).
    """
    if not available:
        return FAME_CYCLE[0]
    start = _last_fame(ever_used)
    idx = FAME_CYCLE.index(start) if start in FAME_CYCLE else -1
    for step in range(1, len(FAME_CYCLE) + 1):
        cand = FAME_CYCLE[(idx + step) % len(FAME_CYCLE)]
        if cand in available:
            return cand
    return min(available)


def _roster_candidates(exclude_keys: list = None, ever_used: list = None,
                       limit: int = 40) -> list:
    """후보 도시 [(key, entry)] — **미발행 우선 → 이번 차례의 fame 등급 하나**만.

    2026-07-23: 기존엔 100개 전체를 셔플해서 던졌고 프롬프트도 "신선한 도시 선호"라
    첫 편이 Kanazawa로 나갔다. 무명 도시로는 조회수가 안 붙는다.
    → cities.json에 fame 1~4를 부여하고, 한 번에 **한 등급**만 GPT 후보로 제시한다.
    등급은 1→2→3→4로 순환한다(쿠마님 지시: "12341234 순"). 유명 도시를 초반에 다 태워
    나중에 무명만 남는 걸 막기 위한 것으로, 4편 중 1편은 항상 파리·도쿄급이 나간다.

    ever_used(발행 전체 이력)를 주면 '한 번도 안 쓴 도시'를 먼저 소진한다. 이게 없으면
    exclude(최근 USED_KEEP개)에서 풀려난 옛 도시가 계속 재선택돼
    일부 도시가 영영 발행되지 않는다(로스터 100 > USED_KEEP 85).
    """
    excl = {(k or "").lower().strip() for k in (exclude_keys or [])}
    ever = {(k or "").lower().strip() for k in (ever_used or [])}
    remaining = [(key, c) for key, c in ANIMALS.items() if key.lower() not in excl]
    if not remaining:
        return []
    fresh = [(key, c) for key, c in remaining if key.lower() not in ever]
    pool = fresh or remaining          # 전부 한 번씩 돌았으면 재방문 허용(등급 순환은 유지)
    target = _next_fame_tier({_fame(c) for _, c in pool}, ever_used)
    tier = [(key, c) for key, c in pool if _fame(c) == target]
    random.shuffle(tier)
    tier = tier[:limit]
    log.info(f"로스터 tier: 직전 fame={_last_fame(ever_used)} → 이번 차례 fame={target} "
             f"(등급별 발행 {_fame_counts(ever_used)}) "
             f"→ 후보 {len(tier)}개 제시 (제외 후 {len(remaining)}개 중 미발행 {len(fresh)}개"
             f"{'' if fresh else ' → 전량 1순회 완료, 재방문 모드'})")
    return tier


def _roster_lines(exclude_keys: list = None, ever_used: list = None,
                  limit: int = 40) -> str:
    """_roster_candidates 결과를 GPT 프롬프트용 목록 문자열로."""
    lines = []
    for key, c in _roster_candidates(exclude_keys, ever_used, limit):
        city = c.get("display") or c.get("single") or key
        country = c.get("country", "")
        lines.append(f"- {city}" + (f", {country}" if country else "") + f" (key: {key})")
    return "\n".join(lines)


def _resolve_roster_key(animal_key: str, single: str = "", display: str = "") -> str:
    """GPT가 로스터 키를 약간 다르게 반환해도(예: 'kyoto' vs 'kyoto_japan') canonical 로스터 키로 정규화.

    중복 방지(used_cities/history 키 일관) 핵심 — 같은 도시가 다른 키로 중복 발행되는 것을 막는다.
    로스터에 정확히 있으면 그대로, 없으면 display/single 일치로 찾고, 없으면 원본 유지.
    """
    if not animal_key:
        return animal_key
    if animal_key in ANIMALS:
        return animal_key
    sl = (single or "").strip().lower()
    dl = (display or "").strip().lower()
    for key, c in ANIMALS.items():
        cs = (c.get("single") or "").strip().lower()
        cd = (c.get("display") or "").strip().lower()
        if (sl and (sl == cs or sl == cd)) or (dl and (dl == cs or dl == cd)):
            return key
    # 키 접두 일치 (kyoto → kyoto_japan)
    for key in ANIMALS:
        if key.startswith(animal_key + "_") or animal_key.startswith(key + "_"):
            return key
    return animal_key


def pick_random_animal(exclude_keys: list = None, history: dict = None,
                       avoid_formats: list = None, avoid_angles: list = None,
                       ever_used: list = None) -> dict:
    """GPT가 로스터(cities.json)에서 도시 1곳 + 앵글 + 하이라이트 4~5개 메타를 일괄 생성.

    콘텐츠 모델: 한 편 = 한 도시 60초 소개. 앵글 1개(iconic_landmarks/best_things_to_do/
    food_and_specialty/history_story/hidden_gem)를 중심으로 하이라이트 4~5개(각 8~20단어, 타입 믹스).
    로스터 엔트리의 country/landmark_lock/pexels_keywords는 curated 값으로 meta에 흘려보낸다.
    실패 시 사전에서 fallback.

    history: content_history dict (재등장 도시 차별화 + per-city angle 회피용).
    avoid_formats: 전역 직전 N편 format (연속 방지).
    avoid_angles: 전역 직전 N편 angle (서로 다른 도시로 같은 앵글 연속 방지, 2026-07-17 배선).
    ever_used: 발행 전체 이력(used_cities 전량) — 미발행 도시 우선 + fame 게이팅용.

    반환: {animal_key(city_key), display(city), single(city), country, category, landmark_lock,
           pexels_keywords, fact_keywords, title_en, description_en, hook, intro, facts, outro,
           tags, comment, angle, format}
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY 없음 — fallback 사전 사용")
        key = pick_animal(exclude=exclude_keys or [], ever_used=ever_used)
        return _legacy_meta(key)

    exclude_str = ", ".join(exclude_keys[-100:]) if exclude_keys else "none"
    avoid_fmt_str = ", ".join(avoid_formats or []) or "none"
    avoid_angle_str = ", ".join(avoid_angles or []) or "none"
    # 참고: angle 회피는 프롬프트 advisory + per-city 회피(_differentiate_content)가 방어선.
    # format과 달리 angle은 대본 내용과 결합돼 있어 파싱 후 라벨만 강제 교체하면
    # 앵글-대본 부정합이 생기므로 코드 강제는 하지 않고, 겹치면 경고 로그만 남긴다(하단).
    roster_cands = _roster_candidates(exclude_keys, ever_used)
    allowed_keys = {k for k, _ in roster_cands}   # GPT가 이 tier 밖을 픽하면 코드가 거절
    roster_block = "\n".join(
        f"- {(c.get('display') or c.get('single') or k)}"
        + (f", {c.get('country')}" if c.get("country") else "") + f" (key: {k})"
        for k, c in roster_cands)
    if not roster_block:
        # USED_KEEP(85) < 로스터(100)라 정상적으로는 도달 불가. 도달 시 off-roster 픽을
        # 유도하지 않고(큐레이션·landmark_lock 상실 방지) 예외 → main의 legacy fallback
        # (로스터 내 재사용)으로 위임한다. fail-closed.
        raise RuntimeError("로스터 후보 0 — off-roster GPT 픽 금지, legacy fallback으로 위임")

    # 전역 중복 차단 (2026-07-10). 기존엔 '같은 topic 재등장' 시에만 과거 facts를 봤기 때문에
    # topic이 달라도 같은 사실을 반복했고, 제목은 대상만 갈아끼운 양산 템플릿이 반복됐다.
    prev_facts, prev_titles = [], []
    if history:
        try:
            from pipeline import history as _hist
            prev_facts = _hist.recent_facts(history, 120, limit=30)
            prev_titles = _hist.recent_titled_topics(history, 12)  # (title, topic_key)
        except Exception as e:
            log.warning(f"전역 이력 조회 실패(무시): {e}")
    prev_title_strs = [t[0] for t in prev_titles]
    facts_block = ("\n\nALREADY-COVERED FACTS on this channel — do NOT repeat any of these, "
                   "even for a different topic:\n"
                   + "\n".join("- " + f for f in prev_facts)) if prev_facts else ""
    titles_block = ("\n\nRECENT TITLES on this channel. Your title MUST NOT reuse these sentence "
                    "templates with a different subject swapped in (YouTube demonetizes "
                    "template-mass-produced content). Invent a genuinely different structure:\n"
                    + "\n".join("- " + t for t in prev_title_strs)) if prev_title_strs else ""

    prompt = f"""You are a viral travel scriptwriter for the YouTube Shorts channel "Wander 60s".
Pick ONE city from the ROSTER below and write a 60-second city introduction (1-minute Shorts).

ROSTER — pick ONE city from THIS list only (use its exact key). The channel has already
pre-filtered this list for you, so do NOT try to be clever or "fresh" by reaching for a
destination outside it — from THIS list, pick the city the widest global audience would
recognise and click on:
{roster_block}

AVOID picking these cities (recently used): {exclude_str}{facts_block}{titles_block}

CONTENT MODEL (city showcase, 60s):
- Choose ONE **angle** from {ANGLES} and build the whole video around it:
  · iconic_landmarks — the must-see landmarks
  · best_things_to_do — what to actually do / why it's great
  · food_and_specialty — local food, drink, specialties
  · history_story — a real historical event or story of the city
  · hidden_gem — the underrated, quieter side of the city
- Write 4 or 5 **highlights** (prefer 5). Each highlight = ONE punchy destination one-liner and is its own visual slot.
- You MAY mix highlight types within the video (e.g. 2 landmarks + 1 food + 1 history), but keep them coherent with the chosen angle.

REQUIREMENTS:
- **Each highlight MUST be 20-30 words** (this fills the 60-second narration — shorter lines leave dead air). Warm, first/second-person travel tone. Example: "Fushimi Inari's thousands of vermilion gates climb the mountainside — go at sunrise and you'll have whole stretches of the winding trail almost to yourself."
- Every highlight must be TRUE for THIS city. Landmarks, dishes, and historical events must actually belong to it. Every number/year will be checked against Wikipedia and rewritten if wrong.
- **VOLATILE info stays qualitative**: do NOT state exact ticket prices, opening hours, or exchange rates as fixed facts — say "cheap street food", "go early", "worth the climb" instead. Verifiable facts (a founding year, a landmark's height) are fine only if real.
- **INTERNAL CONSISTENCY**: any number/year you use must agree across highlights, title, and comment. title and description MUST be about the SAME city as the highlights.
- **Hook (intro narration)** — 12 words or FEWER. A concrete, inviting reason to visit. NEVER start with "Did you know". Example: "Kyoto packs a thousand years of Japan into one walkable city."
- **BANNED PHRASES (never use in intro or highlights)**: "paradise on earth", "will blow your mind", "must-see before you die", "best kept secret", "more interesting than people think". Write concrete, specific lines instead.
- Each highlight MUST start differently (vary openers).
- **angle**: choose ONE from {ANGLES}. AVOID these recently-used angles unless nothing else fits the city: {avoid_angle_str}.
- **format**: choose ONE from {FORMATS}. AVOID these recently-used formats: {avoid_fmt_str}.
- **pexels_keywords**: 5 stock-video search keywords. **EVERY keyword MUST contain the city name** (add the country too if the city name is ambiguous, e.g. "Paris France", not just "Paris"). Examples for Kyoto: "Kyoto Japan", "Kyoto temple", "Kyoto street", "Kyoto Fushimi Inari", "Kyoto old town". NEVER use bare generic terms like "city" or "temple" alone.
- **fact_keywords**: one background-video search keyword per highlight IN ORDER, each containing the city name and describing that highlight's visual (e.g. a torii-gate highlight -> "Kyoto Fushimi Inari"; a food highlight -> "Kyoto matcha sweets").
- **category**: one of {sorted(_VALID_CATEGORIES)} (the continent / travel theme — used for background music).
- **country**: the country the city is in.
- **landmark_lock**: "City, Country — Landmark1, Landmark2, Landmark3" (the 3 most recognizable landmarks, for visual verification).
- **Title (CRITICAL — single most important CTR factor)**: max 70 chars, MUST include the city name + a CURIOSITY GAP or clear promise. NEVER use "compilation", "best of". NO emoji. Hype ok but no false claim.
  Use ONE of these viral patterns (vary across videos):
  (a) In 60s: "Kyoto in 60 Seconds"
  (b) Why visit: "Why Kyoto Should Be Your Next Trip"
  (c) The one thing: "The One Thing You Can't Miss in Kyoto"
  (d) Reasons: "5 Reasons Kyoto Will Steal Your Heart"
  (e) Hidden side: "The Kyoto Locals Don't Tell Tourists About"
  (f) First timer: "First Time in Kyoto? Start Here"
  (g) Bucket list: "Put Kyoto on Your Bucket List — Here's Why"
- Description: 2 sentences + 3-5 hashtags (no emoji).
- Hook (on-screen overlay text): short, like "Kyoto in 60s" or "Kyoto • Japan".
- **comment** (auto-pinned reply-magnet comment under the video): ONE line, 8-20 words.
  GOAL: provoke viewers to actually reply.
  **CRITICAL — TOPIC LOCK**: The comment MUST be about THE EXACT CITY YOU JUST PICKED.
  Do NOT mention any OTHER city. Do NOT contrast against another city (no "Kyoto or Tokyo?" comments).
  Pick ONE reply-magnet pattern below; the patterns show STYLE — replace the example city with your pick.
  (a) Hot take / fight me — STYLE: "Hot take: [city] is best in winter. Fight me 🔥"
  (b) Forced choice WITHIN the city — STYLE: "In [city]: temples first or street food first?"
  (c) What-if — STYLE: "If you had 24 hours in [city], where would you go first?"
  (d) Personal challenge — STYLE: "Bet you can't visit [city] and skip [famous dish]."
  (e) Confession bait — STYLE: "Confession: I could move to [city] tomorrow. Anyone else?"
  (f) Fill-in-the-blank — STYLE: "The most underrated thing in [city] is ___"
  Use 0-1 emoji max. **NEVER** these dead patterns: "Which one surprised you?", "Tag a friend who...", "Did you know any of these?", "Like if you learned something".

Output STRICT JSON only:
{{
  "animal_key": "snake_case_id matching the roster key (e.g. kyoto_japan)",
  "display": "Kyoto",
  "single": "Kyoto",
  "country": "Japan",
  "category": "asia_culture",
  "landmark_lock": "Kyoto, Japan — Fushimi Inari, Kiyomizu-dera, Arashiyama Bamboo Grove",
  "pexels_keywords": ["Kyoto Japan", "Kyoto temple", "Kyoto street", "Kyoto Fushimi Inari", "Kyoto old town"],
  "fact_keywords": ["Kyoto Fushimi Inari", "Kyoto Kiyomizu-dera", "Kyoto matcha sweets", "Kyoto Arashiyama bamboo", "Kyoto geisha district"],
  "angle": "one_of_the_angle_labels",
  "format": "one_of_the_format_labels",
  "title_en": "...",
  "description_en": "... #shorts #travel #kyoto #japan #wander",
  "hook": "Kyoto in 60s",
  "intro": "Kyoto packs a thousand years of Japan into one walkable city.",
  "facts": ["highlight1 (20-30 words)", "highlight2 (20-30 words)", "highlight3 (20-30 words)", "highlight4 (20-30 words)", "highlight5 (20-30 words)"],
  "outro": "Save this for your trip.",
  "comment": "In Kyoto: temples first or street food first?",
  "tags": ["shorts","travel","kyoto","japan","things to do","city guide","wander","travel tips","asia"]
}}"""

    # 2026-05-07 fix (동물봇 9연속 pangolin 사고 동기화): GPT가 prompt example/recent topic에
    # bias되어 같은 토픽 반복 → 코드 reject + retry로 강제 다양화.
    exclude_set = {k.lower().strip() for k in (exclude_keys or [])}

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    d = None
    last_pick = None
    source_text = ""
    # 재시도 시 거부 목록을 누적한다. 직전 1건만 알려주면 모델이 같은 인기 토픽으로 계속
    # 돌아온다 (실측: 4회 연속 saturn 픽). 누적 + 미사용 후보 양성 제시로 밀어낸다.
    rejected = []
    for attempt in range(4):
        try_prompt = prompt
        if attempt > 0 and rejected:
            # 힌트도 반드시 현재 tier 후보 안에서 (여기서 무명 도시를 추천하면 게이팅이 뚫린다)
            unused = [k for k, _ in roster_cands if k.lower() not in exclude_set][:12]
            hint = (f"\nPick one of these unused topics instead: {', '.join(unused)}.\n"
                    if unused else "\n")
            try_prompt = (
                f"STOP. You already picked these and they are ALL REJECTED (already used on the "
                f"channel): {', '.join(rejected)}.\n"
                f"Do NOT pick any of them again. Pick a COMPLETELY DIFFERENT topic that is not in "
                f"the exclude list below.{hint}\n"
                + try_prompt
            )
        try:
            resp = client.chat.completions.create(
                model=SCRIPT_MODEL,
                messages=[{"role": "user", "content": try_prompt}],
                response_format={"type": "json_object"},
                temperature=1.0,
                max_tokens=1200,
            )
            cand = json.loads(resp.choices[0].message.content)
        except Exception as e:
            log.warning(f"GPT 도시 픽 실패 → legacy fallback: {e}")
            key = pick_animal(exclude=exclude_keys or [], ever_used=ever_used)
            return _legacy_meta(key)

        cand_key = (cand.get("animal_key") or "").strip().lower().replace(" ", "_")
        cand_single = (cand.get("single") or "").strip().lower()
        cand_display = (cand.get("display") or "").strip().lower()
        is_excluded = (
            cand_key in exclude_set
            or cand_single in exclude_set
            or cand_display in exclude_set
            or any(e and (e in cand_key or e in cand_single) for e in exclude_set if e)
        )
        if is_excluded:
            last_pick = cand_key or cand_single
            if last_pick and last_pick not in rejected:
                rejected.append(last_pick)
            log.warning(f"[picker try {attempt+1}] GPT가 exclude된 '{last_pick}' 픽 → 재요청")
            continue
        # fame 게이팅 강제: 프롬프트로 tier를 제시해도 모델이 로스터 밖/하위 등급 도시를
        # 집어오는 경우가 있다(우주봇 실측: 프롬프트 무시 반복). 코드로 fail-closed.
        cand_norm = _resolve_roster_key(cand_key, cand_single, cand_display)
        if allowed_keys and cand_norm not in allowed_keys:
            last_pick = cand_norm or cand_key or cand_single
            if last_pick and last_pick not in rejected:
                rejected.append(last_pick)
            log.warning(f"[picker try {attempt+1}] GPT가 현재 tier 밖 '{last_pick}' 픽 "
                        f"(후보 {len(allowed_keys)}개) → 재요청")
            continue
        # 근거 선행: 위키 SOURCE를 못 구하는 도시는 사실 검증이 불가능하다 → 다른 도시로 재픽.
        # 마지막 시도에서는 SOURCE 없이도 진행해 발행 0을 피한다(보수 모드 팩트체크가 방어).
        # country는 로스터 엔트리(curated) 우선, 없으면 GPT 값 — 동명 도시 grounding 오매칭 완화.
        _cand_entry = ANIMALS.get(cand_key) or {}
        cand_country = _cand_entry.get("country") or cand.get("country", "")
        src = fetch_source_for(cand.get("display", ""), cand.get("single", ""),
                               cand.get("category", ""), country=cand_country)
        if not src and attempt < 3:
            last_pick = cand_key or cand_single
            if last_pick and last_pick not in rejected:
                rejected.append(last_pick)
            log.warning(f"[picker try {attempt+1}] '{last_pick}' 위키 SOURCE 확보 실패 → 다른 토픽 재픽")
            continue
        source_text = src
        d = cand
        break

    if d is None:
        log.warning(f"GPT가 4회 모두 exclude/tier 밖 도시만 픽 → legacy fallback + expand")
        key = pick_animal(exclude=exclude_keys or [], ever_used=ever_used)
        legacy = _legacy_meta(key)
        # 2026-05-08 fix 계승: placeholder facts 그대로 발행 직전까지 가던 것 → expand로 진짜 하이라이트 생성.
        try:
            rich = expand_legacy_to_rich(legacy)
            legacy.update(rich)
            log.info(f"picker fallback → expand 완료 ({len(legacy['facts'])} highlights)")
        except Exception as e:
            log.warning(f"picker fallback expand 실패 → placeholder facts 유지: {e}")
        # legacy 경로도 정상 경로의 최종 방어(전역 fact 중복 + 제목 양산/금지opener 검사)를 적용한다.
        try:
            _lsrc = fetch_source_for(legacy.get("display", ""), legacy.get("single", ""),
                                     legacy.get("category", ""), country=legacy.get("country", ""))
            _res = _enforce_global_fact_novelty(legacy, prev_facts, _lsrc)
            if isinstance(_res, dict) and _res.get("facts"):
                legacy = _res
        except Exception as e:
            log.warning(f"legacy 전역 fact 중복 검사 실패(무시): {e}")
        try:
            if _title_needs_fix(legacy.get("title_en", ""), prev_titles,
                                (legacy.get("single", ""), legacy.get("display", ""))):
                legacy["title_en"] = _retitle_unique(legacy.get("display", ""),
                                                     legacy.get("single", ""),
                                                     legacy.get("title_en", ""), prev_titles)
                log.warning(f"제목 양산/금지opener(legacy) 차단 → {legacy['title_en']!r}")
        except Exception as e:
            log.warning(f"제목 검사(legacy) 실패(무시): {e}")
        return legacy

    # 검증 + 정제
    animal_key = (d.get("animal_key") or "").strip().lower().replace(" ", "_") or "city"
    display = (d.get("display") or "This City").strip()
    single = (d.get("single") or display).strip()
    # GPT가 로스터 키를 다르게 줘도 canonical 키로 정규화 (중복 방지 핵심)
    animal_key = _resolve_roster_key(animal_key, single, display)

    # === 로스터 엔트리(curated) 우선 병합 — country/landmark_lock/category/curated 키워드 ===
    entry = ANIMALS.get(animal_key) or {}
    country = (entry.get("country") or d.get("country") or "").strip()
    landmark_lock = (entry.get("landmark_lock") or d.get("landmark_lock") or "").strip()
    confusion_pair = entry.get("confusion_pair") or []
    category = (entry.get("category") or d.get("category") or "").strip().lower()
    if category not in _VALID_CATEGORIES:
        category = "historic_ancient"

    # === 매칭 안전선: pexels_keywords는 curated(엔트리) 우선, 모든 키워드에 도시명 포함 강제 ===
    raw_kw = entry.get("pexels_keywords") or d.get("pexels_keywords") or []
    if not isinstance(raw_kw, list):
        raw_kw = []
    sanitized_kw = []
    for kw in raw_kw:
        if not isinstance(kw, str):
            continue
        kw = kw.strip()
        if not kw or len(kw) < 3:
            continue
        # 도시명 포함 강제 (엉뚱한 도시/generic 매칭 방지). 모호하면 국가도 붙음(엔트리 confusion 대비).
        if single.lower() not in kw.lower():
            kw = f"{single} {kw}".strip()
        sanitized_kw.append(kw[:80])
    if not sanitized_kw:
        base = f"{single} {country}".strip()
        sanitized_kw = [base or single, f"{single} city", f"{single} travel",
                        f"{single} landmark", f"{single} street"]
    pexels_kw = sanitized_kw[:8]

    title_en = (d.get("title_en") or f"{display} in 60 Seconds").strip()[:95]
    if _has_bad(title_en, _BAD_TITLE_EN):
        title_en = f"{display} in 60 Seconds"
    _generic = ["amazing facts about", "wild facts about", "interesting facts about",
                "fascinating facts about", "facts you didn't know"]
    if any(g in title_en.lower() for g in _generic):
        log.warning(f"generic title pattern → CTR-friendly fallback: {title_en}")
        title_en = f"Why You Need to See {display}"
    # 템플릿 양산 차단 (2026-07-10): 최근 제목과 앞 3단어 지문이 같으면 대상만 바꾼 복제본이다.
    # 실제 사고: "You Won't Believe What Foxes/Comets/Jupiter/the Big Bang Does at Night".
    # 유튜브 2025-07 inauthentic content 정책의 수익화 리스크라 코드로 강제한다.
    try:
        if _title_needs_fix(title_en, prev_titles, (single, display)):
            alt = _retitle_unique(display, single, title_en, prev_titles)
            log.warning(f"제목 양산/금지opener 차단: {title_en!r} → {alt!r}")
            title_en = alt
    except Exception as e:
        log.warning(f"제목 검사 실패(무시): {e}")

    facts = d.get("facts") or []
    if not isinstance(facts, list) or len(facts) < 3:
        facts = [_safe_fact(display, i) for i in range(4)]
    facts = [str(f).strip() for f in facts[:5] if str(f).strip()]
    if len(facts) < 3:
        facts = facts + [_safe_fact(display, i) for i in range(3 - len(facts))]
    # 하이라이트는 20~30단어(60초 내레이션 채움). 짧으면(< 14단어) 여행 톤으로 1회 보강.
    if any(len(f.split()) < 14 for f in facts):
        log.warning(f"highlights too short ({[len(f.split()) for f in facts]}), rewriting to 20-30 words each...")
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            n = len(facts)
            joined = "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
            expand_prompt = f"""Rewrite each city highlight to 20-30 words in a warm first/second-person travel tone. Keep each one TRUE for {display} (real landmark/food/activity/history). Add only true, concrete detail — never invent numbers. Vary the openers.

Original {n} highlights about {display}:
{joined}

Output STRICT JSON: {{"facts": [{', '.join(['"highlight (20-30 words)"'] * n)}]}}"""
            r2 = client.chat.completions.create(
                model=SCRIPT_MODEL,
                messages=[{"role": "user", "content": expand_prompt}],
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=700,
            )
            d2 = json.loads(r2.choices[0].message.content)
            new_facts = d2.get("facts") or []
            if isinstance(new_facts, list) and len(new_facts) >= len(facts):
                facts = [str(f).strip() for f in new_facts[:len(facts)]]
                log.info(f"highlights rewritten to {[len(f.split()) for f in facts]}")
        except Exception as e:
            log.warning(f"highlight 보강 실패 — 원본 유지: {e}")

    comment = (d.get("comment") or "").strip()
    if not comment or len(comment) < 10:
        comment = f"What's the first thing you'd do in {display}?"
    comment = comment[:280]

    # angle / format 파싱 + 정규화 (로테이션 + strict 랜드마크 슬롯 판정용)
    angle = _normalize_angle(d.get("angle"))
    if avoid_angles and angle in {a.lower() for a in avoid_angles}:
        # angle은 대본 내용과 결합돼 있어 format처럼 라벨만 강제 교체하면 부정합 —
        # 프롬프트 회피가 뚫린 경우 관측용 경고만 남긴다 (codex P3, 2026-07-17)
        log.warning(f"angle '{angle}'이 최근 사용 목록 {avoid_angles}과 중복 — 프롬프트 회피 미적용된 픽")
    fmt = str(d.get("format") or "").strip().lower().replace(" ", "_")
    avoid = {f.lower() for f in (avoid_formats or [])}
    # 유효하지 않거나 '최근 사용 format'이면 회피 목록 밖에서 강제 재선택
    # (GPT가 valid하지만 직전 format을 반복하면 연속 중복이 되므로 코드로 차단)
    if fmt not in FORMATS or fmt in avoid:
        choices = [f for f in FORMATS if f not in avoid] or FORMATS
        fmt = random.choice(choices)
    fact_kw = _validate_fact_keywords(d.get("fact_keywords"), single, pexels_kw, n=len(facts))

    meta = {
        "animal_key": animal_key,
        "display": display,
        "single": single,
        "country": country,
        "category": category,
        "landmark_lock": landmark_lock,
        "confusion_pair": confusion_pair,
        "pexels_keywords": pexels_kw,
        "fact_keywords": fact_kw,
        "_kw_facts": list(facts),  # (F) 키워드 도출 시점 facts 스냅샷
        "angle": angle,
        "format": fmt,
        "title_en": title_en,
        "description_en": (d.get("description_en") or f"A 60-second guide to {display}, {country}.".strip())[:4900],
        "hook": (d.get("hook") or f"{display} in 60s")[:40],
        "intro": (d.get("intro") or f"Here's why {display} belongs on your travel list.").strip(),
        "facts": facts,
        "outro": (d.get("outro") or "Save this for your trip.").strip(),
        "comment": comment,
        "tags": d.get("tags") or ["shorts", "travel", single, country, "things to do",
                                  "city guide", "wander", "travel tips", "60 seconds"],
    }

    # 재등장 topic 차별화 (과거 이력 있으면 완전히 새로운 내용으로 재생성)
    prev = []
    if history:
        try:
            from pipeline import history as _hist
            prev = _hist.topic_entries(history, animal_key)
        except Exception:
            prev = []
    if prev:
        log.info(f"'{animal_key}' 과거 이력 {len(prev)}건 → 재등장 차별화 시도")
        meta = _differentiate_content(meta, prev, avoid_formats)

    # 근거 우선 재작성 → 적대적 팩트체크(허위 자막 차단) → 내부 일관성 검증.
    # SOURCE는 pick 단계에서 이미 확보했으므로 재fetch하지 않는다.
    meta = reground_facts(meta, source_text)
    meta = _apply_grounded_factcheck(meta, source_text)
    meta = verify_consistency(meta)
    # 전역 fact 중복 코드 강제 (codex 지적 2026-07-10): 프롬프트 주입만으론 GPT가 무시할 때
    # 못 막는다. 최종 facts를 과거 전역 facts와 실제로 대조해 겹치면 SOURCE로 1회 재작성한다.
    meta = _enforce_global_fact_novelty(meta, prev_facts, source_text)
    # (F) 모든 fact 변경 끝난 뒤 fact_keywords 최종 확정 (배경 fetch가 최종 facts와 정합)
    meta = _finalize_fact_keywords(meta, pexels_kw)
    # 제목 템플릿 최종 검사 — _differentiate_content가 title을 덮어쓰므로 여기서 한 번 더 본다.
    try:
        if _title_needs_fix(meta.get("title_en", ""), prev_titles, (single, display)):
            meta["title_en"] = _retitle_unique(display, single, meta["title_en"], prev_titles)
            log.warning(f"제목 양산/금지opener(최종) 차단 → {meta['title_en']!r}")
    except Exception as e:
        log.warning(f"제목 최종 검사 실패(무시): {e}")
    return meta


def expand_legacy_to_rich(meta: dict) -> dict:
    """legacy fallback에서 만든 generic 도시 meta를 GPT로 풀어 진짜 하이라이트 생성.
    실패 시 호출 측에서 원본 meta 그대로 사용 (안전).
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 없음 — expand 불가")

    display = meta.get("display", "This City")
    single = meta.get("single", display)
    country = meta.get("country", "")
    where = f"{display}, {country}".strip().strip(",") if country else display

    prompt = f"""Write a 60-second YouTube Shorts city introduction about {where} ({single}).

REQUIREMENTS:
- Choose ONE angle from {ANGLES} and build the video around it.
- 5 travel HIGHLIGHTS (one-liners), each 20-30 words. Warm first/second-person travel tone. You MAY mix types (landmark / food / activity / history / hidden corner).
- **FILMABLE (CRITICAL — each highlight becomes stock B-roll)**: every highlight must center on a place or scene that realistically HAS travel stock video of {single} — outdoor landmarks, streets, old-town districts, the castle, gardens, temples/shrines, markets, riverside, bridges, viewpoints, or festivals. AVOID subjects with little or no stock footage: museum interiors, specific indoor exhibits, abstract concepts, or a single named dish shown alone. For food, frame it as a MARKET or STREET-FOOD scene (not an isolated plate). Prefer visually iconic OUTDOOR locations.
- Each highlight opens DIFFERENTLY. NEVER use empty hype: "paradise on earth", "will blow your mind", "best kept secret", "more interesting than people think".
- Every highlight must be TRUE for {single}: real landmarks, dishes, and historical events only. Volatile info (prices, hours) stays qualitative ("cheap street food", "go early").
- **INTERNAL CONSISTENCY (CRITICAL)**: any number/year must agree across highlights, title, and comment. No contradictions.
- Intro (hook narration): 12 words or FEWER, a concrete inviting reason to visit, NEVER start with "Did you know".
- Outro: 4-7 words CTA, one of: "Save this for your trip." / "Which city next?" / "Follow for a new city daily."
- Hook (overlay text): short, like "{display} in 60s".
- **angle**: one of {ANGLES}. **format**: one of {FORMATS}.
- **fact_keywords**: 5 background-video search keywords, ONE per highlight IN ORDER, each containing "{single}" and describing a FILMABLE OUTDOOR visual (a landmark / street / district / castle / garden / market / riverside), NOT an abstract concept or indoor exhibit.
- **Title (CRITICAL CTR — pick ONE viral pattern, max 70 chars, must include "{single}", NO emoji)**:
  (a) In 60s: "{display} in 60 Seconds"
  (b) Why visit: "Why {display} Should Be Your Next Trip"
  (c) The one thing: "The One Thing You Can't Miss in {display}"
  (d) Reasons: "5 Reasons {display} Will Steal Your Heart"
  (e) Hidden side: "The {display} Locals Don't Tell Tourists About"
  (f) First timer: "First Time in {display}? Start Here"
  NEVER use "Amazing Facts About", "Facts You Didn't Know". Do NOT start with "You Won't Believe" / "Wait Until You See" / "Did You Know". Hype ok, but no false claim.
- Description: 2 sentences + 3-5 hashtags (no emoji).
- **comment** (reply-magnet, 8-20 words): hot take / forced choice WITHIN {display} only / what-if / personal challenge / confession bait / fill-in-the-blank.
  **TOPIC LOCK**: comment MUST be about {display} ONLY. NEVER mention any OTHER city (no "{display} or Tokyo?" cross-city comments). 0-1 emoji max. NEVER "Which one surprised you?" or "Tag a friend".

Output STRICT JSON: {{
  "title_en": "...",
  "description_en": "... #shorts #travel #{single.lower().replace(' ', '')} #wander",
  "hook": "{display} in 60s",
  "intro": "Here's why {display} belongs on your travel list.",
  "facts": ["highlight1 (20-30 words)", "highlight2", "highlight3", "highlight4", "highlight5"],
  "fact_keywords": ["{single} visual1", "{single} visual2", "{single} visual3", "{single} visual4", "{single} visual5"],
  "angle": "one_of_the_angle_labels",
  "format": "one_of_the_format_labels",
  "outro": "Save this for your trip.",
  "comment": "..."
}}"""

    # Rate limit 429 회피: 최대 3회 retry + exponential backoff (1s → 5s → 15s)
    import time as _time
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    last_err = None
    d = None
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=SCRIPT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.9,
                max_tokens=1100,
            )
            d = json.loads(resp.choices[0].message.content)
            break
        except Exception as e:
            last_err = e
            es = str(e).lower()
            if "rate_limit" in es or "429" in es or "tpm" in es:
                wait = [5, 15, 30][attempt]
                log.warning(f"expand_legacy_to_rich rate limit → {wait}s 후 재시도 (attempt {attempt+1}/3)")
                _time.sleep(wait)
            else:
                raise
    if d is None:
        raise last_err or RuntimeError("expand_legacy_to_rich GPT 호출 3회 모두 실패")

    facts = d.get("facts") or []
    if not isinstance(facts, list) or len(facts) < 3:
        raise RuntimeError(f"GPT facts 부족: {facts}")
    facts = [str(f).strip() for f in facts[:5] if str(f).strip()]

    title_en = (d.get("title_en") or f"{display} in 60 Seconds").strip()[:95]
    if _has_bad(title_en, _BAD_TITLE_EN):
        title_en = f"{display} in 60 Seconds"
    _generic = ["amazing facts about", "wild facts about", "interesting facts about",
                "fascinating facts about", "facts you didn't know"]
    if any(g in title_en.lower() for g in _generic):
        log.warning(f"legacy expand: generic title → CTR-friendly fallback: {title_en}")
        title_en = f"Why You Need to See {display}"

    comment = (d.get("comment") or "").strip()
    if not comment or len(comment) < 10:
        comment = f"What's the first thing you'd do in {display}?"
    comment = comment[:280]

    angle = _normalize_angle(d.get("angle"))
    fmt = str(d.get("format") or "").strip().lower().replace(" ", "_")
    if fmt not in FORMATS:
        fmt = random.choice(FORMATS)
    fact_kw = _validate_fact_keywords(d.get("fact_keywords"), single,
                                      meta.get("pexels_keywords") or [], n=len(facts))

    rich = {
        "title_en": title_en,
        "description_en": (d.get("description_en") or f"A 60-second guide to {where}.")[:4900],
        "intro": (d.get("intro") or f"Here's why {display} belongs on your travel list.").strip(),
        "facts": facts,
        "fact_keywords": fact_kw,
        "angle": angle,
        "format": fmt,
        "outro": (d.get("outro") or "Save this for your trip.").strip(),
        "hook": (d.get("hook") or f"{display} in 60s").strip()[:40],
        "comment": comment,
    }
    # 사실 근거(위키) + 적대적 팩트체크 → 내부 일관성 검증 → (F) fact_keywords 최종 확정
    review_input = {**rich, "display": display, "single": single, "country": country,
                    "category": meta.get("category", ""), "_kw_facts": list(facts)}
    # fallback 경로도 SOURCE 기반 재작성을 태운다. 예전엔 이 경로만 reground를 건너뛰어
    # 훅이 공허한 문장으로 나갔다. country를 grounding context에 넘겨 동명 도시 오매칭 완화.
    _src = fetch_source_for(display, single, meta.get("category", ""), country=country)
    review_input = reground_facts(review_input, _src)
    review_input = _apply_grounded_factcheck(review_input, _src)
    review_input = verify_consistency(review_input)
    review_input = _finalize_fact_keywords(review_input, meta.get("pexels_keywords") or [])
    rich["intro"] = review_input.get("intro", rich["intro"])
    rich["facts"] = review_input.get("facts", rich["facts"])
    rich["fact_keywords"] = review_input.get("fact_keywords", rich["fact_keywords"])
    rich["comment"] = review_input.get("comment", rich["comment"])
    return rich


def _legacy_meta(animal_key: str) -> dict:
    """fallback: ANIMALS 사전(=CITIES 도시) + 기본 하이라이트 템플릿.

    cities.json이 비어 있으면(CITIES={}) 호출 자체가 불가하므로, 방어적으로 최소 도시 메타를 만든다.
    """
    cfg = ANIMALS.get(animal_key)
    if cfg is None:
        # 2026-07-17 fix: unknown key가 조용히 로스터 첫 도시(Paris)로 대체되어
        # 엉뚱한 도시 영상이 생성/발행되던 문제 → 정규화 시도 후 실패 시 명시적 에러.
        resolved = _resolve_roster_key(animal_key)
        cfg = ANIMALS.get(resolved)
        if cfg is None:
            raise ValueError(
                f"_legacy_meta: 로스터에 없는 도시 키 '{animal_key}' — "
                f"cities.json의 city_key를 확인하세요 (예: kyoto_japan)")
        animal_key = resolved
    display = cfg.get("display") or animal_key.replace("_", " ").title()
    single = cfg.get("single") or display
    country = cfg.get("country", "")
    landmark_lock = cfg.get("landmark_lock", "")
    confusion_pair = cfg.get("confusion_pair", [])
    pexels_keywords = cfg.get("pexels_keywords") or [single, f"{single} city", f"{single} travel",
                                                      f"{single} landmark", f"{single} street"]
    category = cfg.get("category") or "historic_ancient"
    if category not in _VALID_CATEGORIES:
        category = "historic_ancient"
    where = f"{display}, {country}".strip().strip(",") if country else display
    return {
        "animal_key": animal_key,
        "display": display,
        "single": single,
        "country": country,
        "category": category,
        "landmark_lock": landmark_lock,
        "confusion_pair": confusion_pair,
        "pexels_keywords": pexels_keywords,
        "fact_keywords": _validate_fact_keywords([], single, pexels_keywords, n=5),
        "angle": "",
        "format": random.choice(FORMATS),
        "title_en": f"{display} in 60 Seconds",
        "description_en": f"A 60-second guide to {where}. \n\n#shorts #travel #wander",
        "hook": f"{display} in 60s",
        "intro": f"Here's why {display} belongs on your travel list.",
        "facts": [
            f"{display} rewards slow wandering — skip the checklist for an afternoon and let its quiet streets, small squares, and everyday corners tell you the real story.",
            f"Start your day early in {display}: soft morning light, calmer streets, and the first fresh local food make it feel like a completely different city.",
            f"Half the fun of {display} is getting a little lost — the alleys between the landmarks are where you'll find the moments you actually remember.",
            f"{display} keeps its best moments just off the main square — walk ten extra minutes past the crowds and the city quietly opens up for you.",
            f"Come hungry to {display}: the local specialties, the markets, and the small family-run places are honestly half the reason to make the trip.",
        ],
        "outro": "Save this for your trip.",
        "comment": f"What's the first thing you'd do in {display}?",
        "tags": ["shorts", "travel", single, country, "things to do",
                 "city guide", "wander", "travel tips", "60 seconds"],
    }


def make_meta(animal_key: str) -> dict:
    """legacy compat: ANIMALS(=CITIES) 키 받아 기본 도시 메타 + GPT 보강 (main.py 미사용)."""
    cfg = ANIMALS[animal_key]
    display = cfg["display"]
    single = cfg["single"]
    country = cfg.get("country", "")
    where = f"{display}, {country}".strip().strip(",") if country else display

    fallback = {
        "title_en": f"{display} in 60 Seconds",
        "description_en": (
            f"A 60-second guide to {where}. "
            f"\n\n#shorts #travel #wander #{single.lower().replace(' ', '')}"
        ),
        "intro": f"Here's why {display} belongs on your travel list.",
        "facts": [
            f"{display} rewards slow wandering — its streets tell the real story.",
            f"Start early in {display}: soft light, fewer crowds, and the best food.",
            f"Half the fun of {display} is getting a little lost between landmarks.",
            f"Come hungry to {display} — the local specialties are half the reason to visit.",
        ],
        "outro": "Save this for your trip.",
        "hook": f"{display} in 60s",
        "tags": [
            "shorts", "travel", single, country, "things to do",
            "city guide", "wander", "travel tips", "60 seconds",
        ],
    }

    if not OPENAI_API_KEY:
        return fallback

    prompt = f"""You are a viral YouTube Shorts scriptwriter for the travel channel "Wander 60s".

Generate a ~60-second city introduction about {where} ({single}). Structure:
- 0~3s: hook line (a concrete, inviting reason to visit — do NOT start with "Did you know")
- 3~52s: 5 travel highlights (one-liners), each ~8-11s spoken (20-30 words each), mix of landmark/food/activity/history
- 52~60s: outro CTA

HARD RULES:
- Each highlight MUST be 20-30 words, warm first/second-person travel tone, TRUE for {single}.
- Volatile info (prices, hours) stays qualitative; any number/year must be real.
- Plain spoken English, no jargon, no emoji.
- Title: catchy, max 90 chars, includes "{display}". NEVER use "compilation", "best of".
- Description: 2 sentences + 5 hashtags (no emoji).
- Hook (on-screen overlay text): short, e.g. "{display} in 60s".
- Intro: 8-14 words, a concrete inviting reason to visit.
- Outro: 4~7 words CTA, one of: "Save this for your trip." / "Which city next?" / "Follow for a new city daily."

Output STRICT JSON only:
{{
  "title_en": "...",
  "description_en": "...",
  "hook": "...",
  "intro": "...",
  "facts": ["highlight1 (20-30 words)", "highlight2", "highlight3", "highlight4", "highlight5"],
  "outro": "..."
}}"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=SCRIPT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.9,
            max_tokens=900,
        )
        d = json.loads(resp.choices[0].message.content)
    except Exception as e:
        log.warning(f"GPT 메타 실패 fallback: {e}")
        return fallback

    title_en = (d.get("title_en") or fallback["title_en"])[:95]
    if _has_bad(title_en, _BAD_TITLE_EN):
        log.warning(f"bad word in title fallback: {title_en}")
        title_en = fallback["title_en"]

    facts = d.get("facts") or []
    if not isinstance(facts, list) or len(facts) < 3:
        log.warning(f"facts 부족 fallback: {facts}")
        facts = fallback["facts"]
    facts = [str(f).strip() for f in facts[:5]]

    return {
        "title_en": title_en,
        "description_en": (d.get("description_en") or fallback["description_en"])[:4900],
        "intro": (d.get("intro") or fallback["intro"]).strip(),
        "facts": facts,
        "outro": (d.get("outro") or fallback["outro"]).strip(),
        "hook": (d.get("hook") or fallback["hook"]).strip()[:40],
        "tags": fallback["tags"],
    }
