"""Bounded, report-only copies of already collected public evidence."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
LIMITS = {'news': 24, 'disclosure': 8, 'macro': 8, 'calendar': 6}
MACRO_LABELS = {'us10y': ('미국 10년 국채금리', '%'), 'us_short_yield': ('미국 13주 국채금리', '%'), 'dxy': ('달러지수', 'pt'), 'krw_usd': ('원/달러 환율', '원/달러'), 'wti': ('WTI 원유', '달러/배럴'), 'gold': ('금', '달러/온스'), 'copper': ('구리', '달러/파운드'), 'vix': ('변동성 지수', 'pt')}
PUBLICATION_STATUSES = {'unknown', 'provided', 'verified', 'receipt_date_only', 'observation_date'}
FIELDS = ('kind', 'title', 'excerpt', 'source', 'url', 'published_at', 'publication_status', 'collected_at', 'observed_at', 'event_date', 'content_kind', 'limitations')


def _text(value, limit):
    return ''.join(c for c in value if not unicodedata.category(c).startswith('C') and unicodedata.category(c) not in ('Zl', 'Zp'))[:limit] if isinstance(value, str) else ''


def _dt(value):
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            return None
        return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt
    except (ValueError, OverflowError):
        return None


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, '%Y%m%d').date() if re.fullmatch(r'\d{8}', value) else datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _url(value):
    if not isinstance(value, str) or len(value) > 1000 or any(c in '<>"\\' or c.isspace() or unicodedata.category(c).startswith('C') or unicodedata.category(c) in ('Zl', 'Zp') for c in value):
        return None
    try:
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.port not in (None, 80, 443):
            return None
        host = u.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local')) or '.' not in host:
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if (not re.fullmatch(r'[a-z0-9.-]+', host) or '..' in host
                    or not re.fullmatch(r'[a-z]{2,63}', host.rsplit('.', 1)[-1])
                    or any(not part or part.startswith('-') or part.endswith('-') for part in host.split('.'))):
                return None
        return value
    except ValueError:
        return None


def empty_public_research(now, status):
    return {'version': 'public-research-v1', 'captured_at': _dt(now).isoformat() if _dt(now) else None,
            'coverage': [{'source': 'public_research', 'status': _text(status, 40), 'detail': ''}], 'records': []}


def normalize_public_research(bundle):
    if not isinstance(bundle, dict):
        bundle = {}
    captured = _dt(bundle.get('captured_at'))
    out = {'version': 'public-research-v1', 'captured_at': captured.isoformat() if captured else None, 'coverage': [], 'records': []}
    for item in bundle.get('coverage', [])[:20] if isinstance(bundle.get('coverage'), list) else []:
        if isinstance(item, dict):
            out['coverage'].append({k: _text(item.get(k), 160 if k == 'detail' else 40) for k in ('source', 'status', 'detail')})
    track_origins = bundle.get('_capture_origins') is True
    counts = dict.fromkeys(LIMITS, 0)
    seen_titles, seen_urls = set(), set()
    candidates = bundle.get('records', []) if isinstance(bundle.get('records'), list) else []
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get('kind'), str) or item.get('kind') not in LIMITS:
            continue
        kind = item['kind']
        title = _text(item.get('title'), 200)
        if not captured or not title or counts[kind] >= LIMITS[kind]:
            continue
        times = {}
        valid = True
        for field in ('published_at', 'collected_at', 'observed_at'):
            raw = item.get(field)
            value = _dt(raw)
            if raw not in (None, '') and (value is None or value > captured):
                valid = False
                break
            times[field] = value
        if not valid:
            continue
        raw_event = item.get('event_date')
        event = _date(raw_event)
        if kind != 'calendar' and isinstance(raw_event, str) and ('T' in raw_event or ' ' in raw_event):
            event_time = _dt(raw_event)
            if not event_time or event_time > captured:
                continue
        today = captured.astimezone(KST).date()
        if kind == 'news':
            collected = times['collected_at']
            pub = times['published_at']
            if not collected or not timedelta(0) <= captured - collected <= timedelta(hours=24):
                continue
            if pub and captured - pub > timedelta(days=7):
                continue
        elif kind in ('disclosure', 'macro'):
            days = 3 if kind == 'disclosure' else 7
            if not event or not 0 <= (today - event).days <= days:
                continue
        else:
            if not times['collected_at'] or not event or not 0 <= (event - today).days <= 5:
                continue
            if captured - times['collected_at'] > timedelta(days=5):
                continue
        url = _url(item.get('url'))
        key = ' '.join(title.casefold().split())
        if kind == 'news' and (key in seen_titles or (url and url in seen_urls)):
            continue
        if kind == 'news':
            seen_titles.add(key)
            if url:
                seen_urls.add(url)
        limitation = item.get('limitations')
        if isinstance(limitation, list):
            limitation = ' · '.join(_text(value, 150) for value in limitation if isinstance(value, str))
        status = item.get('publication_status')
        if not isinstance(status, str) or status not in PUBLICATION_STATUSES:
            status = 'unknown'
        allowed = {'news': {'unknown', 'provided', 'verified'},
                   'disclosure': {'unknown', 'receipt_date_only'},
                   'macro': {'unknown', 'observation_date'}, 'calendar': {'unknown'}}
        if status not in allowed[kind] or (status in ('provided', 'verified') and not times['published_at']):
            status = 'unknown'
        if kind != 'news':
            times['published_at'] = None
        record = {'kind': kind, 'title': title, 'excerpt': _text(item.get('excerpt'), 600),
                  'source': _text(item.get('source'), 100), 'url': url,
                  'publication_status': status,
                  'content_kind': _text(item.get('content_kind'), 80), 'limitations': _text(limitation, 300)}
        for field, value in times.items():
            record[field] = value.isoformat() if value else None
        record['event_date'] = event.isoformat() if event else None
        if track_origins:
            record['_origin'] = item.get('_origin') if isinstance(item.get('_origin'), str) else ''
        record['id'] = f"E{len(out['records']) + 1}"
        out['records'].append(record)
        if len(json.dumps(out, ensure_ascii=False).encode('utf-8')) > 46 * 1024:
            # Give each represented kind room before admitting more news.
            if kind != 'news' and counts[kind] == 0:
                for index in range(len(out['records']) - 2, -1, -1):
                    if out['records'][index]['kind'] == 'news' and counts['news'] > 1:
                        del out['records'][index]
                        counts['news'] -= 1
                        if len(json.dumps(out, ensure_ascii=False).encode('utf-8')) <= 46 * 1024:
                            break
            if len(json.dumps(out, ensure_ascii=False).encode('utf-8')) > 46 * 1024:
                out['records'].pop()
                continue
        counts[kind] += 1
    origin_counts = {}
    for index, record in enumerate(out['records'], 1):
        record['id'] = f'E{index}'
        origin = record.pop('_origin', '')
        origin_counts[origin] = origin_counts.get(origin, 0) + 1
    if track_origins:
        for row in out['coverage']:
            if row['status'] == 'ok' and not origin_counts.get(row['source']):
                row['status'] = 'partial'
                row['detail'] = _text(row['detail'], 145) + ' · 반영 0건'

    return out


def _opinion(agents, name, now):
    agent = agents.get(name) if isinstance(agents, dict) else None
    opinion = getattr(agent, '_cached_opinion', None)
    raw = getattr(opinion, 'raw_evidence', None)
    issued = getattr(opinion, 'issued_at', None)
    if issued is not None and (not _dt(issued) or _dt(issued) > now):
        return {}, None
    return raw if isinstance(raw, dict) else {}, issued


async def capture_public_research(bot, *, now):
    now = _dt(now)
    if now is None:
        return empty_public_research(now, 'invalid_time')
    records, coverage = [], []
    def cover(source, status, detail=''):
        coverage.append({'source': source, 'status': status, 'detail': detail})
    def fresh(value, hours):
        dt = _dt(value)
        return bool(dt and timedelta(0) <= now - dt <= timedelta(hours=hours))
    storage = getattr(getattr(bot, 'theme_detector', None), '_storage', None)
    if storage is None:
        cover('news_storage', 'not_ready')
    else:
        try:
            result = await asyncio.wait_for(storage.get_report_news(now=now), 2)
            cover('news_storage', result.get('status', 'failed'), result.get('detail', '최근 24시간 수집 기사 · 발행시각 미검증'))
            for row in result.get('rows', [])[:80]:
                if isinstance(row, dict) and fresh(row.get('collected_at'), 24):
                    records.append(dict(_origin='news_storage', kind='news', title=row.get('title'), excerpt=row.get('content'), source=row.get('source'), url=row.get('url'), collected_at=row.get('collected_at'), publication_status='unknown', content_kind='article_excerpt', limitations='기존 저장 기사의 발행시각 미검증'))
        except Exception as exc:
            cover('news_storage', 'failed', type(exc).__name__)
    agents = getattr(getattr(bot, 'expert_orchestrator', None), 'agents', {})
    raw, issued = _opinion(agents, 'news_curator', now)
    articles = raw.get('articles', [])
    news_count = 0
    for article in articles[:50] if isinstance(articles, list) else []:
        if not isinstance(article, dict) or not fresh(article.get('fetched_at'), 24): continue
        pub = _dt(article.get('published_at'))
        if pub and pub > now: continue
        records.append(dict(_origin='news_curator', kind='news', title=article.get('title'), excerpt=article.get('summary'), source=article.get('source'), url=article.get('url'), collected_at=article.get('fetched_at'), published_at=pub, publication_status='provided' if pub else 'unknown', content_kind='article_excerpt', limitations='원천 제공 발췌'))
        news_count += 1
    cover('news_curator', 'ok' if news_count else ('stale' if articles else 'not_ready' if not raw else 'empty'), '기존 기사 캐시 · 기사별 수집시각')
    raw, issued = _opinion(agents, 'earnings_expert', now)
    kr = raw.get('kr_disclosures', {}) if isinstance(raw.get('kr_disclosures'), dict) else {}
    for item in kr.get('sample', []) if isinstance(kr.get('sample'), list) else []:
        if not isinstance(item, dict): continue
        date = _date(item.get('rcept_dt'))
        if not date or not 0 <= (now.astimezone(KST).date() - date).days <= 3: continue
        no = item.get('rcept_no')
        url = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}' if isinstance(no, str) and re.fullmatch(r'\d{14}', no) else None
        records.append(dict(_origin='dart_earnings', kind='disclosure', title=f"{_text(item.get('corp'), 80)} {_text(item.get('report'), 120)}".strip(), source='DART', url=url, event_date=date.isoformat(), collected_at=kr.get('collected_at'), publication_status='receipt_date_only', content_kind='filing_index', limitations='정기공시 첫 페이지의 일부 제목 · 원문 내용과 실적 방향 미검증'))
    cover('dart_earnings', kr.get('status', 'not_ready'), '정기공시 첫 페이지의 일부 · 접수일만 제공')
    upcoming = raw.get('upcoming', {}) if isinstance(raw.get('upcoming'), dict) else {}
    for item in upcoming.get('events', []) if isinstance(upcoming.get('events'), list) else []:
        if not isinstance(item, dict): continue
        date = _date(item.get('date'))
        if date and 0 <= (date - now.astimezone(KST).date()).days <= 5:
            records.append(dict(_origin='earnings_calendar', kind='calendar', title=f"{_text(item.get('symbol'), 30)} earnings", source='Finnhub', event_date=date.isoformat(), collected_at=upcoming.get('collected_at'), publication_status='unknown', content_kind='earnings_calendar', limitations='예정일은 변경될 수 있음'))
    cover('earnings_calendar', upcoming.get('status', 'not_ready'), '기존 실적 일정 · 앞으로 5달력일')
    from ..data.providers.disclosure_feed import peek_report_snapshot
    try:
        snap = peek_report_snapshot()
        status = snap.get('status', 'not_ready')
        if status in ('ok', 'empty') and not fresh(snap.get('attempt_at'), 6): status = 'stale'
        cover('disclosure_feed', status, '공개 피드 스냅샷 · 전체 DART 공시가 아님')
        if status == 'ok':
            for item in snap.get('events', []):
                if not isinstance(item, dict): continue
                date = _date(item.get('rcept_dt'))
                if not date or not 0 <= (now.astimezone(KST).date() - date).days <= 3: continue
                no = item.get('rcept_no')
                url = f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}' if isinstance(no, str) and re.fullmatch(r'\d{14}', no) else None
                records.append(dict(_origin='disclosure_feed', kind='disclosure', title=f"{_text(item.get('name'), 80)} {_text(item.get('label'), 80)}".strip(), excerpt=item.get('meaning'), source='DART / aikstockdata', url=url, event_date=date.isoformat(), observed_at=snap.get('attempt_at'), publication_status='receipt_date_only', content_kind='filing_type_description', limitations='공시 유형의 일반 설명 · 원문 내용이나 실적 방향이 아님'))
    except Exception as exc:
        cover('disclosure_feed', 'failed', type(exc).__name__)
    raw, issued = _opinion(agents, 'macro_economist', now)
    prices = raw.get('prices', {}) if isinstance(raw.get('prices'), dict) else {}
    evidence = raw.get('indicator_evidence', {}) if isinstance(raw.get('indicator_evidence'), dict) else {}
    count = 0
    for key, info in evidence.items():
        if not isinstance(info, dict) or not isinstance(key, str) or key not in MACRO_LABELS: continue
        as_of = info.get('as_of')
        value, date, source = prices.get(key), _date(as_of), info.get('source')
        if isinstance(as_of, str) and ('T' in as_of or ' ' in as_of):
            stamp = _dt(as_of)
            if not stamp or stamp > now:
                continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not isinstance(source, str) or not source or not date or not 0 <= (now.astimezone(KST).date() - date).days <= 7: continue
        records.append(dict(_origin='macro_economist', kind='macro', title=MACRO_LABELS[key][0], excerpt=f'{value} {MACRO_LABELS[key][1]}', source=source, event_date=date.isoformat(), publication_status='observation_date', content_kind='indicator_value', limitations='지표 기준일 · 보고서 발행일이 아님'))
        count += 1
    cover('macro_economist', 'ok' if count else ('stale' if evidence else 'not_ready'), '출처와 기준일이 있는 지표만 포함')
    # Round-robin sources prevents one publisher from crowding out other news.
    news = [r for r in records if r['kind'] == 'news']
    news.sort(key=lambda r: (bool(r.get('published_at')), bool(r.get('excerpt'))), reverse=True)
    others = [r for r in records if r['kind'] != 'news']
    groups = {}
    for item in news: groups.setdefault(item.get('source') or '', []).append(item)
    diverse = []
    while groups:
        for key in list(groups):
            diverse.append(groups[key].pop(0))
            if not groups[key]: del groups[key]
    return normalize_public_research({'captured_at': now, 'coverage': coverage, 'records': diverse + others, '_capture_origins': True})
