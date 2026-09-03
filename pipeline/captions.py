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

from config import META_MODEL, OPENAI_API_KEY, no_openai_mode

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
# CC 예산 중 자막 업로드(captions.insert 8회) 몫으로 남겨 두는 시간(초).
CC_UPLOAD_RESERVE = 60.0


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


# 번호를 붙여 돌려받은 줄을 원본 인덱스에 맞춰 넣는 최소 커버리지.
# 이 아래로 빠지면 영어가 너무 많이 섞여 자막 품질이 떨어지므로 그 언어는 포기한다.
_TR_MIN_COVERAGE = 0.8


def _tr_call(client, prompt, timeout):
    resp = client.chat.completions.create(
        model=META_MODEL, messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}, temperature=0.3, max_tokens=1800,
        timeout=timeout)
    return json.loads(resp.choices[0].message.content)


# 한 번에 30줄을 통째로 넘기면 모델이 줄을 재분배해 번역이 몇 칸씩 밀린다(실측).
# 문장 경계에서 끊어 보내면 밀림이 그 문장 안에 갇힌다. 한국어처럼 어순이 크게 다른
# 언어는 문장 중간을 잘라 주면 반드시 재분배가 일어나므로 이 경계가 중요하다.
_TR_SENT_PER_CALL = 1
_TR_MAX_GROUP = 6           # 문장부호가 없는 대본이 와도 한 덩어리로 뭉치지 않게
_TR_MAX_CALLS = 24          # 언어당 호출 상한(쪼개기 폭주 방지)
_SENT_END = (".", "!", "?", "…", "\"", "'")


def _tr_groups(texts: list) -> list:
    """문장 끝을 기준으로 자막 줄 인덱스를 묶는다.

    마침표가 하나도 없는 대본이 들어오면 전부 한 덩어리가 돼 쪼개기 호출 상한에
    걸려 뒷부분이 통째로 빠진다. 그래서 줄 수 상한도 함께 둔다.
    """
    groups, cur, sents = [], [], 0
    for i, t in enumerate(texts):
        cur.append(i)
        if str(t).rstrip().endswith(_SENT_END):
            sents += 1
            if sents >= _TR_SENT_PER_CALL:
                groups.append(cur)
                cur, sents = [], 0
                continue
        if len(cur) >= _TR_MAX_GROUP:
            groups.append(cur)
            cur, sents = [], 0
    if cur:
        groups.append(cur)
    return groups


def _tr_chunk(client, lang, texts, idxs, timeout, state):
    """idxs 구간을 번역해 state["picked"] 에 채운다.

    모델은 줄을 합치거나 쪼개서 개수를 자주 어긴다. 이때 번호(n)를 같이 달라고 해도
    자기 출력 기준으로 매기는 경우가 있어 엉뚱한 줄에 번역이 붙는다(2026-08-20 실측:
    ko 15번 자리에 22번 문장의 번역이 들어갔다). 그래서 번호를 신뢰하지 않고,
    개수가 어긋나면 구간을 반으로 쪼개 다시 물어본다. 1줄까지 내려가면 그 답은
    그 줄의 번역일 수밖에 없으므로 정렬이 깨질 여지가 없다.
    """
    if not idxs or state["calls"] >= _TR_MAX_CALLS or time.monotonic() > state["until"]:
        return
    lines = [texts[i] for i in idxs]
    numbered = "\n".join(f"{k+1}. {t}" for k, t in enumerate(lines))
    prompt = (
        f"Translate each numbered English subtitle line to {LANG_NAMES.get(lang, lang)} ({lang}). "
        f"Natural spoken style, concise (fit a short-video caption). Keep the SAME order and EXACT "
        f"count ({len(lines)} lines). Do NOT merge, split, add, or drop lines. Translate text only."
        f"\n\n{numbered}"
        f"\n\nOutput STRICT JSON: {{\"lines\": [ {len(lines)} translated strings, same order ]}}")
    state["calls"] += 1
    try:
        got = _tr_call(client, prompt, timeout).get("lines")
    except Exception as e:
        log.warning(f"[{lang}] 번역 호출 실패({len(lines)}줄): {e}")
        return
    got = [str(x).strip() for x in got] if isinstance(got, list) else []

    if len(got) == len(idxs):
        blanks = []
        for i, t in zip(idxs, got):
            if t:
                state["picked"][i] = t
            else:
                blanks.append(i)
        # 개수는 맞는데 특정 칸만 빈 문자열로 오는 경우가 있다(옆 줄에 뭉쳐 번역).
        # 그 줄만 따로 물어본다. 1줄짜리 요청에서 또 비면 그냥 포기(무한 재귀 방지).
        if len(idxs) > 1:
            for i in blanks:
                _tr_chunk(client, lang, texts, [i], timeout, state)
        return
    if len(idxs) == 1:
        # 한 줄만 물었으므로 무엇이 오든 그 줄의 번역이다. 쪼개 왔으면 이어 붙인다.
        t = " ".join(x for x in got if x).strip()
        if t:
            state["picked"][idxs[0]] = t
        return
    mid = len(idxs) // 2
    _tr_chunk(client, lang, texts, idxs[:mid], timeout, state)
    _tr_chunk(client, lang, texts, idxs[mid:], timeout, state)


def translate_srt(en_srt_text: str, lang: str, timeout: float = DEFAULT_CALL_TIMEOUT,
                  until: float = None) -> str:
    """GPT로 SRT의 자막 텍스트만 번역, 타임코드 유지.

    모델이 줄을 합치거나 빠뜨려 개수가 어긋나는 일이 잦다(2026-08-20 실측: 8개 언어 중
    6개가 이 사유로 통째 스킵돼 en·ja만 올라갔다). 개수만 보고 버리면 자막이 사라지므로
    _tr_chunk 로 구간을 쪼개 가며 채운다. 못 채운 줄은 영어 원문을 쓰되 커버리지가
    낮으면 그 언어는 포기한다(엉뚱한 줄에 붙는 것보다 없는 편이 낫다).
    """
    if no_openai_mode():
        raise RuntimeError("NO_OPENAI 모드에서 translate_srt 호출 — 대본 captions(generate_from_lines) 를 써야 한다")
    if not OPENAI_API_KEY:
        return None
    cues = parse_srt(en_srt_text)
    texts = [c["text"] for c in cues]
    if not texts:
        return None
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=timeout)

    wall = time.monotonic() + max(timeout, 20.0) * 3
    if until is not None:
        wall = min(wall, until)
    state = {"picked": {}, "calls": 0, "until": wall}
    for g in _tr_groups(texts):
        _tr_chunk(client, lang, texts, g, timeout, state)
    picked = state["picked"]

    if not picked:
        log.warning(f"[{lang}] 번역 결과 없음 → 스킵")
        return None
    coverage = len(picked) / len(texts)
    if coverage < _TR_MIN_COVERAGE:
        log.warning(f"[{lang}] 번역 커버리지 부족 ({len(picked)}/{len(texts)}) → 스킵")
        return None
    if coverage < 1.0:
        log.info(f"[{lang}] {len(texts) - len(picked)}줄은 영어 원문 유지 ({len(picked)}/{len(texts)})")

    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{c['tc']}\n{picked.get(n - 1) or c['text']}\n")
    return "\n".join(out).strip() + "\n"


MAX_PIECES_PER_LINE = 2  # 번역 자막: TTS 라인당 최대 조각 수(generate_from_lines)


def _split_balanced(text: str, k: int) -> list:
    """번역 문장 1개를 k 조각으로 나눈다 — 길이 균형 + 공백·구두점 경계 우선(2026-09-03 NO_OPENAI).

    공백이 있는 언어는 단어 단위, 없는 언어(ja 등)는 글자 단위. 빈 조각은 만들지 않는다.
    토큰이 k 개보다 적으면 있는 만큼만 돌려준다(남는 cue 는 호출 측이 빈 텍스트로 버림).
    """
    text = (text or "").strip()
    if not text:
        return []
    if k <= 1:
        return [text]
    if " " in text:
        toks, joiner = text.split(" "), " "
    else:
        toks, joiner = list(text), ""
    toks = [t for t in toks if t != ""]
    if len(toks) <= k:
        return toks
    cum, acc = [], 0
    for t in toks:
        acc += len(t) + len(joiner)
        cum.append(acc)
    total = cum[-1]
    punct = tuple("、。,.!?;:，．！？")
    window = total * 0.25  # 목표점 ±25% 안의 구두점 경계는 균형점보다 우선(문장 중간 절단 방지)
    cuts, prev = [], 0
    for j in range(1, k):
        target = total * j / k
        best, best_score = None, None
        # 토큰 i 뒤에서 자른다. 뒤에 남는 컷(k-j)마다 토큰이 최소 1개는 남도록 상한을 둔다.
        for i in range(prev + 1, len(toks) - (k - j) + 1):
            score = abs(cum[i - 1] - target)
            if toks[i - 1].endswith(punct) and score <= window:
                score -= total  # 창 안의 구두점 경계가 항상 이긴다(그중 목표점에 가장 가까운 것)
            if best_score is None or score < best_score:
                best, best_score = i, score
        if best is None:
            break
        cuts.append(best)
        prev = best
    pieces, start = [], 0
    for c in cuts + [len(toks)]:
        pieces.append(joiner.join(toks[start:c]).strip())
        start = c
    return [p for p in pieces if p]


def build_cues_by_line(tts_lines: list, pad_between: float = DEFAULT_PAD_BETWEEN) -> list:
    """build_cues 와 같은 타이밍 규칙으로, 라인 경계를 보존한 cue 목록([[cue,...] per line], 불변식 적용 전)."""
    per_line, cursor = [], 0.0
    for line in tts_lines:
        words = line.get("words") or []
        dur = float(line.get("duration") or 0.0)
        cues = []
        if words:
            cues = _cue_from_words(words, cursor)
        else:
            txt = (line.get("text") or "").strip()
            if txt:
                cues = [{"start": cursor, "end": cursor + max(0.6, dur), "text": txt}]
        per_line.append(cues)
        cursor += dur + pad_between
    return per_line


def generate_from_lines(tts_lines: list, work_dir: str, line_translations: dict,
                        pad_between: float = DEFAULT_PAD_BETWEEN, total_dur: float = None) -> dict:
    """OpenAI 없이 CC 생성(2026-09-03 NO_OPENAI). 반환 {lang: srt_path} — en 은 항상.

    en 은 TTS 단어 타이밍으로 만들고, 다른 언어는 대본이 준 라인별 번역(줄 수 = tts_lines 수)을
    그 라인이 만든 cue 개수만큼 길이 균형으로 나눠 **같은 타임코드**에 붙인다. 줄 수가 안 맞거나
    빈 줄이 있으면 그 언어만 스킵(경고). 타이밍이 언어마다 같으므로 병합·clamp 결과도 같다.
    """
    os.makedirs(work_dir, exist_ok=True)
    result = {}
    per_line = build_cues_by_line(tts_lines, pad_between)

    def _finish(cues):
        cues = [c for c in cues if str(c.get("text", "")).strip()]
        cues = _enforce_srt_invariants(cues)
        if total_dur is not None:
            cues = _clamp_to_total(cues, total_dur)
        return cues

    en_cues = _finish([c for cs in per_line for c in cs])
    if not en_cues:
        log.warning("en cue 없음 → 자막 전체 스킵 (영상 정상)")
        return {}
    en_path = os.path.join(work_dir, "cc_en.srt")
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(cues_to_srt(en_cues))
    result["en"] = en_path

    n_lines = len(tts_lines)
    for lang in CAPTION_LANGS:
        lines = (line_translations or {}).get(lang)
        if lines is None:
            continue
        if not isinstance(lines, list) or len(lines) != n_lines or not all(
                isinstance(t, str) and t.strip() for t in lines):
            got = len(lines) if isinstance(lines, list) else "?"
            log.warning(f"[{lang}] 라인 번역 줄 수/내용 불일치({got}/{n_lines}) → 스킵")
            continue
        cues = []
        for li, lcues in enumerate(per_line):
            if not lcues:
                continue
            # 어순이 다른 언어(ko/ja/hi)는 en cue 단위로 잘게 나누면 음성과 뜻이 어긋나고 짧은 조각이
            # 깜빡인다. 라인당 최대 MAX_PIECES_PER_LINE 조각으로 묶고, 각 조각은 en cue 묶음의 구간을 통째로 쓴다.
            g = min(len(lcues), MAX_PIECES_PER_LINE)
            pieces = _split_balanced(lines[li], g)
            g = min(g, len(pieces))
            if g <= 0:
                continue
            bounds = [round(len(lcues) * j / g) for j in range(g + 1)]
            for j in range(g):
                span = lcues[bounds[j]:bounds[j + 1]] or [lcues[min(bounds[j], len(lcues) - 1)]]
                cues.append({"start": span[0]["start"], "end": span[-1]["end"], "text": pieces[j]})
        cues = _finish(cues)
        if not cues:
            continue
        p = os.path.join(work_dir, f"cc_{lang}.srt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(cues_to_srt(cues))
        result[lang] = p
    log.info(f"CC SRT 생성(무API): {len(result)}개 언어 ({','.join(result.keys())})")
    return result


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

    # 언어는 서로 독립이다. 조각을 잘게 끊으면서 순차로 돌면 180초 예산을 넘기므로
    # 언어끼리는 병렬로 돌린다(언어별 fail-soft는 그대로).
    # 번역이 예산을 끝까지 먹으면 업로드할 시간이 남지 않는다. 마감의 일부는 업로드 몫.
    gen_until = None if deadline is None else deadline - CC_UPLOAD_RESERVE

    def _one(lang):
        eff = _budget_timeout(deadline, call_timeout)
        if eff is None:
            log.warning(f"[{lang}] CC 예산 소진 → 번역 skip")
            return lang, None
        try:
            return lang, translate_srt(en_text, lang, timeout=eff, until=gen_until)
        except Exception as e:
            log.warning(f"[{lang}] SRT 생성 실패 (계속): {e}")
            return lang, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(langs)))) as ex:
        done = list(ex.map(_one, langs))
    for lang, srt in done:
        if not srt:
            continue
        p = os.path.join(work_dir, f"cc_{lang}.srt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(srt)
        result[lang] = p
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
