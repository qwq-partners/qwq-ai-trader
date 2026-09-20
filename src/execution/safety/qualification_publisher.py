"""판단 증거 → 출처 게시 → 판단 사실 게시 (계약 6 의 순서 고정).

engine 을 import 하지 않는다. 입력은 `QualificationEvidence` 하나이고 게시는 넘겨받은
`RequestBoundCommands` 가 한다. `intent_id` 발급과 `config_version` 조립은 S3 gateway 몫이라
여기서 만들지 않고 인자로 받는다 — 이 모듈은 어느 요청인지도, 설정이 무엇인지도 모른다.

게시하지 않는 경우를 조용히 만들지 않는다: builder 의 `QualificationRefused`(사유 코드)와
owner 의 `CommandValidationError` 는 그대로 올라가고, 그때 판단 사실은 0건이다. 반환한 facts
는 기록일 뿐 송신 허가가 아니다 — 소비(prepare·final)는 owner 가 다시 검사한다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .decisions import ConsumedSource, EntryDecisionFacts
from .qualification import build_decision_facts, regime_digest

_KST = ZoneInfo('Asia/Seoul')


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    """판단 한 건이 실제로 쓴 증거.

    `cv_decision` 과 `sizing_inputs` 는 CV·RiskManager 가 다음 판단에 덮어쓰는 살아있는
    dict 다. 생성 시 끊어 두지 않으면 게시본이 나중 판단의 값으로 바뀐다.

    `observed_at` 은 CV 판독 시각(aware KST)이다 — 판단 시각이 아니라 이 값이 출처 as_of 가
    된다(계약 3). 둘 사이에는 llm_second_check·섹터 조회 await 가 있다.
    """
    token: str
    symbol: str
    side: str
    strategy: str
    origin: str
    sector: str | None
    cv_decision: dict
    llm_reason: str
    sizing_inputs: dict
    regime_used: str
    observed_at: datetime

    def __post_init__(self):
        object.__setattr__(self, 'cv_decision', deepcopy(self.cv_decision))
        object.__setattr__(self, 'sizing_inputs', deepcopy(self.sizing_inputs))


def _reusable(row, digest, as_of):
    """계약 5 — 같은 값을 다시 게시하면 앞선 in-flight 판단의 version 이 헛되이 stale 이 된다.

    재사용 조건은 final 의 `_consumed_sources` 가 요구하는 것과 같다: 같은 digest, 판단
    시각 이전의 관측, 그리고 그 게시본이 판단 당일(KST)일 것.
    """
    if row is None or row['digest'] != digest:
        return False
    published_at = datetime.fromisoformat(row['as_of'])
    return (published_at <= as_of
            and published_at.astimezone(_KST).date() == as_of.astimezone(_KST).date())


async def _cite(commands, name, digest, as_of):
    """게시본 하나를 확보한다 — 재사용할 수 없을 때만 새로 게시한다."""
    row = commands.read_qualification_source(name)
    if not _reusable(row, digest, as_of):
        await commands.publish_qualification_source(
            name, as_of=as_of, digest=digest, expected_version=commands.owner.version)
        row = commands.read_qualification_source(name)
    return row


def _consumed(name, row):
    return ConsumedSource(name=name, version=row['version'],
                          as_of=datetime.fromisoformat(row['as_of']), digest=row['digest'])


async def publish_qualification(commands, evidence, *, intent_id, config_version,
                                decided_at) -> EntryDecisionFacts:
    """① regime 게시/재사용 → ② builder → ③ pending 출처 게시 → ④ 판단 사실 게시.

    regime 을 먼저 게시하는 이유는 builder 가 게시 신원이 확정된 출처만 `facts.sources` 에
    담기 때문이다(version 은 게시해야 정해진다). 나머지 출처는 `PendingSource` 로 돌아오므로
    같은 idempotent 규칙으로 게시한 뒤 인용해 붙인다.
    """
    if type(evidence) is not QualificationEvidence:
        raise ValueError('invalid_qualification_evidence')
    # regime 의 as_of 도 판독 시각이다 — builder 가 돌려주는 pending 출처와 같은 기준이어야
    # 한 판단 안에서 출처끼리 시각이 어긋나지 않는다.
    regime_row = await _cite(commands, 'regime', regime_digest(evidence.regime_used),
                             evidence.observed_at)
    facts, pending = build_decision_facts(
        intent_id=intent_id, symbol=evidence.symbol, side=evidence.side,
        strategy=evidence.strategy, origin=evidence.origin, sector=evidence.sector,
        cv_decision=evidence.cv_decision, llm_reason=evidence.llm_reason,
        sizing_inputs=evidence.sizing_inputs, config_version=config_version,
        regime_used=evidence.regime_used, regime_row=regime_row, decided_at=decided_at,
        observed_at=evidence.observed_at)
    cited = []
    for source in pending:
        cited.append(_consumed(source.name, await _cite(commands, source.name, source.digest,
                                                        source.as_of)))
    if cited:
        facts = replace(facts, sources=facts.sources + tuple(cited))
    await commands.publish_decision_facts(facts, expected_version=commands.owner.version)
    return facts
