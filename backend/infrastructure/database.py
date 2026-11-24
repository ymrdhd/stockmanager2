"""Single-file SQLite identity, lifecycle and transaction boundary."""

from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = ROOT / 'data' / 'stock_manager.sqlite'
DEFAULT_BINDING = ROOT / "data" / "official.json"
SCHEMA_VERSION = 7
REVISION = '0007'
BUSY_TIMEOUT_MS = 5000
Role = Literal['formal', 'sandbox', 'backup']
InitialRole = Literal['formal', 'sandbox']


class DatabaseError(RuntimeError):
    """A database safety check failed; callers must stop rather than switch."""


def absolute_path(path: Path) -> Path:
    if not path.is_absolute():
        raise DatabaseError('database paths must be absolute')
    return path.resolve()


def _single_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_nlink != 1:
        raise DatabaseError('database must be an existing regular file without hard links')


@dataclass(frozen=True)
class Identity:
    database_uuid: str
    instance_role: Role
    generation: int
    schema_version: int
    created_at: str

    @classmethod
    def new(cls, role: InitialRole) -> 'Identity':
        return cls(str(uuid4()), role, 1, SCHEMA_VERSION, datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class DatabaseSettings:
    path: Path
    expected_uuid: str
    generation: int
    official_path: Path = DEFAULT_DATABASE
    mode: Literal['server', 'local'] = 'server'
    writable_sandbox: bool = False
    binding_path: Path = DEFAULT_BINDING

    def __post_init__(self) -> None:
        object.__setattr__(self, 'path', absolute_path(self.path))
        object.__setattr__(self, 'official_path', absolute_path(self.official_path))
        try:
            if str(UUID(self.expected_uuid)) != self.expected_uuid:
                raise ValueError('UUID must be canonical')
        except (ValueError, AttributeError) as exc:
            raise DatabaseError('expected database UUID must be canonical') from exc
        if type(self.generation) is not int or self.generation < 1:
            raise DatabaseError('expected generation must be a positive integer')
        if self.mode == 'server':
            if self.path != self.official_path or self.writable_sandbox:
                raise DatabaseError('server must use configured formal (official) path only')
        elif self.mode == 'local':
            if self.path == self.official_path or (
                self.path.exists() and self.official_path.exists()
                and self.path.samefile(self.official_path)
            ):
                raise DatabaseError('local mode must use a distinct snapshot or sandbox')
        else:
            raise DatabaseError('client must not open a local database')

    @property
    def writable(self) -> bool:
        return self.mode == 'server' or self.writable_sandbox


@contextmanager
def writer_lock(path: Path) -> Iterator[None]:
    """Persistent sidecar inode; OS releases the lock on process death.

    Do not unlink the sidecar, even on normal exit. Canonical paths and rejection
    of hard-linked database files prevent ordinary path aliases bypassing it.
    """
    lock_path = path.with_name(path.name + '.writer.lock')
    if lock_path.is_symlink():
        raise DatabaseError('writer lock must not be a symlink')
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1 or not lock_path.is_file():
            raise DatabaseError('invalid writer lock file')
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            elif os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                raise DatabaseError('unsupported writer lock platform')
        except OSError as exc:
            raise DatabaseError('writer lock unavailable; another writer may be running') from exc
        yield
    finally:
        os.close(fd)


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    _single_file(path)
    mode = 'ro' if readonly else 'rw'
    conn = sqlite3.connect(f'{path.as_uri()}?mode={mode}', uri=True,
                           timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        if readonly:
            conn.execute('PRAGMA query_only=ON')
        return conn
    except BaseException:
        conn.close()
        raise


def read_identity(conn: sqlite3.Connection, *, allow_previous: bool = False) -> Identity:
    try:
        rows = conn.execute('SELECT database_uuid, instance_role, generation, schema_version, created_at FROM system_metadata').fetchall()
        versions = conn.execute('SELECT version_num FROM alembic_version').fetchall()
        if len(rows) != 1:
            raise DatabaseError('invalid metadata row count')
        expected = {SCHEMA_VERSION: REVISION, **({1: '0001', 2: '0002', 3: '0003', 4: '0004', 5: '0005', 6: '0006'} if allow_previous else {})}
        if rows[0][3] not in expected or versions != [(expected[rows[0][3]],)]:
            raise DatabaseError('incompatible database metadata or Alembic revision')
        identity = Identity(*rows[0])
        if (str(UUID(identity.database_uuid)) != identity.database_uuid
                or identity.instance_role not in ('formal', 'sandbox', 'backup')
                or type(identity.generation) is not int or identity.generation < 1
                or identity.schema_version not in expected
                or datetime.fromisoformat(identity.created_at).utcoffset() is None):
            raise DatabaseError('invalid or incompatible database identity')
        return identity
    except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
        raise DatabaseError('cannot validate database identity or schema') from exc


def validate_identity(conn: sqlite3.Connection, settings: DatabaseSettings, *, allow_previous: bool = False) -> Identity:
    identity = read_identity(conn, allow_previous=allow_previous)
    if (identity.database_uuid != settings.expected_uuid or identity.generation != settings.generation
            or (settings.mode == 'server' and identity.instance_role != 'formal')
            or (settings.writable_sandbox and identity.instance_role != 'sandbox')):
        raise DatabaseError('database UUID, role or generation mismatch')
    return identity


def _enable_wal(conn: sqlite3.Connection) -> None:
    if conn.execute('PRAGMA journal_mode=WAL').fetchone() != ('wal',):
        raise DatabaseError('cannot enable SQLite WAL mode')


class Database:
    """Valid only inside open_database; owns no long-lived SQLite connection."""

    def __init__(self, settings: DatabaseSettings, identity: Identity) -> None:
        self.settings = settings
        self.identity = identity
        self._active = True

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        if not self._active:
            raise DatabaseError('database lifecycle has ended')
        validate_binding(self.settings)
        with closing(_connect(self.settings.path, readonly=True)) as conn:
            conn.execute('BEGIN')
            try:
                validate_identity(conn, self.settings)
                yield conn
            finally:
                conn.rollback()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Caller uses execute/executemany, never commit/rollback/executescript."""
        if not self._active or not self.settings.writable:
            raise DatabaseError('database is not open for writing')
        validate_binding(self.settings)
        with closing(_connect(self.settings.path, readonly=False)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                validate_identity(conn, self.settings)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise


def validate_binding(settings: DatabaseSettings) -> None:
    from backend.infrastructure.binding import load_binding
    bound = load_binding(settings.binding_path)
    if bound.path != settings.official_path or (settings.mode == 'server' and
            (bound.path, bound.uuid, bound.generation) !=
            (settings.path, settings.expected_uuid, settings.generation)):
        raise DatabaseError('configuration conflicts with fixed formal binding')


@contextmanager
def open_database(settings: DatabaseSettings) -> Iterator[Database]:
    validate_binding(settings)
    _single_file(settings.path)  # no lock sidecar or SQLite file on missing path
    guard = writer_lock(settings.path) if settings.writable else nullcontext()
    with guard:
        # Validate without changing journal mode or any database content first.
        with closing(_connect(settings.path, readonly=True)) as conn:
            identity = validate_identity(conn, settings)
        if settings.writable:
            with closing(_connect(settings.path, readonly=False)) as conn:
                validate_identity(conn, settings)
                _enable_wal(conn)
        db = Database(settings, identity)
        try:
            yield db
        finally:
            db._active = False


def migrate_schema(engine: Engine, identity: Identity, *, existing: bool = False, target: str = REVISION) -> None:
    """Run ordered Alembic and metadata update in one writer-locked transaction."""
    config = Config()
    config.set_main_option('script_location', str(ROOT / 'migrations').replace('%', '%%'))
    with engine.connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        conn.exec_driver_sql(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        # sqlite3 legacy transaction mode does not automatically transact DDL.
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            if existing:
                actual = conn.exec_driver_sql('SELECT database_uuid,instance_role,generation,schema_version,created_at FROM system_metadata WHERE id=1').fetchone()
                if actual is None or tuple(actual) != tuple(asdict(identity).values()) or conn.exec_driver_sql('SELECT version_num FROM alembic_version').fetchall() != [(f'{identity.schema_version:04d}',)]:
                    raise DatabaseError('database identity changed before migration')
            config.attributes['connection'] = conn
            command.upgrade(config, target)
            if existing:
                conn.execute(text("UPDATE system_metadata SET schema_version=:version WHERE id=1"), {"version": SCHEMA_VERSION})
            else:
                conn.execute(text('INSERT INTO system_metadata (id, database_uuid, instance_role, generation, schema_version, created_at) VALUES (1, :database_uuid, :instance_role, :generation, :schema_version, :created_at)'), asdict(identity))
            expected_version = SCHEMA_VERSION if existing else identity.schema_version
            if (conn.exec_driver_sql('SELECT schema_version FROM system_metadata WHERE id=1').fetchall() != [(expected_version,)]
                    or conn.exec_driver_sql('SELECT version_num FROM alembic_version').fetchall() != [(target,)]
                    or conn.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
                    or conn.exec_driver_sql('PRAGMA integrity_check').fetchall() != [('ok',)]):
                raise DatabaseError('migration verification failed before commit')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _initialize_database(path: Path, *, role: InitialRole, confirm: bool,
                        official_path: Path | None = None) -> Identity:
    if not confirm or role not in ('formal', 'sandbox'):
        raise DatabaseError('explicit initialization confirmation and valid role required')
    if path.is_symlink():
        raise DatabaseError('cannot initialize via a symlink')
    path = absolute_path(path)
    official = absolute_path(official_path or (path if role == 'formal' else DEFAULT_DATABASE))
    if (role == 'formal' and path != official) or (role == 'sandbox' and path == official):
        raise DatabaseError('initialization path conflicts with official database role')
    if path.exists():
        raise DatabaseError('database already exists; initialization never overwrites')
    path.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(path):
        # Never open an existing database or orphaned SQLite sidecars in initialization.
        if any(p.exists() or p.is_symlink() for p in
               (Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
            raise DatabaseError('orphaned SQLite sidecars; manual inspection required')
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise DatabaseError('database already exists; initialization never overwrites') from exc
        os.close(fd)
        identity = Identity.new(role)
        try:
            engine = create_engine(URL.create('sqlite', database=str(path)))
            try:
                migrate_schema(engine, identity)
            finally:
                engine.dispose()
            with closing(_connect(path, readonly=False)) as conn:
                _enable_wal(conn)
                if read_identity(conn) != identity:
                    raise DatabaseError('initialization identity verification failed')
        except BaseException:
            # Only the file exclusively created by this operation is eligible.
            for suffix in ('-wal', '-shm', '-journal', ''):
                Path(str(path) + suffix).unlink(missing_ok=True)
            raise
    return identity


def initialize_database(path: Path, *, role: InitialRole, confirm: bool,
                        official_path: Path | None = None, binding_path: Path = DEFAULT_BINDING) -> Identity:
    from backend.infrastructure.binding import Binding, load_binding, publish_binding
    if not confirm:
        raise DatabaseError('explicit initialization confirmation required')
    if binding_path.is_symlink():
        raise DatabaseError('formal binding must not be a symlink')
    binding_path = absolute_path(binding_path)
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(binding_path):
        if role == 'sandbox':
            bound = load_binding(binding_path)
            if official_path is not None and absolute_path(official_path) != bound.path:
                raise DatabaseError('sandbox conflicts with fixed formal binding')
            return _initialize_database(path, role=role, confirm=True, official_path=bound.path)
        if binding_path.exists() or binding_path.is_symlink():
            raise DatabaseError('formal binding already exists; cannot initialize another formal database')
        identity = _initialize_database(path, role=role, confirm=True, official_path=official_path)
        # If publication fails, leave the new database intact for explicit adoption;
        # never delete an existing binding or guess which database to use.
        publish_binding(binding_path, Binding(absolute_path(path), identity.database_uuid, identity.generation))
        return identity


def adopt_database(settings: DatabaseSettings, *, confirm: bool) -> Identity:
    from backend.infrastructure.binding import Binding, publish_binding
    if not confirm or settings.mode != 'server':
        raise DatabaseError('explicit formal adoption confirmation required')
    file = settings.binding_path
    if file.is_symlink():
        raise DatabaseError('formal binding must not be a symlink')
    file.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(file):
        if file.exists() or file.is_symlink():
            raise DatabaseError('formal binding already exists')
        _single_file(settings.path)
        with writer_lock(settings.path), closing(_connect(settings.path, readonly=True)) as conn:
            conn.execute('BEGIN')
            identity = validate_identity(conn, settings, allow_previous=True)
            if (conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                    or conn.execute('PRAGMA foreign_key_check').fetchall()):
                raise DatabaseError('formal database integrity or foreign key check failed')
            publish_binding(file, Binding(settings.path, identity.database_uuid, identity.generation))
            return identity


def upgrade_database(settings: DatabaseSettings, destination: Path, *, confirm: bool) -> None:
    from backend.infrastructure.backup import create_backup
    if not confirm or settings.mode != 'server':
        raise DatabaseError('explicit formal upgrade confirmation required')
    validate_binding(settings)
    _single_file(settings.path)
    with writer_lock(settings.path):
        with closing(_connect(settings.path, readonly=True)) as conn:
            identity = validate_identity(conn, settings, allow_previous=True)
        if identity.schema_version == SCHEMA_VERSION:
            return
        create_backup(settings, destination, allow_previous=True)
        engine = create_engine(URL.create('sqlite', database=str(settings.path)))
        try:
            migrate_schema(engine, identity, existing=True)
        finally:
            engine.dispose()
        with closing(_connect(settings.path, readonly=True)) as conn:
            validate_identity(conn, settings)
