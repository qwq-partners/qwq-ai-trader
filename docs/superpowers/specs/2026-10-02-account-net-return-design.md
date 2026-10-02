# 42차 — 명시 계좌 자료의 순손익과 현금흐름 경계 수익률

기존 체결/후보 가격 CLI는 계좌 입출금·운영비를 계산하지 않으므로 별도 작은 오프라인 계산기를 보완한다. 실제 증권사 원본 변환을 추정하거나 기존29건 원장 identity를 생성하지 않는다. 40차 용량·41차 오류 근거를 완료한 뒤 구현한다. 사용자 순차 전체 처리 범위의 로컬 개발이며 API/운영 접근은 없다.

## 산식과 경계

[공식 GIPS Handbook](https://www.gipsstandards.org/standards/gips-standards-for-firms/gips-standards-handbook-for-firms/)의 현금흐름 경계별 평가와 기하 연결 원칙을 따른다. 이는 GIPS 준수 인증이 아니다. 배당/분배금/이자는 외부 납입으로 차감하지 않고 평가액에 포함해야 한다.

계좌 손익 = 끝 순자산−시작 순자산−외부 순납입. 계좌 안에서 반영한 거래 수수료/세금은 다시 빼지 않는다. 계좌 밖에서 지불한 귀속 운영비만 별도로 빼서 최종 순손익을 낸다. TWR은 각 외부 현금흐름 직전 평가/직후 평가를 요구하고, 이전 직후 평가에서 다음 직전 평가까지의 비율을 곱한다. 경계 평가가 없으면 원화 손익과 정확한 TWR을 구분하고 TWR=null로 둔다. 계좌 밖 운영비가 있으면 이 TWR을 전체 비용 후 TWR로 부르지 않는다.

## 고정 입력

새 `account-net-return-v1` JSON: dataset_kind(synthetic/account_export), as_of, currency=KRW, tax_basis_ref, account, benchmarks. account/benchmark 공통 portfolio는 source_ref/source_sha256, declared_complete, start/end{at,equity}, income_in_equity, trading_costs_in_equity, cashflows[], external_operating_costs[]다. 금액은 유한 Decimal 문자열, 날짜는 timezone aware, 전체 기간/보고 인과성을 요구한다.

현금흐름 행은 id/at/kind(deposit/withdrawal/transfer_in/transfer_out)/amount(양수)/pre_equity/post_equity다. 기간 내부 시간순·중복ID/동일시각 없음. pre/post는 둘 다 값 또는 둘 다 null이고 값이 있으면 post=pre+signed amount를 만족해야 한다. 당일만 알고 시각을 모르는 자료를 임의 정렬/보간하지 않는다. 운영비 행은 id/at/amount/ allocation_ref이며 평가액에 이미 포함한 비용은 이 목록에 넣지 않는다. 같은 비용ID 중복을 거부한다.

benchmarks는 선택 목록(최대2)이며 name=KODEX200 또는 KODEX200_EX_SEMICONDUCTORS와 portfolio 및 methodology_ref를 가진다. 비교에는 같은 시작/끝·초기자본·외부흐름을 요구한다. 제공된 비교 계좌/바스켓 평가액의 조건부 산술 비교이며 실제 ETF 체결·세금·과거 구성 인증은 하지 않는다. 반도체 제외 자료가 없으면 빈 목록/null을 유지한다. 원가·현금흐름·배당 포함 범위가 불명확하면 모든 경제값을 null로 두고 필요한 자료를 나열한다.

입력 선언은 진실성/완전성 인증이 아니다. 출력 source_authenticity_verified=false, engine_attribution_verified=false, production_eligible=false와 실제29체결 대사/현재 엔진 효과 별도임을 고정한다. synthetic 결과는 실제 계좌 성과로 표시하지 않는다. CLI는 명시 파일과 byte상한만 읽고 input_sha256을 연결한다.

## 검증

추가 납입·인출·분배금 포함·거래비용 이중차감 방지·외부 운영비·누락 경계·0원 평가/분모0·불완전 선언·미래/역전·중복·금액 부호/비유한·벤치마크 시각/흐름/자본 불일치, 실제 CLI 입출력/읽기전용 지문을 검사한다. 기존 대사·선정/진입·동일진입 청산 시험도 다시 연결한다. 실제 자료 미제공 상태에서 수익 수치는 만들지 않는다.

독립 사전 검토 반영: 각 TWR 분모(start 및 flow 직후 equity)는 양수여야 한다. 분모0/음수 또는 음수 NAV 구간은 원화 손익과 별도로 TWR=null이다. benchmark flow 대조는 at/kind/signed amount로 하며 서로 다른 원천 ID나 성과에 따라 달라지는 pre/post NAV는 동일 요구하지 않는다. 금액 문자열은 부호 선택+정수24자리 이내+소수8자리 이내, 지수 표기 금지, 계산 Decimal 정밀도50. cashflows/비용은 각각 최대10,000행, benchmark 최대2개, 참조/ID200자 이내다. 순자산에는 부채를 반영한 음수도 보존하되 TWR을 적격으로 승격하지 않는다.
