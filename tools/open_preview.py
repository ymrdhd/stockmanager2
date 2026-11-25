"""Double-click preview: open the built web app on a synthetic sandbox.

Started by ``打开网页.cmd`` in the repository root. It never opens the formal
database: it creates (once) an isolated sandbox demo under ``data/preview-app/``
and serves it on loopback only. Nothing outside that directory is written, except
that a busy port is skipped and the next free one is used.

Environment overrides for testing: ``STOCK_PREVIEW_PORT`` (first port to try),
``STOCK_PREVIEW_NO_BROWSER=1`` and ``STOCK_PREVIEW_PASSWORD`` (non-interactive
credential, never stored).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The launcher lives in tools/, so imports of backend.* and running start.py
# need the repository root as the working directory and on sys.path.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = ROOT / '.venv' / 'Scripts' / 'python.exe'
FRONTEND = ROOT / 'frontend'
DIST = FRONTEND / 'dist' / 'index.html'
PREVIEW = ROOT / 'data' / 'preview-app'
AUTH = PREVIEW / 'auth.json'
STATE = PREVIEW / 'PREVIEW.json'
FORMAL = ROOT / 'data' / 'official.json'
FORMAL_DB = ROOT / 'data' / 'stock_manager.sqlite'
FIRST_PORT = int(os.environ.get('STOCK_PREVIEW_PORT', '9091'))
PORT_TRIES = 10


class PreviewError(RuntimeError):
    """A precondition failed; the message is shown to the operator as-is."""


def say(message: str = '') -> None:
    print(message, flush=True)


def usable_console() -> None:
    """Keep Chinese readable in cmd.exe whether it runs UTF-8 or the local code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass


def healthy(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/health',
                                    timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


def read_state() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run(argv: list[str], *, cwd: Path = ROOT) -> int:
    return subprocess.run([str(PY), *argv], cwd=str(cwd)).returncode


def ensure_dependencies() -> None:
    if not PY.is_file():
        raise PreviewError('未找到 Python 虚拟环境 .venv。请在本目录执行一次  uv sync --locked  后重试。')
    if DIST.is_file():
        return
    say('未找到前端生产构建，正在生成（首次可能需要几分钟）…')
    if subprocess.run(['npm.cmd', '--version'], cwd=str(FRONTEND),
                      capture_output=True).returncode != 0:
        raise PreviewError('未找到 npm。请安装 Node.js 22.13+（22.x）后重试，'
                           '或手工执行 cd frontend && npm ci && npm run build。')
    for args in (['ci'], ['run', 'build']):
        say(f'npm {" ".join(args)}')
        if subprocess.run(['npm.cmd', *args], cwd=str(FRONTEND)).returncode != 0:
            raise PreviewError('前端构建失败。请查看上方输出，或手工执行 '
                               'cd frontend && npm ci && npm run build。')
    if not DIST.is_file():
        raise PreviewError('构建后仍未找到 frontend/dist/index.html。')


def load_demo() -> dict:
    manifest = PREVIEW / 'DEMO.json'
    if not manifest.is_file():
        say('首次使用：正在创建独立的合成演示库（不含任何真实数据）…')
        if PREVIEW.exists() and any(entry for entry in PREVIEW.iterdir()
                                    if entry.name != 'PREVIEW.json'):
            raise PreviewError(f'目录已存在但不是完整演示库：{PREVIEW}\n'
                               '请确认其中没有需要保留的文件后删除该目录，再重新双击。')
        code = run([str(ROOT / 'tools' / 'create_demo.py'), '--target', str(PREVIEW)])
        if code != 0 or not manifest.is_file():
            raise PreviewError('创建合成演示库失败，请查看上方输出。')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if data.get('role') != 'sandbox':
        raise PreviewError('演示目录不是 sandbox 角色，拒绝启动。')
    database = Path(str(data.get('database', ''))).resolve()
    if database == FORMAL_DB.resolve() or PREVIEW.resolve() not in database.parents:
        raise PreviewError('演示数据库不在 data\\preview-app 内，拒绝启动以免碰到正式库。')
    if FORMAL.is_file():
        try:
            bound = Path(str(json.loads(FORMAL.read_text(encoding='utf-8')).get('path', '')))
        except (OSError, ValueError, TypeError):
            bound = None
        if bound is not None and bound.resolve() == database:
            raise PreviewError('演示数据库与正式绑定指向同一文件，拒绝启动。')
    return data


def ensure_auth(port: int) -> None:
    hosts = [f'127.0.0.1:{port}', f'localhost:{port}']
    if AUTH.is_file():
        try:
            current = json.loads(AUTH.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise PreviewError(f'登录凭据文件损坏：{AUTH}（{exc}）。请删除该文件后重新双击。') from exc
        if isinstance(current, dict) and set(hosts) <= set(current.get('allowed_hosts') or ()):
            return
        # The port changed: keep the password hash, only refresh the host allow-list.
        if isinstance(current, dict) and current.get('password_hash'):
            from backend.config import AuthConfig
            from backend.infrastructure.authfile import save_auth
            save_auth(AUTH, AuthConfig(
                username=str(current.get('username') or 'owner'),
                password_hash=str(current['password_hash']),
                allowed_hosts=tuple(current.get('allowed_hosts') or ()) + tuple(hosts),
                allowed_origins=tuple(current.get('allowed_origins') or ()),
                session_ttl_seconds=int(current.get('session_ttl_seconds') or 43200),
                cookie_secure=bool(current.get('cookie_secure', False))))
            return
        raise PreviewError(f'登录凭据文件缺少密码哈希：{AUTH}。请删除该文件后重新双击。')
    password = os.environ.get('STOCK_PREVIEW_PASSWORD')
    if password:
        from backend.config import AuthConfig
        from backend.infrastructure.authfile import save_auth
        from backend.security.passwords import hash_password
        save_auth(AUTH, AuthConfig(username='owner', password_hash=hash_password(password),
                                   allowed_hosts=tuple(hosts), allowed_origins=(),
                                   cookie_secure=False))
        return
    say()
    say('首次使用：请为本地预览设置登录密码。密码只保存在本机的 data 目录，写入的是哈希。')
    if run([str(ROOT / 'start.py'), 'set-password', '--auth-file', str(AUTH),
            '--port', str(port), '--insecure-cookie']) != 0 or not AUTH.is_file():
        raise PreviewError('密码设置未完成。请重新双击；或在终端执行 '
                           'start.py set-password 后重试。')


def choose_port(demo: dict) -> tuple[int, bool]:
    """Return (port, already_running). Never guesses that a foreign service is ours."""
    state = read_state()
    port = state.get('port')
    if (isinstance(port, int) and state.get('database') == demo.get('database')
            and state.get('uuid') == demo.get('uuid')
            and alive(state.get('pid')) and healthy(port)):
        return port, True
    start = FIRST_PORT
    for candidate in range(start, start + PORT_TRIES):
        if port_free(candidate):
            return candidate, False
        if healthy(candidate):
            say(f'端口 {candidate} 已被另一个 Stock Manager 实例占用，改用下一个空闲端口。')
    raise PreviewError(f'端口 {start}–{start + PORT_TRIES - 1} 都被占用。'
                       '请关闭其他实例，或设置环境变量 STOCK_PREVIEW_PORT 指定端口。')


def serve(demo: dict, port: int, already_running: bool) -> int:
    base = f'http://127.0.0.1:{port}'
    def open_browser() -> None:
        if os.environ.get('STOCK_PREVIEW_NO_BROWSER') != '1':
            webbrowser.open(base + '/')

    if already_running:
        say(f'预览服务已在运行：{base}/ ，正在打开浏览器。')
        open_browser()
        return 0

    command = [str(PY), str(ROOT / 'start.py'), 'local',
               '--database', demo['database'], '--uuid', demo['uuid'],
               '--generation', str(demo['generation']), '--writable-sandbox',
               '--port', str(port), '--auth-file', str(AUTH),
               '--static-dir', str(FRONTEND / 'dist'), '--no-market-refresh']
    say()
    say(f'正在启动：{base}/   （关闭本窗口或按 Ctrl+C 停止服务）')
    say()
    server = subprocess.Popen(command, cwd=str(ROOT))
    saved = False
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if server.poll() is not None:
                say()
                say(f'[错误] 服务启动失败（退出码 {server.returncode}），请查看上方输出。')
                return 1
            if healthy(port):
                break
            time.sleep(0.4)
        else:
            say()
            say('[错误] 90 秒内未能连接服务，请查看上方输出。')
            return 1
        STATE.write_text(json.dumps({'port': port, 'pid': server.pid,
                                     'database': demo['database'],
                                     'uuid': demo['uuid']}, ensure_ascii=False, indent=2),
                         encoding='utf-8')
        saved = True
        open_browser()
        say()
        say(f'已打开 {base}/ 。登录后即可使用；其中数据全部为合成示例，不是真实账本。')
        say('这个窗口保持打开即为服务运行中；按 Ctrl+C 可停止。')
        server.wait()
        return 0
    except KeyboardInterrupt:
        say()
        say('正在停止服务…')
        return 0
    finally:
        if saved:
            STATE.unlink(missing_ok=True)
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()


def main() -> int:
    usable_console()
    say('Stock Manager 本地预览（合成演示库，仅本机 127.0.0.1 可访问）')
    say('=' * 58)
    try:
        ensure_dependencies()
        demo = load_demo()
        port, already_running = choose_port(demo)
        if not already_running:
            ensure_auth(port)
        return serve(demo, port, already_running)
    except PreviewError as exc:
        say()
        say('[错误] ' + str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
