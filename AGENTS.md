# 🤖 AGENTS.md — Guide de Continuité pour Agents IA (Claude, GPT, Gemini, Cursor)

Ce document contient tout le contexte technique, l'architecture et les **Règles d'Or** nécessaires pour qu'une autre IA (ou un développeur) puisse reprendre, maintenir ou faire évoluer ce projet immédiatement.

---

## ⚠️ RÈGLES D'OR ABSOLUES (À RESPECTER IMPÉRATIVEMENT)

1. **RÈGLE D'OR #1 — INFRASTRUCTURE HOMELAB (`192.168.0.4`)** :
   - Sur le serveur Homelab (`192.168.0.4`, managé via Dockhand sur `http://192.168.0.4:3000`, `env=1`), **vous avez uniquement le droit de modifier la stack `antigravity`**.
   - **INTERDICTION ABSOLUE ET ILLIMITÉE** de toucher à toute autre stack Docker sur le Homelab.
2. **RÈGLE D'OR #2 — PROTECTION ANTI-BAN DES TRACKERS (`C411` & `YGGreborn`)** :
   - **Ne jamais surcharger les indexeurs de requêtes.**
   - `seedbox_pilot.py` limite strictement les interrogations de profil tracker à **1 requête toutes les 30 minutes** (`TRACKER_POLL_INTERVAL_SEC=1800`) et persiste les cookies de session (`ygg_cookies.txt`) pour éviter de se reconnecter à chaque cycle.
   - Dans Autobrr, les flux RSS (`YggReborn` `#1` et `C411` `#4`) doivent rester à **`interval >= 5` minutes** (12 req/heure max, très loin des limites d'API de 600/h sur YGG).
3. **RÈGLE D'OR #3 — SÉCURITÉ DES SECRETS** :
   - Ne jamais commiter de mots de passe, passkeys ou clés API dans le dépôt GitHub (`https://github.com/juli1guellec/seedbox`). Utiliser exclusivement des variables d'environnement ou `.env` (ignoré par `.gitignore`).

---

## 🏗️ Architecture Technique

### 1. Composants sur la Seedbox Ultra.cc (`apone.ares.usbx.me`)
- **qBittorrent (`https://apone.ares.usbx.me/qbittorrent`)** :
  - API WebUI v2 (`/api/v2/...`).
  - Configuré sans aucune limite globale de ratio ni de temps de seed (`max_ratio_enabled: false`, `max_seeding_time_enabled: false`, `dht: false`, `pex: false`, `lsd: false`).
  - Chemin des téléchargements : `/home/apone/downloads/qbittorrent`.
- **Autobrr (`https://apone.ares.usbx.me/autobrr`)** :
  - API REST (`/api/filters`, `/api/feeds`, `/api/release`) authentifiée via le header `X-API-Token`.
  - **Attention spécificité API Autobrr** : Lors d'un `PUT /api/filters/{id}`, Autobrr renvoie une erreur SQL `500 NOT NULL constraint failed` si les tableaux vides omis par `GET` (`codecs`, `sources`, `resolutions`, `match_categories`, etc.) ne sont pas réinitialisés à `[]`. La fonction `update_autobrr_filter_enabled()` dans `seedbox_pilot.py` gère automatiquement cette normalisation.
  - Filtres configurés :
    - `ID 6` : `YGG - Freeleech Priority` (Priorité 20, `freeleech: true`, `300MiB`-`30GiB`, `3/HOUR`)
    - `ID 2` : `YGG - 2026 Hits FR/MULTi` (Priorité 5, `years: "2026"`, `1080p/2160p`, `FR/MULTi`, `700MiB`-`16GiB`, `2/HOUR`)
    - `ID 7` : `C411 - Freeleech Priority` (Priorité 20, `freeleech: true`, `300MiB`-`25GiB`, `2/HOUR`)
    - `ID 3` : `C411 - 2026 Hits FR/MULTi` (Priorité 5, `years: "2026"`, `1080p/2160p`, `FR/MULTi`, `700MiB`-`12GiB`, `2/HOUR`)
  - Script de vérification d'espace disque utilisé par les filtres : `/home/apone/freespace-ultra.sh {{.Size}}` (basé sur `quota -u apone`).
- **Quota JSON (`https://apone.ares.usbx.me/quota.json`)** :
  - Sur Ultra.cc, l'appel système `statvfs` (`df`) retourne l'espace libre de la partition partagée `/home23` et non le quota utilisateur (`1863 GiB`).
  - Un script cron (`*/5 * * * * /home/apone/update-quota-json.sh`) exécute `quota -u apone` toutes les 5 minutes et écrit `/home/apone/www/quota.json`, accessible en HTTPS avec Basic Auth (`SEEDBOX_USER:SEEDBOX_PASS`).

### 2. Démon `seedbox_pilot.py` (Homelab Stack `antigravity`)
- Écrit en **Python 3.12 standard library pure** (zéro dépendance externe requise, démarre instantanément dans `python:3.12-alpine`).
- **Port** : `8080` à l'intérieur du conteneur, mappé sur **`8100`** sur l'hôte Homelab (`http://192.168.0.4:8100`).
- **Fichiers d'état persistants (`DATA_DIR=/data`, mappé sur `/appdata/antigravity/seedbox`)** :
  - `pilot_events.jsonl` : Journal horodaté des actions automatiques (nettoyage, bascule de filtre, etc.).
  - `stats_history.jsonl` : Historique périodique du ratio, de l'upload et de l'espace disque.
  - `ygg_cookies.txt` : Cookie de session YGGreborn persisté pour éviter les reconnexions inutiles.

---

## 🔄 Logique de Décision de `seedbox_pilot.py`

1. **Cycle Rapide (toutes les 5 minutes)** :
   - Vérifie et corrige les préférences qBittorrent (`enforce_qbit_preferences()`).
   - Lit le quota disque réel via `/quota.json` (`fetch_ultra_quota()`).
   - Analyse tous les torrents et leurs trackers dans qBittorrent (`inspect_and_maintain_torrents()`) :
     - Supprime les trackers publics parasites des torrents privés YGG/C411.
     - Force `ratioLimit=-2` et `seedingTimeLimit=-2` (illimité).
     - Supprime les torrents bloqués à `0%` (`progress < 0.001`) depuis plus de 15 minutes dont le tracker renvoie `Forbidden` ou `Torrent removed`.
     - Calcule le statut Hit & Run (`hnr_safe`) et le score de rentabilité (`score`) de chaque torrent.
   - Si le disque dépasse `DISK_HIGH_WATERMARK_PCT` (`82 %`), évince les torrents `hnr_safe=True` ayant le score le plus faible jusqu'à redescendre sous `DISK_LOW_WATERMARK_PCT` (`75 %`).
2. **Cycle Lent Trackers & Filtres (toutes les 30 minutes)** :
   - Met à jour les statistiques du compte YGGreborn (`poll_yggreborn_account()`) et C411 (`poll_c411_account()`).
   - Synchronise l'état `enabled` des 4 filtres Autobrr (`sync_autobrr_filters()`) selon le disque disponible et le ratio de chaque tracker.

---

## 🧪 Commandes Utiles de Diagnostic & Maintenance

```bash
# Vérifier la santé du démon depuis le Homelab ou le Mac
curl -s http://192.168.0.4:8100/healthz | jq .

# Consulter l'état complet (ratios, disque, filtres Autobrr, torrents)
curl -s http://192.168.0.4:8100/api/status | jq '{quota: .quota, trackers: .trackers, autobrr: .autobrr}'

# Forcer l'exécution immédiate d'un cycle de pilotage
curl -s -X POST http://192.168.0.4:8100/api/run | jq .
```
