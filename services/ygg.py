import asyncio
import json
import logging
from urllib.parse import quote

import aiohttp

from utils import check_season_episode

RELAY_URL = "https://u2p.anhkagi.net/"
TORRENT_KIND = 2003
WS_TIMEOUT = 5

DEFAULT_TRACKERS = [
    "https://tracker.yggleak.top/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.srv00.com:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://leet-tracker.moe:1337/announce",
    "udp://explodie.org:6969/announce",
]


def _fix_encoding(text: str) -> str:
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def _get_tag(tags: list, key: str):
    for t in tags:
        if t[0] == key:
            return t[1] if len(t) > 1 else None
    return None


def _get_l_prefix(tags: list, prefix: str):
    for t in tags:
        if t[0] == "l" and len(t) > 1 and t[1].startswith(prefix):
            parts = t[1].split(":", 1)
            return parts[1] if len(parts) > 1 else None
    return None


def _parse_event(event: dict):
    try:
        tags = event.get("tags", [])

        title = _get_tag(tags, "title")
        if not title:
            return None
        title = _fix_encoding(title)

        info_hash = _get_tag(tags, "x")
        if not info_hash:
            return None
        info_hash = info_hash.lower()

        size = int(_get_tag(tags, "size") or 0)
        seeders = int(_get_l_prefix(tags, "u2p.seed:") or 1)
        leechers = int(_get_l_prefix(tags, "u2p.leech:") or 0)
        timestamp = int(_get_tag(tags, "published_at") or event.get("created_at", 0))

        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}"
        for tr in DEFAULT_TRACKERS:
            magnet += f"&tr={quote(tr, safe='')}"

        return {
            "name": title,
            "size": size,
            "tracker_name": "YGG",
            "info_hash": info_hash,
            "magnet": magnet,
            "link": magnet,
            "source": "ygg",
            "seeders": seeders,
            "leechers": leechers,
            "timestamp": timestamp,
        }
    except Exception as e:
        logging.warning(f"YGG parse error: {e}")
        return None


async def _query(filters: dict) -> list:
    results = []
    headers = {
        "Origin": "https://www.ygg.re",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        async with asyncio.timeout(WS_TIMEOUT):
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.ws_connect(RELAY_URL) as ws:
                    req = json.dumps(["REQ", "frenchio", filters])
                    await ws.send_str(req)
                    logging.info(f"YGG search: {filters.get('search', '?')}")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data[0] == "EOSE":
                                break
                            if data[0] == "EVENT":
                                parsed = _parse_event(data[2])
                                if parsed:
                                    results.append(parsed)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break

    except TimeoutError:
        logging.warning(f"YGG relay timeout after {WS_TIMEOUT}s ({len(results)} results)")
    except Exception as e:
        logging.error(f"YGG relay error: {e}")

    return results


def _merge(results_list: list) -> list:
    seen = set()
    merged = []
    for results in results_list:
        if isinstance(results, Exception) or not results:
            continue
        for r in results:
            ih = r.get('info_hash')
            if ih and ih not in seen:
                merged.append(r)
                seen.add(ih)
            elif not ih:
                merged.append(r)
    merged.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    return merged


class YggService:
    def __init__(self, passkey=None, cookie=None):
        self.passkey = passkey
        self.cookie = cookie

    async def search_movie(self, title, year, original_title=None, imdb_id=None, tmdb_id=None):
        queries = []
        if title:
            queries.append(f"{title} {year}".strip())
        if original_title and original_title != title:
            queries.append(f"{original_title} {year}".strip())

        if not queries:
            return []

        tasks = [
            _query({
                "kinds": [TORRENT_KIND],
                "search": q,
                "limit": 50
            })
            for q in queries
        ]

        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        return _merge(results_list)

    async def search_series(self, title, season, episode, original_title=None, imdb_id=None, tmdb_id=None):
        seen_t = set()
        unique_titles = []
        for t in [title] + ([original_title] if original_title and original_title != title else []):
            if t and t not in seen_t:
                unique_titles.append(t)
                seen_t.add(t)

        tasks = []
        for t in unique_titles:
            if season is not None and episode is not None:
                tasks.append(_query({
                    "kinds": [TORRENT_KIND],
                    "search": f"{t} S{int(season):02d}E{int(episode):02d}",
                    "limit": 50
                }))
            if season is not None:
                tasks.append(_query({
                    "kinds": [TORRENT_KIND],
                    "search": f"{t} S{int(season):02d}",
                    "limit": 50
                }))

        if not tasks:
            return []

        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        merged = _merge(results_list)
        return [r for r in merged if check_season_episode(r.get('name', ''), season, episode)]
