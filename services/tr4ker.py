import aiohttp
import logging
import xml.etree.ElementTree as ET
from utils import check_season_episode

class Tr4kerService:
    def __init__(self, apikey):
        self.apikey = apikey
        self.base_url = "https://tr4ker.net/torznab"

    async def search(self, params):
        if not self.apikey:
            return []

        params['apikey'] = self.apikey
        log_q = params.get('tmdbid') or params.get('imdbid') or params.get('q', '')
        logging.info(f"Tr4ker Search: {self.base_url}?t={params.get('t')}&{log_q}")

        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                async with session.get(self.base_url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as response:
                    if response.status == 200:
                        text = await response.text()
                        results = self._parse_xml(text)
                        logging.info(f"Tr4ker found {len(results)} results")
                        return results
                    else:
                        logging.warning(f"Tr4ker Error {response.status}")
            except Exception as e:
                logging.error(f"Tr4ker Exception: {e}")
        return []

    def _parse_xml(self, xml_text):
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logging.error(f"Tr4ker XML Parse Error: {e}")
            return []

        ns = {'torznab': 'http://torznab.com/schemas/2015/feed'}
        items = root.findall('.//item')

        results = []
        for item in items:
            title = item.findtext('title', '')
            link = item.findtext('link', '')
            enclosure = item.find('enclosure')
            download_link = enclosure.get('url', '') if enclosure is not None else link
            size = int(enclosure.get('length', 0)) if enclosure is not None else 0

            info_hash = None
            seeders = 0
            leechers = 0

            for attr in item.findall('torznab:attr', ns):
                name = attr.get('name')
                value = attr.get('value')
                if name == 'infohash':
                    info_hash = value.lower() if value else None
                elif name == 'seeders':
                    seeders = int(value) if value else 0
                elif name == 'leechers':
                    leechers = int(value) if value else 0
                elif name == 'size' and value:
                    size = int(value)

            results.append({
                "name": title,
                "size": size,
                "tracker_name": "Tr4ker",
                "info_hash": info_hash,
                "magnet": None,
                "link": download_link,
                "source": "tr4ker",
                "seeders": seeders,
                "leechers": leechers,
            })

        return results

    async def search_movie(self, title, year, imdb_id=None, tmdb_id=None):
        if tmdb_id:
            return await self.search({"t": "movie", "tmdbid": tmdb_id})
        return await self.search({"t": "search", "q": f"{title} {year}".strip()})

    async def search_series(self, title, season, episode, imdb_id=None, tmdb_id=None):
        if tmdb_id:
            params = {"t": "tvsearch", "tmdbid": tmdb_id}
            if season is not None:
                params["season"] = season
            if episode is not None:
                params["episode"] = episode
            results = await self.search(params)
        else:
            if season is not None and episode is not None:
                q = f"{title} S{int(season):02d}E{int(episode):02d}"
            elif season is not None:
                q = f"{title} S{int(season):02d}"
            else:
                q = title
            results = await self.search({"t": "search", "q": q})

        if season is not None:
            results = [r for r in results if check_season_episode(r.get('name', ''), season, episode)]
        return results
