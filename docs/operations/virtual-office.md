# 가상 오피스 (Virtual Office) — 엔진 상태 픽셀아트 시각화

> 대시보드 `/office` · 도입 2026-08-03

숫자 카드로는 "지금 봇이 무엇을 하고 있는가"가 한눈에 안 들어온다. 가상 오피스는
엔진의 살아있는 상태를 **8명의 캐릭터가 있는 사무실 한 장면**으로 보여준다.
장이 열리면 사람들이 움직이고, 리스크가 걸리면 문지기가 막고 서 있고, 마감하면 조용해진다.

- 원본: [KbWen/agent-virtual-office](https://github.com/KbWen/agent-virtual-office) (MIT, v1.6.4 고정)
- 페이지: `src/dashboard/templates/office.html` → 라우트 `/office`
- 정적 번들: `src/dashboard/static/office/` (빌드 산출물 커밋)
- 상태 브릿지: `src/dashboard/office_api.py`
- 재빌드 도구: `tools/office/` (`build.sh`, `ko.json`, README)

---

## 아키텍처

```
브라우저 /office
  └── <iframe> /static/office/index.html      (React 정적 번들, node 프로세스 없음)
        ├── GET  /api/office/status           1~8초 폴링 (ETag 304)
        └── SSE  /api/office/status/stream    변경 시에만 push
                     ▲
                     │  office_api.py 가 3개 소스를 병합
        ┌────────────┼─────────────────────────┐
   엔진 파생        HTTP POST            Claude Code 훅 파일
 (bot 상태 환산)  /api/office/status   ~/.claude/office-status*.json
```

**단일 포트 8080 유지**가 통합의 전제였다. 원본은 자체 node 서버(5174)로 도는 React 앱이라
그대로 붙이면 프로세스와 포트가 하나 더 늘어난다. 그래서 **한 번 빌드해 정적 파일로 넣고,
상태 API만 aiohttp로 다시 구현**했다. 런타임에 node가 필요 없다.

### 소스 우선순위

역할별로 **더 최근 신호가 이긴다**. 외부 신호(POST / 훅 파일)는 **5분 TTL**이며,
만료되면 자동으로 엔진 파생 상태로 되돌아간다. 즉 Claude Code로 코드를 만지는 동안엔
그 활동이 보이고, 조용해지면 다시 트레이딩 엔진 상태가 표시된다.

---

## 역할 매핑 (운영 8명 + 전문가 7명 → 캐릭터 8명)

| role | 캐릭터 | 엔진 소스 | 상태 규칙 |
|------|--------|-----------|-----------|
| `pm` | 총괄 | `bot.running`, `engine.paused`, 세션 | 정지=idle · 일시정지=blocked · 장전=planning · 장중=working |
| `arch` | 체제분석 | `market_regime` (+LLM 코멘트를 hint로) | 장전=planning · 장중=working · 마감=idle |
| `dev` | 스크리너 | `screener._last_screened`, `stats.signals_generated` | 후보 있음=working("스크리닝 N종목") · 마감=idle |
| `qa` | 검증관 | `cross_validator.get_stats()` | 통과 0 + 거부 3건 이상=blocked · 그 외 working |
| `ops` | 집행관 | `risk_manager._pending_orders`, `stats.orders_*` | 대기 주문=working · 체결만=done · KILL_SWITCH_ALL=blocked |
| `res` | 전문가팀 | `expert_orchestrator.agents`, `theme_detector._themes` | 장중=working("전문가 N명 분석") · 마감=idle |
| `gate` | 리스크 | `RiskManager.can_trade`, 킬스위치, 일일손실 | 킬스위치/한도 도달=blocked · 손실 70% 접근=경고 표시 |
| `designer` | 진화 | `evolution/advice_*.json` → 없으면 `evolution_state.json` mtime | 오늘 실행=done · 그 외 idle |

### 분위기(mood)

| 조건 | mood | 화면 효과 |
|------|------|-----------|
| 엔진 정지 / 장 마감 | `idle` | 조용한 사무실 |
| 킬스위치 ON / 거래 중단 | `frustrated` | 답답한 분위기 |
| 일일손실 한도 50% 초과 | `stuck` | 정체된 분위기 |
| 체결 대기 3건 이상 | `intense` | 바쁜 사무실 |
| 일일손익 +1% 이상 | `smooth` | 순조로운 분위기 |
| 장전 준비 | `rushing` | 분주함 |

---

## API

### `GET /api/office/status`

병합된 현재 상태. `ETag` + `If-None-Match` 304를 지원하며, 내용이 바뀌지 않으면
`_seq`도 유지되어 프론트가 중복 렌더를 하지 않는다.

```json
{
  "type": "office-status",
  "agents": [{"role": "gate", "status": "blocked", "task": "킬스위치 ON",
              "reasonCode": "permission-denied", "hint": "급락장 수동 개입"}],
  "activeCount": 1,
  "workflow": "정규장 · 체제 강세",
  "mood": "intense",
  "source": "qwq-engine",
  "_seq": "1785686475220"
}
```

### `POST /api/office/status`

외부 도구(Claude Code, CI 등)가 상태를 밀어넣는다. 두 가지 형식 모두 허용:

```bash
# shorthand
curl -X POST http://localhost:8080/api/office/status \
  -H 'Content-Type: application/json' \
  -d '{"dev":"working","qa":"백테스트 중","workflow":"전략 개편"}'

# full format
curl -X POST http://localhost:8080/api/office/status \
  -H 'Content-Type: application/json' \
  -d '{"type":"office-status","agents":[
        {"role":"ops","status":"blocked","reasonCode":"api-rate-limit","task":"KIS 호출 제한"}]}'
```

- 유효 role: `pm arch dev qa ops res gate designer`
- 유효 status: `idle working blocked done planning awaiting-approval`
- 유효 reasonCode: `test-run-failed build-failed deps-failed blocked-unknown permission-denied api-rate-limit api-auth-failed`
- 알 수 없는 role은 폐기, 알 수 없는 status는 `idle`로 강등, 문자열은 200자 컷
- 본문 16KB 초과 시 413

**인증**: `OFFICE_STATUS_TOKEN` 환경변수를 설정하면 `Authorization: Bearer <토큰>`을 요구한다.
미설정 시 인증 없음(내부망 전제). 대시보드가 외부에 노출돼 있다면 설정할 것.

### `GET /api/office/status/stream`

SSE. 상태가 바뀔 때만 `event: status`를 보내고, 그 외에는 2초마다 keepalive 코멘트.

### `POST /api/office/lang`

언어 설정 영속화 (`ko` / `en` / `zh-TW`). `~/.cache/ai_trader/office_lang.txt`.

---

## Claude Code 활동 연동 (선택)

upstream 훅은 HTTP가 아니라 **파일**로 상태를 남긴다
(`~/.claude/office-status-<세션슬러그>.json`). `office_api.py`가 이 디렉토리를
2초 캐시로 스캔해 자동 병합하므로, 훅만 등록하면 별도 설정이 필요 없다.

`~/.claude/settings.json`에 아래를 추가 (경로는 실제 배포 위치로):

```json
{
  "hooks": {
    "PreToolUse":        [{"hooks":[{"type":"command","command":"node /home/ubuntu/projects/qwq-ai-trader/src/dashboard/static/office/hooks/office-status-hook.js"}]}],
    "PostToolUse":       [{"hooks":[{"type":"command","command":"node /home/ubuntu/projects/qwq-ai-trader/src/dashboard/static/office/hooks/office-status-hook.js"}]}],
    "UserPromptSubmit":  [{"hooks":[{"type":"command","command":"node /home/ubuntu/projects/qwq-ai-trader/src/dashboard/static/office/hooks/office-status-hook.js"}]}],
    "Stop":              [{"hooks":[{"type":"command","command":"node /home/ubuntu/projects/qwq-ai-trader/src/dashboard/static/office/hooks/office-status-hook.js"}]}]
  }
}
```

전체 이벤트 목록은 `static/office/hooks/hooks-config.json` 참조.
훅이 만든 상태도 5분 TTL이라, 세션이 끝나면 화면은 엔진 상태로 돌아온다.

> 훅 라벨은 upstream 구현상 en/zh-TW만 지원한다(영어로 표시됨). 캐릭터 이름·UI는 한국어.

---

## 재빌드 / 업그레이드

```bash
bash tools/office/build.sh                    # 고정 커밋 재현 빌드
UPSTREAM_REF=main bash tools/office/build.sh  # upstream 최신 반영
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader
```

`static/office/`를 직접 수정하면 다음 빌드에서 사라진다. 문구 수정은 `tools/office/ko.json`에서.

### upstream에 적용하는 패치 3가지

1. **API 경로** `/api/status` → `/api/office/status` — 대시보드가 이미 `/api/status`를
   KR 봇 상태로 쓰고 있어 이름이 충돌한다.
2. **한국어 로케일** `ko.json` 추가 + 기본 언어 `ko` — 캐릭터 이름도 트레이딩 역할명으로 교체.
3. **index.html** 제목/설명 한국어화, 외부 OG 이미지 메타 제거.

---

## 트러블슈팅

| 증상 | 원인 / 조치 |
|------|------------|
| 캐릭터가 전부 "엔진 미연결" | `kr_bot` 미주입 상태로 대시보드가 떴다. 서비스 로그 확인 |
| 상태가 5분째 그대로 | 외부 POST/훅이 계속 밀어넣는 중일 수 있다. `~/.claude/office-status*.json` mtime 확인 |
| 화면 우측 하단 "status api offline" | `/api/office/status` 응답 실패. `curl localhost:8080/api/office/status` 확인 |
| 번들이 매번 새로 받아짐 | `server.py`의 `no_cache_middleware`에서 `/static/office/assets/`는 예외 처리돼 있어야 한다 |
| 페이지가 HTTPS 도메인에서 비어 보임 | upstream 프론트는 HTTPS + non-localhost에서 폴링을 스킵한다 (혼합 콘텐츠 방지 로직) |


---

## 에이전트 활동 타임라인 (2026-08-03 추가)

`/office` 하단에 팀 심의 이력을 시간순으로 렌더링한다. 캐릭터는 "지금" 상태만
보여주므로, 지나간 판단을 보려면 별도 뷰가 필요했다.

### 데이터 경로
| 엔드포인트 | 용도 |
|---|---|
| `GET /api/team/verdicts?limit=&date=&approved=` | 목록 요약 + `dates`(보관 일자) |
| `GET /api/team/verdict/detail?symbol=&at=&date=` | Bull/Bear 발언 원문 + 분석가 리포트 |

- 저장소: `~/.cache/ai_trader/team_verdicts/verdicts_YYYYMMDD.json`
- `TradingTeam.load_date(day, limit)` / `available_dates()` — `load_today`는 이 위에 얹혀 있다.
- 카드는 접힌 상태로 렌더되고, 펼칠 때만 상세를 조회한다 (오늘 파일이 100KB를 넘는다).

### ⚠️ 승인 ≠ 매수
`decision.approved=true`는 **PM이 트레이더 제안을 승인했다**는 뜻이다.
제안이 `stance=hold`면 승인돼도 신규 매수는 0건이다.
실제로 2026-08-03 심의 9건은 전부 `approved=true` + `stance=hold`였다.

목록·캐릭터 모두 **stance 기준**으로 표기한다 — 매수 / 보류 / 거부.
`approved` 하나만 세면 "승인 9건"이 매수 9건으로 오독된다.

## 캐릭터 상세 필드 (2026-08-03)

상위 앱은 클릭 시 `label`·`hint`·`activeFile`·`skill`·`reasonCode`를 상세 패널에 쓴다.
도입 초기에는 `task` 한 줄만 채워 클릭해도 빈칸이었다. 지금 채우는 값:

| 역할 | activeFile | skill | label |
|---|---|---|---|
| `qa` 검증관 | 심의 종목 | `팀 심의 · 매수/보류/거부` | 토론 요약 |
| `res` 전문가팀 | 심의 종목 | `분석가 N인` | 분석가 채점 내역 |
| `arch`·`dev`·`designer` | – | `체제 판단`/`스크리닝`/`자가 진화` | – |

`_team_snapshot()`은 심의 파일을 **mtime 기준으로 캐시**한다. SSE가 2초마다
파생을 호출하므로 매번 파싱하면 낭비다.

## SSE 실시간 브리지

iframe 번들에는 이런 가드가 있다:

```js
if (protocol === 'https:' && hostname !== 'localhost') return null   // EventSource 비활성
```

즉 `https://qwq.ai.kr`에서는 앱이 **SSE를 스스로 끄고** 폴링만 쓰며, 유휴 시
백오프까지 걸려 반응이 느렸다. 서버(`/api/office/status/stream`)는 정상 동작 중이었다.

→ 부모 창(`office.html`)이 SSE를 받아 iframe으로 `postMessage` 한다.
앱의 message 리스너는 same-origin이거나 `source === window.parent`면 수신하므로
번들을 수정하지 않고 우회된다. 페이지 상단 라이브 배지로 연결 상태를 표시한다.

- 재연결: 지수 백오프 2초 → 최대 30초 (서버 재시작 중 연결 폭주 방지)
- iframe 자체 폴링은 그대로 백업으로 남는다 — 브리지가 죽어도 화면은 갱신된다
