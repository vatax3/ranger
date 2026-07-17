"""
Construction des objets stream Stremio.

Conventions de nommage compatibles AIOStreams (héritées de Torrentio) :
  - "[AD+]"        -> service identifié + confirmé en cache
  - "[AD download]" -> service identifié, torrent non caché (clic = ajout)
"""

from utils import format_size

DEBRID_TAGS = {"alldebrid": "AD", "realdebrid": "RD", "torbox": "TB", "debridlink": "DL"}

SOURCE_EMOJIS = {
    "ygg": "🐝 YGG",
    "abn": "🎬 ABN",
    "c411": "📡 C411",
    "torr9": "🔥 Torr9",
    "tr4ker": "🎯 Tr4ker",
    "nyaa": "🐈 Nyaa",
    "nekobt": "🐾 NekoBT",
    "apibay": "🏴‍☠️ TPB",
    "eztv": "📺 EZTV",
    "unit3d": "🌐",
    "torznab": "🗂️",
}


def _tracker_label(torrent):
    source = torrent.get("source", "")
    label = SOURCE_EMOJIS.get(source, f"🌐 {source}")
    if source in ("unit3d", "torznab"):
        raw = torrent.get("tracker_name", "")
        if raw.startswith("http"):
            from urllib.parse import urlparse
            raw = (urlparse(raw).hostname or raw).split(".")[0].capitalize()
        label = f"{label} {raw}".strip()
    return label


def _description(torrent):
    meta = torrent["_meta"]
    tech = []
    if meta["resolution"]:
        tech.append(f"📺 {meta['resolution']}")
    if meta["source"]:
        tech.append(f"📦 {meta['source']}")
    if meta["codec"]:
        tech.append(f"🎞️ {meta['codec']}")
    if meta["hdr"]:
        tech.append(f"✨ {' '.join(meta['hdr'])}")
    if meta["audio"]:
        tech.append(f"🔊 {' '.join(meta['audio'][:2])}")

    stats = [f"💾 {format_size(torrent.get('size', 0))}"]
    seeders = torrent.get("seeders")
    if seeders:
        stats.append(f"👤 {seeders}")
    stats.append(_tracker_label(torrent))

    lines = []
    if tech:
        lines.append(" | ".join(tech))
    if meta["lang_display"]:
        lines.append(meta["lang_display"])
    lines.append(torrent.get("name", ""))
    lines.append(" ".join(stats))
    return "\n".join(lines)


def _behavior_hints(torrent, service=None):
    meta = torrent["_meta"]
    hints = {
        "bingeGroup": f"ranger|{service or 'p2p'}|{meta['resolution']}|{meta['codec']}",
        "filename": torrent.get("name", ""),
    }
    size = torrent.get("size", 0)
    if size:
        hints["videoSize"] = size
    return hints


def build_debrid_stream(torrent, service, cached, resolve_url):
    """Stream débrideur (caché ou non). resolve_url : URL /resolve à la lecture."""
    meta = torrent["_meta"]
    tag = DEBRID_TAGS.get(service, service.upper())
    status = f"[{tag}+]" if cached else f"[{tag} download]"
    res = f" {meta['resolution']}" if meta["resolution"] else ""

    description = _description(torrent)
    if not cached:
        description = "📥 Non caché — lecture = ajout au débrideur\n" + description

    return {
        "name": f"Ranger {status}{res}",
        "description": description,
        "url": resolve_url,
        "behaviorHints": _behavior_hints(torrent, service),
    }


def build_p2p_stream(torrent):
    """Stream P2P via le moteur torrent intégré de Stremio (sans débrideur)."""
    meta = torrent["_meta"]
    res = f" {meta['resolution']}" if meta["resolution"] else ""
    return {
        "name": f"Ranger [P2P]{res}",
        "description": _description(torrent),
        "infoHash": torrent["info_hash"],
        "behaviorHints": _behavior_hints(torrent),
    }
