# Engine 탭 대시보드 — 설계 문서

> 작성: 2026-03-08  
> 상태: 📋 설계 완료, 검증 대기 중  
> 목적: 자가수정 에이전트 현황 + 엔진 핵심 로그 통합 뷰

---

## 1. 배경 및 목표

### 구현 배경
- `qwq-self-healer.service` 자가수정 에이전트 구축 완료 (2026-03-08, `146ed0e`)
- 에이전트 동작 현황, 수정 이력, 엔진 로그를 한눈에 볼 수 있는 전용 탭 필요
- 기존 대시보드 탭: 실시간 / 거래 / 성과 / 자산 / 테마 / 복기 / 설정
- **"엔진" 탭 신규 추가**

### 핵심 목표
1. 자가수정 에이전트 상태와 수정 이력 실시간 확인
2. 봇 엔진 ERROR/WARNING 로그를 NOISE 제거 후 가독성 있게 표시
3. LLM 운영 루프 상태 확인 (레짐분류기, daily_bias, FN분석)

---

## 2. 페이지 레이아웃

```
┌─────────────────────────────────────────────────────────────────┐
│  [헤더] 자가수정 에이전트 상태 바 (항상 상단 고정)                   │
│  ● ACTIVE   오늘 0/3회 수정   쿨다운: 없음   마지막: —              │
├────────────────────────┬────────────────────────────────────────┤
│                        │                                        │
│   ① 수정 이력           │   ② 실시간 엔진 로그                    │
│   (타임라인, 좌 40%)    │   (ERROR/WARNING 필터, 우 60%)          │
│                        │                                        │
├────────────┬───────────┴──────────────┬──────────────────────── ┤
│ ③ LLM 레짐 │    ④ Daily Bias          │   ⑤ FN 분석              │
│ (오늘 08:10)│    (익일 운영 보정)        │   (주간 누적 패턴)         │
└────────────┴──────────────────────────┴────────────────────────┘
```

### 반응형 브레이크포인트
- ≥1024px: 상단 40/60 분할 + 하단 3열
- 768~1024px: 상단 2열 + 하단 2+1열
- <768px: 전체 1열 세로 스택

---

## 3. 섹션별 상세 스펙

### [헤더] 자가수정 에이전트 상태 바

**표시 데이터**
- 서비스 상태 dot: `active`(green) / `inactive`(red) / `deactivating`(amber)
- 오늘 수정 횟수: `N / 3회` (MAX_FIXES_PER_DAY = 3)
- 쿨다운 상태: "없음" 또는 "N초 후 가능"
- 마지막 수정 시각: timestamp → "12분 전" 포맷

**인터랙션**
- [↺ 새로고침] 버튼 (수동)
- 5초 자동 폴링 (SSE 불필요)

---

### [섹션①] 수정 이력 타임라인

**테이블 컬럼**
| 컬럼 | 데이터 소스 |
|------|-----------|
| 시각 | `timestamp` → HH:MM 포맷 |
| 티어 | T1(blue) / T2(amber) / T3(red) 배지 |
| 오류 유형 | `error_type` (AttributeError 등) |
| 파일:라인 | `file_path:line_number` |
| 수정 요약 | Claude Code `SUMMARY:` 추출값 |
| 커밋 | 7자리 해시 → GitHub 링크 |
| 결과 | ✅ 성공 / ⚠️ 롤백 / ❌ 실패 |

**데이터 소스**
- `~/.cache/ai_trader/self_healer_history.json` (최근 50건)

**빈 상태 메시지**
```
"자가수정 이력 없음 — 봇이 정상 동작 중입니다 ✓"
```

---

### [섹션②] 실시간 엔진 로그

**컨트롤 바**
```
[ERROR] [WARNING] [INFO]    [NOISE 숨김 ●]    [↺ 30초 자동]
```

**로그 아이템 구조**
```
13:42:01 | ERROR | kis_kr:get_balance:1291 | 잔고 조회 실패: 기간이 만료된 token
```
- ERROR 라인: `rgba(248,113,113,0.06)` tint
- WARNING 라인: `rgba(251,191,36,0.06)` tint
- 폰트: JetBrains Mono (기존 mono 클래스 활용)

**NOISE 필터 패턴** (기본 ON — `patterns.yaml` noise 섹션과 동기화)
- 주말 KIS API 500 오류
- pykrx 캐시 없음
- scikit-learn/mcp 미설치
- WS 자동 재연결
- 하트비트 로깅

**서버사이드 처리**
- `journalctl -u qwq-ai-trader -n 200 --no-pager` 실행
- NOISE 패턴 필터링 후 최대 100줄 반환
- 레벨 파라미터: `?level=error,warning` (기본)

---

### [섹션③] LLM 레짐 현황

**데이터 소스**: `~/.cache/ai_trader/llm_regime_today.json`

**표시 항목**
| 항목 | 예시 |
|------|------|
| 레짐 | `TRENDING_BULL` (green 배지) |
| 리드 전략 | `SEPA` |
| SEPA min_score | `65` |
| RSI2 min_score | `60` |
| 신뢰도 | 프로그레스 바 0.82 |
| 생성 시각 | "오늘 08:10:23" |
| 한줄 요약 | reasoning 텍스트 |

**레짐별 배색**
- `trending_bull` → accent-green
- `ranging` → accent-amber
- `trending_bear` → accent-red
- `turning_point` → accent-purple

**빈 상태 메시지**
```
"오늘 레짐 미분류 — 08:10 이전이거나 비장일입니다"
```

---

### [섹션④] Daily Bias

**데이터 소스**: `~/.cache/ai_trader/daily_bias.json`

**표시 항목**
| 항목 | 표시 방식 |
|------|---------|
| 전체 평가 | `poor`(red) / `fair`(amber) / `good`(green) |
| SEPA score boost | `+5` green / `-5` red / `0` muted |
| RSI2 score boost | 동일 |
| 진입 제한 시간 | `10:00 이전 진입 금지` or `—` |
| 교훈 | top_lesson 텍스트 |
| 생성 시각 | "어제 20:31" |

---

### [섹션⑤] False Negative 분석

**데이터 소스**: `~/.cache/ai_trader/false_negative_patterns.json`

**표시 항목**
- 최근 분석 날짜 + 놓친 종목 수
- 공통 패턴 목록 (bullet list)
- 개선 제안 목록 (bullet list)
- 최근 20주 `놓친 종목 수` 미니 바차트

---

## 4. 신규 파일 목록

| 파일 | 설명 | 신규/수정 |
|------|------|---------|
| `src/dashboard/templates/engine.html` | 페이지 HTML + 인라인 CSS | 신규 |
| `src/dashboard/static/js/engine.js` | API 호출 + 렌더링 로직 | 신규 |
| `src/dashboard/engine_api.py` | `/api/engine/*` 라우트 7개 | 신규 |
| `src/dashboard/server.py` | `/engine` 라우트 + engine_api 등록 | 수정 (3줄) |
| 기존 모든 `.html` 7개 | nav에 "엔진" 탭 추가 | 수정 (nav 1줄씩) |

---

## 5. API 엔드포인트 명세

### GET `/api/engine/healer/status`
```json
{
  "service_active": true,
  "fixes_today": 0,
  "max_fixes_per_day": 3,
  "cooldown_remaining_secs": 0,
  "last_fix_at": null,
  "last_fix_summary": null
}
```

### GET `/api/engine/healer/history`
```json
[
  {
    "timestamp": "2026-03-08T12:30:00",
    "tier": "T1",
    "error_type": "AttributeError",
    "file_path": "src/core/engine.py",
    "line_number": 342,
    "error_message": "'NoneType' object has no attribute 'positions'",
    "summary": "None 가드 조건 추가",
    "commit_hash": "abc1234",
    "rollback": false,
    "success": true
  }
]
```

### GET `/api/engine/logs?level=error,warning&noise=hide&limit=100`
```json
{
  "logs": [
    {
      "timestamp": "2026-03-08T13:42:01",
      "level": "ERROR",
      "source": "kis_kr:get_balance:1291",
      "message": "잔고 조회 실패: 기간이 만료된 token 입니다."
    }
  ],
  "total": 42,
  "noise_filtered": 218
}
```

### GET `/api/engine/llm-regime`
```json
{
  "regime": "trending_bull",
  "lead_strategy": "sepa",
  "sepa_min_score_today": 65,
  "rsi2_min_score_today": 60,
  "entry_start_time": "09:01",
  "confidence": 0.82,
  "reasoning": "AI/반도체 강세, 외국인 순매수 지속으로 SEPA 우선",
  "generated_at": "2026-03-08T08:10:23",
  "date": "2026-03-08"
}
```

### GET `/api/engine/daily-bias`
```json
{
  "date": "2026-03-08",
  "assessment": "fair",
  "sepa_score_boost": 5,
  "rsi2_score_boost": 0,
  "avoid_entry_before": null,
  "regime_hint": "neutral",
  "top_lesson": "갭업 과열 종목 피할 것",
  "generated_at": "2026-03-07T20:31:00"
}
```

### GET `/api/engine/false-negatives`
```json
{
  "latest": {
    "date": "2026-03-08",
    "missed_count": 3,
    "missed_symbols": ["005930", "000660", "035720"],
    "patterns": ["거래량 급증 후 단기 조정 구간 종목 미포착"],
    "suggestions": ["유니버스 확장 (KOSDAQ150 외 시총 5000억 이상)"],
    "summary": "소형주 중심으로 스크리닝 커버리지 확장 필요"
  },
  "history": [
    { "date": "2026-03-01", "missed_count": 5 },
    { "date": "2026-02-22", "missed_count": 2 }
  ]
}
```

---

## 6. 기술 스택 및 디자인 원칙

- **UI 프레임워크**: Tailwind CSS (기존 페이지와 동일)
- **폰트**: DM Sans (UI) + JetBrains Mono (로그)
- **테마**: 기존 dark 테마 CSS 변수 그대로 사용
- **폴링**: 5초(헤더 상태) / 30초(로그)
- **SSE 미사용**: 로그는 단순 폴링으로 충분
- **미니 차트**: FN 분석 바차트는 순수 CSS 또는 inline SVG (외부 라이브러리 없이)

---

## 7. 구현 예상 범위

| 항목 | 예상 코드량 |
|------|-----------|
| `engine.html` | ~400줄 |
| `engine.js` | ~300줄 |
| `engine_api.py` | ~200줄 |
| `server.py` 수정 | 3줄 |
| 기존 HTML nav 수정 | 7페이지 × 1줄 |
| **총계** | **~910줄** |

---

## 8. 구현 순서 (권장)

1. `engine_api.py` — API 먼저 구현 + 로컬 테스트
2. `server.py` 수정 — 라우트 등록
3. `engine.html` + `engine.js` — UI 구현
4. 기존 nav 일괄 수정

---

## 9. 향후 확장 가능 항목 (v2)

- T2 승인/거부 버튼을 대시보드에서 직접 처리
- 로그 라인 클릭 → 해당 GitHub 파일:라인 바로 열기
- 자가수정 에이전트 강제 실행 버튼 (on-demand 수동 트리거)
- 엔진 로그 키워드 검색 기능

---

*문서 끝 — 검증 후 구현 진행 예정*
