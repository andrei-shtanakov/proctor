"""Shared fnmatch-glob heuristics.

One home for the glob-heuristic family: config validation uses
subsumption (`is_strictly_broader`), the TaskRouter scope invariant
uses overlap (`patterns_overlap`). Both are heuristics over fnmatch
semantics — deliberately conservative, favouring false positives.
"""

from fnmatch import fnmatchcase


def is_strictly_broader(a: str, b: str) -> bool:
    """True if fnmatch pattern ``a`` strictly subsumes pattern ``b``.

    Heuristic: treat ``b`` as a literal string. If ``fnmatch(b, a)``
    matches and ``fnmatch(a, b)`` does not, then ``a`` covers every
    concrete value that ``b`` covers, plus more.
    """
    return fnmatchcase(b, a) and not fnmatchcase(a, b)


def _is_path_prefix(prefix: str, path: str) -> bool:
    """True if ``prefix`` is a whole-segment path prefix of ``path``."""
    return path.startswith(prefix.rstrip("/") + "/")


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative overlap test between two scope globs.

    Two patterns conflict if either fnmatches the other or one is a
    path-prefix of the other. May report overlap where none exists
    (queues a runnable task); must not miss a real conflict.
    """
    return (
        fnmatchcase(a, b)
        or fnmatchcase(b, a)
        or _is_path_prefix(a, b)
        or _is_path_prefix(b, a)
    )
