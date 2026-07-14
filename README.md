# Space Trivia 60s

매 6시간마다 (KST 00/06/12/18) 동물 트리비아 1분 영상을 자동 생성·발행하는 GitHub Actions 봇.

## 흐름
1. ANIMALS 사전에서 직전 5번 사용 안 한 동물 1마리 랜덤
2. GPT-4o-mini가 hook + intro + fact x3 + outro + 영문 title/description 생성
3. edge-tts (en-US-AriaNeural) 영문 내레이션 5개
4. Pexels에서 portrait 동물 영상 3개 다운로드
5. MoviePy로 1분 합성 (Ken Burns + 비네트 + 시간차 자막 + 상단 hook + 하단 brand)
6. GPT가 7개국 localizations 번역
7. YouTube Shorts 업로드 (en + 7 locales, public)

## 로컬 dry-run
```
python main.py --dry-run --animal otter
```

## 실행 (자동)
- GitHub Actions cron 4회/일
- secrets: `OPENAI_API_KEY`, `PEXELS_API_KEY`, `YT_CLIENT_SECRET_B64`, `YT_TOKEN_B64`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
