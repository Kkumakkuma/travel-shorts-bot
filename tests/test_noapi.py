"""NO_OPENAI 잠금 경로(2026-09-03) — 자막 라인 분할·대본 검증·OpenAI 무호출 방어."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import captions  # noqa: E402


def _line(text, n_words, word_dur=0.4):
    words = [{"text": w, "start": i * word_dur, "end": (i + 1) * word_dur}
             for i, w in enumerate(text.split()[:n_words])]
    return {"text": text, "duration": n_words * word_dur, "words": words}


def test_split_balanced_words_and_chars():
    assert captions._split_balanced("a b c d e f", 1) == ["a b c d e f"]
    p = captions._split_balanced("one two three, four five six seven eight", 2)
    assert p == ["one two three,", "four five six seven eight"]  # 구두점 경계 선호
    j = captions._split_balanced("今夜爆発する。昼でも見える。", 2)
    assert j == ["今夜爆発する。", "昼でも見える。"]
    assert captions._split_balanced("a b", 5) == ["a", "b"]  # 토큰 부족 → 있는 만큼
    assert captions._split_balanced("", 3) == []


def test_generate_from_lines_groups_and_skip(tmp_path):
    lines = [_line("this star could explode any night now and you would see it", 12),
             _line("look for the red shoulder of orion tonight", 8)]
    tr = {"ko": ["이 별은 오늘 밤에도 터질 수 있고, 대낮에도 보입니다", "오늘 밤 오리온의 붉은 어깨를 찾아보세요"],
          "ja": ["줄 수가 하나뿐"],
          "es": ["ok", ""]}
    out = captions.generate_from_lines(lines, str(tmp_path), tr, total_dur=8.0)
    assert set(out) == {"en", "ko"}  # ja(줄 수 불일치)·es(빈 줄) 스킵, en 은 항상
    en = captions.parse_srt(open(out["en"], encoding="utf-8").read())
    ko = captions.parse_srt(open(out["ko"], encoding="utf-8").read())
    assert len(en) == 4  # 12단어 → ≤7단어 cue 2개, 8단어 → 2개
    # 번역은 라인당 최대 2조각: 첫 라인은 en cue 2개를 그대로 덮고, 시작·끝 타임코드가 en 과 같다
    assert len(ko) == 4
    assert ko[0]["tc"].split(" --> ")[0] == en[0]["tc"].split(" --> ")[0]
    assert ko[-1]["tc"].split(" --> ")[1] == en[-1]["tc"].split(" --> ")[1]
    assert ko[0]["text"].endswith(",") and all(c["text"].strip() for c in ko)


def test_generate_from_lines_caps_pieces_per_line(tmp_path):
    lines = [_line(" ".join(f"w{i}" for i in range(28)), 28)]  # cue 4개짜리 한 라인
    out = captions.generate_from_lines(lines, str(tmp_path), {"ko": ["앞 부분 문장이다. 뒤 부분 문장이다."]})
    en = captions.parse_srt(open(out["en"], encoding="utf-8").read())
    ko = captions.parse_srt(open(out["ko"], encoding="utf-8").read())
    assert len(en) == 4 and len(ko) == 2  # 번역은 2조각으로 묶여 깜빡임 없음
    assert ko[0]["tc"].split(" --> ")[1] == en[1]["tc"].split(" --> ")[1]  # 조각1 = cue 1~2 구간
    assert ko[1]["tc"].split(" --> ")[0] == en[2]["tc"].split(" --> ")[0]  # 조각2 = cue 3~4 구간


def test_translate_srt_blocked_in_no_openai(monkeypatch):
    monkeypatch.setenv("NO_OPENAI", "1")
    with pytest.raises(RuntimeError):
        captions.translate_srt("1\n00:00:00,000 --> 00:00:01,000\nhi\n", "ko")


BASE = {
    "author": "claude", "animal_key": "lisbon_portugal", "display": "Lisbon", "single": "Lisbon",
    "category": "city", "angle": "a", "format": "f", "hook": "H", "intro": "i", "outro": "o",
    "title_en": "t", "description_en": "d", "comment": "c", "tags": ["shorts"],
    "facts": ["f1", "f2", "f3", "f4", "f5"], "fact_keywords": ["k1", "k2", "k3", "k4", "k5"],
    "pexels_keywords": ["p1", "p2", "p3", "p4", "p5"],
    "country": "Portugal", "landmark_lock": "Lisbon — Tram 28",  # 여행봇 필수 키
}
LOCS = {c: {"title": "t", "description": "d"} for c in ("ko", "ja", "es", "pt", "de", "fr", "hi")}


def test_validate_queue_meta_no_openai(monkeypatch):
    import main
    monkeypatch.setenv("NO_OPENAI", "1")
    r = main.validate_queue_meta(dict(BASE), "001_lisbon_portugal.json")
    assert r and all(x.startswith(main.QUEUE_UNLOCKED_PREFIX) for x in r)  # 잠금 정보만 없음 → pending 유지 사유
    ok = dict(BASE, clips=[[1, 2], [3], [4, 5], [6, 7], [8]], localizations=LOCS,
              captions={"ko": ["1", "2", "3", "4", "5", "6", "7"]})
    assert main.validate_queue_meta(json.loads(json.dumps(ok)), "001_lisbon_portugal.json") == []
    dup = dict(ok, clips=[[1, 2], [2], [4, 5], [6, 7], [8]])
    assert any("중복" in x for x in main.validate_queue_meta(dup, "001_lisbon_portugal.json"))
    bad_caps = dict(ok, captions={"ko": ["1", "2"]})
    assert any("captions" in x for x in main.validate_queue_meta(bad_caps, "001_lisbon_portugal.json"))
    bad_clip = dict(ok, clips=[[1, "2"], [3], [4, 5], [6, 7], [8]])
    r = main.validate_queue_meta(bad_clip, "001_lisbon_portugal.json")
    assert r and not any(x.startswith(main.QUEUE_UNLOCKED_PREFIX) for x in r)  # 형식 오류는 failed 사유
    monkeypatch.setenv("NO_OPENAI", "0")
    assert main.validate_queue_meta(dict(BASE), "001_lisbon_portugal.json") == []  # 모드 꺼지면 선택 키


def test_locked_backgrounds_uses_ids_only(monkeypatch, tmp_path):
    import main
    from pipeline import pexels, verify_visual
    calls = []

    def fake_fetch(cid, out_dir):
        calls.append(str(cid))
        if str(cid) == "3":
            return None  # 다운로드 실패 슬롯 → 이웃 차용
        p = os.path.join(out_dir, f"pexels_{cid}.mp4")
        open(p, "wb").write(b"x")
        return p

    def boom(*a, **k):
        raise AssertionError("vision 호출됨")

    monkeypatch.setattr(pexels, "fetch_by_id", fake_fetch)
    monkeypatch.setattr(verify_visual, "verify_animal", boom)
    meta = {"author": "claude", "facts": ["a", "b", "c", "d"], "clips": [[1, 2], [3], [4], [5, 6]], "country": "X", "landmark_lock": "Y"}
    groups, ids = main.fetch_verified_backgrounds(meta, str(tmp_path), exclude_pexels_ids=[5])
    assert len(groups) == 4 and [len(g) for g in groups] == [2, 1, 1, 2]
    assert groups[1][0][1] in ("1", "4")  # 빈 슬롯은 이웃 클립 차용
    assert ids == ["1", "2", "4", "5", "6"] and calls == ["1", "2", "3", "4", "5", "6"]
