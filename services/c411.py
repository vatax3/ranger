import aiohttp
import logging
import urllib.parse
from utils import check_season_episode

class C411Service:
    def __init__(self, apikey):
        self.apikey = apikey
        self.base_url = "https://c411.org/api"

    async def search(self, params):
        if not self.apikey:
            return []

        params['apikey'] = self.apikey
        params['o'] = 'json'
        
        # Log request (masking apikey)
        log_params = params.copy()
        log_params['apikey'] = '***APIKEY***'
        logging.info(f"C411 Search: {self.base_url}?{urllib.parse.urlencode(log_params)}")

        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                async with session.get(self.base_url, params=params, timeout=20) as response:
                    if response.status == 200:
                        data = await response.json()
                        channel = data.get('channel', {})
                        items = channel.get('item', [])
                        
                        # Handle single item case (JSON conversion of XML sometimes makes single item an object instead of list)
                        if isinstance(items, dict):
                            items = [items]
                        
                        logging.info(f"C411 found {len(items)} results")
                        
                        normalized = []
                        for res in items:
                            # Extract torznab attributes
                            attrs = res.get('torznab:attr', [])
                            if isinstance(attrs, dict):
                                attrs = [attrs]
                            
                            info_hash = None
                            seeders = 0
                            leechers = 0
                            
                            for attr in attrs:
                                attr_data = attr.get('@attributes', {})
                                name = attr_data.get('name')
                                value = attr_data.get('value')
                                
                                if name == 'infohash':
                                    info_hash = value
                                elif name == 'seeders':
                                    seeders = int(value) if value else 0
                                elif name == 'peers': # Some torznab use peers/leechers
                                    leechers = int(value) if value else 0
                            
                            # Fallback hash to guid if infohash not found (guid is often hash in torznab)
                            if not info_hash:
                                info_hash = res.get('guid')

                            enclosure = res.get('enclosure', {}).get('@attributes', {})
                            download_link = enclosure.get('url')

                            item = {
                                "name": res.get("title"),
                                "size": int(res.get("size", 0)),
                                "tracker_name": "C411",
                                "info_hash": info_hash,
                                "magnet": None,
                                "link": download_link,
                                "source": "c411",
                                "seeders": seeders,
                                "leechers": leechers
                            }
                            normalized.append(item)
                        return normalized
                    else:
                        logging.warning(f"C411 Error {response.status}")
                        text = await response.text()
                        logging.warning(f"C411 Body: {text[:200]}")
            except Exception as e:
                logging.error(f"C411 Exception: {e}")
        return []

    async def search_movie(self, title, year, imdb_id=None, tmdb_id=None):
        params = {"t": "movie"}
        if imdb_id:
            if not str(imdb_id).startswith('tt'):
                imdb_id = f"tt{imdb_id}"
            params["imdbid"] = imdb_id
        elif tmdb_id:
            params["tmdbid"] = tmdb_id
        else:
            params["q"] = f"{title} {year}"
            
        return await self.search(params)

    async def search_series(self, title, season, episode, imdb_id=None, tmdb_id=None):
        params = {"t": "tvsearch"}
        if imdb_id:
            if not str(imdb_id).startswith('tt'):
                imdb_id = f"tt{imdb_id}"
            params["imdbid"] = imdb_id
        elif tmdb_id:
            params["tmdbid"] = tmdb_id
        else:
            params["q"] = title
            
        # Torznab filters for season/episode if supported by tracker
        if season is not None:
            params["season"] = season
        if episode is not None:
            params["episode"] = episode
            
        return await self.search(params)
