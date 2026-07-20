"""
EZTV via son API publique (recherche par IMDB ID, séries uniquement).
"""

import logging

import aiohttp

API_URL = "https://eztvx.to/api/get-torrents"
FALLBACK_URLS = ["https://eztv.re/api/get-torrents", "https://eztv1.xyz/api/get-torrents"]


class EztvService:
    async def _fetch(self, imdb_numeric, page=1):
        params = {"imdb_id": imdb_numeric, "limit": 100, "page": page}
        for url in [API_URL] + FALLBACK_URLS:
            try:
                async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
                    async with session.get(
                        url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json(content_type=None)
                        return data.get("torrents") or []
            except Exception as e:
                logging.debug(f"EZTV {url} error: {e}")
                continue
        logging.warning("EZTV: tous les miroirs ont échoué")
        return []

    @staticmethod
    def _normalize(item):
        return {
            "name": item.get("title", "").replace(" EZTV", ""),
            "size": int(item.get("size_bytes", 0) or 0),
            "tracker_name": "EZTV",
            "info_hash": (item.get("hash") or "").lower(),
            "magnet": item.get("magnet_url"),
            "link": item.get("magnet_url"),
            "source": "eztv",
            "seeders": int(item.get("seeds", 0) or 0),
            "leechers": int(item.get("peers", 0) or 0),
            "_season": int(item.get("season", 0) or 0),
            "_episode": int(item.get("episode", 0) or 0),
        }

    async def search_movie(self, title, year, original_title=None, imdb_id=None, tmdb_id=None):
        # EZTV n'indexe que des séries
        return []

    async def search_series(self, title, season, episode, original_title=None, imdb_id=None, tmdb_id=None):
        if not imdb_id:
            return []
        imdb_numeric = str(imdb_id).replace("tt", "").lstrip("0")
        if not imdb_numeric:
            return []

        torrents = await self._fetch(imdb_numeric)
        if len(torrents) == 100:
            torrents += await self._fetch(imdb_numeric, page=2)

        results = []
        for item in torrents:
            r = self._normalize(item)
            if not r["info_hash"]:
                continue
            # episode 0 = pack de saison ; sinon match exact via les champs de l'API
            if season is not None:
                if r["_season"] != int(season):
                    continue
                if episode is not None and r["_episode"] not in (0, int(episode)):
                    continue
            r.pop("_season", None)
            r.pop("_episode", None)
            results.append(r)

        logging.info(f"EZTV: {len(results)} résultats")
        return results
