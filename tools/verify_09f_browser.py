"""Real Playwright browser smoke/duplicate-intent check on a NEW synthetic demo only."""
import argparse
from getpass import getpass
from urllib.parse import urlparse


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',required=True);p.add_argument('--username',default='owner');p.add_argument('--confirm-disposable-demo',action='store_true');args=p.parse_args()
    u=urlparse(args.url)
    if not args.confirm_disposable_demo or u.scheme!='http' or u.hostname not in ('127.0.0.1','localhost') or u.username or u.password or u.path not in ('','/'):
        raise SystemExit('Only confirmed disposable local demo is permitted')
    from playwright.sync_api import sync_playwright, expect
    password=getpass('Disposable demo password: ')
    base=args.url.rstrip('/')
    with sync_playwright() as pw:
        browser=pw.chromium.launch();ctx=browser.new_context();a=ctx.new_page();a.goto(base+'/')
        a.get_by_role('textbox',name='用户名').fill(args.username);a.locator('input[type=password]').fill(password);a.get_by_role('button',name='登录',exact=True).click()
        expect(a.get_by_text('已登录：',exact=False)).to_be_visible(timeout=15000)
        def read(path):
            r=ctx.request.get(base+'/api/v1'+path);assert r.ok,(r.status,path);return r.json()
        status=read('/system/status');assert status['mode']=='sandbox' and status['identity']['instance_role']=='sandbox' and status['writable'] is True
        accounts=read('/accounts?limit=100')['items'];assert all(x['name'].startswith('演示-') for x in accounts)
        securities=next(x for x in accounts if x['kind']=='securities');instrument=next(x for x in read('/instruments?limit=100')['items'] if x['code']=='510300')
        before=read('/trades?limit=500');assert before['total']==4,'must start with fresh four-trade demo'
        b=ctx.new_page();b.goto(base+'/')
        for page in (a,b):
            page.get_by_role('button',name='交易',exact=True).click()
            page.get_by_role('button',name='录入买入 / 卖出',exact=True).click()
            form=page.locator('section[aria-label="业务表单"]');expect(form).to_be_visible()
            form.get_by_label('证券账户').select_option(securities['id']);form.get_by_label('证券标的').select_option(instrument['id'])
            form.get_by_label('方向').select_option('buy');form.get_by_label('成交数量（股）').fill('10');form.get_by_label('实际成交价格（元，最多四位小数）').fill('4.2');form.get_by_label('成交费用（元；未知留空）').fill('0');form.get_by_label('真实成交时间（北京时间）').fill('2026-01-08T10:00');page.on('dialog',lambda d:d.accept())
        a.locator('section[aria-label="业务表单"]').get_by_role('button',name='确认提交',exact=True).click()
        # B's existing form is the same outstanding intent even if A has finished.
        expect(a.get_by_text('提交已确认。',exact=False)).to_be_visible(timeout=15000)
        b.locator('section[aria-label="业务表单"]').get_by_role('button',name='确认提交',exact=True).click(timeout=10000)
        expect(b.get_by_role('alert').filter(has_text='意图')).to_be_visible(timeout=15000)
        after=read('/trades?limit=500');assert after['total']==before['total']+1
        added=[x for x in after['items'] if x['id'] not in {t['id'] for t in before['items']}];assert len(added)==1 and added[0]['status']=='effective'
        cash=read('/cash?limit=500')['items'];lots=read('/lots?limit=500')['items'];assert sum(x.get('execution_id')==added[0]['id'] and x['kind']=='trade' for x in cash)==1;assert sum(x.get('open_execution_id')==added[0]['id'] for x in lots)==1
        print('PASS: real browser stale intent and actual synthetic ledger/cash/batch counts')
        browser.close()

if __name__=='__main__':main()
