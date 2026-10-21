"""Atomic local JSON writes with one validated recovery copy per file."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def read_json(path, *, validate=None, on_recover=None):
    path = Path(path)
    errors = []
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict) or (validate and not validate(value)):
                raise ValueError("Invalid saved-data structure")
            if candidate != path:
                log.warning("Recovered local data from %s", candidate.name)
                if on_recover:
                    on_recover(candidate)
            return value
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(exc)
    if not path.exists() and not path.with_suffix(path.suffix + ".bak").exists():
        raise FileNotFoundError(path)
    raise ValueError(
        f"Cannot read {path.name} or its recovery copy; existing files were retained"
    ) from errors[0]


def _replace_bytes(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def write_json(path, value, *, preserve_backup=False):
    path = Path(path)
    content = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = path.read_bytes()
        if preserve_backup:
            damaged = path.with_suffix(path.suffix + ".corrupt")
            if not damaged.exists():
                _replace_bytes(damaged, previous)
        try:
            if not preserve_backup and isinstance(json.loads(previous), dict):
                _replace_bytes(path.with_suffix(path.suffix + ".bak"), previous)
        except (ValueError, UnicodeError):
            # Preserve a damaged source without replacing an existing good backup.
            damaged = path.with_suffix(path.suffix + ".corrupt")
            if not damaged.exists():
                _replace_bytes(damaged, previous)
    _replace_bytes(path, content)


def transaction(state, mutate, save):
    """Restore in-memory state too if its disk commit fails."""
    previous = copy.deepcopy(state)
    try:
        result = mutate()
        save()
        return result
    except Exception:
        state.clear()
        state.update(previous)
        raise
