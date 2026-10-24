"""Back-compat alias: search implementation lives in ``app.shared.search``.

Keeping a single module object means mutable state (rotation counters,
cooldowns) stays shared between the subsidiary feature and any new caller.
"""
import sys

from app.shared import search as _shared_search

sys.modules[__name__] = _shared_search