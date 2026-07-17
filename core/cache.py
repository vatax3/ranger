"""
Cache SQLite de Ranger.

Trois usages :
  - availability : statut cache des hashes par débrideur (évite de re-vérifier
    les mêmes hashes à chaque requête Stremio)
  - searches     : résultats de recherche par tracker (les épisodes suivants
    d'une même série réutilisent la recherche)
  - meta         : métadonnées TMDB/Cinemeta par ID

Accès synchrone (opérations courtes, WAL activé) — suffisant pour un addon
mono-processus.
"""

import json
import logging
import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("RANGER_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ranger.db"))

# TTLs (secondes), surchargables par variables d'environnement
TTL_AVAIL_CACHED = int(os.getenv("RANGER_TTL_CACHED", 6 * 3600))      # torrent vu en cache
TTL_AVAIL_MISS = int(os.getenv("RANGER_TTL_UNCACHED", 20 * 60))       # torrent vu non-caché
TTL_SEARCH = int(os.getenv("RANGER_TTL_SEARCH", 30 * 60))
TTL_META = int(os.getenv("RANGER_TTL_META", 7 * 24 * 3600))

_lock = threading.Lock()
_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS availability (
                service TEXT NOT NULL,
                hash TEXT NOT NULL,
                cached INTEGER NOT NULL,
                checked_at INTEGER NOT NULL,
                PRIMARY KEY (service, hash)
            );
            CREATE TABLE IF NOT EXISTS searches (
                key TEXT PRIMARY KEY,
                results TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        _conn.commit()
        logging.info(f"Cache SQLite initialisé : {DB_PATH}")
    return _conn


def get_availability(service, hashes):
    """
    Retourne (known, unknown) :
      known   : {hash: bool} pour les hashes dont le statut en cache est encore valide
      unknown : liste des hashes à re-vérifier auprès du débrideur
    """
    if not hashes:
        return {}, []
    now = int(time.time())
    known = {}
    with _lock:
        conn = _get_conn()
        placeholders = ",".join("?" * len(hashes))
        rows = conn.execute(
            f"SELECT hash, cached, checked_at FROM availability WHERE service = ? AND hash IN ({placeholders})",
            [service] + list(hashes),
        ).fetchall()
    for h, cached, checked_at in rows:
        ttl = TTL_AVAIL_CACHED if cached else TTL_AVAIL_MISS
        if now - checked_at <= ttl:
            known[h] = bool(cached)
    unknown = [h for h in hashes if h not in known]
    return known, unknown


def set_availability(service, availability):
    """Enregistre un dict {hash: bool} pour un débrideur."""
    if not availability:
        return
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO availability (service, hash, cached, checked_at) VALUES (?, ?, ?, ?)",
            [(service, h, 1 if c else 0, now) for h, c in availability.items()],
        )
        conn.commit()


def mark_cached(service, info_hash):
    """Marque un hash comme caché (après un débridage réussi)."""
    set_availability(service, {info_hash: True})


def get_search(key):
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT results, created_at FROM searches WHERE key = ?", (key,)).fetchone()
    if row and now - row[1] <= TTL_SEARCH:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None


def set_search(key, results):
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO searches (key, results, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(results, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


def get_meta(key):
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT value, created_at FROM meta WHERE key = ?", (key,)).fetchone()
    if row and now - row[1] <= TTL_META:
        try:
            return json.loads(row[0])
        except Exception:
            return None
    return None


def set_meta(key, value):
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


def cleanup():
    """Purge les entrées expirées (appelé périodiquement)."""
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM availability WHERE checked_at < ?", (now - max(TTL_AVAIL_CACHED, TTL_AVAIL_MISS),))
        conn.execute("DELETE FROM searches WHERE created_at < ?", (now - TTL_SEARCH,))
        conn.execute("DELETE FROM meta WHERE created_at < ?", (now - TTL_META,))
        conn.commit()
