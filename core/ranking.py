"""
Moteur de filtrage, déduplication et tri des torrents.
"""

import logging

from core.parsing import parse_release, best_language, resolution_rank, quality_score
from utils import (
    check_title_match,
    check_title_tokens,
    check_season_episode,
    check_absolute_episode,
    check_special_episode,
    is_video_file,
)

ANIME_SOURCES = ("nyaa", "nekobt")


def enrich(torrents):
    """Attache le parsing de release à chaque torrent (clé '_meta')."""
    for t in torrents:
        if "_meta" not in t:
            t["_meta"] = parse_release(t.get("name", ""))
    return torrents


def filter_relevance(torrents, stream_type, media_info, season, episode, absolute_episode, exclude_packs=False):
    """
    Filtre de pertinence : bon titre, bonne saison/épisode, fichier vidéo.
    (Indépendant des préférences utilisateur — toujours appliqué.)
    """
    title = (media_info or {}).get("title", "")
    original_title = (media_info or {}).get("original_title", "")
    year = (media_info or {}).get("year", "")

    kept = []
    for t in torrents:
        name = t.get("name", "")
        if not t.get("info_hash"):
            continue
        if not is_video_file(name):
            continue

        source = t.get("source", "")
        is_anime_source = source in ANIME_SOURCES

        # Vérification du titre (sauf nommage fansub, trop variable)
        if not is_anime_source:
            if not check_title_match(name, title, original_title, year=year, is_movie=(stream_type == "movie")):
                continue

        # Vérification saison/épisode
        if stream_type == "series" and season is not None:
            if season == 0 and is_anime_source:
                se_ok = check_special_episode(name, episode, exclude_packs=exclude_packs)
            else:
                se_ok = check_season_episode(name, season, episode, exclude_packs=exclude_packs)
                if not se_ok and is_anime_source:
                    se_ok = check_absolute_episode(name, absolute_episode, exclude_packs=exclude_packs)
            # Les matchs "mous" (packs, absolu) des sources anime doivent contenir le titre
            if se_ok and is_anime_source and not check_season_episode(name, season, episode, exclude_packs=True):
                se_ok = check_title_tokens(name, title, original_title)
            if not se_ok:
                continue

        kept.append(t)
    return kept


def filter_preferences(torrents, filters):
    """Filtres de préférence utilisateur : taille, résolution, codec, langue, CAM."""
    min_size = (filters.get("min_size_gb") or 0) * 1024**3
    max_size = (filters.get("max_size_gb") or 0) * 1024**3
    allowed_res = set(filters.get("resolutions") or [])
    allowed_codecs = set(filters.get("codecs") or [])
    allowed_langs = set(filters.get("languages") or [])
    exclude_cam = filters.get("exclude_cam", True)

    kept = []
    for t in torrents:
        meta = t["_meta"]
        size = t.get("size", 0) or 0

        # Les tailles à 0 (inconnues) ne sont pas filtrées
        if size:
            if min_size and size < min_size:
                continue
            if max_size and size > max_size:
                continue

        if exclude_cam and meta["source"] == "CAM":
            continue
        if allowed_res and meta["resolution"] and meta["resolution"] not in allowed_res:
            continue
        if allowed_codecs and meta["codec"] and meta["codec"] not in allowed_codecs:
            continue
        if allowed_langs and not (set(meta["languages"]) & allowed_langs):
            continue

        kept.append(t)
    return kept


def dedupe(torrents, providers_order):
    """
    Déduplication par info_hash. En cas de doublon, on garde celui du tracker
    le mieux classé dans providers_order et on fusionne les seeders max.
    """
    def prov_rank(t):
        try:
            return providers_order.index(t.get("source", ""))
        except ValueError:
            return len(providers_order)

    ordered = sorted(torrents, key=prov_rank) if providers_order else torrents

    unique = {}
    for t in ordered:
        ih = (t.get("info_hash") or "").lower().strip()
        if not ih:
            continue
        if ih in unique:
            existing = unique[ih]
            existing["seeders"] = max(existing.get("seeders", 0) or 0, t.get("seeders", 0) or 0)
        else:
            t["info_hash"] = ih
            unique[ih] = t
    return list(unique.values())


def score_of(torrent, language_order):
    """Score qualité mémoïsé sur le torrent."""
    if "_score" not in torrent:
        torrent["_score"] = quality_score(
            torrent["_meta"], torrent.get("seeders", 0), language_order
        )
    return torrent["_score"]


def sort_entries(entries, config):
    """
    Trie les entrées de stream selon les critères configurés (dans l'ordre).
    Une entrée = dict {torrent, service (débrideur ou None), cached (bool)}.
    """
    criteria = config.get("sort") or ["cached", "language", "resolution", "size_desc"]
    lang_order = config.get("language_order") or []
    res_order = config.get("resolution_order") or []
    prov_order = config.get("providers_order") or []

    def key(entry):
        t = entry["torrent"]
        meta = t["_meta"]
        parts = []
        for criterion in criteria:
            if criterion == "cached":
                parts.append(0 if entry["cached"] else 1)
            elif criterion == "quality":
                parts.append(-score_of(t, lang_order))
            elif criterion == "language":
                parts.append(best_language(meta["languages"], lang_order))
            elif criterion == "resolution":
                parts.append(resolution_rank(meta["resolution"], res_order))
            elif criterion == "size_desc":
                parts.append(-(t.get("size", 0) or 0))
            elif criterion == "size_asc":
                parts.append(t.get("size", 0) or 0)
            elif criterion == "seeders":
                parts.append(-(t.get("seeders", 0) or 0))
            elif criterion == "tracker":
                source = t.get("source", "")
                try:
                    parts.append(prov_order.index(source))
                except ValueError:
                    parts.append(len(prov_order))
        return tuple(parts)

    entries.sort(key=key)
    return entries


def apply_limits(entries, filters):
    """Limite le nombre de résultats (global et par résolution)."""
    max_results = filters.get("max_results") or 0
    max_per_res = filters.get("max_per_resolution") or 0

    if max_per_res:
        counts = {}
        limited = []
        for entry in entries:
            res = entry["torrent"]["_meta"]["resolution"] or "?"
            counts[res] = counts.get(res, 0) + 1
            if counts[res] <= max_per_res:
                limited.append(entry)
        entries = limited

    if max_results:
        entries = entries[:max_results]

    logging.info(f"Après limites : {len(entries)} streams")
    return entries
