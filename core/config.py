"""
Configuration Ranger : encodage/décodage de la config utilisateur (base64 JSON
dans l'URL, comme Torrentio) et valeurs par défaut.
"""

import base64
import copy
import json
import logging

# Trackers publics activables par simple case à cocher (pas de clé requise)
PUBLIC_TRACKERS = ["ygg", "apibay", "eztv", "nyaa"]

# Trackers privés/semi-privés nécessitant une clé/passkey
KEYED_TRACKERS = ["c411", "torr9", "tr4ker", "nekobt"]

# Débrideurs supportés (ordre d'affichage par défaut)
DEBRID_SERVICES = ["alldebrid", "realdebrid", "torbox", "debridlink"]

# Critères de tri disponibles
SORT_CRITERIA = ["cached", "quality", "language", "resolution", "size_desc", "size_asc", "seeders", "tracker"]

DEFAULT_CONFIG = {
    # Clé TMDB (recommandée : titres FR, détection anime, épisodes absolus).
    # Sans clé, fallback sur Cinemeta (titres anglais uniquement).
    "tmdb_key": "",

    # Débrideurs, dans l'ordre de priorité. [{"service": "alldebrid", "key": "..."}]
    "debrids": [],

    # "first" : n'affiche que le débrideur prioritaire où le torrent est en cache
    # "all"   : un stream par débrideur où le torrent est en cache
    "debrid_mode": "first",

    # Mode d'affichage des résultats dans Stremio :
    # "detailed" : liste complète (tous les liens + détails) — pour les initiés
    # "simple"   : 1 lien par résolution, meilleur choix auto — pour la famille
    "display_mode": "detailed",

    # StremThru : proxy d'API débrideur (contourne les blocages IP datacenter)
    "stremthru": {"url": "", "auth": ""},

    # Trackers cochés (publics sans clé + privés si clé fournie)
    "trackers": ["ygg", "apibay", "eztv", "nyaa"],

    # Clés des trackers privés/semi-privés
    "tracker_keys": {},          # {"c411": "...", "torr9": "...", "tr4ker": "...", "nekobt": "..."}

    # ABN (login/mot de passe)
    "abn": {"username": "", "password": ""},

    # Trackers UNIT3D : [{"url": "https://...", "key": "..."}]
    "unit3d": [],

    # Indexeurs Torznab (Jackett/Prowlarr) : accès à des centaines de trackers
    # [{"name": "MonJackett", "url": "http://host:9117/api/v2.0/indexers/xxx/results/torznab", "apikey": "..."}]
    "torznab": [],

    "filters": {
        "min_size_gb": 0,
        "max_size_gb": 0,             # 0 = illimité
        "resolutions": [],            # vide = tout, sinon sous-ensemble de ["4K", "1080p", "720p", "SD"]
        "codecs": [],                 # vide = tout, sinon sous-ensemble de ["x265", "x264", "AV1"]
        "languages": [],              # vide = tout, sinon ["MULTI", "VFF", "VF", "VFQ", "VOSTFR", "VO"]
        "exclude_cam": True,
        "exclude_season_packs": False,
        "max_results": 30,            # nombre max de streams renvoyés
        "max_per_resolution": 10,     # 0 = illimité
        "cached_only": False,         # ne montre que les torrents en cache débrideur
        "show_uncached": True,        # affiche les non-cachés (clic = ajout au débrideur)
        "show_p2p": False,            # streams P2P via le moteur torrent de Stremio (sans débrideur)
    },

    # Ordre des critères de tri (appliqués dans l'ordre)
    "sort": ["cached", "quality", "resolution", "language", "size_desc", "seeders", "tracker"],

    # Priorités utilisées par les critères "language" / "resolution" / "tracker"
    "language_order": ["MULTI", "VFF", "VF", "VFQ", "VOSTFR", "VO"],
    "resolution_order": ["4K", "1080p", "720p", "SD"],
    "providers_order": [],
}


def _deep_merge(base, override):
    """Fusionne récursivement override dans base (sans modifier base)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def decode_config(config_str):
    """Décode la config base64 de l'URL et la fusionne avec les défauts."""
    if not config_str:
        return None
    try:
        padded = config_str + "=" * (-len(config_str) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        except Exception:
            decoded = base64.b64decode(padded).decode("utf-8")
        user_config = json.loads(decoded)
        if not isinstance(user_config, dict):
            return None
        return _deep_merge(DEFAULT_CONFIG, user_config)
    except Exception as e:
        logging.error(f"Config decode error: {e}")
        return None


def encode_config(config):
    """Encode une config en base64 URL-safe (utilisé par la page /configure)."""
    raw = json.dumps(config, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def get_debrids(config):
    """
    Liste [(service, key, use_stremthru)] dans l'ordre de priorité, entrées
    valides uniquement. use_stremthru : router ce débrideur via StremThru
    (défaut True pour compat — décochable par débrideur dans la config).
    """
    out = []
    seen = set()
    for entry in config.get("debrids", []):
        service = (entry.get("service") or "").strip().lower()
        key = (entry.get("key") or "").strip()
        if service in DEBRID_SERVICES and key and service not in seen:
            use_stremthru = entry.get("stremthru", True)
            out.append((service, key, bool(use_stremthru)))
            seen.add(service)
    return out
