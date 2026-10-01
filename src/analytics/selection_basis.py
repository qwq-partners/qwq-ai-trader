"""반환 후보의 계산 근거. 원천 수집 성공·신선도·인과적 수익 기여는 증명하지 않는다."""
from copy import deepcopy
from datetime import datetime, timezone
import math
from uuid import uuid4

VERSION = 'kr-selection-basis-v1'
MAX_TERMS = 16
SOURCE_IDS = frozenset(('premarket_gap', 'kis_volume_surge', 'kis_institutional_buying',
    'kis_new_highs', 'kis_fluctuation_rank', 'kis_foreign_buying', 'naver_volume_rank',
    'naver_rise_rank', 'naver_new_high', 'theme_news', 'llm_news'))
TERM_FIELDS = frozenset(('source_id', 'input_rank', 'score_at_merge', 'weight', 'weighted_score'))
BASIS_FIELDS = frozenset(('version', 'basis_id', 'produced_at', 'policy_ref', 'source_terms',
    'merge_score_sum', 'post_merge_adjustment', 'occurrence_count', 'occurrence_bonus',
    'pre_normalization_score', 'normalization_mode', 'normalization_min', 'normalization_max',
    'final_score', 'returned_rank', 'cache_fallback', 'source_status_known', 'source_as_of_known'))


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e12:
        raise ValueError('선정 점수는 제한된 유한 수여야 함')
    return value


class SelectionBasisBuilder:
    """단일 호출의 병합 입력만 복사한다. 관측 실패는 선정 점수를 변경하지 않는다."""
    def __init__(self):
        self.basis_id = uuid4().hex
        self.terms, self.before, self.pre = {}, {}, {}
        self.failed = False

    def record(self, stock, weight, source_id, input_rank):
        try:
            if self.failed:
                return
            if source_id not in SOURCE_IDS or len(self.terms) >= 500:
                raise ValueError('관측 원천/후보 한도')
            terms = self.terms.setdefault(stock.symbol, [])
            if len(terms) >= MAX_TERMS:
                raise ValueError('관측 병합 횟수 한도')
            raw = number(stock.score)
            terms.append({'source_id': source_id, 'input_rank': input_rank,
                          'score_at_merge': raw, 'weight': number(weight),
                          'weighted_score': number(raw * weight)})
        except Exception:
            self.failed = True

    def before_bonus(self, result, counts):
        try:
            self.before = {s.symbol: (number(s.score), counts[s.symbol]) for s in result}
        except Exception:
            self.failed = True

    def normalization(self, result, low, high):
        try:
            self.low, self.high = number(low), number(high)
            self.mode = 'affine_30_100' if high > low and high > 100 else 'clamp'
            self.pre = {s.symbol: number(s.score) for s in result}
        except Exception:
            self.failed = True

    def finish(self, result):
        try:
            if self.failed:
                return
            at = datetime.now(timezone.utc).isoformat()
            snapshots = []
            for rank, stock in enumerate(result, 1):
                terms = self.terms[stock.symbol]
                total = sum(t['weighted_score'] for t in terms)
                before, count = self.before[stock.symbol]
                pre = self.pre[stock.symbol]
                snapshots.append({'version': VERSION, 'basis_id': self.basis_id, 'produced_at': at,
                    'policy_ref': 'kr-screen-all-source-accounting-v1', 'source_terms': deepcopy(terms),
                    'merge_score_sum': number(total), 'post_merge_adjustment': number(before-total),
                    'occurrence_count': count, 'occurrence_bonus': number(pre-before),
                    'pre_normalization_score': pre, 'normalization_mode': self.mode,
                    'normalization_min': self.low, 'normalization_max': self.high,
                    'final_score': number(stock.score), 'returned_rank': rank, 'cache_fallback': False,
                    'source_status_known': False, 'source_as_of_known': False})
            for stock, snapshot in zip(result, snapshots):
                stock.selection_basis = snapshot
        except Exception:
            self.failed = True


def validate_settings(settings):
    if (type(settings) is not dict or set(settings) != {'version', 'max_candidates', 'max_source_terms'}
            or settings['version'] != 'selection-basis-v1'
            or type(settings['max_candidates']) is not int or not 1 <= settings['max_candidates'] <= 100
            or type(settings['max_source_terms']) is not int or not 1 <= settings['max_source_terms'] <= MAX_TERMS):
        raise ValueError('선정 근거 관측 한도/버전 필요')
    return deepcopy(settings)


def _time(value):
    if not isinstance(value, str):
        raise ValueError('선정 근거 시각 필요')
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('선정 근거 시각의 시간대 필요')
    return at


def _equal(a, b):
    return math.isclose(number(a), number(b), rel_tol=1e-12, abs_tol=1e-9)


def validate_basis(basis, *, score, rank, observed_at, max_terms=MAX_TERMS):
    if (type(basis) is not dict or set(basis) != BASIS_FIELDS or basis['version'] != VERSION
            or basis['policy_ref'] != 'kr-screen-all-source-accounting-v1'
            or not isinstance(basis['basis_id'], str) or len(basis['basis_id']) != 32
            or any(c not in '0123456789abcdef' for c in basis['basis_id'])
            or type(basis['cache_fallback']) is not bool
            or basis['source_status_known'] is not False or basis['source_as_of_known'] is not False):
        raise ValueError('선정 계산 근거 형식/주장 불일치')
    if _time(basis['produced_at']) > _time(observed_at):
        raise ValueError('선정 계산 근거가 관측보다 늦음')
    terms = basis['source_terms']
    if type(terms) is not list or not 1 <= len(terms) <= max_terms:
        raise ValueError('선정 병합 항 수 한도')
    total = 0
    for term in terms:
        if (type(term) is not dict or set(term) != TERM_FIELDS or term['source_id'] not in SOURCE_IDS
                or type(term['input_rank']) is not int or not 1 <= term['input_rank'] <= 10000
                or number(term['weight']) < 0
                or not _equal(term['weighted_score'], number(term['score_at_merge']) * term['weight'])):
            raise ValueError('선정 원천 항 형식/산술 불일치')
        total += term['weighted_score']
    count = basis['occurrence_count']
    if (type(count) is not int or count != len(terms) or type(basis['returned_rank']) is not int
            or basis['returned_rank'] != rank or not _equal(total, basis['merge_score_sum'])
            or not _equal(basis['occurrence_bonus'], 20 if count >= 3 else 10 if count == 2 else 0)):
        raise ValueError('선정 출현 수/보너스/합계/순위 불일치')
    pre = number(basis['pre_normalization_score'])
    low, high = number(basis['normalization_min']), number(basis['normalization_max'])
    affine = high > low and high > 100
    expected = 30 + (pre-low)/(high-low)*70 if affine else max(0.0, min(100.0, pre))
    if (not low <= pre <= high or basis['normalization_mode'] != ('affine_30_100' if affine else 'clamp')
            or not _equal(pre, total + number(basis['post_merge_adjustment']) + basis['occurrence_bonus'])
            or not _equal(expected, basis['final_score']) or not _equal(basis['final_score'], score)):
        raise ValueError('선정 보정/정규화/최종 점수 불일치')
    return deepcopy(basis)


def build_selection_report(observations):
    """전체 반환 후보를 기준으로 명시 근거의 보유/산술만 판정한다."""
    if observations.get('schema_version') != 1 or not isinstance(observations.get('records'), list):
        raise ValueError('선정 관측 원장 필요')
    candidates, seen, scans = {}, set(), []
    limits_respected = True
    counts = dict(candidates=0, expected=0, recorded=0, accounted=0, cache_fallback=0,
                  unavailable=0, missing_records=0, disabled=0)
    rows = []
    for seq, record in enumerate(observations['records'], 1):
        if type(record) is not dict or type(record.get('sequence')) is not int or record['sequence'] != seq:
            raise ValueError('선정 관측 순서 불일치')
        kind = record.get('kind')
        if kind == 'scan':
            expected = record.get('selection_basis_expected', False)
            if type(expected) is not bool:
                raise ValueError('선정 관측 활성 여부 불일치')
            if expected:
                settings = validate_settings({'version': 'selection-basis-v1',
                    'max_candidates': record.get('selection_basis_max_candidates'),
                    'max_source_terms': record.get('selection_basis_max_terms')})
                if len(record['candidates']) > settings['max_candidates']:
                    limits_respected = False
                    if observations.get('complete') is True:
                        raise ValueError('선정 후보 상한 초과를 완전 원장으로 선언')
            else:
                if {'selection_basis_max_candidates', 'selection_basis_max_terms'} & set(record):
                    raise ValueError('비활성 선정 관측에 활성 한도 선언')
                settings = {'max_source_terms': MAX_TERMS}
            group = []
            for rank, candidate in enumerate(record['candidates'], 1):
                cid = candidate['candidate_id']
                if cid in candidates or cid != f"{record['scan_id']}:{candidate['symbol']}":
                    raise ValueError('선정 후보 ID 불일치/중복')
                row = {'candidate_id': cid, 'symbol': candidate['symbol'], 'returned_rank': rank,
                       'status': 'missing_record' if expected else 'disabled', 'basis': None}
                rows.append(row); group.append(row)
                candidates[cid] = (candidate, record, settings, row)
                counts['candidates'] += 1
                counts['expected' if expected else 'disabled'] += 1
            scans.append(group)
        elif kind == 'selection_basis':
            cid = record.get('candidate_id')
            if cid not in candidates or cid in seen:
                raise ValueError('선정 근거의 미연결/중복 후보')
            candidate, scan, settings, row = candidates[cid]
            if (scan.get('selection_basis_expected') is not True or record.get('symbol') != candidate['symbol']
                    or _time(record['observed_at']) < _time(scan['observed_at'])):
                raise ValueError('선정 근거의 활성 여부/종목/시각 불일치')
            seen.add(cid); counts['recorded'] += 1
            common = {'kind', 'sequence', 'candidate_id', 'symbol', 'observed_at', 'basis_status'}
            if record.get('basis_status') == 'unavailable':
                if set(record) != common:
                    raise ValueError('없는 선정 근거에 값 추가')
                row['status'] = 'unavailable'; counts['unavailable'] += 1
            elif record.get('basis_status') == 'observed':
                basis = {k:v for k,v in record.items() if k not in common}
                basis = validate_basis(basis, score=candidate['score'], rank=row['returned_rank'],
                                       observed_at=scan['observed_at'], max_terms=settings['max_source_terms'])
                row.update(status='cached_basis' if basis['cache_fallback'] else 'accounted', basis=basis)
                counts['accounted'] += 1
                counts['cache_fallback'] += int(basis['cache_fallback'])
            else:
                raise ValueError('선정 근거 상태 불일치')
    counts['missing_records'] = counts['expected'] - counts['recorded']
    for group in scans:
        bases = [r['basis'] for r in group if r['basis'] is not None]
        if bases:
            profiles = {(b['basis_id'], b['produced_at'], b['normalization_mode'], b['normalization_min'],
                         b['normalization_max'], b['cache_fallback']) for b in bases}
            if len(profiles) != 1:
                raise ValueError('선정 계산 ID/정규화 프로필 혼합')
            if (len(bases) == len(group) and
                    (not _equal(min(b['pre_normalization_score'] for b in bases), bases[0]['normalization_min'])
                     or not _equal(max(b['pre_normalization_score'] for b in bases), bases[0]['normalization_max']))):
                raise ValueError('선정 모집단 정규화 근거 불일치')
    return {'version': 'selection-basis-report-v1', 'evaluation_epoch': observations.get('evaluation_epoch'),
            'capture_complete': limits_respected and observations.get('complete') is True
                                and observations.get('dropped_records') == 0,
            'population_limits_respected': limits_respected,
            'counts': counts, 'candidates': rows, 'profit_contribution_verified': False,
            'production_eligible': False, 'source_status_known': False, 'source_as_of_known': False,
            'limitations': ['반환 후보만 포함하며 스크리너 내부 제외 종목은 포함하지 않음.',
                '점수 항은 병합 시점 값이며 원천 응답 성공/실패·신선도를 증명하지 않음.',
                '후속 보정은 합계이며 각 보정의 평가/미평가/실패를 구분하지 않음.',
                '출현 횟수는 독립 정보원 수가 아님. 정규화는 전체 반환 후보에 의존함.',
                '계산 기여는 비용 차감 수익 기여나 인과적 효과가 아님.']}
