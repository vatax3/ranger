"""
Couche d'abstraction débrideur.

Unifie les services natifs (AllDebrid, Real-Debrid, TorBox, DebridLink) et leur
proxy StremThru derrière une interface commune :
  - check_availability(hashes) -> {hash: bool}
  - resolve(hash, season, episode, media_type) -> url directe

Le cache SQLite est consulté avant tout appel réseau ; StremThru remplace le
service natif quand il est configuré (contournement blocage IP).
"""

import asyncio
import logging

from core import cache
from services.alldebrid import AllDebridService
from services.realdebrid import RealDebridService
from services.torbox import TorBoxService
from services.debridlink import DebridLinkService
from services.stremthru import StremThruService, STREMTHRU_STORES

NATIVE_FACTORIES = {
    "alldebrid": AllDebridService,
    "realdebrid": RealDebridService,
    "torbox": TorBoxService,
    "debridlink": DebridLinkService,
}


class DebridBackend:
    """Un débrideur configuré (natif ou via StremThru)."""

    def __init__(self, service_name, api_key, stremthru=None, client_ip=None):
        self.name = service_name
        self.api_key = api_key
        self._stremthru_cfg = stremthru or {}
        self._client_ip = client_ip
        self._impl = self._build_impl()

    def _build_impl(self):
        url = (self._stremthru_cfg.get("url") or "").strip().rstrip("/")
        if url and self.name in STREMTHRU_STORES:
            logging.info(f"StremThru actif pour {self.name} ({url})")
            return StremThruService(
                url, self.name, self.api_key,
                auth=(self._stremthru_cfg.get("auth") or "").strip() or None,
                client_ip=self._client_ip,
            )
        return NATIVE_FACTORIES[self.name](self.api_key)

    @property
    def uses_stremthru(self):
        return isinstance(self._impl, StremThruService)

    def clean_hash(self, info_hash):
        if hasattr(self._impl, "_clean_hash"):
            return self._impl._clean_hash(info_hash)
        return info_hash.lower().strip()

    async def check_availability(self, hashes):
        """Vérifie la disponibilité en cache, cache SQLite d'abord."""
        if not hashes:
            return {}
        cleaned = [self.clean_hash(h) for h in hashes]
        cleaned = list(dict.fromkeys([h for h in cleaned if h]))

        known, unknown = cache.get_availability(self.name, cleaned)
        if unknown:
            fresh = await self._check_remote(unknown)
            cache.set_availability(self.name, fresh)
            known.update(fresh)
        return known

    async def _check_remote(self, hashes):
        try:
            if self.name == "torbox" and not self.uses_stremthru:
                # TorBox natif : un appel par hash
                results = await asyncio.gather(
                    *[self._impl.check_availability(h) for h in hashes],
                    return_exceptions=True,
                )
                return {
                    h: bool(r)
                    for h, r in zip(hashes, results)
                    if not isinstance(r, Exception)
                }
            avail = await self._impl.check_availability(hashes)
            # Normalise les clés en hash nettoyés
            return {h: bool(avail.get(h, avail.get(self.clean_hash(h), False))) for h in hashes}
        except Exception as e:
            logging.error(f"{self.name}: check_availability a échoué: {e}")
            return {}

    async def resolve(self, info_hash, season=None, episode=None, media_type=None):
        """Génère l'URL de lecture directe (ajoute au débrideur si non caché)."""
        try:
            if self.name == "torbox" and not self.uses_stremthru:
                magnet = f"magnet:?xt=urn:btih:{info_hash}"
                stream_type = "series" if (season and episode) else "movie"
                url = await self._impl.get_stream_link(magnet, stream_type, season=season, episode=episode)
            else:
                url = await self._impl.unlock_magnet(info_hash, season=season, episode=episode, media_type=media_type)
            if url:
                cache.mark_cached(self.name, self.clean_hash(info_hash))
            return url
        except Exception as e:
            logging.error(f"{self.name}: resolve a échoué: {e}")
            return None


def build_backends(config, client_ip=None):
    """Construit les DebridBackend dans l'ordre de priorité de la config."""
    from core.config import get_debrids
    stremthru = config.get("stremthru") or {}
    backends = []
    for service, key in get_debrids(config):
        try:
            backends.append(DebridBackend(service, key, stremthru=stremthru, client_ip=client_ip))
        except Exception as e:
            logging.error(f"Impossible d'initialiser {service}: {e}")
    return backends
