# wallet-events-collector

카드·페이 이벤트 원문을 매일 모아 `data/`에 저장해요.
정리와 페이지 갱신은 새벽 1시 Claude 예약 작업이 맡아요.

```
00:00 KST  GitHub Actions  → 사이트별 원문 수집 → data/sources/*.json, data/index.json
01:00 KST  Claude 예약 작업 → index.json 읽기 + 페이지의 결제수단 목록 읽기
                            → 사람별로 필요한 자료만 골라 정리 → 혜택판 갱신
```

## 처음 설정

1. 이 폴더를 새 **공개(public)** 저장소로 올려요.
   Claude가 `raw.githubusercontent.com` 주소로 읽어야 해서 공개여야 해요.
   저장소에는 공개된 이벤트 공지만 들어가고, 두 사람의 결제수단 목록은 들어가지 않아요.
2. Settings → Actions → General → Workflow permissions를
   **Read and write permissions**로 바꿔요. (수집 결과를 커밋해야 해서)
3. Actions 탭 → collect-events → **Run workflow**로 한 번 돌려 보고
   `data/index.json`에 `"ok": true`가 찍히는지 확인해요.

## 새 카드사·페이 추가 (재희가 새 결제수단을 넣었을 때)

`collector/sources.yml`에 항목을 하나 추가해요. 그 카드사의 **진행 중 이벤트 목록 주소**만 알면 돼요.

- 서버가 HTML을 그대로 주면 `type: static`
- 자바스크립트로 그리는 페이지면 `type: render` (+ 상세 링크를 따라가려면 `follow`)

수집기가 없는 결제수단은 Claude 예약 작업이 웹 검색으로 대신 찾고,
페이지의 ‘확인이 덜 된 것’에 “수집기 없음”으로 표시해요.
매일 아침 요약에 추가하면 좋을 목록 주소도 같이 제안해요.

## 수집하지 않는 것

- robots.txt로 수집을 막은 사이트 (예: 현대카드) — 자동으로 건너뛰고 index.json에 이유를 남겨요
- 로그인이 필요한 앱 전용 혜택 (카카오페이 앱, KB Pay 앱 이벤트 등)

## 참고

- GitHub 예약 실행은 UTC 기준이고, 붐비는 시간엔 수십 분 늦어질 수 있어요.
- 매일 커밋이 생기므로 “60일 동안 활동 없는 저장소의 예약 중지” 규칙에 걸리지 않아요.
- 로컬 테스트: `pip install -r collector/requirements.txt && python -m playwright install chromium && python collector/collect.py tosspay kbcard`
