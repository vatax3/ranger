"""
Métadonnées du média demandé : titre FR + titre original + année + détection
anime + numérotation absolue des épisodes.

Source principale : TMDB (clé utilisateur). Fallback : Cinemeta (sans clé,
titres anglais uniquement).
Résultats mis en cache SQLite (TTL 7 jours).
"""

import logging

import aiohttp

from core import cache

TMDB_BASE = "https://api.themoviedb.org/3"
CINEMETA_BASE = "https://v3-cinemeta.strem.io/meta"


async def get_media_info(imdb_id, stream_type, tmdb_key):
    """
    Retourne un dict :
      {tmdb_id, title, original_title, year, is_anime, seasons: [{season_number, episode_count}]}
    ou None si introuvable.
    """
    cache_key = f"media:{stream_type}:{imdb_id}:{'tmdb' if tmdb_key else 'cinemeta'}"
    cached = cache.get_meta(cache_key)
    if cached:
        return cached

    info = None
    if tmdb_key:
        info = await _from_tmdb(imdb_id, stream_type, tmdb_key)
    if not info:
        info = await _from_cinemeta(imdb_id, stream_type)

    if info:
        cache.set_meta(cache_key, info)
    return info


async def _from_tmdb(imdb_id, stream_type, tmdb_key):
    kind = "movie" if stream_type == "movie" else "tv"
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            # 1. IMDB -> TMDB
            async with session.get(
                f"{TMDB_BASE}/find/{imdb_id}",
                params={"api_key": tmdb_key, "external_source": "imdb_id"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logging.warning(f"TMDB find HTTP {resp.status}")
                    return None
                data = await resp.json()
            results = data.get("movie_results" if kind == "movie" else "tv_results", [])
            if not results:
                return None
            tmdb_id = results[0]["id"]

            # 2. Détails complets en FR (titre FR + saisons + genres)
            async with session.get(
                f"{TMDB_BASE}/{kind}/{tmdb_id}",
                params={"api_key": tmdb_key, "language": "fr-FR"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                details = await resp.json()
    except Exception as e:
        logging.error(f"TMDB error: {e}")
        return None

    title = details.get("title") or details.get("name") or ""
    original_title = details.get("original_title") or details.get("original_name") or ""
    date = details.get("release_date") or details.get("first_air_date") or ""
    year = date.split("-")[0] if date else ""

    # Détection anime : origine JP/KR/CN ET genre Animation (16) requis.
    # Un simple "or orig_lang == 'ja'" classerait n'importe quel drama
    # live-action japonais comme anime — le genre Animation est nécessaire
    # dans tous les cas.
    orig_lang = details.get("original_language", "")
    origin_country = details.get("origin_country", [])
    genre_ids = [g.get("id") for g in details.get("genres", [])]
    is_anime = False
    if (orig_lang in ("ja", "ko", "zh") or "JP" in origin_country) and 16 in genre_ids:
        is_anime = True
    if "anime" in (details.get("overview") or "").lower() and 16 in genre_ids:
        is_anime = True

    seasons = [
        {"season_number": s.get("season_number"), "episode_count": s.get("episode_count")}
        for s in details.get("seasons", [])
        if s.get("season_number") is not None
    ]

    return {
        "tmdb_id": tmdb_id,
        "title": title,
        "original_title": original_title,
        "year": year,
        "is_anime": is_anime,
        "seasons": seasons,
    }


async def _from_cinemeta(imdb_id, stream_type):
    kind = "movie" if stream_type == "movie" else "series"
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(
                f"{CINEMETA_BASE}/{kind}/{imdb_id}.json",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        logging.error(f"Cinemeta error: {e}")
        return None

    meta = data.get("meta") or {}
    if not meta:
        return None

    name = meta.get("name", "")
    year = str(meta.get("year") or "").split("–")[0].split("-")[0]

    # Détection anime approximative via genres Cinemeta
    genres = [g.lower() for g in (meta.get("genres") or [])]
    is_anime = "animation" in genres and (meta.get("country") or "").lower() in ("japan", "jp")

    # Saisons reconstruites depuis la liste des épisodes (pour l'épisode absolu)
    season_counts = {}
    for video in meta.get("videos") or []:
        s = video.get("season")
        if s is not None and s > 0:
            season_counts[s] = season_counts.get(s, 0) + 1
    seasons = [{"season_number": s, "episode_count": c} for s, c in sorted(season_counts.items())]

    return {
        "tmdb_id": None,
        "title": name,
        "original_title": name,
        "year": year,
        "is_anime": is_anime,
        "seasons": seasons,
    }


def compute_absolute_episode(media_info, season, episode):
    """
    Numérotation absolue pour les animes (nommage fansub "One Piece 1122").
    Somme des épisodes des saisons précédentes + numéro dans la saison.
    """
    if not media_info or not season or not episode:
        return None
    if season == 1:
        return episode
    prev = [
        s for s in media_info.get("seasons", [])
        if s.get("season_number") and 0 < s["season_number"] < season
    ]
    if len(prev) == season - 1 and all(s.get("episode_count") for s in prev):
        return sum(s["episode_count"] for s in prev) + episode
    return None
