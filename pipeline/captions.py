"""CC 자막 트랙 — en SRT(TTS 단어 타임스탬프) + 7개국 번역(타임코드 유지) + captions().insert.

동물봇/우주봇 공통 모듈 (도메인 무관, 동일 코드 — byte-identical 유지).

흐름:
1. build_en_srt: TTS word cues를 자연 구(≤7단어/≤3.4s) 단위로 묶어 영어 SRT. 타이밍 단조증가·겹침 없음.
2. translate_srt: GPT로 각 자막 텍스트만 번역(타임코드 그대로). ko/ja/es/pt/de/fr/hi 7개국.
3. upload_captions: 업로드된 영상에 captions().insert로 언어별 트랙 등록.

**fail-soft**: 자막 생성/업로드 실패는 영상 발행을 절대 실패시키지 않는다. 언어별 try/except +
성공 언어 수 로그("captions: N/M langs uploaded") → 워크플로 텔레그램에서 표기.

**불변식(자막 트랙)**:
- 겹침 금지: cue[i].end <= cue[i+1].start - EPS (다음 cue 이전 종료 — 최우선). 공간 없으면 merge.
- 최종 영상 길이(total_dur)로 clamp/drop: 영상이 59.7s로 잘려도 자막이 그 뒤로 삐져나오지 않음.
- CC 카운트 분모 고정: 번역 실패 언어 포함 항상 en+7 = EXPECTED_CC_LANGS(8) 기준.

**예산/타임아웃**: generate_all/upload_captions는 deadline(monotonic)·call_timeout 인자로
전체 예산·호출별 타임아웃을 받는다(main.py가 CC 단계 전체 180s 예산 지정). 예산 초과 시 남은 언어 skip.

토큰 scope: captions.insert는 youtube.force-ssl scope 요구. upload_captions가 업로드 전
youtube_upload.has_caption_scope()로 현재 토큰 scope 확인 → 없으면 자막만 skip(영상 정상).
"""
import json
import logging
import os
import re
import socket
import time

from config import META_MODEL, OPENAI_API_KEY

log = logging.getLogger("shortsbot.captions")

# en은 원본. 나머지 7개국 번역.
CAPTION_LANGS = ["ko", "ja", "es", "pt", "de", "fr", "hi"]
LANG_NAMES = {
    "en": "English", "ko": "Korean", "ja": "Japanese", "es": "Spanish",
    "pt": "Portuguese", "de": "German", "fr": "French", "hi": "Hindi",
}

# CC 카운트 분모(고정): en + 7개국 = 8. 번역 실패 언어가 있어도 항상 "N/8"로 로그/알림.
EXPECTED_CC_LANGS = 1 + len(CAPTION_LANGS)

# compose의 pad_between과 일치해야 자막 타이밍이 영상 음성과 맞음.
DEFAULT_PAD_BETWEEN = 0.18

# 자막 최소 지속(가독성) / 타이밍 비교 허용오차(겹침 방지).
MIN_CUE_DUR = 0.4
CUE_EPS = 1e-3

# CC 단계 호출별 기본 타임아웃(초) — OpenAI 번역·captions.insert 각각.
DEFAULT_CALL_TIMEOUT = 30.0
# 남은 예산이 이 값(초) 미만이면 새 호출을 시작하지 않고 skip (성공 가망 없는 호출 방지).
MIN_CALL_BUDGET = 2.0


def _budget_timeout(deadline: float, call_timeout: float, floor: float = MIN_CALL_BUDGET):
    """deadline까지 남은 시간으로 이번 호출 timeout을 축소해 180s 예산을 hard cap에 가깝게 유지.

    반환: 이번 호출에 쓸 timeout(초). deadline이 None이면 call_timeout 그대로.
    남은 시간이 floor 미만이면 None(→ 호출 skip 신호)."""
    if deadline is None:
        return call_timeout
    remaining = deadline - time.monotonic()
    if remaining <= floor:
        return None
    return min(call_timeout, remaining)


def _fmt_ts(sec: float) -> str:
    """초 → SRT 타임코드 HH:MM:SS,mmm."""
    if sec < 0:
        sec = 0.0
    ms_total = int(round(sec * 1000))
    h, rem = divmod(ms_total, 3600 * 1000)
    m, rem = divmod(rem, 60 * 1000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _cue_from_words(words: list, line_start: float, max_words: int = 7, max_dur: float = 3.4) -> list:
    """word cues(상대시간)를 자연 구(≤max_words, ≤max_dur초) 단위로 묶어 절대시간 cue 리스트."""
    cues = []
    i = 0
    n = len(words)
    while i < n:
        chunk = [words[i]]
        i += 1
        while i < n and len(chunk) < max_words:
            if (words[i]["end"] - chunk[0]["start"]) > max_dur:
                break
            chunk.append(words[i])
            i += 1
        text = " ".join(str(w["text"]) for w in chunk).strip()
        cues.append({"start": line_start + float(chunk[0]["start"]),
                     "end": line_start + float(chunk[-1]["end"]), "text": text})
    return cues


def _enforce_srt_invariants(cues: list, min_dur: float = MIN_CUE_DUR, eps: float = CUE_EPS) -> list:
    """cue 리스트를 단조 증가 + 겹침 없는 트랙으로 정규화.

    불변식(우선순위 순):
    1) 겹침 금지: out[i].end <= out[i+1].start - eps  (다음 cue 이전 종료 — 최우선)
    2) 단조: out[i].start >= out[i-1].end + eps
    3) 최소 지속 min_dur 보장 — 단, (1)을 깨지 않는 범위에서만.

    현재 cue 시작이 다음 cue 시작(-eps) 이상이라 공간이 없으면 현재 cue를 다음 cue에 병합
    (텍스트를 앞에 붙이고 시작은 더 이른 쪽). 겹침을 만들지 않는 것이 최소지속보다 우선한다.
    """
    work = [{"start": float(c["start"]), "end": float(c["end"]),
             "text": str(c.get("text", "")).strip()}
            for c in cues if str(c.get("text", "")).strip()]
    out = []
    i = 0
    n = len(work)
    while i < n:
        c = work[i]
        start = c["start"]
        if out:  # 단조: 직전 cue 종료 이후 시작
            start = max(start, out[-1]["end"] + eps)
        next_start = work[i + 1]["start"] if i + 1 < n else None
        if next_start is not None and start >= next_start - eps:
            # 공간 없음 → 다음 cue에 병합 (텍스트 이어붙임, 시작은 더 이른 쪽)
            nxt = work[i + 1]
            nxt["start"] = min(nxt["start"], c["start"])
            nxt["text"] = (c["text"] + " " + nxt["text"]).strip()
            i += 1
            continue
        end = max(c["end"], start + min_dur)  # 최소 지속 시도
        if next_start is not None:
            end = min(end, next_start - eps)   # 겹침 금지(최우선)
        if end <= start:
            end = start + eps
        out.append({"start": start, "end": end, "text": c["text"]})
        i += 1
    return out


def _clamp_to_total(cues: list, total_dur: float, eps: float = CUE_EPS) -> list:
    """최종 영상 길이(total_dur)로 cue clamp/drop. 시작이 영상 끝 이후면 drop, 종료가 넘치면 clamp."""
    if not total_dur or total_dur <= 0:
        return cues
    out = []
    for c in cues:
        if c["start"] >= total_dur - eps:
            continue  # 영상 끝 이후 시작 → 화면에 못 뜸(drop)
        end = min(c["end"], total_dur)
        if end <= c["start"]:
            continue  # clamp 후 길이 소멸 → drop
        out.append({"start": c["start"], "end": end, "text": c["text"]})
    return out


def build_cues(tts_lines: list, pad_between: float = DEFAULT_PAD_BETWEEN,
               total_dur: float = None) -> list:
    """tts_lines → cue 리스트 (절대 시간, 단조 증가·겹침 없음). words 없으면 라인당 1 cue.

    total_dur 주어지면 최종 영상 길이로 clamp/drop (영상이 잘려도 자막이 안 삐져나옴)."""
    cues = []
    cursor = 0.0
    for line in tts_lines:
        words = line.get("words") or []
        dur = float(line.get("duration") or 0.0)
        if words:
            cues.extend(_cue_from_words(words, cursor))
        else:
            txt = (line.get("text") or "").strip()
            if txt:
                cues.append({"start": cursor, "end": cursor + max(0.6, dur), "text": txt})
        cursor += dur + pad_between

    cues = [c for c in cues if str(c.get("text", "")).strip()]
    cues = _enforce_srt_invariants(cues)
    if total_dur is not None:
        cues = _clamp_to_total(cues, total_dur)
    return cues


def cues_to_srt(cues: list) -> str:
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{_fmt_ts(c['start'])} --> {_fmt_ts(c['end'])}\n{c['text']}\n")
    return "\n".join(out).strip() + "\n"


def build_en_srt(tts_lines: list, out_path: str, pad_between: float = DEFAULT_PAD_BETWEEN,
                 total_dur: float = None):
    """영어 SRT 파일 생성. 반환: (out_path, cues)."""
    cues = build_cues(tts_lines, pad_between, total_dur=total_dur)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(cues_to_srt(cues))
    return out_path, cues


def parse_srt(text: str) -> list:
    """SRT → [{tc, text}] (타임코드 문자열 그대로 유지)."""
    out = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [l for l in block.splitlines() if l.strip() != ""]
        ti = next((k for k, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        out.append({"tc": lines[ti].strip(), "text": " ".join(lines[ti + 1:]).strip()})
    return out


def translate_srt(en_srt_text: str, lang: str, timeout: float = DEFAULT_CALL_TIMEOUT) -> str:
    """GPT로 SRT의 자막 텍스트만 번역, 타임코드 유지. 실패/개수불일치 시 None."""
    if not OPENAI_API_KEY:
        return None
    cues = parse_srt(en_srt_text)
    texts = [c["text"] for c in cues]
    if not texts:
        return None
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=timeout)
    prompt = (
        f"Translate each numbered English subtitle line to {LANG_NAMES.get(lang, lang)} ({lang}). "
        f"Natural spoken style, concise (fit a short-video caption). Keep the SAME order and EXACT count "
        f"({len(texts)} lines). Do NOT merge, split, add, or drop lines. Translate text only.\n\n"
        + "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        + f"\n\nOutput STRICT JSON: {{\"lines\": [ {len(texts)} translated strings, same order ]}}"
    )
    try:
        resp = client.chat.completions.create(
            model=META_MODEL, messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}, temperature=0.3, max_tokens=1800,
            timeout=timeout)
        tr = json.loads(resp.choices[0].message.content).get("lines") or []
    except Exception as e:
        log.warning(f"[{lang}] 자막 번역 호출 실패: {e}")
        return None
    if not isinstance(tr, list) or len(tr) != len(texts):
        log.warning(f"[{lang}] 번역 라인 수 불일치 ({len(tr)}!={len(texts)}) → 스킵")
        return None
    out = []
    for n, (c, t) in enumerate(zip(cues, tr), 1):
        txt = str(t).strip() or c["text"]
        out.append(f"{n}\n{c['tc']}\n{txt}\n")
    return "\n".join(out).strip() + "\n"


def generate_all(tts_lines: list, work_dir: str,
                 pad_between: float = DEFAULT_PAD_BETWEEN, langs: list = None,
                 total_dur: float = None, deadline: float = None,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT) -> dict:
    """en + 각 언어 SRT 파일 생성. 반환: {lang: srt_path}. fail-soft(언어별).

    total_dur: 최종 영상 길이 — cue를 이 길이로 clamp/drop.
    deadline: time.monotonic() 기준 마감. 초과 시 남은 언어 번역 skip.
    call_timeout: OpenAI 번역 호출별 타임아웃(초)."""
    langs = langs if langs is not None else CAPTION_LANGS
    os.makedirs(work_dir, exist_ok=True)
    result = {}
    try:
        en_path = os.path.join(work_dir, "cc_en.srt")
        build_en_srt(tts_lines, en_path, pad_between, total_dur=total_dur)
        result["en"] = en_path
    except Exception as e:
        log.warning(f"en SRT 생성 실패 → 자막 전체 스킵 (영상 정상): {e}")
        return {}
    en_text = open(en_path, encoding="utf-8").read()
    for lang in langs:
        eff = _budget_timeout(deadline, call_timeout)
        if eff is None:
            log.warning(f"CC 예산 소진 → 남은 언어 번역 skip (마지막: {lang})")
            break
        try:
            srt = translate_srt(en_text, lang, timeout=eff)
            if srt:
                p = os.path.join(work_dir, f"cc_{lang}.srt")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(srt)
                result[lang] = p
        except Exception as e:
            log.warning(f"[{lang}] SRT 생성 실패 (계속): {e}")
    log.info(f"CC SRT 생성: {len(result)}개 언어 ({','.join(result.keys())})")
    return result


def upload_captions(video_id: str, srt_by_lang: dict, expected_total: int = None,
                    deadline: float = None, call_timeout: float = DEFAULT_CALL_TIMEOUT) -> dict:
    """captions().insert로 언어별 CC 트랙 등록. fail-soft(언어별 try/except).
    반환: {lang: bool}. 요약 로그 'captions: N/M langs uploaded' — 분모 M은 expected_total(기본 8).

    deadline: time.monotonic() 마감. 초과 시 남은 언어 skip.
    call_timeout: captions.insert 호출별 소켓 타임아웃(초)."""
    if expected_total is None:
        expected_total = EXPECTED_CC_LANGS
    if not video_id or not srt_by_lang:
        log.info(f"captions: 0/{expected_total} langs uploaded")
        return {}
    try:
        from pipeline import youtube_upload
        from googleapiclient.http import MediaFileUpload
        # 토큰 scope 런타임 검증: youtube.force-ssl 없으면 captions만 skip (영상 정상, fail-soft)
        if not youtube_upload.has_caption_scope():
            log.warning("captions: YT token scope migration required "
                        "(youtube.force-ssl 미포함) → 자막 스킵 (영상 정상)")
            log.info(f"captions: 0/{expected_total} langs uploaded")
            return {lang: False for lang in srt_by_lang}
        svc = youtube_upload.get_service()
    except Exception as e:
        log.warning(f"captions: YouTube 서비스 준비 실패 → 자막 스킵 (영상 정상): {e}")
        log.info(f"captions: 0/{expected_total} langs uploaded")
        return {lang: False for lang in srt_by_lang}
    ok = {}
    old_timeout = socket.getdefaulttimeout()
    try:
        for lang, path in srt_by_lang.items():
            eff = _budget_timeout(deadline, call_timeout)  # 남은 예산으로 소켓 타임아웃 축소
            if eff is None:
                log.warning(f"CC 예산 소진 → 남은 언어 업로드 skip (마지막: {lang})")
                ok[lang] = False
                continue
            socket.setdefaulttimeout(eff)  # captions.insert 호출별 상한 (finally에서 복구)
            try:
                if not path or not os.path.exists(path):
                    ok[lang] = False
                    continue
                body = {"snippet": {"videoId": video_id, "language": lang,
                                    "name": LANG_NAMES.get(lang, lang), "isDraft": False}}
                media = MediaFileUpload(path, mimetype="application/octet-stream", resumable=False)
                svc.captions().insert(part="snippet", body=body, media_body=media).execute()
                ok[lang] = True
                log.info(f"caption track uploaded: {lang}")
            except Exception as e:
                ok[lang] = False
                log.warning(f"[{lang}] caption 업로드 실패 (영상 정상): {e}")
    finally:
        socket.setdefaulttimeout(old_timeout)
    n = sum(1 for v in ok.values() if v)
    log.info(f"captions: {n}/{expected_total} langs uploaded")
    return ok
