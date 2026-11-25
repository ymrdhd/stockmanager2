"""Offline dependency calendar verification/update, not a runtime data loader."""
import argparse
import json
from pathlib import Path
import os
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.domain.strategy.calendar_release import (RELEASE, load_release, installed_candidate,
    check_installed, validate_candidate, version)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--statement',type=Path,help='independent official extension session list JSON')
    parser.add_argument('--approve-official',action='store_true',help='explicit operator confirmation; writes source release metadata')
    args=parser.parse_args()
    try:
        cal=installed_candidate(); old=load_release(); ver=version('exchange-calendars')
        if not args.statement:
            if args.approve_official: raise ValueError('independent statement required')
            check_installed(cal,old,ver); print(json.dumps(old,ensure_ascii=False,indent=2)); return 0
        candidate=validate_candidate(cal,old,json.loads(args.statement.read_text(encoding='utf8')),ver)
        if args.approve_official:
            tmp=RELEASE.with_suffix('.json.tmp')
            with tmp.open('x',encoding='utf8') as f:
                json.dump(candidate,f,ensure_ascii=False,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,RELEASE)
        print(json.dumps({'written':args.approve_official,'candidate':candidate},ensure_ascii=False,indent=2)); return 0
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        print(f'calendar verification refused: {exc}',file=sys.stderr); return 1


if __name__=='__main__':
    raise SystemExit(main())
