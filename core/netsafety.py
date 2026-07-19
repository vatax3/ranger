"""
Protection SSRF pour les URLs fournies par la configuration utilisateur
(indexeurs Torznab, trackers UNIT3D).

Ranger est exposé publiquement : n'importe qui peut se construire sa propre
config (le schéma base64/JSON est public, pas besoin de connaître les clés
de qui que ce soit) et faire pointer un indexeur vers une adresse interne —
LAN du VPS/homelab, localhost, endpoint de métadonnées cloud
(169.254.169.254), API Docker locale, etc. Ce module valide l'IP réellement
résolue avant chaque requête sortante vers ces URLs utilisateur.
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse


def _is_blocked_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # IP illisible : on bloque par prudence

    # On bloque loopback (peut taper l'API admin ou un service local du VPS),
    # link-local — ce qui couvre 169.254.169.254, l'endpoint de métadonnées
    # cloud utilisé par AWS/GCP/Azure/OCI — et les plages réservées/multicast.
    #
    # Les plages privées RFC1918 (10.x, 172.16-31.x, 192.168.x) restent
    # volontairement AUTORISÉES : c'est la cible normale d'un Jackett/Prowlarr
    # auto-hébergé sur le LAN de l'opérateur, l'usage prévu de Torznab. Un
    # blocage total casserait cette fonctionnalité pour son propre usage.
    return (
        ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


async def is_url_safe(url):
    """
    Résout l'hôte de l'URL et vérifie qu'aucune IP résolue n'est privée,
    loopback, link-local (couvre le 169.254.169.254 des métadonnées cloud)
    ou réservée. Rejette aussi les schémas autres que http/https.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False

        loop = asyncio.get_event_loop()
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
        ips = {info[4][0] for info in infos}
        if not ips:
            return False
        if any(_is_blocked_ip(ip) for ip in ips):
            logging.warning(f"SSRF bloqué : {host} résout vers une IP interne ({ips})")
            return False
        return True
    except Exception as e:
        logging.warning(f"Vérification SSRF échouée pour {url!r}: {e}")
        return False
