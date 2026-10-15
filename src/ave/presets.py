"""Render presets: codec, quality and loudness targets. Data, not logic, so a human can tune them.

Loudness: `lufs` is the integrated target; `tp` is the loudnorm true-peak ceiling. It sits at -1.5 dBTP (not -1.0) on purpose:
lossy encoding (AAC/MP3) overshoots the pre-encode true peak by up to ~0.5 dB, and the delivered file must still be <= -1 dBTP.
`height` (proxy only) renders the whole picture smaller, which is what makes drafts fast.
"""

from __future__ import annotations

PRESETS: dict[str, dict] = {
    # Archive/master: 1080p-class H.264 at visually lossless quality, -16 LUFS (podcast/web norm).
    "master": {"kind": "video", "ext": ".mp4", "crf": 18, "x264_preset": "medium", "audio": "aac", "audio_kbps": 256,
               "lufs": -16.0, "tp": -1.5, "height": None,
               "about": "Canvas size, H.264 CRF 18, AAC 256k, -16 LUFS / -1.5 dBTP ceiling"},
    # YouTube normalizes to -14 LUFS; delivering there avoids their re-gain.
    "youtube": {"kind": "video", "ext": ".mp4", "crf": 18, "x264_preset": "medium", "audio": "aac", "audio_kbps": 256,
                "lufs": -14.0, "tp": -1.5, "height": None,
                "about": "As master but -14 LUFS (YouTube / shorts loudness)"},
    # Review drafts: small and quick. Overlay/caption geometry scales with the picture.
    "proxy": {"kind": "video", "ext": ".mp4", "crf": 28, "x264_preset": "veryfast", "audio": "aac", "audio_kbps": 128,
              "lufs": -16.0, "tp": -1.5, "height": 540,
              "about": "540p draft, CRF 28 veryfast, AAC 128k, -16 LUFS"},
    # Clip previews for the review UI (`ave clips previews`): small, fast to seek, hardware-decodes big sources.
    "preview": {"kind": "video", "ext": ".mp4", "crf": 27, "x264_preset": "veryfast", "audio": "aac", "audio_kbps": 96,
                "lufs": -16.0, "tp": -1.5, "height": 540, "hwaccel": True,
                "about": "540p review preview, CRF 27 veryfast, AAC 96k, -16 LUFS, VideoToolbox decode"},
    # Audio only, AAC in .m4a (podcast apps, Resolve import).
    "audio_only": {"kind": "audio", "ext": ".m4a", "audio": "aac", "audio_kbps": 256, "lufs": -16.0, "tp": -1.5,
                   "about": "AAC 256k .m4a, -16 LUFS"},
    # Podcast RSS feeds still want MP3.
    "podcast_mp3": {"kind": "audio", "ext": ".mp3", "audio": "mp3", "audio_kbps": 160, "lufs": -16.0, "tp": -1.5,
                    "about": "MP3 160k, -16 LUFS"},
    # --- Vertical shorts (1080x1920 comes from the short EditDoc's canvas). H.264 High/yuv420p/BT.709/faststart come from the
    # compiler; AAC 192k, -14 LUFS (what the feeds normalize to), -1.5 dBTP ceiling. They are separate keys, not one, so a
    # platform can diverge later (and so `ave render --preset shorts_x` says what it is). Per-platform LIMITS live in config/shorts.yaml.
    **{f"shorts_{k}": {"kind": "video", "ext": ".mp4", "crf": 18, "x264_preset": "medium", "audio": "aac", "audio_kbps": 192,
                       "lufs": -14.0, "tp": -1.5, "height": None, "about": f"Vertical short for {k}: H.264 CRF 18, AAC 192k, -14 LUFS / -1.5 dBTP"}
       for k in ("youtube", "reels", "facebook", "x")},
}


def get(name: str) -> dict:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown preset {name!r}; choose one of {sorted(PRESETS)}") from None
