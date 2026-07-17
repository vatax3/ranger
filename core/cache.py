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

# Compteurs de performance du cache (depuis le démarrage)
_metrics = {"search_hit": 0, "search_miss": 0, "avail_hit": 0, "avail_miss": 0}


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
    _metrics["avail_hit"] += len(known)
    _metrics["avail_miss"] += len(unknown)
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
            results = json.loads(row[0])
            _metrics["search_hit"] += 1
            return results
        except Exception:
            pass
    _metrics["search_miss"] += 1
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


# ============================================================================
# Fonctions d'administration (panel admin)
# ============================================================================

def get_metrics():
    """Compteurs hit/miss du cache depuis le démarrage."""
    m = dict(_metrics)
    s_tot = m["search_hit"] + m["search_miss"]
    a_tot = m["avail_hit"] + m["avail_miss"]
    m["search_hit_rate"] = round(m["search_hit"] / s_tot * 100, 1) if s_tot else 0.0
    m["avail_hit_rate"] = round(m["avail_hit"] / a_tot * 100, 1) if a_tot else 0.0
    return m


def stats():
    """Statistiques globales du cache SQLite pour le dashboard admin."""
    with _lock:
        conn = _get_conn()
        avail_total = conn.execute("SELECT COUNT(*) FROM availability").fetchone()[0]
        avail_cached = conn.execute("SELECT COUNT(*) FROM availability WHERE cached = 1").fetchone()[0]
        by_service = conn.execute(
            "SELECT service, COUNT(*), SUM(cached) FROM availability GROUP BY service ORDER BY COUNT(*) DESC"
        ).fetchall()
        searches_total = conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
        by_source = conn.execute(
            "SELECT substr(key, 8, instr(substr(key, 8), ':') - 1) AS src, COUNT(*) "
            "FROM searches GROUP BY src ORDER BY COUNT(*) DESC"
        ).fetchall()
        meta_total = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]

    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    for wal in (DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(wal):
            db_size += os.path.getsize(wal)

    return {
        "db_path": DB_PATH,
        "db_size_bytes": db_size,
        "availability": {
            "total": avail_total,
            "cached": avail_cached,
            "uncached": avail_total - avail_cached,
            "by_service": [
                {"service": s, "total": t, "cached": c or 0} for s, t, c in by_service
            ],
        },
        "searches": {
            "total": searches_total,
            "by_source": [{"source": s or "?", "total": t} for s, t in by_source],
        },
        "meta": {"total": meta_total},
        "ttl": {
            "cached": TTL_AVAIL_CACHED,
            "uncached": TTL_AVAIL_MISS,
            "search": TTL_SEARCH,
            "meta": TTL_META,
        },
        "metrics": get_metrics(),
    }


def _parse_search_key(key):
    # search:{source}:{type}:{imdb}[:season:episode]
    parts = key.split(":")
    if len(parts) < 4 or parts[0] != "search":
        return {"key": key}
    info = {"key": key, "source": parts[1], "type": parts[2], "imdb": parts[3]}
    if len(parts) >= 6:
        info["season"], info["episode"] = parts[4], parts[5]
    return info


def list_searches(limit=300):
    """Liste les entrées du cache de recherche (clé parsée + âge)."""
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT key, created_at, LENGTH(results) FROM searches ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for key, created, size in rows:
        info = _parse_search_key(key)
        info["age_seconds"] = now - created
        info["expires_in"] = max(0, TTL_SEARCH - (now - created))
        info["bytes"] = size
        out.append(info)
    return out


def list_meta(limit=300):
    now = int(time.time())
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT key, created_at FROM meta ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"key": k, "age_seconds": now - c, "expires_in": max(0, TTL_META - (now - c))} for k, c in rows]


def delete_key(table, key):
    """Supprime une entrée précise (table = searches|meta)."""
    if table not in ("searches", "meta"):
        return 0
    with _lock:
        conn = _get_conn()
        cur = conn.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount


def refresh_media(imdb_id):
    """
    Force le refresh d'un média : supprime ses recherches et métadonnées en
    cache. Le prochain fetch Stremio ré-interrogera les trackers et TMDB.
    Retourne le nombre d'entrées supprimées.
    """
    imdb_id = (imdb_id or "").strip()
    if not imdb_id:
        return {"searches": 0, "meta": 0}
    like = f"%{imdb_id}%"
    with _lock:
        conn = _get_conn()
        s = conn.execute("DELETE FROM searches WHERE key LIKE ?", (like,)).rowcount
        m = conn.execute("DELETE FROM meta WHERE key LIKE ?", (like,)).rowcount
        conn.commit()
    return {"searches": s, "meta": m}


def flush(table):
    """Vide une table (searches|availability|meta|all)."""
    targets = ["searches", "availability", "meta"] if table == "all" else [table]
    deleted = {}
    with _lock:
        conn = _get_conn()
        for t in targets:
            if t in ("searches", "availability", "meta"):
                deleted[t] = conn.execute(f"DELETE FROM {t}").rowcount
        conn.commit()
    return deleted
