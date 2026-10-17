import posixpath
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class PathPlan:
    src: str   # normalized source path, no trailing slash
    dest: str  # location inside the staging dir, relative, no leading slash


def plan_paths(entries: list[str]) -> tuple[list[PathPlan], list[str]]:
    """
    Maps each `paths:` entry to its location inside the archive.

      "/etc/haproxy"          -> etc/haproxy            (full source path is kept)
      "/etc/haproxy:config"   -> config/haproxy         (alias = folder that holds it)

    If two aliased entries would land on the same location (e.g. two different
    ".../marzneshin" directories, both aliased "data"), they are stored under
    their full source path instead (data/etc/opt/marzneshin, data/var/lib/marzneshin)
    so one can never overwrite the other. Raises ValueError for unusable entries.
    Returns (plans, notes); notes describe anything that was adjusted.
    """
    notes: list[str] = []
    parsed: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()

    for entry in entries:
        src_raw, sep, alias_raw = entry.partition(':')
        src = posixpath.normpath(src_raw.strip()) if src_raw.strip() else ''
        if src in ('', '.', '/'):
            raise ValueError(f"Invalid path entry {entry!r}: a concrete source path is required.")
        if '..' in src.split('/'):
            raise ValueError(f"Invalid path entry {entry!r}: '..' is not allowed.")

        alias = None
        if sep:
            alias = posixpath.normpath(alias_raw.strip().strip('/')) if alias_raw.strip().strip('/') else ''
            if alias in ('', '.') or '..' in alias.split('/'):
                raise ValueError(f"Invalid alias in path entry {entry!r}.")

        if (src, alias) in seen:
            notes.append(f"Duplicate path entry ignored: {entry!r}")
            continue
        seen.add((src, alias))
        parsed.append((src, alias))

    defaults = [
        f"{alias}/{posixpath.basename(src)}" if alias else src.lstrip('/')
        for src, alias in parsed
    ]
    counts = Counter(defaults)

    plans: list[PathPlan] = []
    for (src, alias), dest in zip(parsed, defaults):
        if alias and counts[dest] > 1:
            resolved = f"{alias}/{src.lstrip('/')}"
            notes.append(f"'{src}' would collide at '{dest}' inside the archive; stored as '{resolved}' instead.")
            dest = resolved
        plans.append(PathPlan(src, dest))

    clashes = sorted(d for d, n in Counter(p.dest for p in plans).items() if n > 1)
    if clashes:
        raise ValueError(f"Several path entries map to the same location in the archive: {', '.join(clashes)}")
    return plans, notes
