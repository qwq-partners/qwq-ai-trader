# 가상 오피스 (Agent Virtual Office) 통합

대시보드 `/office` 페이지에서 돌아가는 픽셀아트 사무실 시각화의 빌드 도구.

- 원본: [KbWen/agent-virtual-office](https://github.com/KbWen/agent-virtual-office) (MIT)
- 고정 커밋: `685e767b060a65c15f64122520dab5f2192fa16a` (v1.6.4, 2026-08-01)
- 배포 위치: `src/dashboard/static/office/` (빌드 산출물, 커밋됨)
- 상태 브릿지: `src/dashboard/office_api.py`
- 페이지: `src/dashboard/templates/office.html`

## 왜 정적 번들인가

원본은 React 19 + Vite + 자체 node 서버(포트 5174)로 동작하지만, 이 프로젝트는
**단일 포트 8080** 원칙을 유지한다. 따라서 한 번 빌드해 정적 파일로 붙이고,
상태 API는 aiohttp(Python)로 다시 구현했다. 런타임에 node 프로세스가 필요 없다.

## 재빌드

```bash
bash tools/office/build.sh                    # 고정 커밋으로 재현 빌드
UPSTREAM_REF=main bash tools/office/build.sh  # upstream 최신으로 업그레이드
```

빌드 후 대시보드 재시작:

```bash
echo 'user123!' | sudo -S -k systemctl restart qwq-ai-trader
```

## upstream에 적용하는 패치 3가지

| # | 대상 | 내용 | 이유 |
|---|------|------|------|
| 1 | `src/inference/inferStatus.js`, `src/i18n.js` | `/api/status` → `/api/office/status`, `/api/lang` → `/api/office/lang` | 대시보드가 이미 `/api/status`를 KR 봇 상태로 사용 중 (충돌) |
| 2 | `src/locales/ko.json`, `src/i18n.js` | 한국어 로케일 추가 + 기본 언어 `ko` | 대시보드 전체가 한국어. 캐릭터명도 트레이딩 역할로 교체 |
| 3 | `index.html` | 제목/설명 한국어화, 외부 OG 이미지 메타 제거 | 내부 페이지이므로 외부 자원 참조 불필요 |

`ko.json`은 이 디렉토리에 원본을 보관한다. 문구를 고치려면 `tools/office/ko.json`을
수정한 뒤 재빌드할 것 (`static/office/`를 직접 고치면 다음 빌드에서 날아감).

## 역할 매핑 (8 캐릭터 ← 엔진 상태)

| role | 캐릭터 | 엔진 소스 |
|------|--------|-----------|
| `pm` | 총괄 | `bot.running` / `engine.paused` / 세션 |
| `arch` | 체제분석 | `market_regime` (강세/약세/횡보) |
| `dev` | 스크리너 | `screener._last_screened`, `stats.signals_generated` |
| `qa` | 검증관 | `cross_validator.get_stats()` |
| `ops` | 집행관 | `risk_manager._pending_orders`, `stats.orders_*` |
| `res` | 전문가팀 | `expert_orchestrator.agents`, `theme_detector._themes` |
| `gate` | 리스크 | `RiskManager.can_trade`, 킬스위치, 일일손실 |
| `designer` | 진화 | `~/.cache/ai_trader/evolution/advice_*.json` |

상세는 `docs/operations/virtual-office.md` 참조.
