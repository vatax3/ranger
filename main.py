"""
Ranger — Stremio addon multi-trackers / multi-débrideurs.

v1 : films / séries / anime, contenu français comme étranger.
  - Multi-débrideurs avec priorité (AllDebrid, Real-Debrid, TorBox, DebridLink)
  - StremThru intégré (contournement blocage IP datacenter)
  - Trackers publics (YGG, TPB, EZTV, Nyaa) + privés/semi (C411, Torr9, Tr4ker,
    NekoBT, UNIT3D) + Torznab générique (Jackett/Prowlarr)
  - TMDB (titres FR, anime, épisode absolu) avec fallback Cinemeta
  - Filtres taille/résolution/codec/langue, tri multi-critères, déduplication
  - Cache SQLite (dispo débrideur + recherches + métadonnées)
  - Compatible AIOStreams (tags [XX+] / statut de cache)
"""

import asyncio
import logging
import os

import aiofiles
import aiohttp
from aiohttp import web

from core import cache
from core.config import decode_config, DEFAULT_CONFIG
from core.debrid import build_backends
from core.metadata import get_media_info, compute_absolute_episode
from core.search import run_search
from core import ranking
from core import formatting

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

APP_VERSION = "1.0.0"
ADDON_ID = "community.ranger.addon"
PORT = int(os.getenv("PORT", "7000"))
BASE_PATH = os.path.dirname(os.path.abspath(__file__))


# ============================================================================
# Middleware
# ============================================================================

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


def get_client_ip(request):
    """IP publique du lecteur Stremio, transmise à StremThru pour le débrideur."""
    import ipaddress
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = forwarded.split(",")[0].strip() if forwarded else request.remote
    try:
        if ip and ipaddress.ip_address(ip).is_global:
            return ip
    except ValueError:
        pass
    return None


# ============================================================================
# Page de configuration
# ============================================================================

async def handle_configure(request):
    try:
        async with aiofiles.open(os.path.join(BASE_PATH, "templates", "configure.html"),
                                 mode="r", encoding="utf-8") as f:
            content = await f.read()
        content = content.replace("__APP_VERSION__", APP_VERSION)
        return web.Response(text=content, content_type="text/html")
    except Exception as e:
        logging.error(f"configure error: {e}")
        return web.Response(text=str(e), status=500)


# ============================================================================
# Manifest
# ============================================================================

def _manifest(configured):
    return {
        "id": ADDON_ID,
        "version": APP_VERSION,
        "name": "Ranger",
        "description": "Addon ultime multi-trackers / multi-débrideurs (FR & international) — films, séries, anime.",
        "logo": "https://i.imgur.com/MgdGxnR.png",
        "types": ["movie", "series"],
        "catalogs": [],
        "resources": ["stream"],
        "idPrefixes": ["tt"],
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": not configured,
        },
    }


async def handle_manifest_no_config(request):
    return web.json_response(_manifest(configured=False))


async def handle_manifest(request):
    config = decode_config(request.match_info.get("config", ""))
    if not config:
        return web.json_response(_manifest(configured=False))
    return web.json_response(_manifest(configured=True))


async def handle_stream_no_config(request):
    host_url = f"{request.scheme}://{request.host}"
    return web.json_response({"streams": [{
        "name": "Ranger",
        "title": "⚙️ Configure Ranger",
        "externalUrl": f"{host_url}/configure",
    }]})


# ============================================================================
# Stream
# ============================================================================

def _parse_stream_id(stream_id):
    imdb_id, season, episode = stream_id, None, None
    if ":" in stream_id:
        parts = stream_id.split(":")
        imdb_id = parts[0]
        if len(parts) >= 3:
            try:
                season, episode = int(parts[1]), int(parts[2])
            except ValueError:
                pass
    return imdb_id, season, episode


async def handle_stream(request):
    config = decode_config(request.match_info.get("config", ""))
    if not config:
        return web.json_response({"streams": []})

    stream_type = request.match_info.get("type")
    stream_id = request.match_info.get("id")
    imdb_id, season, episode = _parse_stream_id(stream_id)
    logging.info(f"Stream {stream_type} {imdb_id} S{season}E{episode}")

    config_str = request.match_info.get("config", "")
    host_url = f"{request.scheme}://{request.host}"
    filters = config.get("filters") or {}

    # 1. Métadonnées (TMDB ou Cinemeta)
    media_info = await get_media_info(imdb_id, stream_type, (config.get("tmdb_key") or "").strip())
    if not media_info:
        media_info = {"title": "", "original_title": "", "year": "", "is_anime": False, "seasons": [], "tmdb_id": None}
    tmdb_id = media_info.get("tmdb_id")
    absolute_episode = compute_absolute_episode(media_info, season, episode) if media_info.get("is_anime") else None

    # 2. Recherche parallèle sur tous les trackers activés
    raw = await run_search(config, media_info, stream_type, imdb_id, tmdb_id, season, episode, absolute_episode)
    if not raw:
        return web.json_response({"streams": []})

    # 3. Filtrage pertinence + préférences, enrichissement parsing, dédup
    torrents = ranking.enrich(raw)
    torrents = ranking.filter_relevance(
        torrents, stream_type, media_info, season, episode, absolute_episode,
        exclude_packs=filters.get("exclude_season_packs", False),
    )
    torrents = ranking.dedupe(torrents, config.get("providers_order") or [])
    torrents = ranking.filter_preferences(torrents, filters)
    if not torrents:
        return web.json_response({"streams": []})
    logging.info(f"{len(torrents)} torrents pertinents après filtrage")

    # 4. Disponibilité débrideur (parallèle) + cache SQLite
    backends = build_backends(config, client_ip=get_client_ip(request))
    hashes = [t["info_hash"] for t in torrents if t.get("info_hash")]
    availability = {}
    if backends and hashes:
        results = await asyncio.gather(*[b.check_availability(hashes) for b in backends])
        for backend, avail in zip(backends, results):
            availability[backend.name] = avail
            logging.info(f"{backend.name}: {sum(1 for v in avail.values() if v)}/{len(hashes)} en cache")

    # 5. Construction des entrées (torrent x débrideur)
    entries = _build_entries(torrents, backends, availability, config)

    # 6. Tri + limites
    entries = ranking.sort_entries(entries, config)
    entries = ranking.apply_limits(entries, filters)

    # 7. Sérialisation en streams Stremio
    streams = _serialize_streams(entries, config_str, host_url, stream_type, season, episode)
    logging.info(f"{len(streams)} streams renvoyés à Stremio")
    return web.json_response({"streams": streams})


def _build_entries(torrents, backends, availability, config):
    """
    Une entrée = {torrent, service|None, cached}.
    - debrid_mode 'first' : premier débrideur (par priorité) où c'est caché.
    - debrid_mode 'all'   : une entrée par débrideur où c'est caché.
    - non caché : selon show_uncached / cached_only.
    - P2P : selon show_p2p.
    """
    filters = config.get("filters") or {}
    debrid_mode = config.get("debrid_mode", "first")
    cached_only = filters.get("cached_only", False)
    show_uncached = filters.get("show_uncached", True) and not cached_only
    show_p2p = filters.get("show_p2p", False)

    entries = []
    for t in torrents:
        ih = t["info_hash"]
        cached_backends = [b for b in backends if availability.get(b.name, {}).get(b.clean_hash(ih), False)]

        if cached_backends:
            chosen = cached_backends if debrid_mode == "all" else cached_backends[:1]
            for b in chosen:
                entries.append({"torrent": t, "service": b.name, "cached": True})
        elif backends and show_uncached:
            # Non caché : proposé via le débrideur prioritaire (clic = ajout)
            entries.append({"torrent": t, "service": backends[0].name, "cached": False})

        if show_p2p:
            entries.append({"torrent": t, "service": None, "cached": False})

    return entries


def _serialize_streams(entries, config_str, host_url, stream_type, season, episode):
    streams = []
    for entry in entries:
        t = entry["torrent"]
        service = entry["service"]
        if service is None:
            streams.append(formatting.build_p2p_stream(t))
            continue

        resolve_url = f"{host_url}/{config_str}/resolve/{service}/{t['info_hash']}"
        if season is not None and episode is not None:
            resolve_url += f"?season={season}&episode={episode}"
        elif stream_type == "movie":
            resolve_url += "?type=movie"

        streams.append(formatting.build_debrid_stream(t, service, entry["cached"], resolve_url))
    return streams


# ============================================================================
# Resolve (au moment de la lecture)
# ============================================================================

async def handle_resolve(request):
    config = decode_config(request.match_info.get("config", ""))
    if not config:
        return web.Response(status=400, text="Config invalide")

    service_name = request.match_info.get("service")
    info_hash = request.match_info.get("hash")
    season = request.query.get("season")
    episode = request.query.get("episode")
    media_type = request.query.get("type")

    backends = build_backends(config, client_ip=get_client_ip(request))
    backend = next((b for b in backends if b.name == service_name), None)
    if not backend:
        return web.Response(status=400, text=f"Débrideur non configuré : {service_name}")

    url = await backend.resolve(
        info_hash,
        season=int(season) if season else None,
        episode=int(episode) if episode else None,
        media_type=media_type,
    )
    if url:
        raise web.HTTPFound(url)
    return web.Response(status=404, text="Impossible de résoudre le stream")


# ============================================================================
# Divers
# ============================================================================

async def handle_health(request):
    return web.json_response({"status": "ok", "version": APP_VERSION})


async def _cache_cleanup_loop(app):
    """Tâche de fond : purge périodique du cache SQLite."""
    try:
        while True:
            await asyncio.sleep(3600)
            try:
                cache.cleanup()
                logging.info("Cache SQLite purgé")
            except Exception as e:
                logging.error(f"Cache cleanup error: {e}")
    except asyncio.CancelledError:
        pass


async def _on_startup(app):
    app["cleanup_task"] = asyncio.create_task(_cache_cleanup_loop(app))


async def _on_cleanup(app):
    task = app.get("cleanup_task")
    if task:
        task.cancel()


def get_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_configure)
    app.router.add_get("/configure", handle_configure)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/manifest.json", handle_manifest_no_config)
    app.router.add_get("/stream/{type}/{id}.json", handle_stream_no_config)

    app.router.add_get("/{config}/configure", handle_configure)
    app.router.add_get("/{config}/manifest.json", handle_manifest)
    app.router.add_get("/{config}/stream/{type}/{id}.json", handle_stream)
    app.router.add_get("/{config}/resolve/{service}/{hash}", handle_resolve)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(get_app(), host="0.0.0.0", port=PORT)
