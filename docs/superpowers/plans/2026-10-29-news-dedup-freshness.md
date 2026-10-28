# E04-NEWS-DEDUP — 뉴스 중복 제거 시작·반환 신선도 복구

**목적:** 종목 선정에 들어가는 같은 뉴스의 반복 분석과 오래된 테마의 재전달을 줄인다. 야간 호출 횟수를 비용 낭비나 실제 절감액으로 바꾸지 않는다.

**기준:** PR #293/main/운영 `9a193331`. 사용자의 정상 배포 후 남은 우선 작업 순차 개발 요청에 따라, 도래 기록 판독에서 발견한 구체 결함을 처리한다. 정본은 [작업 원장](../../operations/execution-roadmap.md#r02-live-followthrough-20261029)이다.

## Plan — 확인한 원인과 최소 범위

- 빈 similarity cache면 첫 기사 삽입 전 False로 반환한다. 처음부터 비었거나2시간 TTL로 전부 제거되면 비교 목록이 다시 채워지지 않는다. 실제 sklearn/합성 공급자로3개 실패를 재현했다.
- `detect_themes`의 반환 목록은 finally 정리 전에 만들어진다. 빈 뉴스/빈 결과/예외에서 내부 stale 상태를 지워도 호출자는 오래된 ThemeInfo를 받을 수 있다. 주기 내 force=False 반환은 정리 이전에 끝난다.
- scheduler는 반환 목록으로 ThemeEvent를 만들고 소비자는 이벤트 수신 시각을 새로 기록하므로, seed만 고치고 반환 신선도를 방치하지 않는다. 종목 입력이 달라질 수 있는 수정이며 행동 불변으로 주장하지 않는다.
- 독립 중요 계약 검토 요청 Astra/high의 판단에 따라, 첫 유효 기사 등록과 모든 테마 목록 반환 전 기존1시간 stale 정리를 함께 적용한다. 실제 모델/effort metadata는 미노출이다.

## Do — 소유권·구현 계약

구현자 Terra/high, 별도 worker checkout, 기준 SHA 동일. 소유 파일은 `src/signals/sentiment/kr_theme_detector.py`와 `tests/test_news_similarity_bootstrap.py` 두 개다. coordinator는 문서·통합·배포를 소유한다. worker의 운영/자격/실자료/외부 API·fanout 접근은 없다.

1. sklearn이 있고 기존 텍스트 조건을 충족하면 빈 cache에도 첫 기사를 한 번 등록한다. 기존 seed가 있을 때의 TF-IDF/0.90 문턱·500개 상한·2시간 TTL·짧은 텍스트·예외 처리는 유지한다.
2. 모든 `detect_themes` 테마 목록 반환은 기존 theme/sentiment stale 정리 뒤의 목록을 반환한다. 주기 내 반환·빈 뉴스·빈 분석 결과·정상·예외 경로를 포함한다. 기존 finally의 실패 정리 계약도 보존한다.
3. 새 공용 `get_active_themes` API를 만들지 않는다. 현재 별도 장전 진단 소비자의 이 이름 불일치를 함께 고치면 미검토 LLM/시세 경로가 활성화될 수 있다. private 정리/반환 helper 등 좁은 구현을 사용한다.
4. 기존 실패 LLM 뒤 cache-before-success 소비·JSON 재시도/공급자 대체·DB 저장 순서를 보존한다. 이번 범위에서 실패 재소비/캐시 rollback 정책을 새로 만들지 않는다.
5. 탐지 주기/force·시장 시간·모델/예산·상위20 뉴스·진입/청산 문턱·운영 설정은 변경하지 않는다. 새 지문 원장이나 장기 수집은 없다.

## See — 완료 조건

- 첫 기사 A 통과/seed → 같은 내용·다른 URL A 제외 → 새 B 통과, TTL 전체 만료 뒤 같은 계약을 실제 sklearn으로 검사한다.
- sklearn 없음·짧은 텍스트·유사도 예외·기존 seed·500개 상한·TTL/1시간 신선도 경계를 검사한다.
- force=False 주기 내 반환과 빈 뉴스/빈 결과/예외/정상 경로에서 stale 테마·감정은 반환/상태에서 제외하고 fresh 항목·기존 조정/부작용 순서를 유지한다.
- 실제 scheduler 한 주기와 mock 전달로 stale ThemeEvent/NewsEvent가 새로 전달되지 않으며 정상 fresh 이벤트는 유지되는지 확인한다. 공급자/LLM은 가짜이고 운영 상태·네트워크 접근0이어야 한다.
- 실패 LLM 후 기사 소비의 기존 계약을 특성화한다. 첫 seed 복구가 cold 상태에도 같은 계약을 적용한다는 영향과 실제 운영 절감 미측정을 기록한다.
- 관련/전체 verify·비밀 검사·독립 중요 최종 리뷰를 통과한 뒤 고정 SHA 통합/배포를 검토한다. 배포 시 새 보호 지문·미체결/복구·예약 대조와 기존 rollback/150초 인수를 사용한다.

## 별도 항목

장전 진단의 미구현 공용 API 이름과 실패 LLM 후 기사 재소비 정책은 별도 계약 검토가 필요하다. 이번 변경으로 해당 경로를 조용히 켜거나 문턱을 바꾸지 않는다. 자연 Claude 호출·실제 RR 신호·13:10 보고/모바일·T04 만료/O02 기한은 원장의 기존 조건을 유지한다.

## 구현 인수 진행

Terra/high 구현 뒤 coordinator 관련104검사/3.52초·격리0를 확인했다. 최초3RED/3PASS→반환 경계 포함9RED/3PASS를 재현했고, 첫 seed의 빈 vocabulary 검증 우회도 추가1RED 뒤 수정했다. 전체 검증과 독립 중요 최종 리뷰는 진행 중이며 운영은9a193331을 유지한다. 정리 실패를 삼키는 기존 malformed-object best-effort 계약은 그대로이며 모든 잘못된 객체를 정상화한 결과로 주장하지 않는다.

## 중요 리뷰 결과와 잔여 한계

구독 Claude CLI의 요청 Opus5.5/high·응답/terminal 실제 claude-opus-5-5·도구/MCP0·rc0·147.73초, 정적 APPROVE/P0~P2 0을 확인했다. 테스트 실행은 reviewer가 하지 않았다. 표시USD0.5806338은 구독 증분 청구액이 아니며 실제 청구는 미확인이다.

비차단 P3는 범위를 넓히지 않고 남긴다. sklearn은 requirements.txt의 명시 의존성이므로 이번 검증에서 임의 skip으로 바꾸지 않는다. 잘못된 객체의 best-effort 정리, 반환/마지막 정리 사이 시계 경계, 기존 발행시각 기반TTL에 의한 오래된 기사 재통과는 유지한다. coordinator는 새 private helper 이름이 유일하고 기본 ThemeInfo 시각이 naive이며 현재5개 수집 경로가 기본 now 또는 명시 tz 제거를 사용함을 소스로 대조했다. 이는 과거 DB/모든 외부 객체의 시각을 실자료로 보증하는 검사가 아니다. 감정 getter는 새 dict를 만들며 새 helper는 await 없이 기존 만료 규칙만 적용한다. 정상 운영의 실제 절감이나 선정 성과는 별도 자료로 판단한다.

최종 로컬 전체 verify:9,675passed/2skipped/2xfailed/8warnings·782.07초·rc0·격리0·문법/비밀PASS. 문서 날짜/현재 예약표P2 두 곳 수정 후 독립 최종APPROVE/P0~P2 0. 최종CI/병합·새 운영 인수는 이번 PR의 완료 영수증으로 확정한다.

## 최종 마감

[PR #294 운영 완료 영수증](https://github.com/qwq-partners/qwq-ai-trader/pull/294#issuecomment-6474809303). 운영 고정 `9c187307`·PID3771021, 10/29 01:36:10 KST 인수·164.6초/20항목 전부PASS. 운영 전체9,675passed·2skipped·2xfailed/783.28초, 보호48파일/cron 동일·복구ready·미체결0·NRestarts0·시작 오류0. 매수 중지·월USD100/log_and_continue·GPT/Claude 계약 유지. PR/main CI9,675검사 PASS. 위 구현 인수 진행 중의 상태는 당시 이력이며 구현·검증·독립 리뷰·배포는 DONE이다. 다음 작업은 단일 원장의 자연 입력/기한 조건만 따른다. 이 문서 마감은 제품 변경/재배포 대상이 아니다.
