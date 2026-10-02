# 계좌 순손익 입력과 실행 순서

대상은 실제 계좌 전체의 순자산과 외부 입출금이다. 종목별 체결 손익 합계나 내부 daily_pnl_pct를 계좌 총수익으로 대체하지 않는다. 실제 계좌번호·API 키는 입력하지 않고 익명 source_ref와 원천 파일 지문을 사용한다. 원본은 비공개 로컬에 보관하고 공개 저장소에 올리지 않는다.

## 원천 확보 후 순서

1. 증권사 원본의 계좌·기간·통화·입출금·평가액·배당/분배금·비용·정정 범위를 확인한다. 과거29건의 원주문/체결 ID는 독립 명세와 별도로 대사한다. 기존 `reconcile_execution_evidence.py`는 새 실행 원장에 없는 과거 주문을 자동 복원하지 않는다.
2. 아래 정규화 입력을 만든다. source_sha256은 제공자가 선언한 원천 지문이며 도구가 증권사 원본을 인증하는 것은 아니다. 변환에서 불명인 항목을0/true로 채우지 않는다.
3. 순손익 CLI는 명시 파일과 상한만 읽는다. 기존 파일을 수정하거나 원천을 탐색하지 않는다. 입력 원문 SHA-256을 출력에 연결한다. 계산 성공은 자료 진실성·거래 대사·현재 엔진 귀속 인증이 아니다.
4. KODEX200 비교는 같은 시작/끝/초기자본/입출금·세금 정의의 별도 비교 포트폴리오가 있을 때만 수행한다. 반도체 제외 자료는 과거 당시 구성·기업행위·분배금 반영 근거가 필요하다. 자료가 없으면 비교를 생략하며 일부11구간을 연간 성과로 확대하지 않는다.

## 정규화 계약

최상위 schema_version=`account-net-return-v1`, dataset_kind=`synthetic` 또는 `account_export`, as_of, currency=`KRW`, tax_basis_ref, account, benchmarks를 사용한다. 모든 날짜는 시간대를 포함하고 금액은 십진 문자열이다. exponent/비유한/숫자형 변환은 허용하지 않는다.

account와 각 비교 portfolio의 필드:

| 필드 | 의미 |
|---|---|
| source_ref / source_sha256 | 익명 원천 참조와 SHA-256 선언 |
| declared_complete | 해당 전체 기간의 외부흐름·평가·비용 자료 완전성 선언 |
| start / end | `{at,equity}`. 현금+보유 평가+미수령 수익−부채 등을 반영한 순자산 |
| income_in_equity | 배당·분배금·이자가 평가액에 포함됐는지 확인 |
| trading_costs_in_equity | 실제 수수료·거래세 등 거래비용이 평가액에 포함됐는지 확인 |
| cashflows | `{id,at,kind,amount,pre_equity,post_equity}` 목록. kind는 deposit/withdrawal/transfer_in/transfer_out, amount양수. 기간 내부 시간순이며 중복 시각은 원천에서 명시 합산 |
| external_operating_costs | `{id,at,amount,allocation_ref}`. 계좌 밖 지출 중 같은 기간 귀속 운영비만, 계좌에 이미 반영한 비용은 제외 |

pre_equity/post_equity는 둘 다 값 또는 둘 다 null이다. 값이 있으면 post−pre가 부호를 반영한 해당 외부흐름과 정확히 같아야 한다. 배당/분배금/이자는 cashflows의 외부 납입으로 넣지 않는다. 입출금 경계 평가가 없어도 시작/끝과 외부흐름이 완전하면 원화 손익은 계산할 수 있으나 정확한 TWR은 null이다.

benchmarks는 최대2개 `{name,methodology_ref,portfolio}`다. name은 KODEX200 또는 KODEX200_EX_SEMICONDUCTORS다. 서로 다른 원천 ID와 pre/post 평가액은 허용하되 시각·방향·금액으로 같은 흐름인지 대조한다. 동일하지 않으면 억지 비교하지 않는다.

## 결과 해석

계좌 거래비용 후 손익은 끝−시작−외부 순납입이다. 계좌 밖 귀속 운영비를 추가로 뺀 원화 순손익을 별도로 낸다. TWR은 각 입출금 경계 사이의 수익을 기하 연결하며 분모0/음수·경계 누락은 null이다. 외부 운영비가 있으면 계좌 평가액 기반 TWR을 모든 비용 차감 후 TWR로 부르지 않는다.

자료 선언이 불완전하거나 수익/비용 포함이 미확인이면 경제값을 null로 보존한다. 입력 계산 완료와 실제 계좌 원본 대사 완료를 구분하며 모든 결과에서 실거래 승격은 false다. 전체 과정은 [42차 설계](../superpowers/specs/2026-10-02-account-net-return-design.md)를 따른다.


## 로컬 실행

```bash
python scripts/report_account_net_return.py --input /explicit/private/normalized-account.json --max-input-bytes 16777216
```

입력 상한은 최대16MiB다. symlink/FIFO/디렉터리·읽는 중 변경·중복 JSON 키·비유한 값을 거부한다. 표준 출력은 결과 JSON 한 개이며 입력은 변경하지 않는다. 위 경로는 실제 파일이 아닌 예시다. 숫자 결과는 Decimal 정밀도50의 문자열이며 매우 크거나 작은 출력은 지수 표기일 수 있다. 입력 금액은 정수24자리/소수8자리 이내의 지수 없는 문자열로 제한한다.
