# 대본 대기열 (Claude 작성 → 봇이 그대로 발행)

1. 새 대본은 `pending/NNN_slug.json` 으로 넣는다. 파일명 슬러그와 JSON 의 `animal_key`(=cities.json 의 city_key)가 같아야 하고, `author` 는 반드시 `"claude"` 다.
2. 필수 키: `animal_key/display/single/country/category/angle/format/landmark_lock/hook/intro/facts(5개)/outro/title_en/description_en/comment/tags/pexels_keywords(5개 이상)/fact_keywords(5개)`.
   - 선택 키: `hook_line`(내레이션용 원문 훅)·`title_ko`·`description_ko`(있으면 번역 대신 그대로 ko 로컬라이제이션에 씀)·`confusion_pair`(없으면 cities.json 로스터 값을 자동 사용).
   - `hook` 은 화면 첫 2.5초에 크게 뜨는 **영어 대문자 펀치 문구(2~6단어)** 다. `"Lisbon in 60s"` 같은 라벨을 넣으면 펀치가 아니라 도시명 표기로 자동 강등된다.
3. 봇은 파일명 오름차순으로 최대 3개까지 시도한다. 스키마 검증에 걸리면 `failed/` 로 옮기고 같은 이름의 `.reason.txt` 에 사유를 남긴다.
4. Pexels 소재를 못 구하면(비전 검증 부족) 그 파일은 `pending/` 에 그대로 두고 다음 파일로 넘어간다 — 손대지 말고 다음 회차를 기다리면 된다.
5. 내레이션이 길어 음성이 잘리는 대본(59.5초 초과)은 렌더 전에 `failed/(narration_too_long)` 로 빠진다. 하이라이트 5개 합계 150단어 안쪽이 안전선이다(실측: 150단어 ≈ 55초).
6. 실제 업로드가 videoId 를 받은 뒤에만 `done/` 으로 옮겨지고 `published_at`·`video_id`·`video_url` 이 덧붙는다. `--dry-run`/`--no-upload` 는 대기열을 소비하지 않는다.

## OpenAI 무호출 모드 (NO_OPENAI=1, 2026-09-03부터 기본)
- `clips`: facts(하이라이트) 순서로 5슬롯, 슬롯당 Pexels 동영상 id 1~2개(`[[id, id], ...]`). 포스터를 사람이 보고 고른다(=vision 대체). 봇은 이 id 만 내려받고 검색·검증을 하지 않는다. 첫 슬롯 첫 클립이 도입샷·썸네일 소스가 된다.
- `localizations`: `{ko, ja, es, pt, de, fr, hi: {title(≤95자), description}}` 7개 전부 필수.
- `captions`(선택): `{lang: [7줄]}` — intro, facts 5개, outro 순서의 번역. 있는 언어만 CC 등록(en 은 항상). 줄 수가 다르면 그 언어만 건너뛴다.
- NO_OPENAI 인데 clips/localizations 가 없으면 `[unlocked]` 로 pending 에 남고 다음 대본을 시도한다(failed 로 보내지 않음).
