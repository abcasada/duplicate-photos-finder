"""Named drive pools from a TOML config: sets of drives compared only among themselves.

A pool is a list of paths or glob patterns (`*`, `?`, `[..]`, matched per path segment
as in a shell). Patterns match both the directories mounted now and the roots in the
index, so a pool also covers drives that are unplugged but were scanned before.
"""

from __future__ import annotations

import getpass
import glob
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase

_MAGIC = re.compile(r"[*?[]")


class PoolError(ValueError):
    """A missing or invalid pools file, or a pool that cannot be used."""


@dataclass(frozen=True, slots=True)
class Pool:
    name: str
    patterns: tuple[str, ...]


Pools = Mapping[str, Pool]


def default_pools_path() -> str:
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "dpf", "pools.toml")


def sample_config() -> str:
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = "USER"
    media = f"/run/media/{user}"
    return (
        "# Each pool is a list of paths or glob patterns (fnmatch-style * ? [..]).\n"
        "[pools]\n"
        f'A = ["{media}/Storage 1TB", "{media}/Storage [0-9]A *"]\n'
        f'B = ["{media}/Storage [0-9]B *"]\n'
    )


def load_pools(path: str | None = None) -> dict[str, Pool]:
    """Pools defined in `path` (default: default_pools_path()), keyed by casefolded name."""
    path = path or default_pools_path()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise PoolError(_with_sample(f"no pools file at {path}; create it, e.g.:")) from None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise PoolError(_with_sample(f"cannot read pools file {path}: {e}\nExpected e.g.:"))
    return parse_pools(data, path)


def parse_pools(data: Mapping[str, object], path: str = "pools file") -> dict[str, Pool]:
    table = data.get("pools")
    if not isinstance(table, dict) or not table:
        raise PoolError(_with_sample(f"{path} defines no [pools] table; expected e.g.:"))
    pools: dict[str, Pool] = {}
    for name, value in table.items():
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise PoolError(f"{path}: pool {name} must be a list of paths or glob patterns")
        if not value:
            raise PoolError(f"{path}: pool {name} has no paths or patterns")
        patterns = tuple(_normalize(v) for v in value)
        for p in patterns:
            if not os.path.isabs(p):
                raise PoolError(f"{path}: pool {name}: {p} is not an absolute path")
        key = name.casefold()
        if key in pools:
            raise PoolError(f"{path}: pools {pools[key].name} and {name} differ only in case")
        pools[key] = Pool(name, patterns)
    return pools


def get_pool(pools: Pools, name: str) -> Pool:
    """The pool called `name` (case-insensitive)."""
    pool = pools.get(name.casefold())
    if pool is None:
        defined = ", ".join(p.name for p in pools.values())
        raise PoolError(f"unknown pool {name}; defined pools: {defined}")
    return pool


def is_glob(pattern: str) -> bool:
    return _MAGIC.search(pattern) is not None


def matches(path: str, pattern: str) -> bool:
    """True if `path`, or a folder containing it, matches `pattern`.

    Matching is per path segment, as in glob: `*`, `?` and `[..]` never match "/", so
    "/m/Storage [0-9]A *" covers "/m/Storage 2A 8TB/x" but not
    "/m/Storage 2B 8TB/old/Storage 1A x".
    """
    parts, pattern_parts = _segments(path), _segments(pattern)
    return len(parts) >= len(pattern_parts) and all(
        fnmatchcase(part, pat) for part, pat in zip(parts, pattern_parts)
    )


def _segments(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def pools_of(pools: Pools, path: str) -> list[str]:
    """Names of the pools with a pattern matching `path` (more than one is a config mistake)."""
    return [p.name for p in pools.values() if any(matches(path, pat) for pat in p.patterns)]


def pool_paths(pool: Pool, indexed_roots: Iterable[str]) -> list[str]:
    """Sorted paths a pool stands for, before online/offline root resolution.

    Each pattern contributes the mounted directories it globs to, the indexed roots it
    matches, and itself when it is a literal path (so a literal path that is neither
    connected nor indexed still reaches the usual "never scanned" error). Raises
    PoolError when nothing matches.
    """
    indexed = list(indexed_roots)
    found: set[str] = set()
    for pattern in pool.patterns:
        if not is_glob(pattern):
            found.add(pattern)
        found.update(d.rstrip("/") for d in glob.glob(pattern) if os.path.isdir(d))
        found.update(r for r in indexed if matches(r, pattern))
    if not found:
        listing = "\n".join(f"  {p}" for p in pool.patterns)
        raise PoolError(
            f"pool {pool.name} matches no connected or indexed drive; its patterns are:\n{listing}"
        )
    return sorted(found)


def _normalize(pattern: str) -> str:
    return os.path.expanduser(pattern).rstrip("/") or "/"


def _with_sample(message: str) -> str:
    return f"{message}\n\n{sample_config()}"
