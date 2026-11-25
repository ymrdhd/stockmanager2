"""Reviewed XSHG dependency releases; runtime never accepts unreviewed extensions.

The metadata freezes session dates AND times, not only package version. A new
release is prepared offline, independently reviewed, locked, and deployed as
code. No application endpoint can install a calendar or expand coverage.
"""
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from datetime import date, timedelta
from urllib.parse import urlparse

RELEASE = Path(__file__).with_name('calendar_release.json')


def installed_candidate():
    import exchange_calendars as xc
    from exchange_calendars.exchange_calendar_xshg import XSHGExchangeCalendar
    return xc.get_calendar('XSHG', start=XSHGExchangeCalendar.bound_min(), end=XSHGExchangeCalendar.bound_max())


def schedule_rows(cal, start, end):
    """Canonical exchange session schedule, inclusive calendar-day bounds."""
    return [[str(day.date()), *(v.isoformat() if hasattr(v,'isoformat') else str(v) for v in row)]
            for day,row in zip(cal.schedule.loc[start:end].index,
                               cal.schedule.loc[start:end,['open','break_start','break_end','close']].itertuples(index=False,name=None))]


def digest(rows):
    return sha256(json.dumps(rows,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()


def load_release():
    return json.loads(RELEASE.read_text(encoding='utf8'))


def package_bounds(cal):
    return str(cal.bound_min().date()), str(cal.bound_max().date())


def check_installed(cal, metadata, package_version):
    start,end=package_bounds(cal)
    if (metadata.get('calendar')!='XSHG' or metadata.get('package_version')!=package_version
            or (metadata.get('start'),metadata.get('end'))!=(start,end)
            or metadata.get('schedule_sha256')!=digest(schedule_rows(cal,start,end))):
        raise ValueError('installed XSHG calendar does not match reviewed release')
    return cal


@lru_cache(maxsize=1)
def reviewed_xshg():
    return check_installed(installed_candidate(),load_release(),version('exchange-calendars'))


def validate_candidate(cal, old, independent, package_version):
    """Verify full old schedule plus independent official extension session labels.

    The human reviewer must obtain and verify the official source; this function
    checks the supplied list, not the authenticity of a URL or announcement.
    """
    start,end=package_bounds(cal)
    if old['calendar']!='XSHG' or start!=old['start'] or end<=old['end']:
        raise ValueError('candidate must preserve start and extend reviewed coverage')
    if digest(schedule_rows(cal,start,old['end']))!=old['schedule_sha256']:
        raise ValueError('historical calendar conflicts with prior reviewed release')
    extension_start=(date.fromisoformat(old['end'])+timedelta(days=1)).isoformat()
    if independent.get('start')!=extension_start or independent.get('end')!=end:
        raise ValueError('independent statement does not cover complete extension')
    sources=independent.get('official_sources')
    def official(url):
        parsed=urlparse(url)
        host=parsed.hostname or ''
        return parsed.scheme=='https' and (host=='sse.com.cn' or host.endswith('.sse.com.cn') or host=='szse.cn' or host.endswith('.szse.cn')) and not parsed.username and not parsed.password
    if not isinstance(sources,list) or not sources or any(not isinstance(u,str) or not official(u) for u in sources):
        raise ValueError('official exchange source references required')
    actual=[r[0] for r in schedule_rows(cal,extension_start,end)]
    supplied=independent.get('sessions')
    if not actual or supplied!=actual:
        raise ValueError('independent session list missing, duplicate or conflicting')
    return {'calendar':'XSHG','package_version':package_version,'start':start,'end':end,
            'schedule_sha256':digest(schedule_rows(cal,start,end)),
            'source':'exchange_calendars package + operator-reviewed official session statement',
            'official_sources':sources,'independent_statement_sha256':digest(independent),
            'previous_schedule_sha256':old['schedule_sha256']}
