"""License terms per stock provider, as data (spec 5.1, 11 WS1: "data inside broll/, not licenses.py").

Pexels and Pixabay come from `assets.licenses.PROVIDER_TERMS` unchanged. Unsplash and Coverr are added here. EVERY entry is an UNVERIFIED
paraphrase written from memory of the public terms (`verified: False`): a human reads the live terms before relying on them, then updates
the snapshot. Nothing here has been checked against a live page.
"""

from __future__ import annotations

from ..assets import licenses

SNAPSHOT = "2026-10-19"

EXTRA_TERMS = {
    "unsplash": {
        "license": "Unsplash License",
        "license_url": "https://unsplash.com/license",
        "snapshot_date": SNAPSHOT,
        "verified": False,
        "text": ("(UNVERIFIED) Free to use for commercial and non-commercial purposes without asking or crediting, and may be modified. "
                 "Not allowed: selling unaltered copies, or compiling photos to build a competing stock service. API guidelines (UNVERIFIED): "
                 "hotlink or download via the API, trigger the photo's download-tracking endpoint when a photo is downloaded, and credit the "
                 "photographer and Unsplash with links where the photo is shown. Recorded here as attribution required, to be safe."),
        "attribution_required": True,
        "restrictions": [
            "Identifiable people, logos and property are not guaranteed to be released.",
            "Do not resell the unaltered file or build a competing library from the API.",
            "API use: the download-tracking call is made when a human picks the photo (best effort, noted in the log entry).",
        ],
    },
    "coverr": {
        "license": "Coverr License",
        "license_url": "https://coverr.co/license",
        "snapshot_date": SNAPSHOT,
        "verified": False,
        "text": ("(UNVERIFIED) Free stock video for commercial and non-commercial projects; attribution is not required but appreciated. "
                 "Not allowed (UNVERIFIED): redistributing or reselling the clips as stock, implying endorsement by depicted people or brands. "
                 "The API is available by request, with a key; the API terms may ask apps to report downloads (not implemented here)."),
        "attribution_required": False,
        "restrictions": [
            "Identifiable people and logos are not guaranteed to be released.",
            "Do not resell or redistribute the unaltered clip as stock.",
            "The API may require download reporting; this client does not send it. Check the terms before heavy use.",
        ],
    },
}


def terms_for(provider: str) -> dict:
    t = {**licenses.PROVIDER_TERMS, **EXTRA_TERMS}.get(provider)
    if t is None:
        raise KeyError(f"no license terms recorded for provider {provider!r}")
    return t
