"""Installation-scoped formal database identity; never selected by launch arguments."""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from backend.infrastructure.database import DatabaseError, absolute_path


@dataclass(frozen=True)
class Binding:
    path: Path
    uuid: str
    generation: int


def load_binding(file: Path) -> Binding:
    try:
        if file.is_symlink() or not file.is_file():
            raise ValueError('binding must be a regular file')
        data = json.loads(file.read_text(encoding='utf-8'))
        path = Path(data['path'])
        if (path != absolute_path(path) or str(UUID(data['uuid'])) != data['uuid']
                or type(data['generation']) is not int or data['generation'] < 1):
            raise ValueError('invalid binding')
        return Binding(path, data['uuid'], data['generation'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise DatabaseError('formal binding missing or invalid; explicit initialization/adoption required') from exc


def publish_binding(file: Path, binding: Binding) -> None:
    """Atomic no-overwrite publication; caller holds installation lock."""
    temp = file.with_name(f'.binding-{uuid4().hex}.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump({'path': str(binding.path), 'uuid': binding.uuid,
                       'generation': binding.generation}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, file)
    finally:
        temp.unlink(missing_ok=True)
