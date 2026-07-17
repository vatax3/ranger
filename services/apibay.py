"""
ThePirateBay via l'API publique apibay.org (JSON, pas de clé requise).
Bon complément international (VO) aux trackers français.
"""

import asyncio
import logging

import aiohttp

API_URL = "https://apibay.org/q.php"

# Catégories vidéo TPB : 200-299 (dont 201 films, 205 TV, 207 films HD, 208 TV HD)
def _is_video_category(cat):
    try:
        return 200 <= int(cat) < 300
    except (TypeError, ValueError):
        return False


class ApibayService:
    async def _query(self, query):
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(
                    API_URL,
                    params={"q": query},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"apibay HTTP {resp.status}")
                        return []
                    data = await resp.json(content_type=None)
        except Exception as e:
            logging.error(f"apibay error: {e}")
            return []

        if not isinstance(data, list):
            return []

        results = []
        for item in data:
            # apibay renvoie une entrée factice quand il n'y a aucun résultat
            if item.get("id") == "0" or item.get("info_hash", "").strip("0") == "":
                continue
            if not _is_video_category(item.get("category")):
                continue
            results.append({
                "name": item.get("name", ""),
                "size": int(item.get("size", 0) or 0),
                "tracker_name": "TPB",
                "info_hash": (item.get("info_hash") or "").lower(),
                "magnet": None,
                "link": None,
                "source": "apibay",
                "seeders": int(item.get("seeders", 0) or 0),
                "leechers": int(item.get("leechers", 0) or 0),
                "imdb": item.get("imdb") or "",
            })
        return results

    async def _search_many(self, queries, imdb_id=None):
        results_list = await asyncio.gather(
            *[self._query(q) for q in queries if q], return_exceptions=True
        )
        merged, seen = [], set()
        for results in results_list:
            if isinstance(results, Exception):
                continue
            for r in results:
                # Si apibay connaît l'IMDB du torrent, on écarte les hors-sujet
                if imdb_id and r.get("imdb") and r["imdb"] != imdb_id:
                    continue
                ih = r["info_hash"]
                if ih and ih not in seen:
                    seen.add(ih)
                    merged.append(r)
        logging.info(f"apibay: {len(merged)} résultats")
        return merged

    async def search_movie(self, title, year, original_title=None, imdb_id=None, tmdb_id=None):
        queries = []
        if imdb_id:
            queries.append(imdb_id)  # apibay indexe les IMDB IDs
        if title:
            queries.append(f"{title} {year}".strip())
        if original_title and original_title != title:
            queries.append(f"{original_title} {year}".strip())
        return await self._search_many(queries, imdb_id=imdb_id)

    async def search_series(self, title, season, episode, original_title=None, imdb_id=None, tmdb_id=None):
        queries = []
        se = f"S{int(season):02d}E{int(episode):02d}" if season and episode is not None else ""
        s_only = f"S{int(season):02d}" if season else ""
        for t in dict.fromkeys([title, original_title]):
            if not t:
                continue
            if se:
                queries.append(f"{t} {se}")
            if s_only:
                queries.append(f"{t} {s_only}")
        if imdb_id:
            queries.append(imdb_id)
        return await self._search_many(queries, imdb_id=imdb_id)
