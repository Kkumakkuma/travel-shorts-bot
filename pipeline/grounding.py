"""사실 근거(grounding) — Wikipedia에서 topic 발췌를 가져와 GPT 프롬프트에 SOURCE TEXT로 주입.

동물봇/우주봇 공통 모듈 (동일 코드). API 키 불필요 (en.wikipedia.org 퍼블릭 REST/Action API).

용도: 시청자가 자막 fact를 거짓이라 지적하는 사고(예: 토성 위성 82 vs 79 모순) 재발 방지.
- 픽된 topic의 위키 요약 + 본문 발췌(explaintext)를 3000~6000자 확보.
- 이 SOURCE TEXT를 생성/팩트체크 프롬프트에 첨부해 "SOURCE에서 확인 가능하거나 널리 확립된
  과학 상식만" 쓰도록 강제.
- fetch 실패/timeout은 안전하게 "" 반환 → 호출 측은 보수 모드(팩트체크만)로 폴백.
"""
import logging

import requests

log = logging.getLogger("shortsbot.grounding")

_SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_EXTRACT_API = "https://en.wikipedia.org/w/api.php"
_SEARCH_API = "https://en.wikipedia.org/w/api.php"
_UA = "ShortsTriviaBot/1.0 (educational fact-grounding; contact via youtube)"
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


def _search_title(query: str, timeout: float = 8.0) -> str:
    """위키 검색으로 가장 근접한 문서 제목 1개. 실패 시 ""."""
    try:
        r = requests.get(
            _SEARCH_API,
            params={
                "action": "query", "list": "search", "srsearch": query,
                "srlimit": 1, "format": "json", "srnamespace": 0,
            },
            headers=_HEADERS, timeout=timeout,
        )
        r.raise_for_status()
        hits = r.json().get("query", {}).get("search", [])
        if hits:
            return hits[0].get("title", "") or ""
    except Exception as e:
        log.warning(f"wiki search 실패 ({query!r}): {e}")
    return ""


def _summary(title: str, timeout: float = 8.0) -> str:
    """REST summary API로 요약 문단. 실패 시 ""."""
    try:
        url = _SUMMARY_API.format(title=requests.utils.quote(title.replace(" ", "_"), safe=""))
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            return ""
        d = r.json()
        if d.get("type") == "disambiguation":
            return ""
        return (d.get("extract") or "").strip()
    except Exception as e:
        log.warning(f"wiki summary 실패 ({title!r}): {e}")
        return ""


def _extract(title: str, max_chars: int, timeout: float = 12.0) -> str:
    """Action API explaintext로 본문 발췌 (여러 섹션). 호출 측에서 max_chars로 자름.

    exchars로 자르면 intro만(~1500자) 와서 근거 부족 → 팩트체커가 참인 주장도
    over-reject. 전체 plaintext를 받아 호출 측에서 앞 max_chars로 절단.
    """
    try:
        r = requests.get(
            _EXTRACT_API,
            params={
                "action": "query", "prop": "extracts", "explaintext": 1,
                "titles": title, "format": "json", "redirects": 1,
            },
            headers=_HEADERS, timeout=timeout,
        )
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", {})
        for _pid, page in pages.items():
            txt = (page.get("extract") or "").strip()
            if txt:
                return txt
    except Exception as e:
        log.warning(f"wiki extract 실패 ({title!r}): {e}")
    return ""


def fetch_source(display: str, single: str = "", context: str = "",
                 category: str = "", min_chars: int = 3000, max_chars: int = 6000,
                 must_include=None) -> str:
    """topic의 위키 발췌 SOURCE TEXT 반환 (약 3000~6000자). 실패/애매 시 "".

    context: 도메인 맥락 키워드(예: "space astronomy planet moon ... NASA").
    category: 토픽 분류(예: "moon"/"planet"/"mission"/"mammal") — 검색 정확도 향상용
    (예: "Titan moon" → 'Titan (moon)', "Apollo mission" → 'Apollo program').
    must_include: 필수 포함 토큰(str 또는 list of str) — 텍스트에 하나도 없으면 off-topic.
    제네릭 컨텍스트('city' 등)는 모든 도시 문서에 존재해 any() 게이트가 못 되므로,
    여행 도메인의 동명 도시(Tripoli 레바논 vs 리비아) 방어는 국가명을 여기로 강제한다.

    동음이의(single="Apollo"→그리스 신, "Titan"/"Europa"→위성 vs 신화) 문서에 잘못
    grounding되는 것을 막는다. 검색 우선순위: display/single 직접(on-topic) →
    "topic category" 검색 → "topic 도메인맥락" 검색. 전부 off-topic이면 "" (보수 모드).
    """
    ctx_kw = [w.lower() for w in (context or "").split() if len(w) > 3]
    if isinstance(must_include, str):
        must_include = [must_include] if must_include.strip() else []
    must_kw = [m.strip().lower() for m in (must_include or []) if m and m.strip()]

    def _on_topic(txt: str) -> bool:
        low = (txt or "").lower()
        # 필수 토큰(국가 등)이 하나도 없으면 제네릭 매칭과 무관하게 off-topic
        if must_kw and not any(m in low for m in must_kw):
            return False
        if not ctx_kw:
            return True
        return any(k in low for k in ctx_kw)

    candidates = []
    for c in (display, single):
        c = (c or "").strip()
        if c and c not in candidates:
            candidates.append(c)

    best_title = ""
    # 1) 직접 제목 매칭 — on-topic일 때만 채택 (동음이의 오매칭 차단)
    for c in candidates:
        s = _summary(c)
        if s and _on_topic(s):
            best_title = c
            break
    # 2) 실패/off-topic → "topic+category"(정확) → "topic+도메인맥락"(폴백) 순 검색.
    #    검색 결과도 on-topic 검증. category를 넣으면 'Titan (moon)' 같은 정확한 문서를 잡음.
    if not best_title:
        cat = (category or "").strip()
        queries = []
        for c in candidates:
            if cat:
                queries.append(f"{c} {cat}".strip())
        for c in candidates:
            queries.append(f"{c} {context}".strip())
        seen = set()
        for q in queries:
            if not q or q in seen:
                continue
            seen.add(q)
            found = _search_title(q)
            if found:
                s = _summary(found)
                if s and _on_topic(s):
                    best_title = found
                    break

    if not best_title:
        log.warning(f"grounding: on-topic 위키 문서 못 찾음 (display={display!r}) — 보수 모드 폴백")
        return ""

    summary = _summary(best_title)
    extract = _extract(best_title, max_chars=max_chars)
    # 본문 발췌(extract)는 intro를 이미 포함하므로 그것을 우선 사용.
    text = (extract or summary or "").strip()
    # 최종 방어: 발췌 전체가 off-topic이면 잘못된 근거 → 차라리 SOURCE 없음(보수 모드)
    if not _on_topic(text):
        log.warning(f"grounding: 발췌 off-topic (title={best_title!r}) — 잘못된 근거 회피, 보수 모드")
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
    if len(text) < 120:
        log.warning(f"grounding: 발췌 너무 짧음 ({len(text)}자, title={best_title!r}) — 보수 모드")
        return ""
    log.info(f"grounding: '{best_title}' 위키 발췌 {len(text)}자 확보")
    return text
