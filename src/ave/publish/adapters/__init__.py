"""Adapter registry. Direct adapters: youtube, facebook. Anything else goes through the experimental aggregator."""

from __future__ import annotations

from .base import Publisher

DIRECT = ("youtube", "facebook")
AGGREGATED = ("instagram", "x", "tiktok")
ALL_PLATFORMS = DIRECT + AGGREGATED


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
    raise ValueError(f"unknown platform {name!r}; choose from {', '.join(ALL_PLATFORMS)}. "
                     "Direct Instagram/X/TikTok are not implemented: they route through the aggregator adapter.")
