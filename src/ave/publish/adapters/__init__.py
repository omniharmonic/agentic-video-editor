"""Adapter registry. Direct adapters: youtube, facebook. Anything else goes through the experimental aggregator, or (studio platforms)
through `ave.platforms.registry`."""

from __future__ import annotations

from .base import Publisher

DIRECT = ("youtube", "facebook")
AGGREGATED = ("instagram", "x", "tiktok")
# Platforms only the studio layer knows (`ave.platforms`); queue items for them preflight against an UNBOUND (dry-run only) adapter
# unless the caller passes account-bound adapters via `publishers=`.
STUDIO = ("bluesky", "threads", "linkedin", "pinterest", "manual")
ALL_PLATFORMS = DIRECT + AGGREGATED + STUDIO


def get_publisher(name: str, **kw) -> Publisher:
    if name == "youtube":
        from .youtube import YouTubePublisher
        return YouTubePublisher(**kw)
    if name == "facebook":
        from .facebook import FacebookPublisher
        return FacebookPublisher(**kw)
    if name in AGGREGATED:
        from .aggregator import AggregatorPublisher
        return AggregatorPublisher(network=name, **kw)
    from ...platforms import registry          # E3: everything else is the studio registry's call (it raises ValueError for unknown names)
    return registry.get_publisher(name, **kw)
