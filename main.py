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
import hmac
import json
import logging
import os
import re
import time

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

# Token du panel admin (si non défini, le panel est désactivé)
ADMIN_TOKEN = (os.getenv("RANGER_ADMIN_TOKEN") or "").strip()

# Compteurs runtime (dashboard admin)
RUNTIME = {
    "started_at": time.time(),
    "stream_requests": 0,
    "streams_served": 0,
    "resolve_requests": 0,
    "resolve_ok": 0,
}


_JSON_SCRIPT_ESCAPES = {ord(">"): "\\u003E", ord("<"): "\\u003C", ord("&"): "\\u0026"}


def json_for_script(obj):
    """
    Sérialise en JSON pour injection dans un <script> inline, sans risque
    d'évasion : un champ de config contenant "</script>" (ou "<script>",
    "<!--"...) casserait le parsing HTML et permettrait une XSS réfléchie.
    Les caractères dangereux sont échappés en séquences unicode, inertes
    en HTML mais interprétées normalement par JSON.parse / le littéral JS.
    """
    return json.dumps(obj, ensure_ascii=False).translate(_JSON_SCRIPT_ESCAPES)


# URL publique fixe (optionnelle). Si définie, elle prime sur tout header —
# c'est la option la plus sûre : X-Forwarded-Proto/Host sont des en-têtes
# client, usurpables par quiconque atteint directement le port du conteneur
# sans passer par le reverse proxy (le cas si le port est exposé publiquement,
# ex: Portainer qui mappe le port du conteneur sur l'hôte). Sans cette
# variable, on retombe sur X-Forwarded-Proto (suffisant si le VPS n'accepte
# des connexions entrantes que depuis le reverse proxy/Cloudflare).
PUBLIC_URL = (os.getenv("RANGER_PUBLIC_URL") or "").strip().rstrip("/")


def external_scheme(request):
    """
    Schéma (http/https) tel que vu par le client externe.

    Derrière un reverse proxy ou Cloudflare, la connexion à l'app peut être
    en clair même quand le client parle en HTTPS : request.scheme refléterait
    alors "http" et toutes les URLs générées (logo, resolve...) casseraient
    le HTTPS attendu par Stremio (mixed content -> manifest bloqué en chargement).
    On fait confiance à X-Forwarded-Proto (standard, posé par Cloudflare/Caddy/nginx)
    à défaut de RANGER_PUBLIC_URL.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    scheme = forwarded.split(",")[0].strip().lower()
    return scheme if scheme in ("http", "https") else request.scheme


def external_host_url(request):
    if PUBLIC_URL:
        return PUBLIC_URL
    return f"{external_scheme(request)}://{request.host}"


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

        # Persistance : si une config est présente dans l'URL, on la ré-injecte
        # pour pré-remplir le formulaire (clés API, trackers, filtres, tri...).
        # json_for_script échappe </script> et consorts (XSS réfléchie sinon,
        # une valeur de config pouvant casser hors du <script> inline).
        prefill = "null"
        config_str = request.match_info.get("config", "")
        if config_str:
            decoded = decode_config(config_str)
            if decoded:
                prefill = json_for_script(decoded)
        content = content.replace("const PREFILL = null;", f"const PREFILL = {prefill};")

        return web.Response(text=content, content_type="text/html")
    except Exception as e:
        logging.error(f"configure error: {e}")
        return web.Response(text=str(e), status=500)


# ============================================================================
# Manifest
# ============================================================================

# Logo de l'addon : "R" violet sur fond sombre arrondi (reflète la homepage web)
LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
  <rect width="256" height="256" rx="56" fill="#0f1115"/>
  <rect x="6" y="6" width="244" height="244" rx="52" fill="none" stroke="#7c5cff" stroke-width="4" opacity="0.35"/>
  <text x="50%" y="52%" dominant-baseline="central" text-anchor="middle"
        font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
        font-size="170" font-weight="800" fill="#7c5cff">R</text>
</svg>"""


def _logo_url(request):
    return f"{external_host_url(request)}/logo.svg"


def _manifest(configured, request):
    logo = _logo_url(request)
    return {
        "id": ADDON_ID,
        "version": APP_VERSION,
        "name": "Ranger",
        "description": "Addon ultime multi-trackers / multi-débrideurs (FR & international) — films, séries, anime.",
        "logo": logo,
        "icon": logo,
        "types": ["movie", "series"],
        "catalogs": [],
        "resources": ["stream"],
        "idPrefixes": ["tt"],
        "behaviorHints": {
            "configurable": True,
            "configurationRequired": not configured,
        },
    }


async def handle_logo(request):
    return web.Response(body=LOGO_SVG.encode("utf-8"), content_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


async def handle_manifest_no_config(request):
    return web.json_response(_manifest(configured=False, request=request))


async def handle_manifest(request):
    config = decode_config(request.match_info.get("config", ""))
    if not config:
        return web.json_response(_manifest(configured=False, request=request))
    return web.json_response(_manifest(configured=True, request=request))


async def handle_stream_no_config(request):
    host_url = external_host_url(request)
    return web.json_response({"streams": [{
        "name": "Ranger",
        "title": "⚙️ Configure Ranger",
        "externalUrl": f"{host_url}/configure",
    }]})


# ============================================================================
# Stream
# ============================================================================

# IMDB ID strict : ce champ finit dans des clés de cache, des logs et le
# dashboard admin (rendu via innerHTML côté client) — un ID non validé est
# un vecteur de XSS stockée si on le laisse passer tel quel.
_IMDB_ID_RE = re.compile(r"^tt\d{5,9}$")


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
    if not _IMDB_ID_RE.match(imdb_id):
        return None, None, None
    return imdb_id, season, episode


async def handle_stream(request):
    RUNTIME["stream_requests"] += 1
    config = decode_config(request.match_info.get("config", ""))
    if not config:
        return web.json_response({"streams": []})

    stream_type = request.match_info.get("type")
    stream_id = request.match_info.get("id")
    imdb_id, season, episode = _parse_stream_id(stream_id)
    if imdb_id is None:
        logging.warning(f"ID de stream invalide rejeté: {stream_id!r}")
        return web.json_response({"streams": []})
    logging.info(f"Stream {stream_type} {imdb_id} S{season}E{episode}")

    config_str = request.match_info.get("config", "")
    host_url = external_host_url(request)
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
    # Budget global : un débrideur lent/qui ne répond pas ne doit pas bloquer
    # toute la réponse /stream (aiohttp n'a pas de timeout par défaut sur ces
    # appels, qui hériteraient sinon du timeout aiohttp par défaut de 5 min).
    backends = build_backends(config, client_ip=get_client_ip(request))
    hashes = [t["info_hash"] for t in torrents if t.get("info_hash")]
    availability = {}
    if backends and hashes:
        tasks = [asyncio.create_task(b.check_availability(hashes)) for b in backends]
        done, pending = await asyncio.wait(tasks, timeout=20)
        for task in pending:
            task.cancel()
        for backend, task in zip(backends, tasks):
            if task in done:
                try:
                    avail = task.result()
                except Exception as e:
                    logging.error(f"{backend.name}: échec vérification dispo: {e}")
                    continue
                availability[backend.name] = avail
                logging.info(f"{backend.name}: {sum(1 for v in avail.values() if v)}/{len(hashes)} en cache")
            else:
                logging.warning(f"{backend.name}: budget de 20s dépassé, backend ignoré pour cette requête")

    # 5-7. Construction des streams selon le mode d'affichage
    if config.get("display_mode") == "simple":
        # Mode famille : 1 lien par résolution, meilleur choix automatique
        entries = _build_simple_entries(torrents, backends, availability, config)
        streams = _serialize_streams(entries, config_str, host_url, stream_type, season, episode, absolute_episode, simple=True)
    else:
        entries = _build_entries(torrents, backends, availability, config)
        entries = ranking.sort_entries(entries, config)
        entries = ranking.apply_limits(entries, filters)
        streams = _serialize_streams(entries, config_str, host_url, stream_type, season, episode, absolute_episode)

    RUNTIME["streams_served"] += len(streams)
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


def _build_simple_entries(torrents, backends, availability, config):
    """
    Mode simplifié : une seule entrée par résolution, le meilleur torrent
    (cache d'abord, puis score qualité) sur le débrideur prioritaire dispo.
    """
    filters = config.get("filters") or {}
    cached_only = filters.get("cached_only", False)
    show_uncached = filters.get("show_uncached", True) and not cached_only
    lang_order = config.get("language_order") or []
    res_order = config.get("resolution_order") or ["4K", "1080p", "720p", "SD"]

    # Candidats : (torrent, service, cached) au meilleur débrideur possible
    candidates = []
    for t in torrents:
        ih = t["info_hash"]
        cached_backends = [b for b in backends if availability.get(b.name, {}).get(b.clean_hash(ih), False)]
        if cached_backends:
            candidates.append({"torrent": t, "service": cached_backends[0].name, "cached": True})
        elif backends and show_uncached:
            candidates.append({"torrent": t, "service": backends[0].name, "cached": False})

    # Groupement par résolution, meilleur candidat par groupe
    best_by_res = {}
    for c in candidates:
        res = c["torrent"]["_meta"]["resolution"] or "Auto"
        rank = (0 if c["cached"] else 1, -ranking.score_of(c["torrent"], lang_order))
        if res not in best_by_res or rank < best_by_res[res][0]:
            best_by_res[res] = (rank, c)

    # Ordonné selon la préférence de résolution
    def res_key(res):
        try:
            return res_order.index(res)
        except ValueError:
            return len(res_order)

    entries = []
    for res in sorted(best_by_res, key=res_key):
        entry = best_by_res[res][1]
        entry["resolution"] = res
        entries.append(entry)
    return entries


def _serialize_streams(entries, config_str, host_url, stream_type, season, episode, absolute_episode=None, simple=False):
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
            # Numérotation absolue (fansub anime) : sans elle, la sélection du
            # bon fichier dans un pack multi-épisodes peut se tromper (les
            # heuristiques de sélection cherchent "SxxExx" dans les noms de
            # fichiers, absent des noms fansub qui utilisent l'absolu).
            if absolute_episode is not None:
                resolve_url += f"&absolute={absolute_episode}"
        elif stream_type == "movie":
            resolve_url += "?type=movie"

        if simple:
            streams.append(formatting.build_simple_stream(
                t, service, entry["cached"], resolve_url, entry.get("resolution", "")))
        else:
            streams.append(formatting.build_debrid_stream(t, service, entry["cached"], resolve_url))
    return streams


# ============================================================================
# Resolve (au moment de la lecture)
# ============================================================================

async def handle_resolve(request):
    RUNTIME["resolve_requests"] += 1
    config = decode_config(request.match_info.get("config", ""))
    if not config:
        return web.Response(status=400, text="Config invalide")

    service_name = request.match_info.get("service")
    info_hash = request.match_info.get("hash")
    season = request.query.get("season")
    episode = request.query.get("episode")
    absolute_episode = request.query.get("absolute")
    media_type = request.query.get("type")
    client_ip = get_client_ip(request)

    backends = build_backends(config, client_ip=client_ip)
    backend = next((b for b in backends if b.name == service_name), None)
    if not backend:
        return web.Response(status=400, text=f"Débrideur non configuré : {service_name}")

    # Cache court des liens résolus : évite de re-débrider à chaque seek
    # (libmpv rappelle /resolve à chaque Range request). La clé inclut une
    # empreinte de la clé API du débrideur : sans ça, deux comptes différents
    # (ex: plusieurs profils d'une même famille) partageant la même IP
    # publique (NAT domestique) pourraient se voir servir le lien streaming
    # généré pour le compte de l'autre.
    account_fp = cache.fingerprint(backend.api_key)
    link_key = f"{service_name}|{account_fp}|{info_hash}|{season or ''}|{episode or ''}|{client_ip or ''}"
    cached_url = cache.get_link(link_key)
    if cached_url:
        RUNTIME["resolve_ok"] += 1
        logging.info(f"⚡ Lien résolu servi depuis le cache ({service_name})")
        raise web.HTTPFound(cached_url)

    url = await backend.resolve(
        info_hash,
        season=int(season) if season else None,
        episode=int(episode) if episode else None,
        media_type=media_type,
        absolute_episode=int(absolute_episode) if absolute_episode else None,
    )
    if url:
        RUNTIME["resolve_ok"] += 1
        cache.set_link(link_key, url)
        raise web.HTTPFound(url)
    return web.Response(status=404, text="Impossible de résoudre le stream")


# ============================================================================
# Panel admin
# ============================================================================

def _admin_authorized(request):
    """
    Vrai si le token admin est configuré et correspond.

    Le token n'est accepté que via l'en-tête X-Admin-Token, jamais en query
    string (les query strings finissent dans les logs d'accès, les logs de
    proxy/CDN comme Cloudflare, l'historique navigateur, le Referer). La
    comparaison est à temps constant (hmac.compare_digest) pour éviter une
    fuite du token par timing sur la comparaison caractère par caractère.
    """
    if not ADMIN_TOKEN:
        return None  # panel désactivé
    token = request.headers.get("X-Admin-Token") or ""
    return hmac.compare_digest(token, ADMIN_TOKEN)


def _require_admin(request):
    """Retourne une web.Response d'erreur si non autorisé, sinon None."""
    auth = _admin_authorized(request)
    if auth is None:
        return web.json_response({"error": "Panel admin désactivé (RANGER_ADMIN_TOKEN non défini)"}, status=503)
    if not auth:
        return web.json_response({"error": "Token admin invalide"}, status=401)
    return None


async def handle_admin_page(request):
    try:
        async with aiofiles.open(os.path.join(BASE_PATH, "templates", "admin.html"),
                                 mode="r", encoding="utf-8") as f:
            content = await f.read()
        content = content.replace("__APP_VERSION__", APP_VERSION)
        content = content.replace("__ADMIN_ENABLED__", "true" if ADMIN_TOKEN else "false")
        return web.Response(text=content, content_type="text/html")
    except Exception as e:
        return web.Response(text=str(e), status=500)


async def handle_admin_stats(request):
    err = _require_admin(request)
    if err is not None:
        return err
    uptime = int(time.time() - RUNTIME["started_at"])
    return web.json_response({
        "version": APP_VERSION,
        "uptime_seconds": uptime,
        "runtime": {k: v for k, v in RUNTIME.items() if k != "started_at"},
        "cache": cache.stats(),
    })


async def handle_admin_searches(request):
    err = _require_admin(request)
    if err is not None:
        return err
    limit = int(request.query.get("limit", "300"))
    return web.json_response({"searches": cache.list_searches(limit)})


async def handle_admin_meta(request):
    err = _require_admin(request)
    if err is not None:
        return err
    limit = int(request.query.get("limit", "300"))
    return web.json_response({"meta": cache.list_meta(limit)})


async def handle_admin_refresh(request):
    err = _require_admin(request)
    if err is not None:
        return err
    body = await request.json()
    imdb = (body.get("imdb") or "").strip()
    if not imdb:
        return web.json_response({"error": "imdb requis"}, status=400)
    deleted = cache.refresh_media(imdb)
    logging.info(f"Admin: refresh {imdb} -> {deleted}")
    return web.json_response({"deleted": deleted})


async def handle_admin_delete(request):
    err = _require_admin(request)
    if err is not None:
        return err
    body = await request.json()
    table = body.get("table")
    key = body.get("key")
    if not table or not key:
        return web.json_response({"error": "table et key requis"}, status=400)
    n = cache.delete_key(table, key)
    return web.json_response({"deleted": n})


async def handle_admin_flush(request):
    err = _require_admin(request)
    if err is not None:
        return err
    body = await request.json()
    table = body.get("table", "")
    if table not in ("searches", "availability", "meta", "all"):
        return web.json_response({"error": "table invalide"}, status=400)
    deleted = cache.flush(table)
    logging.info(f"Admin: flush {table} -> {deleted}")
    return web.json_response({"deleted": deleted})


async def handle_admin_cleanup(request):
    err = _require_admin(request)
    if err is not None:
        return err
    cache.cleanup()
    return web.json_response({"status": "ok"})


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
    app.router.add_get("/logo.svg", handle_logo)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/manifest.json", handle_manifest_no_config)
    app.router.add_get("/stream/{type}/{id}.json", handle_stream_no_config)

    # Panel admin
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_get("/admin/api/stats", handle_admin_stats)
    app.router.add_get("/admin/api/searches", handle_admin_searches)
    app.router.add_get("/admin/api/meta", handle_admin_meta)
    app.router.add_post("/admin/api/refresh", handle_admin_refresh)
    app.router.add_post("/admin/api/delete", handle_admin_delete)
    app.router.add_post("/admin/api/flush", handle_admin_flush)
    app.router.add_post("/admin/api/cleanup", handle_admin_cleanup)

    app.router.add_get("/{config}/configure", handle_configure)
    app.router.add_get("/{config}/manifest.json", handle_manifest)
    app.router.add_get("/{config}/stream/{type}/{id}.json", handle_stream)
    app.router.add_get("/{config}/resolve/{service}/{hash}", handle_resolve)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(get_app(), host="0.0.0.0", port=PORT)
