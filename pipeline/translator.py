"""title/description을 7개국 언어로 번역 → YouTube localizations."""
import json
import logging

from config import META_MODEL, OPENAI_API_KEY, no_openai_mode

log = logging.getLogger("travelbot.translator")

LOCALIZATION_LANGS = [
    ("ko", "Korean"),
    ("ja", "Japanese"),
    ("es", "Spanish"),
    ("pt", "Portuguese (Brazilian)"),
    ("de", "German"),
    ("fr", "French"),
    ("hi", "Hindi"),
]


def translate_for_youtube(title_en: str, description_en: str) -> dict:
    if no_openai_mode():
        raise RuntimeError("NO_OPENAI 모드에서 translate_for_youtube 호출 — 대본 localizations 를 써야 한다")
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY 없음 — localizations 스킵")
        return {}

    lang_list = "\n".join(f"- {c}: {n}" for c, n in LOCALIZATION_LANGS)
    prompt = f"""You are a YouTube Shorts localization expert for a travel channel about world cities and destinations. Rewrite the English title for each language as a NATIVE-VIRAL title — NOT a literal translation. Each language has its own click-through patterns.

Languages:
{lang_list}

ENGLISH TITLE (source mood/topic):
{title_en}

ENGLISH DESCRIPTION:
{description_en}

PER-LANGUAGE VIRAL TITLE STYLE (CRITICAL — match the platform feel). Keep the SAME city as the English title; these are just style examples:

- **ko (Korean)**: 짧고 후크감 강한 한국 여행 YouTube Shorts 톤. 패턴 예:
  "교토 60초 여행 브이로그" / "다음 여행지는 무조건 여기;;" /
  "리스본에서 이건 꼭 해야 함" / "현지인만 아는 교토 골목" /
  "이 도시 저장 안 하면 후회함" / "첫 유럽 여행이면 여기부터".
  Max 60 chars. NO emoji. 호기심 갭 + 의문형/추천 활용. 도시명 유지.

- **ja (Japanese)**: ショート動画らしい強いフック(旅行). 例:
  "京都を60秒で旅する" / "次の旅行はここで決まり…" /
  "地元民しか知らない京都の路地". 60文字以内. 絵文字なし. 都市名は維持.

- **es (Spanish)**: titulares cortos de viaje con gancho. 예: "Kioto en 60 segundos" / "Tu próximo viaje tiene que ser aquí". Max 70 chars. Sin emojis.

- **pt (Portuguese-BR)**: títulos curtos de viagem. 예: "Kyoto em 60 segundos" / "Seu próximo destino tem que ser esse". Max 70 chars. Sem emojis.

- **de (German)**: kurze Reise-Hook-Titel. "Kyoto in 60 Sekunden" / "Diese Stadt muss auf deine Reiseliste". Max 75 chars. Keine Emojis.

- **fr (French)**: titres de voyage courts et accrocheurs. "Kyoto en 60 secondes" / "Ta prochaine destination, c'est ici". Max 75 chars. Sans emoji.

- **hi (Hindi)**: छोटे आकर्षक यात्रा शीर्षक. "60 सेकंड में क्योटो" / "आपकी अगली ट्रिप यहीं होनी चाहिए". Max 80 chars.

DO NOT translate literally. Always rewrite with a curiosity gap or clear travel promise, keeping the SAME city. Description: natural translation, keep 3-5 hashtags at end (translate or keep English where idiomatic).

Output STRICT JSON only (no markdown):
{{
  "ko": {{"title":"...", "description":"..."}},
  "ja": {{"title":"...", "description":"..."}},
  "es": {{...}},
  "pt": {{...}},
  "de": {{...}},
  "fr": {{...}},
  "hi": {{...}}
}}"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=META_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=2500,
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception as e:
        log.warning(f"번역 실패 — locales 스킵: {e}")
        return {}

    out = {}
    for code, _ in LOCALIZATION_LANGS:
        e = data.get(code) or {}
        title = (e.get("title") or "").strip()
        desc = (e.get("description") or "").strip()
        if title:
            out[code] = {"title": title[:95], "description": desc[:4900]}
    log.info(f"localizations 완료: {','.join(out.keys())}")
    return out
