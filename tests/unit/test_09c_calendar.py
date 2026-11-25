from copy import deepcopy
import pandas as pd
import pytest
from backend.domain.strategy.calendar_release import (installed_candidate,load_release,check_installed,validate_candidate,version)


def test_reviewed_installed_calendar():
    cal=installed_candidate(); old=load_release()
    assert check_installed(cal,old,version('exchange-calendars')) is cal
    assert old['end']=='2026-12-31'
    bad={**old,'schedule_sha256':'0'*64}
    with pytest.raises(ValueError): check_installed(cal,bad,version('exchange-calendars'))


def test_synthetic_extension_conflicts_never_modify_runtime():
    old=load_release(); real=installed_candidate()
    class Synthetic:
        def bound_min(self): return real.bound_min()
        def bound_max(self): return pd.Timestamp('2027-01-04')
    fake=Synthetic()
    # Synthetic only; no claim about the actual 2027 exchange session.
    extra=real.schedule.iloc[-1:].copy(); extra.index=pd.DatetimeIndex(['2027-01-04'])
    for col in extra.columns: extra[col]=extra[col]+pd.Timedelta(days=4)
    fake.schedule=pd.concat([real.schedule,extra])
    statement={'start':'2027-01-01','end':'2027-01-04','official_sources':['https://www.sse.com.cn/'], 'sessions':['2027-01-04']}
    candidate=validate_candidate(fake,old,statement,'synthetic')
    assert check_installed(fake,candidate,'synthetic') is fake
    for changes in ({'sessions':[]},{'sessions':['2027-01-04']*2},{'start':'2027-01-02'},{'official_sources':['https://example.com/']}):
        with pytest.raises(ValueError): validate_candidate(fake,old,{**statement,**changes},'synthetic')
    fake.schedule=fake.schedule.copy(); fake.schedule.iloc[0,0]+=pd.Timedelta(minutes=1)
    with pytest.raises(ValueError,match='historical calendar conflicts'): validate_candidate(fake,old,statement,'synthetic')
    assert load_release()==old
