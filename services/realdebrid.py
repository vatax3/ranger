"""
Client Real-Debrid natif — API officielle uniquement (api.real-debrid.com).

Historique : l'implémentation précédente routait tous les appels vers un
serveur StremThru public tiers (stremthru.13377001.xyz) codé en dur, y
compris quand l'utilisateur n'avait configuré aucune instance StremThru.
Cela envoyait la clé API Real-Debrid de l'utilisateur à un tiers non
contrôlé, à son insu. Ce module ne contacte plus que l'API officielle ;
pour router via une instance StremThru (recommandé, contourne le
rate-limit RD post-2023 sur la vérification de cache), configurez-la
explicitement dans Ranger — voir services/stremthru.py.
"""

import asyncio
import logging

import aiohttp

from utils import check_absolute_episode

API_BASE = "https://api.real-debrid.com/rest/1.0"

VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.m2ts', '.vob')
BAD_EXTENSIONS = ('.iso', '.pdf', '.epub', '.txt', '.nfo', '.jpg', '.jpeg', '.png', '.rar', '.zip')


class RealDebridService:
    def __init__(self, api_key):
        self.api_key = api_key
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def check_availability(self, hashes):
        """
        Vérifie la disponibilité en cache via l'endpoint officiel
        instantAvailability. Real-Debrid a fortement restreint cet endpoint
        depuis 2023 (anti-abus) : il peut renvoyer peu ou pas de résultats
        selon le compte. C'est une limitation connue de l'API officielle,
        pas un bug — utilisez StremThru pour un check fiable.
        """
        if not hashes:
            return {}

        cleaned = [h.strip().lower() for h in hashes if h]
        availability = {}
        batch_size = 50

        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
            for i in range(0, len(cleaned), batch_size):
                batch = cleaned[i:i + batch_size]
                url = f"{API_BASE}/torrents/instantAvailability/{'/'.join(batch)}"
                try:
                    async with session.get(url, headers=self.headers,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            logging.warning(f"RD instantAvailability HTTP {resp.status}")
                            for h in batch:
                                availability[h] = False
                            continue
                        data = await resp.json(content_type=None)
                        for h in batch:
                            entry = data.get(h) or data.get(h.upper())
                            # Format : {hash: {"rd": [{fileid: {...}}, ...]}} si caché, [] sinon
                            has_cache = bool(entry and entry.get("rd"))
                            availability[h] = has_cache
                except Exception as e:
                    logging.error(f"RD instantAvailability exception: {e}")
                    for h in batch:
                        availability[h] = False

        cached_count = sum(1 for v in availability.values() if v)
        logging.info(f"RD (natif): {cached_count}/{len(cleaned)} cached")
        return availability

    async def unlock_magnet(self, magnet_hash, season=None, episode=None, media_type=None, absolute_episode=None):
        """
        Ajoute le magnet -> sélectionne les fichiers -> attend -> unrestrict.
        Toutes les requêtes vont exclusivement à api.real-debrid.com.
        """
        magnet_hash = magnet_hash.strip().lower()
        magnet_uri = f"magnet:?xt=urn:btih:{magnet_hash}"
        logging.info(f"🔓 RD (natif) unlock: hash={magnet_hash[:16]}..., S{season}E{episode}")

        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=20)) as session:
            # 1. Ajouter le magnet
            try:
                async with session.post(
                    f"{API_BASE}/torrents/addMagnet", headers=self.headers,
                    data={"magnet": magnet_uri}, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status not in (200, 201):
                        logging.error(f"❌ RD addMagnet failed: HTTP {resp.status}")
                        return None
                    torrent_id = (await resp.json()).get("id")
            except Exception as e:
                logging.error(f"❌ RD addMagnet exception: {e}")
                return None

            if not torrent_id:
                return None

            # 2. Récupérer les infos (liste des fichiers)
            info = await self._get_info(session, torrent_id)
            if not info:
                return None

            files = info.get("files", [])
            target_ids = self._select_file_ids(files, season, episode, absolute_episode)
            if not target_ids:
                logging.error("❌ RD: aucun fichier vidéo pertinent trouvé")
                return None

            # 3. Sélectionner les fichiers puis attendre le statut "downloaded"
            try:
                async with session.post(
                    f"{API_BASE}/torrents/selectFiles/{torrent_id}", headers=self.headers,
                    data={"files": ",".join(str(i) for i in target_ids)},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status not in (200, 204):
                        logging.error(f"❌ RD selectFiles failed: HTTP {resp.status}")
                        return None
            except Exception as e:
                logging.error(f"❌ RD selectFiles exception: {e}")
                return None

            info = await self._wait_ready(session, torrent_id)
            if not info:
                return None

            links = info.get("links") or []
            if not links:
                logging.error("❌ RD: torrent prêt mais aucun lien")
                return None

            # 4. Unrestrict du lien correspondant au fichier choisi (le premier
            # sélectionné, RD ordonne links selon l'ordre des fichiers choisis)
            try:
                async with session.post(
                    f"{API_BASE}/unrestrict/link", headers=self.headers,
                    data={"link": links[0]}, timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logging.error(f"❌ RD unrestrict failed: HTTP {resp.status}")
                        return None
                    download = (await resp.json()).get("download")
                    if download:
                        logging.info("✅ RD (natif) lien généré")
                    return download
            except Exception as e:
                logging.error(f"❌ RD unrestrict exception: {e}")
                return None

    async def _get_info(self, session, torrent_id):
        try:
            async with session.get(
                f"{API_BASE}/torrents/info/{torrent_id}", headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as e:
            logging.error(f"RD get_info exception: {e}")
            return None

    async def _wait_ready(self, session, torrent_id, max_wait=30, interval=2):
        elapsed = 0
        while elapsed < max_wait:
            info = await self._get_info(session, torrent_id)
            if not info:
                return None
            status = info.get("status")
            if status == "downloaded":
                return info
            if status in ("error", "magnet_error", "virus", "dead"):
                logging.error(f"❌ RD: statut torrent invalide ({status})")
                return None
            await asyncio.sleep(interval)
            elapsed += interval
        logging.error("❌ RD: timeout en attente du téléchargement")
        return None

    def _select_file_ids(self, files, season, episode, absolute_episode=None):
        """Retourne les IDs de fichiers vidéo pertinents (épisode ciblé sinon tous)."""
        video_files = [f for f in files if any(
            (f.get("path") or "").lower().endswith(ext) for ext in VIDEO_EXTENSIONS
        )]
        if not video_files:
            video_files = [f for f in files if not any(
                (f.get("path") or "").lower().endswith(ext) for ext in BAD_EXTENSIONS
            )]
        if not video_files:
            return []

        if season is not None and episode is not None:
            s_str, e_str = f"{int(season):02d}", f"{int(episode):02d}"
            patterns = [f"S{s_str}E{e_str}", f"{int(season)}x{e_str}", f"S{s_str}.E{e_str}"]
            for f in video_files:
                name = (f.get("path") or "").upper()
                if any(pat in name for pat in patterns):
                    return [f["id"]]

        # Numérotation absolue (fansub anime) : les packs multi-épisodes anime
        # ne matchent pas SxxExx, la sélection tomberait sinon sur le plus
        # gros fichier (mauvais épisode).
        if absolute_episode is not None:
            for f in video_files:
                if check_absolute_episode(f.get("path") or "", absolute_episode, exclude_packs=True):
                    return [f["id"]]

        best = max(video_files, key=lambda f: f.get("bytes", 0))
        return [best["id"]]
