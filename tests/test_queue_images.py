"""2026-10-18 '100만 조회' 최적화 — 사실당 잠금 클립 1~4개, 대본 images(AI 장면 그림), image_to_video .jpg 원본 보존."""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BASE = {
    "author": "claude", "animal_key": "lisbon_portugal", "display": "Lisbon", "single": "Lisbon",
    "category": "city", "country": "Portugal", "landmark_lock": "Lisbon — Tram 28", "angle": "a", "format": "f", "hook": "H", "intro": "i", "outro": "o",
    "title_en": "t", "description_en": "d", "comment": "c", "tags": ["shorts"],
    "facts": ["f1", "f2", "f3", "f4", "f5"], "fact_keywords": ["k1", "k2", "k3", "k4", "k5"],
    "pexels_keywords": ["p1", "p2", "p3", "p4", "p5"],
    "localizations": {c: {"title": "t", "description": "d"} for c in ("ko", "ja", "es", "pt", "de", "fr", "hi")},
    "captions": {"ko": ["1", "2", "3", "4", "5", "6", "7"]},
}


def _ok(**kw):
    return json.loads(json.dumps(dict(BASE, clips=[[1, 2, 3, 4], [5], [6, 7, 8], [9, 10], [12]], **kw)))


def test_clip_cap_four(monkeypatch):
    import main
    monkeypatch.setenv("NO_OPENAI", "1")
    assert main.QUEUE_MAX_CLIPS_PER_SLOT == 4
    assert main.validate_queue_meta(_ok(), "001_lisbon_portugal.json") == []
    five = _ok()
    five["clips"][0] = [1, 2, 3, 4, 11]
    assert any("1~4" in x for x in main.validate_queue_meta(five, "001_lisbon_portugal.json"))


def test_images_validation(monkeypatch):
    import main
    monkeypatch.setenv("NO_OPENAI", "1")
    asset_dir = os.path.join(main.BASE_DIR, "queue", "assets")
    os.makedirs(asset_dir, exist_ok=True)
    real = os.path.join(asset_dir, "_pytest_scene.jpg")
    open(real, "wb").write(b"x")
    try:
        good = _ok(images=[{"fact_index": 0, "cut_index": 1, "path": "queue/assets/_pytest_scene.jpg", "required": True}])
        assert main.validate_queue_meta(good, "001_lisbon_portugal.json") == []
        bad = _ok(images=[{"fact_index": 5, "path": "queue/assets/_pytest_scene.jpg"},     # 사실 번호 범위 밖(여행 = 사실 5개)
                          {"fact_index": 0, "path": "../secret.jpg"},                        # 저장소 밖
                          {"fact_index": 2, "cut_index": -1, "path": "queue/assets/_pytest_scene.jpg", "required": "yes"}])
        r = main.validate_queue_meta(bad, "001_lisbon_portugal.json")
        assert any("fact_index" in x for x in r) and any("queue/assets/" in x for x in r)
        assert any("cut_index" in x for x in r) and any("required" in x for x in r)
        assert not any(x.startswith(main.QUEUE_UNLOCKED_PREFIX) for x in r)  # 형식 오류 = failed 사유
        # 파일 없음: 선택 그림 = 오류 아님(실행 때 건너뜀), 필수 그림 = pending 유지 사유
        assert main.validate_queue_meta(_ok(images=[{"fact_index": 1, "path": "queue/assets/none.jpg"}]), "001_lisbon_portugal.json") == []
        r = main.validate_queue_meta(_ok(images=[{"fact_index": 1, "path": "queue/assets/none.jpg", "required": True}]), "001_lisbon_portugal.json")
        assert r and all(x.startswith(main.QUEUE_UNLOCKED_PREFIX) for x in r)
    finally:
        os.remove(real)


def _fake_env(monkeypatch, tmp_path, fail_image=False):
    import main
    from pipeline import image_to_video, pexels, verify_visual

    def fake_fetch(cid, out_dir):
        p = os.path.join(out_dir, f"pexels_{cid}.mp4")
        open(p, "wb").write(b"x")
        return p

    def fake_i2v(src, out, duration=10.0, **k):
        if fail_image:
            raise RuntimeError("변환 실패")
        open(out, "wb").write(b"v")
        return out

    def boom(*a, **k):
        raise AssertionError("vision 호출됨")

    monkeypatch.setattr(pexels, "fetch_by_id", fake_fetch)
    monkeypatch.setattr(image_to_video, "png_to_video", fake_i2v)
    monkeypatch.setattr(verify_visual, "verify_animal", boom, raising=False)
    return main


def test_locked_four_clips_and_image_insert(monkeypatch, tmp_path):
    main = _fake_env(monkeypatch, tmp_path)
    meta = {"author": "claude", "facts": ["a", "b", "c", "d", "e"], "clips": [[1, 2, 3, 4], [5], [6, 7], [8], [9]],
            "images": [{"fact_index": 0, "cut_index": 1, "path": "queue/assets/scene.jpg", "required": True},
                       {"fact_index": 3, "cut_index": 9, "path": "queue/assets/end.png"}]}
    groups, ids = main.fetch_verified_backgrounds(meta, str(tmp_path))
    assert [len(g) for g in groups] == [5, 1, 2, 2, 1]
    assert groups[0][1][1] == "img:scene.jpg" and groups[3][-1][1] == "img:end.png"  # 자리 지정·끝으로 붙이기
    assert ids == ["1", "2", "3", "4", "5", "6", "7", "8", "9"]  # 그림은 Pexels 사용 기록에서 뺀다


def test_required_image_failure_stops(monkeypatch, tmp_path):
    main = _fake_env(monkeypatch, tmp_path, fail_image=True)
    meta = {"author": "claude", "facts": ["a", "b", "c", "d", "e"], "clips": [[1], [2], [3], [4], [5]],
            "images": [{"fact_index": 2, "path": "queue/assets/x.jpg", "required": True}]}
    with pytest.raises(RuntimeError):
        main.fetch_verified_backgrounds(meta, str(tmp_path))
    meta["images"][0]["required"] = False  # 선택 그림은 건너뛰고 계속
    groups, _ = main.fetch_verified_backgrounds(meta, str(tmp_path))
    assert [len(g) for g in groups] == [1, 1, 1, 1, 1]


def test_image_to_video_keeps_jpg(tmp_path):
    from PIL import Image
    from pipeline import image_to_video
    src = tmp_path / "scene.jpg"
    Image.new("RGB", (400, 300), (120, 80, 40)).save(src)
    before = src.read_bytes()
    out = tmp_path / "work" / "scene.mp4"
    image_to_video.png_to_video(str(src), str(out), duration=0.5, fps=10)
    assert src.exists() and src.read_bytes() == before  # 원본 그대로
    assert out.exists() and out.stat().st_size > 0
    assert not (tmp_path / "work" / "scene_frame.png").exists()  # 임시 프레임 정리


def test_composer_keeps_image_cuts_when_trimming():
    """구간이 짧아 컷을 줄여야 할 때 대본 그림(img:)이 잘려 나가지 않는다."""
    import importlib
    from pipeline import composer
    grp = [("a.mp4", "1"), ("b.mp4", "2"), ("c.mp4", "3"), ("d.mp4", "img:scene.jpg")]
    span, m = 2.0, max(1, min(4, int(2.0 // 0.9) or 1))  # composer 와 같은 계산 → 2컷
    src = open(composer.__file__, encoding="utf-8").read()
    assert "keep = [i for i, g in enumerate(grp) if _is_img(g)][:m]" in src
    def _is_img(g):
        return isinstance(g, (list, tuple)) and len(g) > 1 and str(g[1]).startswith("img:")
    keep = [i for i, g in enumerate(grp) if _is_img(g)][:m]
    rest = [i for i in range(len(grp)) if i not in keep][:m - len(keep)]
    picked = [grp[i] for i in sorted(keep + rest)]
    assert m == 2 and picked == [("a.mp4", "1"), ("d.mp4", "img:scene.jpg")]
