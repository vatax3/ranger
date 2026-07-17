# Ranger 🎯

**L'addon Stremio ultime** — multi-trackers / multi-débrideurs, pensé pour le contenu **français comme international**. Films, séries et anime, avec parsing metadata soigné, filtres, tri par priorité, déduplication et cache.

Écrit en Python (aiohttp), sans dépendance lourde, déployable en une commande Docker sur un VPS.

---

## ✨ Fonctionnalités

| | |
|---|---|
| **Multi-débrideurs + priorité** | AllDebrid, Real-Debrid, TorBox, DebridLink. Ordre de priorité configurable (glisser-déposer). Mode « prioritaire uniquement » ou « tous les débrideurs en cache ». |
| **StremThru intégré** | Proxy d'API débrideur pour contourner les blocages d'IP datacenter (VPS Oracle/OVH/etc.). Optionnel, par simple URL. |
| **Compatible AIOStreams** | Tags `[AD+]`, `[RD+]`… (convention Torrentio) pour l'identification du service et du statut de cache. |
| **Clé TMDB** | Titres FR, détection anime, numérotation absolue des épisodes. Fallback **Cinemeta** sans clé. |
| **Trackers publics** | YGG (leak, relais Nostr), ThePirateBay (apibay), EZTV, Nyaa (anime). Activables à la case. |
| **Trackers privés / semi** | C411, Torr9, Tr4ker, NekoBT (clé/passkey), ABN (login), UNIT3D (multi). |
| **Torznab générique** | Branchez **Jackett / Prowlarr** → des centaines de trackers publics et privés. |
| **Films / séries / anime** | Parsing metadata dédié : résolution, codec, source, HDR/DV, audio, langues (MULTI/VFF/VF/VFQ/VOSTFR/VO). Matching saison/épisode + numérotation absolue anime. |
| **Filtres** | Taille min/max, résolution, codec, langue, exclusion CAM, exclusion packs, nombre de résultats (global + par résolution). |
| **Tri multi-critères** | Cache → langue → résolution → taille → seeders → tracker, ordre entièrement réordonnable. |
| **Déduplication** | Par info_hash, en gardant le tracker prioritaire et le max de seeders. |
| **Cache-only / non-caché** | Afficher uniquement le caché, ou proposer les non-cachés (le clic les ajoute au débrideur). Option liens **P2P** (moteur torrent de Stremio, sans débrideur). |
| **Cache SQLite** | Statut de cache débrideur, résultats de recherche et métadonnées TMDB mis en cache (TTLs configurables) → réponses quasi instantanées sur les épisodes suivants d'une série. |

---

## 🚀 Déploiement (VPS, Docker)

### Option A — image pré-buildée (recommandé, aucun build local)

Le CI GitHub Actions publie l'image publique sur **ghcr.io** à chaque push sur `main`. Aucune authentification n'est requise pour la pull :

```bash
# récupérez le docker-compose.ghcr.yml (ou clonez le repo), puis :
docker compose -f docker-compose.ghcr.yml up -d
```

Pour mettre à jour : `docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d`.

### Option B — build depuis les sources

```bash
git clone https://github.com/vatax3/ranger.git && cd ranger
docker compose up -d --build
```

L'addon écoute sur le port **7000**. La base de cache SQLite est persistée dans le volume `ranger_data`.

### Derrière un reverse proxy (recommandé, HTTPS obligatoire pour Stremio Web)

Exemple **Caddy** :

```caddyfile
ranger.mondomaine.fr {
    reverse_proxy localhost:7000
}
```

Stremio exige du HTTPS pour les addons distants — un reverse proxy avec certificat (Caddy/Traefik/nginx) est indispensable en production.

### Sans Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py            # écoute sur :7000 (surchargeable via PORT)
```

---

## ⚙️ Configuration & installation dans Stremio

1. Ouvrez `https://ranger.mondomaine.fr/configure`
2. Renseignez votre clé TMDB, vos débrideurs (avec ordre de priorité), cochez les trackers, ajustez filtres et tri.
3. Cliquez **Installer dans Stremio** (ou copiez l'URL du manifest).

Toute la configuration est encodée dans l'URL du manifest (aucune donnée stockée côté serveur hormis le cache technique).

---

## 🔧 Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `PORT` | `7000` | Port d'écoute |
| `RANGER_DB` | `/data/ranger.db` | Chemin de la base SQLite |
| `RANGER_TTL_CACHED` | `21600` (6 h) | TTL cache « en cache » |
| `RANGER_TTL_UNCACHED` | `1200` (20 min) | TTL cache « non caché » |
| `RANGER_TTL_SEARCH` | `1800` (30 min) | TTL résultats de recherche |
| `RANGER_TTL_META` | `604800` (7 j) | TTL métadonnées TMDB |
| `HTTP_PROXY` / `HTTPS_PROXY` | — | Proxy sortant optionnel |

---

## 🏗️ Architecture

```
main.py                 # Routes aiohttp : manifest, stream, resolve, configure, health
core/
  config.py             # Encodage/décodage de la config (base64 JSON dans l'URL)
  cache.py              # Cache SQLite (availability / searches / meta)
  metadata.py          # TMDB + fallback Cinemeta, détection anime, épisode absolu
  search.py            # Orchestrateur : lance tous les trackers en parallèle (avec cache)
  debrid.py            # Abstraction débrideur (natif + StremThru) unifiée
  parsing.py           # Parsing de release (résolution/codec/source/HDR/audio/langues)
  ranking.py           # Filtres, déduplication, tri multi-critères, limites
  formatting.py        # Construction des objets stream Stremio (compat AIOStreams)
services/               # Un module par tracker/débrideur (portés de Frenchio + nouveaux)
templates/configure.html
```

### Flux d'une requête `/stream`

1. Décodage de la config depuis l'URL.
2. Métadonnées du média (TMDB/Cinemeta, en cache).
3. Recherche parallèle sur tous les trackers activés (en cache SQLite par tracker).
4. Filtrage pertinence (titre + saison/épisode) → dédup → filtres utilisateur.
5. Vérification de disponibilité auprès des débrideurs (en cache SQLite par hash).
6. Construction des entrées (torrent × débrideur), tri, limites.
7. Sérialisation en streams Stremio.

---

## ⚠️ Avertissement

Ranger est un **agrégateur / proxy** : il n'héberge ni n'indexe aucun contenu. Vous êtes responsable de l'usage que vous en faites et du respect des lois de votre juridiction ainsi que des conditions d'utilisation des trackers et débrideurs que vous configurez.

## 📝 Licence

MIT
