import aiohttp
import logging
import asyncio
import re

class DebridLinkService:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://debrid-link.com/api/v2"
        
    async def _list_existing_torrent_ids(self):
        """Récupère les IDs des torrents déjà présents sur le seedbox."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                list_url = f"{self.base_url}/seedbox/list"
                async with session.get(list_url, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        return set()
                    data = await resp.json()
                    if data.get('success'):
                        return {t.get('id') for t in data.get('value', []) if t.get('id')}
            except Exception as e:
                logging.error(f"DebridLink: Error listing existing torrents: {e}")
        return set()

    async def check_availability(self, hashes):
        """
        Vérifie la disponibilité de plusieurs hash en parallèle.
        Retourne un dict {hash: bool} indiquant si chaque hash est caché.
        Ne supprime que les torrents qu'on a ajouté nous-mêmes (pas les pré-existants).
        """
        if not hashes:
            return {}
        
        logging.info(f"DebridLink: Checking {len(hashes)} hashes in parallel")
        
        # 1. Snapshot des torrents existants AVANT nos ajouts
        existing_ids = await self._list_existing_torrent_ids()
        logging.info(f"DebridLink: {len(existing_ids)} torrents already on seedbox")
        
        # 2. Vérifier chaque hash (retourne (is_cached, torrent_id))
        tasks = [self._check_single_hash(h) for h in hashes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 3. Construire les résultats et collecter les IDs à supprimer
        availability = {}
        ids_to_delete = []
        
        for hash_value, result in zip(hashes, results):
            if isinstance(result, Exception):
                logging.error(f"DebridLink: Error checking {hash_value}: {result}")
                availability[hash_value.lower()] = False
            else:
                is_cached, torrent_id = result
                availability[hash_value.lower()] = is_cached
                # Ne supprimer que si c'est un torrent qu'on a ajouté nous-mêmes
                if torrent_id and torrent_id not in existing_ids:
                    ids_to_delete.append(torrent_id)
        
        cached_count = sum(1 for v in availability.values() if v)
        logging.info(f"DebridLink: {cached_count}/{len(hashes)} hashes are cached")
        
        # Cleanup en arrière-plan — ne bloque pas le retour des résultats
        if ids_to_delete:
            async def _cleanup():
                headers = {"Authorization": f"Bearer {self.api_key}"}
                async with aiohttp.ClientSession(trust_env=True) as session:
                    await asyncio.gather(
                        *[self._remove_torrent(session, headers, tid) for tid in ids_to_delete],
                        return_exceptions=True
                    )
            asyncio.create_task(_cleanup())

        return availability
    
    async def _check_single_hash(self, hash_value):
        """
        Vérifie un seul hash en l'ajoutant au seedbox.
        Retourne (is_cached, torrent_id) pour que le caller gère le cleanup.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        add_url = f"{self.base_url}/seedbox/add"
        
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                payload = {
                    "url": hash_value,
                    "wait": False
                }
                
                async with session.post(add_url, json=payload, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"DebridLink: Failed to add {hash_value[:8]}... status {resp.status}")
                        return (False, None)
                    
                    data = await resp.json()
                    
                    if not data.get('success'):
                        logging.debug(f"DebridLink: {hash_value[:8]}... not successful")
                        return (False, None)
                    
                    torrent = data.get('value', {})
                    torrent_id = torrent.get('id')
                    download_percent = torrent.get('downloadPercent', 0)
                    error = torrent.get('error', 0)
                    
                    is_cached = error == 0 and download_percent == 100
                    
                    if is_cached:
                        logging.debug(f"DebridLink: {hash_value[:8]}... cached!")
                    else:
                        logging.debug(f"DebridLink: {hash_value[:8]}... not cached")
                    
                    return (is_cached, torrent_id)
                    
            except asyncio.TimeoutError:
                logging.warning(f"DebridLink: Timeout checking {hash_value[:8]}...")
                return (False, None)
            except Exception as e:
                logging.error(f"DebridLink: Exception checking {hash_value[:8]}...: {e}")
                return (False, None)
    
    async def _remove_torrent(self, session, headers, torrent_id):
        """Supprime un torrent du seedbox"""
        try:
            remove_url = f"{self.base_url}/seedbox/{torrent_id}/remove"
            async with session.delete(remove_url, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    logging.debug(f"DebridLink: Removed torrent {torrent_id}")
                else:
                    logging.warning(f"DebridLink: Failed to remove {torrent_id}: {resp.status}")
        except Exception as e:
            logging.error(f"DebridLink: Error removing {torrent_id}: {e}")
    
    async def unlock_magnet(self, info_hash, season=None, episode=None, media_type=None):
        """
        Déverrouille un magnet et retourne l'URL de streaming
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        add_url = f"{self.base_url}/seedbox/add"
        
        async with aiohttp.ClientSession(trust_env=True) as session:
            try:
                # Ajouter le torrent
                payload = {
                    "url": info_hash,
                    "wait": False
                }
                
                async with session.post(add_url, json=payload, headers=headers, timeout=15) as resp:
                    if resp.status != 200:
                        logging.error(f"DebridLink: Failed to add torrent: {resp.status}")
                        return None
                    
                    data = await resp.json()
                    
                    if not data.get('success'):
                        logging.error("DebridLink: Add torrent failed")
                        return None
                    
                    torrent = data.get('value', {})
                    torrent_id = torrent.get('id')
                    files = torrent.get('files', [])
                    
                    if not files:
                        logging.error("DebridLink: No files in torrent")
                        return None
                    
                    # Sélectionner le bon fichier
                    selected_file = None
                    
                    if season is not None and episode is not None:
                        patterns = [
                            rf"S{season:02d}E{episode:02d}",
                            rf"S{season}E{episode:02d}",
                            rf"{season}x{episode:02d}",
                            rf"{season}x{episode}\b",
                        ]
                        for f in files:
                            filename = f.get('name', '')
                            if any(re.search(p, filename, re.IGNORECASE) for p in patterns):
                                selected_file = f
                                break
                        
                    else:
                        # Film : prendre le plus gros fichier vidéo
                        video_extensions = ('.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.m2ts')
                        video_files = [f for f in files if f.get('name', '').lower().endswith(video_extensions)]
                        
                        if video_files:
                            selected_file = max(video_files, key=lambda x: x.get('size', 0))
                        else:
                            # Fallback si pas d'extension trouvée (nom sans extension ?)
                            # On exclut au moins les fichiers connus pour ne pas être des vidéos
                            bad_extensions = ('.iso', '.pdf', '.epub', '.txt', '.nfo', '.rar', '.zip')
                            filtered_files = [f for f in files if not f.get('name', '').lower().endswith(bad_extensions)]
                            if filtered_files:
                                selected_file = max(filtered_files, key=lambda x: x.get('size', 0))
                    
                    if selected_file:
                        download_url = selected_file.get('downloadUrl')
                        if download_url:
                            logging.info(f"DebridLink: Stream URL found for torrent {torrent_id}")
                            return download_url
                    
                    logging.error("DebridLink: Could not find suitable file")
                    return None
                    
            except Exception as e:
                logging.error(f"DebridLink: Exception in unlock_magnet: {e}")
                return None

