"""
Orchestrateur de recherche : lance en parallèle tous les trackers activés,
avec cache SQLite par tracker. Retourne une liste brute de torrents normalisés.
"""

import asyncio
import logging

from core import cache
from services.unit3d import Unit3DService
from services.ygg import YggService
from services.abn import ABNService
from services.c411 import C411Service
from services.torr9 import Torr9Service
from services.tr4ker import Tr4kerService
from services.nekobt import NekoBTService
from services.nyaa import NyaaService
from services.apibay import ApibayService
from services.eztv import EztvService
from services.torznab import TorznabService


def _search_cache_key(source, stream_type, imdb_id, season, episode):
    part = f"{stream_type}:{imdb_id}"
    if stream_type == "series":
        part += f":{season}:{episode}"
    return f"search:{source}:{part}"


async def _cached_search(source, coro_factory, stream_type, imdb_id, season, episode):
    """Exécute une recherche avec mise en cache SQLite du résultat brut."""
    key = _search_cache_key(source, stream_type, imdb_id, season, episode)
    cached = cache.get_search(key)
    if cached is not None:
        logging.info(f"[{source}] {len(cached)} résultats (cache)")
        return cached
    try:
        results = await coro_factory()
    except Exception as e:
        logging.error(f"[{source}] erreur: {e}")
        return []
    results = results or []
    for r in results:
        r.setdefault("source", source)
    cache.set_search(key, results)
    logging.info(f"[{source}] {len(results)} résultats (frais)")
    return results


async def run_search(config, media_info, stream_type, imdb_id, tmdb_id, season, episode, absolute_episode):
    """Lance toutes les recherches activées et retourne la liste fusionnée brute."""
    trackers = set(config.get("trackers") or [])
    keys = config.get("tracker_keys") or {}
    title = (media_info or {}).get("title", "")
    original_title = (media_info or {}).get("original_title", "")
    year = (media_info or {}).get("year", "")
    is_anime = (media_info or {}).get("is_anime", False)

    tasks = []
    abn_service = None  # référence pour fermeture propre

    def add(source, factory):
        tasks.append(_cached_search(source, factory, stream_type, imdb_id, season, episode))

    # NB : chaque lambda binde son service via `s=<svc>` (argument par défaut).
    # Sans ce binding, toutes les closures partageraient la même variable et
    # taperaient le dernier tracker construit (bug de capture par référence).

    # --- UNIT3D (multi-trackers privés) ---
    unit3d_cfg = [
        {"url": t["url"].rstrip("/"), "token": t.get("key", ""), "categories": []}
        for t in (config.get("unit3d") or []) if t.get("url") and t.get("key")
    ]
    if unit3d_cfg:
        s = Unit3DService(unit3d_cfg)
        add("unit3d", lambda s=s: s.search_all(
            tmdb_id=tmdb_id, imdb_id=imdb_id, type=stream_type, season=season, episode=episode))

    # --- YGG (public, relais Nostr yggleak) ---
    if "ygg" in trackers:
        s = YggService()
        if stream_type == "movie":
            add("ygg", lambda s=s: s.search_movie(title, year, original_title=original_title, imdb_id=imdb_id, tmdb_id=tmdb_id))
        else:
            add("ygg", lambda s=s: s.search_series(title, season, episode, original_title=original_title, imdb_id=imdb_id, tmdb_id=tmdb_id))

    # --- ABN (login/mot de passe) ---
    abn_cfg = config.get("abn") or {}
    if abn_cfg.get("username") and abn_cfg.get("password"):
        abn_service = ABNService(username=abn_cfg["username"], password=abn_cfg["password"])
        if stream_type == "movie":
            add("abn", lambda s=abn_service: s.search_movie(title, year, original_title=original_title))
        else:
            add("abn", lambda s=abn_service: s.search_series(title, season, episode, original_title=original_title))

    # --- C411 (clé API) ---
    if "c411" in trackers and keys.get("c411"):
        s = C411Service(keys["c411"])
        if stream_type == "movie":
            add("c411", lambda s=s: s.search_movie(title, year, imdb_id=imdb_id, tmdb_id=tmdb_id))
        else:
            add("c411", lambda s=s: s.search_series(title, season, episode, imdb_id=imdb_id, tmdb_id=tmdb_id))

    # --- Torr9 (passkey Torznab) ---
    if "torr9" in trackers and keys.get("torr9"):
        s = Torr9Service(keys["torr9"])
        if stream_type == "movie":
            add("torr9", lambda s=s: s.search_movie(title, year, imdb_id=imdb_id, tmdb_id=tmdb_id))
        else:
            add("torr9", lambda s=s: s.search_series(title, season, episode, imdb_id=imdb_id, tmdb_id=tmdb_id))

    # --- Tr4ker (clé API) ---
    if "tr4ker" in trackers and keys.get("tr4ker"):
        s = Tr4kerService(keys["tr4ker"])
        if stream_type == "movie":
            add("tr4ker", lambda s=s: s.search_movie(title, year, imdb_id=imdb_id, tmdb_id=tmdb_id))
        else:
            add("tr4ker", lambda s=s: s.search_series(title, season, episode, imdb_id=imdb_id, tmdb_id=tmdb_id))

    # --- apibay / ThePirateBay (public, VO) ---
    if "apibay" in trackers:
        s = ApibayService()
        if stream_type == "movie":
            add("apibay", lambda s=s: s.search_movie(title, year, original_title=original_title, imdb_id=imdb_id, tmdb_id=tmdb_id))
        else:
            add("apibay", lambda s=s: s.search_series(title, season, episode, original_title=original_title, imdb_id=imdb_id, tmdb_id=tmdb_id))

    # --- EZTV (public, séries VO) ---
    if "eztv" in trackers and stream_type == "series":
        s = EztvService()
        add("eztv", lambda s=s: s.search_series(title, season, episode, imdb_id=imdb_id, tmdb_id=tmdb_id))

    # --- Torznab (Jackett/Prowlarr : trackers publics & privés) ---
    torznab_cfg = config.get("torznab") or []
    if torznab_cfg:
        s = TorznabService(torznab_cfg)
        if stream_type == "movie":
            add("torznab", lambda s=s: s.search_movie(title, year, original_title=original_title, imdb_id=imdb_id, tmdb_id=tmdb_id))
        else:
            add("torznab", lambda s=s: s.search_series(title, season, episode, original_title=original_title, imdb_id=imdb_id, tmdb_id=tmdb_id))

    # --- Trackers anime (uniquement si le média est un anime) ---
    if is_anime:
        if "nekobt" in trackers and keys.get("nekobt"):
            s = NekoBTService(keys["nekobt"])
            if stream_type == "movie":
                add("nekobt", lambda s=s: s.search_movie(title, year, imdb_id=imdb_id, tmdb_id=tmdb_id))
            else:
                add("nekobt", lambda s=s: s.search_series(title, season, episode, imdb_id=imdb_id, tmdb_id=tmdb_id, absolute_episode=absolute_episode))
        if "nyaa" in trackers:
            s = NyaaService()
            if stream_type == "movie":
                add("nyaa", lambda s=s: s.search_movie(title, year, imdb_id=imdb_id, tmdb_id=tmdb_id))
            else:
                add("nyaa", lambda s=s: s.search_series(title, season, episode, imdb_id=imdb_id, tmdb_id=tmdb_id, absolute_episode=absolute_episode))

    if not tasks:
        return []

    try:
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if abn_service:
            await abn_service.close()

    merged = []
    for results in results_list:
        if isinstance(results, Exception):
            logging.error(f"Tâche de recherche échouée: {results}")
            continue
        merged.extend(results)

    logging.info(f"Recherche totale : {len(merged)} torrents bruts")
    return merged
