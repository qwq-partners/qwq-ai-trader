"""detached stage 입력에 대한 기존 JSON classifier 계산. I/O와 보호 쓰기 없음."""
from copy import deepcopy
from datetime import date, datetime

from ...core.market_regime import classify_intraday_level, max_intraday_level, cap_regime_by_intraday_risk


def prompt_inputs(label, stages, policies, now, *, captured_at=None):
    from ...schedulers.kr_scheduler import _pct_change, _today_bar_action, _index_field, _fmt_pct, _fmt_num
    from .regime_owner import _time
    daily = (stages['daily_bias']['payload'] or {}).get('data', {})
    assessment, top_lesson = daily.get('assessment', 'unknown'), daily.get('top_lesson', '')
    overnight = (stages['us_overnight']['payload'] or {}).get('overnight', {})
    indices, norm = overnight.get('indices', {}), overnight.get('indices_normalized') or {}
    def normalized(key, *aliases, field='change_pct', fields=('change_pct',)):
        entry = norm.get(key)
        # 명시 결측을 legacy 숫자로 되살리지 않는다. key 부재만 호환 fallback이다.
        if key not in norm: return _index_field(indices, key, *aliases, fields=fields)
        value = entry.get(field) if isinstance(entry, dict) and not entry.get('missing') else None
        try: value = float(value) if value is not None else None
        except (TypeError, ValueError): value = None
        return value
    sp500_pct = normalized('SP500', 'S&P500')
    nasdaq_pct = normalized('NASDAQ')
    sox_pct = normalized('SOX', '반도체(SOX)')
    vix = normalized('VIX', 'VIX(공포지수)', field='price', fields=('value', 'price'))
    us_source = 'us_market_data.get_overnight_signal'
    us_as_of = None
    if stages['us_overnight']['outcome'] == 'success':
        provenance = []
        for key, value in (('SP500', sp500_pct), ('NASDAQ', nasdaq_pct), ('SOX', sox_pct), ('VIX', vix)):
            if value is None: continue
            ref = norm.get(key) if isinstance(norm.get(key), dict) else {}
            fetched, market = ref.get('fetched_at'), ref.get('as_of')
            provenance.append(f"{key}: " + (f'마감 {market}' if market else '시장 시각 미제공')
                + (f', 조회 {fetched}' if fetched else ', 조회 시각 미상'))
        us_as_of = '; '.join(provenance) or None
    else: us_source = '조회 실패'
    missing_fields = [name for name, value in (('SP500', sp500_pct), ('NASDAQ', nasdaq_pct),
        ('SOX', sox_pct), ('VIX', vix)) if value is None]
    screener = stages['screener']['payload'] or {}
    closes = screener.get('closes', [])
    c5, c20 = _pct_change(closes, 5), _pct_change(closes, 20)
    kr_as_of, kr_source = '스크리너 캐시 없음', 'batch_analyzer._screener._kospi_closes'
    loaded = screener.get('loaded_at')
    if c5 is not None or c20 is not None:
        kr_as_of = f'{_time(loaded):%Y-%m-%d %H:%M} (스크리너 벤치마크 로드)' if loaded else '로드 시각 미상 (스크리너 캐시)'
    kr_bars_as_of = kr_as_of
    kospi_today_pct = kosdaq_today_pct = None
    quotes = {}
    from .index_risk_input import _observation, _finite_number
    validation_time = captured_at if captured_at is not None else now
    for code in ('0001', '1001'):
        stage = stages.get('index' + code)
        if stage and stage['outcome'] == 'success':
            quote = stage['payload']['quote']
            observation = _observation(quote.get('_observation'), index_code=code)
            if (observation is None or observation[0] > validation_time
                    or observation[0].astimezone(now.tzinfo).date() != now.date()):
                continue
            value = quote.get('change_pct')
            # 원 metadata 결측을 outer zero로 덮지 않는다.
            fact = quote.get('_observation', {}).get('fields', {}).get('change_pct', {})
            if fact.get('status') == 'valid' and _finite_number(value) is not None and value == fact.get('value'):
                quotes[code] = quote
    if 'index0001' in stages:
        if '0001' in quotes:
            kospi_today_pct = float(quotes['0001']['change_pct'])
            level = quotes['0001'].get('price')
            receipt_time = _time(quotes['0001']['_observation']['received_at'])
            kr_as_of = f'KOSPI 수신 {receipt_time.isoformat()} (시장 시각 미제공)'
            kr_source = 'kis_market_data.fetch_index_price(0001/1001)'
            last = date.fromisoformat(screener['last_bar_date']) if screener.get('last_bar_date') else None
            action, reason = _today_bar_action(last, now.date())
            price_fact = quotes['0001'].get('_observation', {}).get('fields', {}).get('price', {})
            if (price_fact.get('status') != 'valid' or _finite_number(level) is None
                    or level != price_fact.get('value')): action, reason = None, '당일 지수 가격 결측'
            elif len(closes) < 6: action, reason = None, '종가열 표본 부족'
            if action is not None:
                series = list(closes[:-1] if action == 'replace' else closes) + [float(level)]
                c5, c20 = _pct_change(series, 5), _pct_change(series, 20)
                kr_bars_as_of = f"{receipt_time:%Y-%m-%d %H:%M} 수신 가격 기반 당일 잠정봉 {'교체' if action == 'replace' else '추가'} (마지막 봉 {last}, 시장 시각 미제공)"
            else:
                kr_bars_as_of += f' — 당일 미반영({reason})'
                missing_fields.append(f'KOSPI봉({reason})')
        else:
            kr_as_of += ' — 장중 갱신 실패'; missing_fields.append('KOSPI당일')
        if '1001' in quotes:
            kosdaq_today_pct = float(quotes['1001']['change_pct'])
            kr_as_of += f"; KOSDAQ 수신 {quotes['1001']['_observation']['received_at']} (시장 시각 미제공)"
        else: missing_fields.append('KOSDAQ당일')
    for name, value in (('KOSPI_c5', c5), ('KOSPI_c20', c20)):
        if value is None: missing_fields.append(name)
    batch, horizon = policies['intraday_policy.current']['value'], policies['regime_policy.horizon']['value']
    crash_level = crash_pct = crash_as_of = None
    if batch['updated_at'] and _time(batch['updated_at']).date() == now.date():
        crash_level, crash_pct, crash_as_of = batch['level'], batch['kospi_pct'], batch['updated_at']
    if crash_level is None: missing_fields.append('급락감지기(당일 갱신 없음)')
    prior = horizon['level'] if horizon['classified_at'] and _time(horizon['classified_at']).date() == now.date() else None
    observed_level = classify_intraday_level(kospi_today_pct)
    cap_level = max_intraday_level(crash_level, observed_level, prior)
    missing_line = ', '.join(missing_fields) if missing_fields else '없음'
    prompt = f'''오늘 한국 주식시장 레짐 분류 (KST {label} 기준)

※ 각 항목의 as_of 는 그 값이 실제로 관측된 시각이다. 결측 항목은 "결측" 으로 표기되며
   0 이나 중립으로 해석하지 말고, 판단 근거에서 제외하고 확신도를 낮춰라.

[미국 마감]  as_of {us_as_of or "결측"} / source {us_source}
- S&P500: {_fmt_pct(sp500_pct)}  NASDAQ: {_fmt_pct(nasdaq_pct)}
- 반도체ETF(SOX): {_fmt_pct(sox_pct)}
- VIX: {_fmt_num(vix)}

[KR 지수 ★ 최우선 판단 근거]  as_of {kr_as_of} / 봉 기준 as_of {kr_bars_as_of} / source {kr_source}
- KOSPI 당일 등락률: {_fmt_pct(kospi_today_pct)}  KOSDAQ 당일: {_fmt_pct(kosdaq_today_pct)}
- KOSPI 5일 변화율: {_fmt_pct(c5, 1)}  20일: {_fmt_pct(c20, 1)}

[장중 급락 감지기]  as_of {crash_as_of or "결측"} / source batch_analyzer._intraday_state
- 감지기 상태: {crash_level or "결측"}  (감지 시 KOSPI {_fmt_pct(crash_pct)})
- 이번 조회 분류: {observed_level or "결측"}  → 적용 캡: {cap_level or "결측(캡 미적용)"}

[결측 항목]
- {missing_line}

[전날 운영 결과]
- LLM 평가: {assessment}  교훈: {top_lesson}

★ 판단 우선순위 (반드시 준수):
1. KOSPI 5일/20일이 약세(c5 ≤ -3% OR c20 ≤ -5%)이면 미장이 강세여도 **trending_bear 또는 turning_point**로 판단
2. KOSPI c5 ≤ -1.5%이면 **turning_point 또는 ranging** (절대 trending_bull 금지)
3. 미장 상승이 한국 약세를 뒤집지 못함 — 5/28 사고 사례에서 미장 강세에도 KR 약세 지속
4. **trending_bull은 KOSPI c5 ≥ +1% AND c20 ≥ 0%일 때만** (둘 다 만족)
5. 장중 급락 감지기가 crash/severe 이면 **trending_bull 금지** (당일 지수가 최신 사실)
6. KR 지수 as_of 가 "장중 갱신 실패" 이면 그 값은 장 시작 전 자료다 — 확신도를 낮춰라

아래 JSON으로 오늘 시장 성격을 판단하세요:
{{"regime": "trending_bull | ranging | trending_bear | turning_point", "lead_strategy": "sepa | rsi2 | balanced", "sepa_min_score_today": 65, "rsi2_min_score_today": 60, "entry_start_time": "09:01", "confidence": 0.75, "reasoning": "한 줄 요약 (KOSPI c5/c20 수치 반드시 포함)"}}'''
    return prompt, {'us_as_of': us_as_of, 'us_source': us_source, 'kr_as_of': kr_as_of,
        'kospi_bars_as_of': kr_bars_as_of, 'kr_source': kr_source, 'kospi_today_pct': kospi_today_pct,
        'kosdaq_today_pct': kosdaq_today_pct, 'kospi_c5': c5, 'kospi_c20': c20,
        'intraday_crash_level': crash_level, 'intraday_crash_as_of': crash_as_of,
        'intraday_level_observed': observed_level, 'intraday_cap_level': cap_level,
        'missing_fields': missing_fields}


def projected_result(raw, meta, now):
    result = deepcopy(raw)
    label = result.get('regime'); cap = meta['intraday_cap_level']
    capped = cap_regime_by_intraday_risk(label, cap)
    if capped != label:
        result.update(regime=capped, llm_regime_raw=label, regime_capped=True,
            regime_capped_reason=f'장중급락({cap})', confidence_raw=result.get('confidence'))
    result.update(generated_at=now.isoformat(timespec='seconds'), date=now.date().isoformat(), input_meta=meta)
    return result
