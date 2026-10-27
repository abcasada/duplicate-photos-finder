"""Named drive pools from a TOML config: sets of drives compared only among themselves.

A pool is a list of paths or glob patterns (`*`, `?`, `[..]`, matched per path segment
as in a shell). Patterns match both the directories mounted now and the roots in the
index, so a pool also covers drives that are unplugged but were scanned before.

An optional `[backups]` table links pools: `B = "A"` says pool B is the backup of pool A.
Duplicates are never searched across pools; a link only enables the backup coverage
check (which files of one pool have no copy anywhere in the other).
"""

from __future__ import annotations

import dataclasses
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
    """A named set of drives. `backs_up` is the casefolded name of the pool it backs up."""

    name: str
    patterns: tuple[str, ...]
    backs_up: str | None = None


BACKUP = "backup"  # the other pool is a backup of this one
PRIMARY = "primary"  # this pool is a backup of the other one


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
        "# Optional: pool B is the backup of pool A (enables the backup coverage check).\n"
        "[backups]\n"
        'B = "A"\n'
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
    for key, primary in parse_backups(data, pools, path).items():
        pools[key] = dataclasses.replace(pools[key], backs_up=primary)
    return pools


def parse_backups(
    data: Mapping[str, object], pools: Pools, path: str = "pools file"
) -> dict[str, str]:
    """The optional [backups] table as {backup pool: pool it backs up}, casefolded.

    Both pools must be defined, a pool cannot back up itself, and links cannot form a
    cycle (B backs up A, A backs up B).
    """
    table = data.get("backups")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise PoolError(f'{path}: [backups] must be a table of BACKUP = "POOL" entries')
    links: dict[str, str] = {}
    for backup, primary in table.items():
        if not isinstance(primary, str) or not primary:
            raise PoolError(f'{path}: [backups] {backup} must name one pool, e.g. {backup} = "A"')
        for name in (backup, primary):
            if name.casefold() not in pools:
                defined = ", ".join(p.name for p in pools.values())
                raise PoolError(
                    f"{path}: [backups] names unknown pool {name}; defined pools: {defined}"
                )
        key = backup.casefold()
        if key in links:
            raise PoolError(f"{path}: [backups] lists pool {pools[key].name} twice")
        if primary.casefold() == key:
            raise PoolError(f"{path}: [backups] pool {backup} cannot back up itself")
        links[key] = primary.casefold()
    for start in links:
        seen, cur = [start], links[start]
        while cur in links:
            if cur in seen:
                cycle = " → ".join(pools[k].name for k in [*seen[seen.index(cur):], cur])
                raise PoolError(f"{path}: [backups] links form a cycle: {cycle}")
            seen.append(cur)
            cur = links[cur]
    return links


def coverage_partners(pools: Pools, pool: Pool) -> list[tuple[Pool, str]]:
    """Pools linked to `pool` by [backups], with the other pool's role.

    (P, PRIMARY) when `pool` backs up P; (Q, BACKUP) for every Q that backs up `pool`.
    """
    found: list[tuple[Pool, str]] = []
    if pool.backs_up is not None and pool.backs_up in pools:
        found.append((pools[pool.backs_up], PRIMARY))
    key = pool.name.casefold()
    found += [(p, BACKUP) for p in pools.values() if p.backs_up == key]
    return found


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
