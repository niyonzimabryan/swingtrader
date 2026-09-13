"""Current-process peak memory, for the bulk backfill's `--max-rss-mb` guard.

`resource.getrusage(RUSAGE_SELF).ru_maxrss` is the one portable-ish way to ask
the OS for a process's own high-water mark with no extra dependency (`psutil`
is not a pinned one), but its unit is not portable: kibibytes on Linux, bytes
on macOS/BSD. Getting that wrong silently under- or over-reports memory by
1024x, which is exactly the kind of error a memory guard must not make.
"""

from __future__ import annotations

import resource
import sys


def max_rss_mb() -> float:
    """This process's peak resident set size so far, in MiB.

    A monotonic high-water mark: it never decreases within a process, which is
    exactly what a sampled abort guard wants — a transient spike is not missed
    just because it happened between samples.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return peak / (1024.0 * 1024.0)
    return peak / 1024.0
