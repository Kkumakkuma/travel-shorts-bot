# -*- coding: utf-8 -*-
"""제목·태그 위생 회귀 테스트 (2026-08-16 실측 기반).

여기 케이스는 전부 **실제로 발행된 영상**에서 나온 것이다. YouTube Data API 로 동물/우주/여행
3채널 발행분을 조회해 결함을 확인하고, 같은 결함이 다시 나가지 않도록 고정한다.

태그 위생 테스트는 3봇 공통(youtube_upload._sanitize_tags).
제목 테스트는 동물/우주 전용 — 여행봇은 도시 소개형이라 제목 파이프라인이 다르므로 자동 skip.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.youtube_upload import _sanitize_tags  # noqa: E402

try:  # 동물/우주 전용 — 여행봇에는 없다
    from pipeline.animals import (
        _BANNED_OPENERS,
        _BANNED_OPENER_HINT,
        _fix_topic_case,
        _starts_with_banned_opener,
        _title_has_agreement_error,
        _title_needs_fix,
    )
    HAS_TITLE_GUARDS = True
except ImportError:  # pragma: no cover
    HAS_TITLE_GUARDS = False


titles_only = unittest.skipUnless(HAS_TITLE_GUARDS, "제목 가드는 동물/우주 봇 전용")


@titles_only
class TestBannedOpener(unittest.TestCase):
    def test_wait_openers_blocked(self):
        """실측: "Wait…" 오프너 23편 중앙값 221회 vs 그 외 49편 326회 → 차단 대상."""
        for t in ("Wait, a Supernova Can Outshine an Entire Galaxy",
                  "Wait, Pangolins Eat 70 Million Ants A Year",
                  "Wait, Tapirs Use Their Noses to Snorkel?",
                  "Wait Until You See Andromeda's Fate",
                  "wait, chameleons' eyes can see in two directions"):
            self.assertTrue(_starts_with_banned_opener(t), t)

    def test_wait_punctuation_variants_blocked(self):
        """codex 지적 — 문자열 prefix 목록만으로는 구두점 변형이 샌다."""
        for t in ("Wait… Saturn's Rings Are Vanishing",
                  "Wait! Octopuses Have Nine Brains",
                  "Wait: Pangolins Eat 70 Million Ants",
                  "Wait — Auroras Are Not Just Green",
                  "Wait?! Harpy Eagles Are That Big?",
                  "Wait"):
            self.assertTrue(_starts_with_banned_opener(t), t)

    def test_legacy_banned_openers_still_blocked(self):
        for t in ("You Won't Believe What Penguins Do at Night",
                  "You won’t believe how smart dolphins really are",
                  "Did You Know Penguin Dads Keep Eggs Warm for 2 Months?"):
            self.assertTrue(_starts_with_banned_opener(t), t)

    def test_good_titles_not_blocked(self):
        """실측 상위 성과 제목은 통과해야 한다 (오탐 = 발행 품질 저하)."""
        for t in ("The Truth About Harpy Eagles' Enormous Claws!",
                  "Stop Scrolling — Hummingbirds' Superpowers Are Unbelievable!",
                  "The Mantis Shrimp's Punch is Faster than a Bullet!",
                  "Mysteries Surrounding the Galactic Center's Gigantic Black Hole",
                  "Mercury: The Planet That's Always Too Hot or Too Cold",
                  # "wait" 로 시작하는 정상 단어는 막지 않는다 (단어 경계가 없으므로)
                  "Waiting 76 Years for Halley's Comet",
                  "Waits of 200 Years Between Eruptions",
                  "Waited 40 Years to Photograph This Owl"):
            self.assertFalse(_starts_with_banned_opener(t), t)

    def test_hint_covers_every_banned_entry(self):
        """지시문과 차단 목록의 드리프트 방지 — 과거에 실제로 어긋나 있었다.
        새 opener 계열을 목록에만 추가하고 GPT 지시문을 안 고치면 여기서 걸린다."""
        hint = _BANNED_OPENER_HINT.lower().replace("’", "'").replace("'", "")
        for b in _BANNED_OPENERS:
            first = b.strip().split()[0].strip(",'\"").replace("'", "")
            self.assertIn(first, hint, f"{b!r} 계열이 GPT 지시문에 없음")


@titles_only
class TestAgreementError(unittest.TestCase):
    def test_real_incident(self):
        """실측 사고: 2026-07-21 우주 채널 발행분 (프롬프트 예시가 비문이었다)."""
        self.assertTrue(_title_has_agreement_error(
            "How Does Exoplanets Survive Such Extreme Conditions?",
            display="Exoplanets", single="exoplanet"))

    def test_other_singular_verbs(self):
        cases = (("Why Is Auroras So Hard to Predict?", "Auroras", "aurora"),
                 ("How Has Black Holes Changed Physics?", "Black Holes", "black hole"),
                 ("What Was Comets Made Of?", "Comets", "comet"))
        for t, d, s in cases:
            self.assertTrue(_title_has_agreement_error(t, display=d, single=s), t)

    def test_correct_grammar_passes(self):
        cases = (("How Do Exoplanets Survive Such Extreme Conditions?", "Exoplanets", "exoplanet"),
                 ("Why Are Auroras Green?", "Auroras", "aurora"),
                 ("How Does Saturn Keep Its Rings?", "Saturn", "Saturn"),
                 ("How Does Mars Stay So Cold?", "Mars", "Mars"),
                 ("The Truth About Harpy Eagles' Enormous Claws!", "Harpy Eagles", "harpy eagle"))
        for t, d, s in cases:
            self.assertFalse(_title_has_agreement_error(t, display=d, single=s), t)

    def test_singular_display_never_flagged(self):
        """Mars/Glass 처럼 s 로 끝나는 단수형은 검사 대상이 아니다."""
        self.assertFalse(_title_has_agreement_error(
            "Why Is Mars So Red?", display="Mars", single="Mars"))
        self.assertFalse(_title_has_agreement_error(
            "Why Is Sea Glass So Rare?", display="Sea Glass", single="sea glass"))

    def test_empty_inputs_safe(self):
        self.assertFalse(_title_has_agreement_error("", "", ""))
        self.assertFalse(_title_has_agreement_error(None, None, None))

    def test_wired_into_title_needs_fix(self):
        """subjects=(single, display) 순서로 실제 배선됐는지 확인."""
        self.assertTrue(_title_needs_fix(
            "How Does Exoplanets Survive Such Extreme Conditions?",
            [], ("exoplanet", "Exoplanets")))
        self.assertFalse(_title_needs_fix(
            "How Do Exoplanets Survive Such Extreme Conditions?",
            [], ("exoplanet", "Exoplanets")))


@titles_only
class TestTopicCase(unittest.TestCase):
    def test_real_incident(self):
        """실측 사고: 2026-07-14 우주 채널 "The aurora Detail Most People Get Wrong"."""
        self.assertEqual(
            _fix_topic_case("The aurora Detail Most People Get Wrong", "Auroras", "aurora"),
            "The Aurora Detail Most People Get Wrong")

    def test_plural_display_lowercased(self):
        self.assertEqual(
            _fix_topic_case("Why do auroras glow green?", "Auroras", "aurora"),
            "Why do Auroras glow green?")

    def test_multiword_display(self):
        self.assertEqual(
            _fix_topic_case("What black holes really eat", "Black Holes", "black hole"),
            "What Black Holes really eat")

    def test_does_not_touch_substrings(self):
        """sunset 안의 sun 을 건드리면 안 된다."""
        self.assertEqual(
            _fix_topic_case("A sunset on Mars", "The Sun", "sun"),
            "A sunset on Mars")

    def test_already_correct_unchanged(self):
        cases = (("The Truth About Harpy Eagles' Enormous Claws!", "Harpy Eagles", "harpy eagle"),
                 ("Mercury: The Planet That's Always Too Hot or Too Cold", "Mercury", "Mercury"),
                 ("The Mantis Shrimp's Punch is Faster than a Bullet!", "Mantis Shrimps", "mantis shrimp"))
        for t, d, s in cases:
            self.assertEqual(_fix_topic_case(t, d, s), t)

    def test_first_letter_capitalized(self):
        self.assertEqual(_fix_topic_case("the truth about it", "", ""), "The truth about it")

    def test_empty_safe(self):
        self.assertEqual(_fix_topic_case("", "Auroras", "aurora"), "")
        self.assertEqual(_fix_topic_case(None, "Auroras", "aurora"), "")
        self.assertEqual(_fix_topic_case("Auroras Glow", None, None), "Auroras Glow")


@titles_only
class TestRetitleFallback(unittest.TestCase):
    """codex 지적 — 결정적 폴백 후보가 단수 display 에서 비문이면 안 된다.

    _title_has_agreement_error 는 오탐 회피를 위해 '단수 주어 + 복수 동사' 방향을 일부러
    보지 않는다. 그래서 폴백 후보 자체가 단수/복수 양쪽에서 성립해야 한다."""

    def _fallback(self, display, single, prev):
        import pipeline.animals as A
        saved = A.OPENAI_API_KEY
        A.OPENAI_API_KEY = ""  # GPT 경로 우회 → 결정적 폴백만 검사
        try:
            return A._retitle_unique(display, single, "Wait, something", prev)
        finally:
            A.OPENAI_API_KEY = saved

    def test_always_returns_nonempty_string(self):
        """발행이 막히면 안 되므로 어떤 입력에도 문자열을 돌려줘야 한다."""
        for d, s in (("Saturn", "Saturn"), ("Harpy Eagles", "harpy eagle"),
                     ("Auroras", "aurora"), ("The Sun", "sun")):
            out = self._fallback(d, s, [])
            self.assertIsInstance(out, str)
            self.assertTrue(out.strip(), f"{d}: 빈 제목")
            self.assertLessEqual(len(out), 95)

    def test_no_subject_verb_break_on_singular_display(self):
        """"What Saturn Actually Do ..." 같은 비문이 나오면 안 된다."""
        for d, s in (("Saturn", "Saturn"), ("Mercury", "Mercury"), ("The Sun", "sun")):
            out = self._fallback(d, s, [])
            self.assertNotRegex(out, rf"{d}\s+(Do|Are|Have)\b")

    def test_later_candidates_also_safe(self):
        """앞 후보가 이력과 겹쳐 뒤 후보로 밀려도 비문/금지opener 가 나오면 안 된다."""
        prev = [("The Truth About Saturn Nobody Explains", "Saturn"),
                ("Saturn: The Part Nobody Mentions", "Saturn")]
        out = self._fallback("Saturn", "Saturn", prev)
        self.assertTrue(out.strip())
        self.assertNotRegex(out, r"Saturn\s+(Do|Are|Have)\b")
        self.assertFalse(_starts_with_banned_opener(out), out)

    def test_fallback_never_starts_with_banned_opener(self):
        for d, s in (("Saturn", "Saturn"), ("Harpy Eagles", "harpy eagle")):
            self.assertFalse(_starts_with_banned_opener(self._fallback(d, s, [])))


class TestTagHygiene(unittest.TestCase):
    """3봇 공통 — youtube_upload._sanitize_tags."""

    def test_real_snake_case_leak(self):
        """실측: 발행분 태그에서 슬러그 8건 확인 (long_tail, bird_of_prey …)."""
        out = _sanitize_tags(["animal facts", "animals", "did you know", "lemur",
                              "long_tail", "shorts", "trivia", "wildlife"])
        self.assertIn("long tail", out)
        self.assertNotIn("long_tail", out)
        for t in out:
            self.assertNotIn("_", t)

    def test_real_case_duplicate(self):
        """실측: ["Supernova", …, "supernova"] 가 한 영상에 동시 등장(슬롯 낭비)."""
        out = _sanitize_tags(["Supernova", "astronomy", "cosmos", "did you know",
                              "nasa", "shorts", "space", "supernova", "trivia"])
        self.assertEqual(len(out), 8)
        self.assertEqual(len([t for t in out if t.lower() == "supernova"]), 1)
        self.assertEqual(out[0], "Supernova")  # 첫 등장 표기 보존

    def test_slug_dedup_interaction(self):
        out = _sanitize_tags(["bird_of_prey", "bird of prey", "harpy_eagle"])
        self.assertEqual(out, ["bird of prey", "harpy eagle"])

    def test_limits_preserved(self):
        """기존 한도 가드가 그대로 살아 있어야 한다 (invalidTags 400 → 발행 실패 방지)."""
        self.assertEqual(_sanitize_tags(["a" * 200])[0], "a" * 100)
        self.assertLessEqual(len(_sanitize_tags([f"tag{i}" for i in range(60)])), 30)
        self.assertEqual(_sanitize_tags(["ok", None, 5, "  ", "fine"]), ["ok", "fine"])
        self.assertEqual(_sanitize_tags(None), [])
        self.assertEqual(_sanitize_tags([]), [])
        total = sum(len(t) + (2 if " " in t else 0) + 1 for t in _sanitize_tags(["x y " * 30] * 40))
        self.assertLessEqual(total, 500)

    def test_no_tag_lost_unnecessarily(self):
        """정상 태그는 하나도 잃지 않아야 한다 (검색 노출 저하 방지)."""
        tags = ["shorts", "space", "astronomy", "cosmos", "trivia",
                "did you know", "galactic center", "milky way center", "nasa"]
        self.assertEqual(_sanitize_tags(tags), tags)
        travel = ["asia", "city guide", "japan", "sapporo", "shorts",
                  "things to do", "travel", "travel tips", "wander"]
        self.assertEqual(_sanitize_tags(travel), travel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
