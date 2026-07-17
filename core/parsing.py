"""
Parsing des noms de release : résolution, codec, source, HDR, audio, langues.
Pensé pour le contenu français (MULTI/VFF/VOSTFR...) comme étranger.
"""

import re

RESOLUTIONS = ["4K", "1080p", "720p", "SD"]
LANGS = ["MULTI", "VFF", "VF", "VFQ", "VOSTFR", "VO"]

LANG_FLAGS = {
    "MULTI": "🇫🇷🇬🇧 MULTI",
    "VFF": "🇫🇷 VFF",
    "VF": "🇫🇷 VF",
    "VFQ": "🇨🇦 VFQ",
    "VOSTFR": "🇯🇵🇫🇷 VOSTFR",
    "VO": "🇬🇧 VO",
}

_WORD = lambda w: re.compile(r"(?<![A-Z0-9])" + w + r"(?![A-Z0-9])")

_RES_PATTERNS = [
    ("4K", re.compile(r"2160P|4K|UHD")),
    ("1080p", re.compile(r"1080P")),
    ("720p", re.compile(r"720P")),
    ("SD", re.compile(r"480P|576P|\bSD\b|DVDRIP")),
]

_CODEC_PATTERNS = [
    ("x265", re.compile(r"X265|HEVC|H\.?265")),
    ("x264", re.compile(r"X264|AVC|H\.?264")),
    ("AV1", _WORD("AV1")),
]

_SOURCE_PATTERNS = [
    ("REMUX", _WORD("REMUX")),
    ("BluRay", re.compile(r"BLU-?RAY|BRRIP|BDRIP|BDLIGHT|\bBD\b")),
    ("WEB-DL", re.compile(r"WEB-?DL")),
    ("WEBRip", _WORD("WEBRIP")),
    ("WEB", _WORD("WEB")),
    ("HDTV", _WORD("HDTV")),
    ("DVDRip", _WORD("DVDRIP")),
    ("CAM", re.compile(r"\bCAM\b|\bHDCAM\b|\bHDTS\b|HD-TS|\bTELESYNC\b|\bTS\b(?!C)")),
]

_AUDIO_PATTERNS = [
    ("Atmos", _WORD("ATMOS")),
    ("TrueHD", re.compile(r"TRUE-?HD")),
    ("DTS-HD", re.compile(r"DTS-?HD")),
    ("DTS", _WORD("DTS")),
    ("DD+", re.compile(r"DDP|EAC-?3|DD\+")),
    ("AC3", re.compile(r"\bAC-?3\b|\bDD5[. ]?1\b")),
    ("AAC", _WORD("AAC")),
]


def parse_release(name):
    """Analyse un nom de release et retourne un dict structuré."""
    if not name:
        return {
            "resolution": "", "codec": "", "source": "", "hdr": [],
            "audio": [], "languages": [], "lang_display": "",
        }

    up = name.upper().replace("_", " ")

    resolution = ""
    for label, pattern in _RES_PATTERNS:
        if pattern.search(up):
            resolution = label
            break

    codec = ""
    for label, pattern in _CODEC_PATTERNS:
        if pattern.search(up):
            codec = label
            break

    source = ""
    for label, pattern in _SOURCE_PATTERNS:
        if pattern.search(up):
            source = label
            break

    hdr = []
    if re.search(r"\bHDR10\+", up):
        hdr.append("HDR10+")
    elif re.search(r"\bHDR", up):
        hdr.append("HDR")
    if re.search(r"\bDV\b|DOLBY[. ]?VISION|\bDOVI\b", up):
        hdr.append("DV")
    if "10BIT" in up or "10-BIT" in up:
        hdr.append("10bit")

    audio = [label for label, pattern in _AUDIO_PATTERNS if pattern.search(up)]

    # Langues — un torrent peut en cumuler (MULTI VFF, VOSTFR + VO...)
    languages = []
    if re.search(r"\bMULTI\b|\bMULTI3\b|\bMULTILANG", up):
        languages.append("MULTI")
    if re.search(r"TRUEFRENCH|\bVFF\b|\bVFI\b|\bVF2\b", up):
        languages.append("VFF")
    if re.search(r"\bVFQ\b", up):
        languages.append("VFQ")
    if re.search(r"\bVOSTFR\b|\bSUBFRENCH\b|\bSTFR\b", up):
        languages.append("VOSTFR")
    if not any(l in languages for l in ("MULTI", "VFF", "VFQ")) and re.search(r"\bFRENCH\b|\bVF\b", up):
        languages.append("VF")
    if not languages:
        # Pas de tag FR : release VO (cas standard des trackers internationaux)
        languages.append("VO")

    display = " ".join(LANG_FLAGS.get(l, l) for l in languages)

    return {
        "resolution": resolution,
        "codec": codec,
        "source": source,
        "hdr": hdr,
        "audio": audio,
        "languages": languages,
        "lang_display": display,
    }


def best_language(languages, language_order):
    """Rang de la meilleure langue du torrent selon l'ordre de préférence."""
    best = len(language_order)
    for lang in languages or []:
        try:
            best = min(best, language_order.index(lang))
        except ValueError:
            continue
    return best


def resolution_rank(resolution, resolution_order):
    try:
        return resolution_order.index(resolution)
    except ValueError:
        return len(resolution_order)
