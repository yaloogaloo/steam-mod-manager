"""Nexus offline provider — manual HTML import (alias module).

Automatic Nexus scraping (requests / Playwright / CDP) has been removed.
Use :class:`NexusManualOfflineProvider` / ``import_offline_page``.
"""

from __future__ import annotations

from services.offline.nexus_manual import (
    NexusManualOfflineProvider,
    NexusOfflineProvider,
    store_snapshot,
    validate_html_path,
)

__all__ = [
    "NexusManualOfflineProvider",
    "NexusOfflineProvider",
    "store_snapshot",
    "validate_html_path",
]
