"""
Client Torznab générique : branchez Jackett ou Prowlarr et accédez à des
centaines de trackers publics et privés (l'authentification tracker est
gérée côté Jackett/Prowlarr).

Config : [{"name": "MonIndexeur", "url": "http://jackett:9117/api/v2.0/indexers/xxx/results/torznab", "apikey": "..."}]
"""

import asyncio
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET

import aiohttp

from core.netsafety import is_url_safe

_HASH_RE = re.compile(r"btih:([a-fA-F0-9]{40})")


class TorznabService:
    def __init__(self, indexers):
        # indexers: liste de {"name", "url", "apikey"}
        self.indexers = [
            i for i in (indexers or [])
            if (i.get("url") or "").strip()
        ]

    async def _query(self, indexer, params):
        url = indexer["url"].strip().rstrip("/")
        if not url.endswith("/api") and "torznab" not in url and "/results" not in url:
            url += "/api"

        # SSRF : l'URL de l'indexeur vient de la config utilisateur, or Ranger
        # est exposé publiquement — n'importe qui peut en forger une pointant
        # vers du loopback ou les métadonnées cloud. Voir core/netsafety.py.
        if not await is_url_safe(url):
            logging.warning(f"Torznab [{indexer.get('name', '?')}]: URL bloquée (cible interne) : {url}")
            return []

        params = dict(params)
        if indexer.get("apikey"):
            params["apikey"] = indexer["apikey"]

        log_params = {k: ("***" if k == "apikey" else v) for k, v in params.items()}
        logging.info(f"Torznab [{indexer.get('name', '?')}]: ?{urllib.parse.urlencode(log_params)}")

        try:
            async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=20),
                    allow_redirects=False,  # une redirection pourrait viser une cible interne non validée
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"Torznab [{indexer.get('name')}] HTTP {resp.status}")
                        return []
                    text = await resp.text()
        except Exception as e:
            logging.error(f"Torznab [{indexer.get('name')}] error: {e}")
            return []

        return self._parse_xml(text, indexer.get("name") or "Torznab")

    @staticmethod
    def _parse_xml(xml_text, indexer_name):
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logging.error(f"Torznab [{indexer_name}] XML invalide: {e}")
            return []

        ns = {"torznab": "http://torznab.com/schemas/2015/feed"}
        results = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            size = int(item.findtext("size", "0") or 0)

            enclosure = item.find("enclosure")
            download_link = enclosure.get("url", "") if enclosure is not None else item.findtext("link", "")

            info_hash = None
            magnet = None
            seeders = 0
            leechers = 0
            for attr in item.findall("torznab:attr", ns):
                name, value = attr.get("name"), attr.get("value")
                if name == "infohash" and value:
                    info_hash = value.lower()
                elif name == "magneturl" and value:
                    magnet = value
                elif name == "seeders" and value:
                    seeders = int(value)
                elif name == "peers" and value:
                    leechers = int(value)

            # Extraction du hash depuis le magnet si besoin
            if not info_hash:
                for candidate in (magnet, download_link, item.findtext("guid", "")):
                    if candidate:
                        m = _HASH_RE.search(candidate)
                        if m:
                            info_hash = m.group(1).lower()
                            break
            if not info_hash:
                guid = (item.findtext("guid", "") or "").strip().lower()
                if re.fullmatch(r"[a-f0-9]{40}", guid):
                    info_hash = guid

            if not info_hash:
                continue  # sans hash : pas de dédup ni de débridage possible

            results.append({
                "name": title,
                "size": size,
                "tracker_name": indexer_name,
                "info_hash": info_hash,
                "magnet": magnet,
                "link": magnet or download_link,
                "source": "torznab",
                "seeders": seeders,
                "leechers": leechers,
            })
        return results

    async def _search_all(self, params_builder):
        tasks = [self._query(indexer, params_builder(indexer)) for indexer in self.indexers]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        merged = []
        for results in results_list:
            if isinstance(results, Exception):
                logging.error(f"Torznab task error: {results}")
                continue
            merged.extend(results)
        logging.info(f"Torznab: {len(merged)} résultats ({len(self.indexers)} indexeurs)")
        return merged

    async def search_movie(self, title, year, original_title=None, imdb_id=None, tmdb_id=None):
        def build(_indexer):
            params = {"t": "movie"}
            if imdb_id:
                params["imdbid"] = imdb_id if str(imdb_id).startswith("tt") else f"tt{imdb_id}"
            else:
                params["t"] = "search"
                params["q"] = f"{title} {year}".strip()
            return params
        return await self._search_all(build)

    async def search_series(self, title, season, episode, original_title=None, imdb_id=None, tmdb_id=None):
        def build(_indexer):
            params = {"t": "tvsearch"}
            if imdb_id:
                params["imdbid"] = imdb_id if str(imdb_id).startswith("tt") else f"tt{imdb_id}"
            else:
                params["q"] = title
            if season is not None:
                params["season"] = season
            if episode is not None:
                params["ep"] = episode
            return params
        return await self._search_all(build)
