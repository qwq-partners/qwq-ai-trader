---
name: dashboard-feature
description: 대시보드 새 기능 추가 절차(5단계)와 주요 API 라우트·페이지 목록. 대시보드 카드/페이지/API/SSE 작업 시 사용.
---

# 대시보드 기능 개발 패턴

새 기능 추가 시 아래 순서를 따름:
1. `src/dashboard/data_collector.py` — 데이터 수집 메서드 추가
2. `src/dashboard/kr_api.py` / `us_api.py` — REST 엔드포인트 추가
3. `src/dashboard/sse.py` — 실시간 이벤트 추가 (필요 시)
4. HTML 템플릿 — 카드/페이지 추가
5. JS — 렌더링 함수 + SSE 핸들러

## 주요 API 라우트
- `/api/portfolio`, `/api/positions`, `/api/orders`, `/api/risk` — KR
- `/api/us/portfolio` — US
- `/api/stream` — SSE 스트림
- `/api/office/status` — 가상 오피스 상태 (GET 조회 / POST 외부 푸시)
- `/api/performance/quantstats` — quantstats tear sheet HTML (6h 캐시, `?refresh=1`)

## 페이지
`/` 실시간 · `/trades` · `/performance` · `/themes` · `/evolution` · `/engine` · `/office` 가상 오피스 · `/principles` · `/settings`

## 주의
- 수수료 계산은 `FeeCalculator` 단일 사용 — data_collector 내 하드코딩 금지
- `static/office/`는 빌드 산출물 — 직접 수정 금지, `bash tools/office/build.sh`로 재빌드 (상세: `docs/operations/virtual-office.md`)
