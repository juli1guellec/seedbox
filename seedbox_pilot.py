#!/usr/bin/env python3
"""
Seedbox Pilot — Démon autonome 24/7 d'optimisation de Ratio (C411 & YGGreborn)
==============================================================================
Pilote automatiquement la seedbox Ultra.cc (qBittorrent + Autobrr + qui) depuis
le Homelab pour maximiser le ratio sur C411 et YGGreborn sans jamais surcharger
les indexeurs (anti-ban strict) ni provoquer de Hit & Run (H&R).

Fonctionnalités principales :
1. Hygiène qBittorrent (cycle de 5 min) :
   - Désactive tout plafonnement de ratio/temps de seed dans qBittorrent pour laisser
     les torrents "pépites" monter à ratio 5, 10, 20+.
   - Désactive DHT / PeX / LSD et supprime automatiquement les trackers publics
     parasites (ex. nyacat.pw, zhuqiy.com) pour préserver le quota de 4 To/mois Ultra.cc.
   - Supprime proprement les téléchargements bloqués à 0 % en erreur tracker (403 Forbidden
     ou "Torrent removed from tracker") après 15 minutes pour stopper les annonces en erreur.
2. Garbage Collector Intelligent & Protection Anti-H&R :
   - Tant que le disque Ultra.cc (2000 Go) est < 80 %, conserve 100 % des torrents en seed
     pour maximiser la génération de Zen (C411) et l'upload long-terme (YGGreborn).
   - Au-delà de 80 %, évince uniquement les torrents les moins rentables ayant STRICTEMENT
     validé les règles H&R :
       * C411      : >= 74h de seed (règle 72h + 2h marge) OU ratio >= 2.1 (règle 2.0)
       * YGGreborn : >= 74h de seed OU ratio >= 1.5
3. Pilotage dynamique des filtres Autobrr & Surveillance Polie des Trackers (cycle de 30 min) :
   - Interroge YGGreborn (avec persistance de cookie) et C411 (API /api/user/profile) au
     maximum 1 fois toutes les 30 minutes (zéro risque de ban / rate-limit).
   - Active/désactive dynamiquement les filtres Autobrr selon le ratio réel, l'état de
     blocage C411 et l'espace disque disponible.
4. Tableau de bord Web & API REST (:8080 en conteneur / :8100 sur l'hôte) :
   - GET /          : Dashboard temps réel Dark Mode
   - GET /api/status: État JSON complet (trackers, seedbox, filtres, torrents, journal)
   - GET /healthz   : Healthcheck Docker
"""

import base64
import datetime
import hashlib
import http.cookiejar
import http.server
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

TZ_PARIS = ZoneInfo(os.environ.get("TZ", "Europe/Paris"))


def _load_dotenv_if_present():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


_load_dotenv_if_present()

PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

# ─── Configuration Ultra.cc (qBittorrent, Autobrr, Quota) ────────────────────
SEEDBOX_BASE_URL = os.environ.get("SEEDBOX_BASE_URL", "https://apone.ares.usbx.me").rstrip("/")
QBIT_URL = os.environ.get("QBIT_URL", f"{SEEDBOX_BASE_URL}/qbittorrent").rstrip("/")
AUTOBRR_URL = os.environ.get("AUTOBRR_URL", f"{SEEDBOX_BASE_URL}/autobrr").rstrip("/")
QUOTA_JSON_URL = os.environ.get("QUOTA_JSON_URL", f"{SEEDBOX_BASE_URL}/quota.json")

SEEDBOX_USER = os.environ.get("SEEDBOX_USER", os.environ.get("QBIT_USER", "apone"))
SEEDBOX_PASS = os.environ.get("SEEDBOX_PASS", os.environ.get("QBIT_PASS", ""))
AUTOBRR_API_KEY = os.environ.get("AUTOBRR_API_KEY", "")

# ─── Configuration Trackers (Anti-Ban : 1 requête / 30 min max) ──────────────
YGG_BASE_URL = os.environ.get("YGG_BASE_URL", "https://www.yggreborn.org").rstrip("/")
YGG_LOGIN = os.environ.get("YGG_LOGIN", os.environ.get("YGG_USER", ""))
YGG_PASS = os.environ.get("YGG_PASS", "")

C411_BASE_URL = os.environ.get("C411_BASE_URL", "https://c411.org").rstrip("/")
C411_API_KEY = os.environ.get("C411_API_KEY", "")

G3MINI_BASE_URL = os.environ.get("G3MINI_BASE_URL", "https://gemini-tracker.org").rstrip("/")
G3MINI_API_TOKEN = os.environ.get("G3MINI_API_TOKEN", "")
CROSS_SEED_INTERVAL_SEC = int(os.environ.get("CROSS_SEED_INTERVAL_SEC", "900"))  # 15 min

# ─── Paramètres de Stratégie & Seuils ────────────────────────────────────────
PILOT_INTERVAL_SEC = int(os.environ.get("PILOT_INTERVAL_SEC", "300"))          # 5 min
TRACKER_POLL_INTERVAL_SEC = int(os.environ.get("TRACKER_POLL_INTERVAL_SEC", "1800"))  # 30 min (Anti-ban strict)
DISK_HIGH_WATERMARK_PCT = float(os.environ.get("DISK_HIGH_WATERMARK_PCT", "82.0"))
DISK_LOW_WATERMARK_PCT = float(os.environ.get("DISK_LOW_WATERMARK_PCT", "75.0"))
DISK_EMERGENCY_STOP_PCT = float(os.environ.get("DISK_EMERGENCY_STOP_PCT", "92.0"))

# Règles Hit & Run (avec marge de sécurité de +2h et +0.1 ratio)
C411_MIN_SEED_HOURS = float(os.environ.get("C411_MIN_SEED_HOURS", "74.0"))     # Officiel C411 : 72h
C411_EXEMPT_RATIO = float(os.environ.get("C411_EXEMPT_RATIO", "2.1"))          # Officiel C411 : 2.0
YGG_MIN_SEED_HOURS = float(os.environ.get("YGG_MIN_SEED_HOURS", "74.0"))       # Marge 74h
YGG_EXEMPT_RATIO = float(os.environ.get("YGG_EXEMPT_RATIO", "1.5"))

# Seuils de ratio pour activer les filtres "Hits 2026" (hors Freeleech)
YGG_MIN_RATIO_FOR_PAID = float(os.environ.get("YGG_MIN_RATIO_FOR_PAID", "1.10"))
C411_MIN_RATIO_FOR_PAID = float(os.environ.get("C411_MIN_RATIO_FOR_PAID", "1.00"))
STALLED_FORBIDDEN_MAX_AGE_SEC = int(os.environ.get("STALLED_FORBIDDEN_MAX_AGE_SEC", "900"))  # 15 min

# IDs des filtres Autobrr
FILTER_ID_YGG_PAID = int(os.environ.get("FILTER_ID_YGG_PAID", "2"))
FILTER_ID_C411_PAID = int(os.environ.get("FILTER_ID_C411_PAID", "3"))
FILTER_ID_YGG_FREELEECH = int(os.environ.get("FILTER_ID_YGG_FREELEECH", "6"))
FILTER_ID_C411_FREELEECH = int(os.environ.get("FILTER_ID_C411_FREELEECH", "7"))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

EVENTS_FILE = os.path.join(DATA_DIR, "pilot_events.jsonl")
HISTORY_FILE = os.path.join(DATA_DIR, "stats_history.jsonl")
TELEMETRY_FILE = os.path.join(DATA_DIR, "telemetry.jsonl")
LEDGER_FILE = os.path.join(DATA_DIR, "torrent_ledger.json")
LEARNING_FILE = os.path.join(DATA_DIR, "learning_state.json")
CROSS_SEED_FILE = os.path.join(DATA_DIR, "cross_seed_state.json")
YGG_COOKIE_FILE = os.path.join(DATA_DIR, "ygg_cookies.txt")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
UNREPAID_DEBT_BRAKE_GB = float(os.environ.get("UNREPAID_DEBT_BRAKE_GB", "12.0"))

BASE_EXCEPT_RELEASES = (
    "*VOSTFR*,*SUBFRENCH*,*Fansub*,*CAM*,*TS*,*HDTS*,*DVDSCR*,"
    "*.ts,*_France*,*Essais.Libres*,*Essais.Qualificatifs*"
)

STATE = {
    "started_at": int(time.time()),
    "last_pilot_ts": None,
    "last_pilot_iso": None,
    "last_tracker_ts": 0,
    "last_tracker_iso": None,
    "last_cross_seed_ts": 0,
    "last_retention_ts": 0,
    "last_error": None,
    "logs_retention": {
        "retention_days": RETENTION_DAYS,
        "telemetry_count": 0,
        "history_count": 0,
        "events_count": 0,
        "last_cleanup_iso": None,
    },
    "quota": {
        "used_gb": 0.0,
        "limit_gb": 2000.0,
        "free_gb": 2000.0,
        "used_pct": 0.0,
    },
    "qbit": {
        "connected": False,
        "dl_speed_mb": 0.0,
        "up_speed_mb": 0.0,
        "session_dl_gb": 0.0,
        "session_up_gb": 0.0,
        "total_torrents": 0,
        "seeding_count": 0,
        "downloading_count": 0,
        "by_tracker": {},
        "torrents": [],
    },
    "trackers": {
        "yggreborn": {
            "status": "unknown",
            "ratio": None,
            "upload_tb": None,
            "download_tb": None,
            "freeleech_tb": None,
            "buffer_gb": None,
            "user_class": None,
            "last_check_iso": None,
        },
        "c411": {
            "status": "unknown",
            "ratio": None,
            "uploaded_gb": None,
            "downloaded_gb": None,
            "zen_balance": None,
            "download_allowed": None,
            "blocked_reason": None,
            "active_seeds_qbit": 0,
            "cross_seeded_count": 0,
            "qbit_up_gb": 0.0,
            "qbit_dl_gb": 0.0,
            "qbit_ratio": 0.0,
            "last_check_iso": None,
        },
        "g3mini": {
            "status": "vip_bridge_ready",
            "user_class": "VIP (DL Illimité)",
            "active_seeds_qbit": 0,
            "qbit_up_gb": 0.0,
            "qbit_dl_gb": 0.0,
            "last_check_iso": None,
        },
    },
    "autobrr": {
        "filters": [],
    },
    "learning": {
        "ledger_total_torrents": 0,
        "mature_torrents_analyzed": 0,
        "strategy_mode": "CALIBRATION",
        "unrepaid_dl_debt_6h_gb": 0.0,
        "debt_brake_active": False,
        "recommended_max_size": "9GiB",
        "recommended_max_downloads": 2,
        "auto_blacklisted_groups": [],
        "top_groups": [],
        "by_category": {},
        "by_size_bucket": {},
        "super_seed_active_count": 0,
        "last_updated_iso": None,
    },
    "recent_events": [],
}


def now_paris_iso():
    return datetime.datetime.now(TZ_PARIS).isoformat(timespec="seconds")


def prune_and_count_jsonl(filepath, retention_days=RETENTION_DAYS):
    """Nettoyage atomique des fichiers .jsonl selon la règle des 30 jours de telemetry-collector."""
    if not os.path.exists(filepath):
        return 0
    cutoff_ts = int(time.time()) - (retention_days * 86400)
    valid_lines = []
    dirty = False
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                    if rec.get("ts", 0) < cutoff_ts:
                        dirty = True
                        continue
                    valid_lines.append(s)
                except Exception:
                    dirty = True
        if dirty:
            tmp_path = filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                for s in valid_lines:
                    f.write(s + "\n")
            os.replace(tmp_path, filepath)
    except Exception:
        pass
    return len(valid_lines)


def run_retention_cleanup():
    t_cnt = prune_and_count_jsonl(TELEMETRY_FILE, RETENTION_DAYS)
    h_cnt = prune_and_count_jsonl(HISTORY_FILE, RETENTION_DAYS)
    e_cnt = prune_and_count_jsonl(EVENTS_FILE, RETENTION_DAYS)
    STATE["logs_retention"] = {
        "retention_days": RETENTION_DAYS,
        "telemetry_count": t_cnt,
        "history_count": h_cnt,
        "events_count": e_cnt,
        "last_cleanup_iso": now_paris_iso(),
    }
    STATE["last_retention_ts"] = int(time.time())


# ─── Moteur d'Auto-Apprentissage & d'Auto-Adaptation (RatioLearner) ──────────
def extract_torrent_features(name, size_gb, tags=""):
    n = name or ""
    n_low = n.lower()
    t_low = (tags or "").lower()
    is_fl = "freeleech" in t_low

    # 1. Classification du type de contenu
    if n_low.endswith(".ts") or "_france " in n_low or "_arte_" in n_low:
        content_type = "tv_raw_ts"
    elif any(k in n_low for k in ("formula.1", "essais.libres", "essais.qualificatifs", "motogp", "ufc.")):
        content_type = "sport_event"
    elif re.search(r"\bs\d{1,2}e\d{1,3}\b", n_low):
        content_type = "episode_tv"
    elif re.search(r"\b(s\d{1,2}|saison\s*\d+)\b", n_low):
        content_type = "season_pack"
    elif "2160p" in n_low or "4k" in n_low:
        content_type = "movie_4k"
    elif "1080p" in n_low:
        content_type = "movie_1080p"
    else:
        content_type = "other"

    # 2. Tranche de taille
    if size_gb < 2.0:
        size_bucket = "S (<2 Go)"
    elif size_gb < 6.0:
        size_bucket = "M (2-6 Go)"
    elif size_gb < 12.0:
        size_bucket = "L (6-12 Go)"
    else:
        size_bucket = "XL (>12 Go)"

    # 3. Extraction de la Team / Release Group
    stem = re.sub(r"\.(mkv|mp4|avi|ts|iso)$", "", n, flags=re.IGNORECASE)
    m_grp = re.search(r"-([A-Za-z0-9_]{2,15})$", stem)
    release_group = m_grp.group(1).upper() if m_grp else "UNKNOWN"

    return {
        "content_type": content_type,
        "size_bucket": size_bucket,
        "release_group": release_group,
        "is_freeleech": is_fl,
    }


def update_learning_engine(enriched_torrents, now_ts):
    ledger = {}
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, "r", encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception:
            ledger = {}

    # Mettre à jour le registre persistant avec les torrents actifs
    for t in enriched_torrents:
        h = t["hash"]
        feats = extract_torrent_features(t["name"], t["size_gb"], t.get("tags", ""))
        t["content_type"] = feats["content_type"]
        t["size_bucket"] = feats["size_bucket"]
        t["release_group"] = feats["release_group"]
        t["is_freeleech"] = feats["is_freeleech"]

        prev = ledger.get(h, {})
        ledger[h] = {
            "hash": h,
            "name": t["name"],
            "tracker": t["tracker"],
            "size_gb": t["size_gb"],
            "uploaded_gb": max(t["uploaded_gb"], prev.get("uploaded_gb", 0.0)),
            "downloaded_gb": max(t["downloaded_gb"], prev.get("downloaded_gb", t["size_gb"])),
            "ratio": max(t["ratio"], prev.get("ratio", 0.0)),
            "seed_hours": max(t["seed_hours"], prev.get("seed_hours", 0.0)),
            "added_on": t.get("added_on", prev.get("added_on", now_ts)),
            "last_seen_ts": now_ts,
            "content_type": feats["content_type"],
            "size_bucket": feats["size_bucket"],
            "release_group": feats["release_group"],
            "is_freeleech": feats["is_freeleech"] or prev.get("is_freeleech", False),
        }

    # Sauvegarder le ledger (en gardant les 1000 derniers torrents max)
    if len(ledger) > 1000:
        sorted_keys = sorted(ledger.keys(), key=lambda k: ledger[k].get("last_seen_ts", 0), reverse=True)[:1000]
        ledger = {k: ledger[k] for k in sorted_keys}
    try:
        tmp_l = LEDGER_FILE + ".tmp"
        with open(tmp_l, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
        os.replace(tmp_l, LEDGER_FILE)
    except Exception:
        pass

    # Analyser tous les torrents ayant au moins 2h de recul
    mature = [v for v in ledger.values() if v.get("seed_hours", 0) >= 2.0 and v.get("size_gb", 0) > 0.05]

    def _agg_bucket(items, key_name):
        buckets = {}
        for item in items:
            k = item.get(key_name, "other")
            b = buckets.setdefault(k, {"count": 0, "up_gb": 0.0, "dl_gb": 0.0, "paid_dl_gb": 0.0, "wins": 0, "seed_h": 0.0})
            b["count"] += 1
            b["up_gb"] += item["uploaded_gb"]
            b["dl_gb"] += item["size_gb"]
            if not item.get("is_freeleech"):
                b["paid_dl_gb"] += item["size_gb"]
            if item["ratio"] >= 0.85 or item.get("is_freeleech"):
                b["wins"] += 1
            b["seed_h"] += max(1.0, item["seed_hours"])
        out = {}
        for k, b in buckets.items():
            roi = round(b["up_gb"] / max(0.1, b["dl_gb"]), 2)
            net_buf = round(b["up_gb"] - b["paid_dl_gb"], 2)
            win_rate = round((b["wins"] / b["count"]) * 100.0, 1) if b["count"] else 0.0
            # Multiplicateur de rentabilité appris (entre 0.3 et 2.2)
            mult = round(max(0.3, min(2.2, roi * 1.15)), 2)
            out[k] = {
                "count": b["count"],
                "up_gb": round(b["up_gb"], 2),
                "dl_gb": round(b["dl_gb"], 2),
                "net_buffer_gb": net_buf,
                "roi": roi,
                "win_rate_pct": win_rate,
                "learned_weight": mult,
            }
        return out

    by_cat = _agg_bucket(mature, "content_type")
    by_size = _agg_bucket(mature, "size_bucket")
    by_grp = _agg_bucket([x for x in mature if x.get("seed_hours", 0) >= 5.0], "release_group")

    # Identifier les Release Groups déficitaires à blacklister sur les filtres payants
    auto_blacklisted = []
    for grp, st in by_grp.items():
        if grp in ("UNKNOWN", "NOTAG"):
            continue
        if st["count"] >= 2 and st["roi"] < 0.45 and st["net_buffer_gb"] <= -2.5:
            auto_blacklisted.append(grp)

    top_groups = sorted(
        [{"group": g, **st} for g, st in by_grp.items() if g != "UNKNOWN"],
        key=lambda x: (x["net_buffer_gb"], x["roi"]),
        reverse=True,
    )[:8]

    # Calibrer dynamiquement la taille max des filtres payants selon le ROI de XL (>12 Go) et L (6-12 Go)
    xl_stats = by_size.get("XL (>12 Go)", {})
    l_stats = by_size.get("L (6-12 Go)", {})
    if xl_stats.get("count", 0) >= 1 and xl_stats.get("net_buffer_gb", 0) <= -4.0 and xl_stats.get("roi", 1.0) < 0.75:
        if l_stats.get("win_rate_pct", 0.0) >= 60.0 or l_stats.get("roi", 1.0) >= 0.72:
            rec_max_size = "8GiB"
        else:
            rec_max_size = "6GiB"
    else:
        rec_max_size = "14GiB"

    # Calculer la "dette de download non remboursée" sur les 6 dernières heures (Frein Anti-Dilution)
    unrepaid_6h = 0.0
    for t in enriched_torrents:
        age_h = max(0.0, (now_ts - t.get("added_on", now_ts)) / 3600.0)
        if age_h <= 6.0 and not t.get("is_freeleech"):
            debt = max(0.0, t["size_gb"] - t["uploaded_gb"])
            unrepaid_6h += debt
    unrepaid_6h = round(unrepaid_6h, 2)
    debt_brake = unrepaid_6h >= UNREPAID_DEBT_BRAKE_GB

    if debt_brake:
        strategy_mode = "AMORTISSEMENT_SWARM (Pause DL payant, Freeleech 100%)"
        rec_max_dl = 1
    elif unrepaid_6h >= 6.0:
        strategy_mode = "PRUDENT (1 Hit/h + Freeleech)"
        rec_max_dl = 1
    elif unrepaid_6h < 2.5 and (STATE["trackers"]["yggreborn"].get("ratio") or 1.4) >= 1.35:
        strategy_mode = "OFFENSIF_CALIBRÉ (2 Hits ≤" + rec_max_size + "/h + Freeleech)"
        rec_max_dl = 2
    else:
        strategy_mode = "CROISIÈRE_OPTIMISÉE"
        rec_max_dl = 2

    learning_summary = {
        "ledger_total_torrents": len(ledger),
        "mature_torrents_analyzed": len(mature),
        "strategy_mode": strategy_mode,
        "unrepaid_dl_debt_6h_gb": unrepaid_6h,
        "debt_brake_active": debt_brake,
        "recommended_max_size": rec_max_size,
        "recommended_max_downloads": rec_max_dl,
        "auto_blacklisted_groups": sorted(auto_blacklisted),
        "top_groups": top_groups,
        "by_category": by_cat,
        "by_size_bucket": by_size,
        "super_seed_active_count": STATE["learning"].get("super_seed_active_count", 0),
        "last_updated_iso": now_paris_iso(),
    }
    STATE["learning"] = learning_summary
    try:
        tmp_s = LEARNING_FILE + ".tmp"
        with open(tmp_s, "w", encoding="utf-8") as f:
            json.dump(learning_summary, f, ensure_ascii=False, indent=2)
        os.replace(tmp_s, LEARNING_FILE)
    except Exception:
        pass
    return learning_summary


def log_event(category, message, details=None):
    entry = {
        "ts": int(time.time()),
        "iso": now_paris_iso(),
        "category": category,
        "message": message,
        "details": details or {},
    }
    print(f"[{entry['iso']}] [{category}] {message}", flush=True)
    STATE["recent_events"].insert(0, entry)
    del STATE["recent_events"][100:]
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_recent_events():
    if not os.path.exists(EVENTS_FILE):
        return
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-80:]
        events = [json.loads(line) for line in reversed(lines) if line.strip()]
        STATE["recent_events"] = events
    except Exception:
        pass


# ─── Connecteur qBittorrent ──────────────────────────────────────────────────
class QBitClient:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=SSL_CTX),
        )
        self._logged_in = False

    def login(self):
        data = urllib.parse.urlencode(
            {"username": SEEDBOX_USER, "password": SEEDBOX_PASS}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{QBIT_URL}/api/v2/auth/login",
            data=data,
            headers={"Referer": f"{QBIT_URL}/", "User-Agent": USER_AGENT},
        )
        with self.opener.open(req, timeout=15) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            self._logged_in = "Ok." in txt
            return self._logged_in

    def request(self, path, data_dict=None, timeout=15):
        if not self._logged_in:
            self.login()
        body = urllib.parse.urlencode(data_dict).encode("utf-8") if data_dict is not None else None
        req = urllib.request.Request(
            f"{QBIT_URL}{path}",
            data=body,
            headers={"Referer": f"{QBIT_URL}/", "User-Agent": USER_AGENT},
        )
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
        except urllib.error.HTTPError as e:
            if e.code == 403:
                self.login()
                with self.opener.open(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
            raise


QBIT = QBitClient()


# ─── Connecteur Autobrr ──────────────────────────────────────────────────────
def autobrr_request(path, method="GET", body=None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "X-API-Token": AUTOBRR_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(
        f"{AUTOBRR_URL}{path}", data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else None


AUTOBRR_ARRAY_FIELDS = [
    "resolutions", "codecs", "sources", "containers", "match_hdr", "except_hdr",
    "match_other", "except_other", "release_types_match", "release_types_ignore",
    "formats", "quality", "media", "match_language", "except_language",
    "origins", "except_origins", "announce_types",
]


def set_autobrr_filter_enabled(
    filter_id,
    enabled,
    reason,
    desired_max_size=None,
    desired_max_downloads=None,
    desired_except_releases=None,
):
    f = autobrr_request(f"/api/filters/{filter_id}", "GET")
    if not f:
        return False

    changed_fields = []
    if bool(f.get("enabled")) != bool(enabled):
        f["enabled"] = bool(enabled)
        changed_fields.append(f"enabled={'ACTIVÉ' if enabled else 'PAUSÉ'}")

    if desired_max_size and f.get("max_size") != desired_max_size:
        old_sz = f.get("max_size")
        f["max_size"] = desired_max_size
        changed_fields.append(f"max_size={old_sz}->{desired_max_size}")

    if desired_max_downloads is not None and int(f.get("max_downloads", 0)) != int(desired_max_downloads):
        old_dl = f.get("max_downloads")
        f["max_downloads"] = int(desired_max_downloads)
        changed_fields.append(f"max_downloads={old_dl}->{desired_max_downloads}/h")

    if desired_except_releases and f.get("except_releases") != desired_except_releases:
        f["except_releases"] = desired_except_releases
        changed_fields.append("except_releases=auto_learned")

    if not changed_fields:
        return False

    for k in AUTOBRR_ARRAY_FIELDS:
        if f.get(k) is None:
            f[k] = []
    autobrr_request(f"/api/filters/{filter_id}", "PUT", f)
    log_event(
        "AUTOBRR_ADAPT",
        f"Filtre '{f.get('name')}' (#{filter_id}) auto-adapté ({', '.join(changed_fields)}) — {reason}",
        {"filter_id": filter_id, "changes": changed_fields, "reason": reason},
    )
    return True


# ─── Lecture du Quota Disque Ultra.cc ────────────────────────────────────────
def fetch_ultra_quota(fallback_used_bytes=0):
    try:
        auth_b64 = base64.b64encode(f"{SEEDBOX_USER}:{SEEDBOX_PASS}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(
            QUOTA_JSON_URL,
            headers={"Authorization": f"Basic {auth_b64}", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=10) as resp:
            q = json.loads(resp.read().decode("utf-8"))
        used_gb = round(float(q.get("used_kib", 0)) / (1024 * 1024), 2)
        limit_gb = round(float(q.get("limit_kib", 1953497088)) / (1024 * 1024), 2)
        free_gb = round(max(0.0, limit_gb - used_gb), 2)
        used_pct = round((used_gb / limit_gb) * 100.0, 2) if limit_gb > 0 else 0.0
        STATE["quota"] = {
            "used_gb": used_gb,
            "limit_gb": limit_gb,
            "free_gb": free_gb,
            "used_pct": used_pct,
        }
    except Exception:
        used_gb = round(fallback_used_bytes / (1024 ** 3), 2)
        limit_gb = 1863.0
        STATE["quota"] = {
            "used_gb": used_gb,
            "limit_gb": limit_gb,
            "free_gb": round(max(0.0, limit_gb - used_gb), 2),
            "used_pct": round((used_gb / limit_gb) * 100.0, 2),
        }


# ─── Surveillance Polie des Trackers (Max 1 fois / 30 min) ───────────────────
def poll_yggreborn_account():
    if not YGG_LOGIN or not YGG_PASS:
        return
    cj = http.cookiejar.MozillaCookieJar(YGG_COOKIE_FILE)
    if os.path.exists(YGG_COOKIE_FILE):
        try:
            cj.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=SSL_CTX),
    )
    opener.addheaders = [("User-Agent", USER_AGENT)]

    def _parse_account_html(html):
        if "MON Compte" not in html and "Statistiques Tracker" not in html:
            return False
        txt = re.sub(r"<[^>]+>", " ", html)
        txt = re.sub(r"\s+", " ", txt)
        m_up = re.search(r"([\d.]+)\s*(To|Go)\s*Upload", txt, re.I)
        m_dl = re.search(r"([\d.]+)\s*(To|Go)\s*Download", txt, re.I)
        m_fl = re.search(r"([\d.]+)\s*(To|Go)\s*Freeleech", txt, re.I)
        m_ra = re.search(r"Ratio\s*:\s*([\d.]+)", txt, re.I) or re.search(r"([\d.]+)\s*Ratio", txt, re.I)
        m_st = re.search(r"Statut Utilisateur\s+([A-Z0-9]+)", txt)

        def _to_tb(m):
            if not m:
                return 0.0
            val = float(m.group(1))
            return round(val if m.group(2).lower() == "to" else val / 1024.0, 3)

        up_tb = _to_tb(m_up)
        dl_tb = _to_tb(m_dl)
        fl_tb = _to_tb(m_fl)
        ratio = float(m_ra.group(1)) if m_ra else (round(up_tb / dl_tb, 2) if dl_tb > 0 else 99.0)
        buffer_gb = round((up_tb - dl_tb) * 1024.0, 1)

        STATE["trackers"]["yggreborn"].update({
            "status": "ok",
            "ratio": ratio,
            "upload_tb": up_tb,
            "download_tb": dl_tb,
            "freeleech_tb": fl_tb,
            "buffer_gb": buffer_gb,
            "user_class": m_st.group(1) if m_st else "Membre",
            "last_check_iso": now_paris_iso(),
        })
        return True

    try:
        with opener.open(f"{YGG_BASE_URL}/account/", timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        if _parse_account_html(html):
            cj.save(ignore_discard=True, ignore_expires=True)
            return

        # Session expirée : 1 login propre avec pause polie
        time.sleep(2)
        with opener.open(f"{YGG_BASE_URL}/login", timeout=15) as resp:
            login_html = resp.read().decode("utf-8", errors="replace")
        m_csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_html)
        if not m_csrf:
            STATE["trackers"]["yggreborn"]["status"] = "csrf_missing"
            return
        time.sleep(1.5)
        post_data = urllib.parse.urlencode({
            "csrf_token": m_csrf.group(1),
            "identifier": YGG_LOGIN,
            "password": YGG_PASS,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{YGG_BASE_URL}/login",
            data=post_data,
            headers={"Referer": f"{YGG_BASE_URL}/login"},
        )
        opener.open(req, timeout=15).read()
        cj.save(ignore_discard=True, ignore_expires=True)
        time.sleep(1.5)
        with opener.open(f"{YGG_BASE_URL}/account/", timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        _parse_account_html(html)
    except Exception as e:
        STATE["trackers"]["yggreborn"]["status"] = f"error: {e}"


def poll_c411_account():
    if not C411_API_KEY:
        return
    req = urllib.request.Request(
        f"{C411_BASE_URL}/api/user/profile",
        headers={
            "Authorization": f"Bearer {C411_API_KEY}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        up_total = float(data.get("transfer", {}).get("uploaded", {}).get("total", 0))
        dl_total = float(data.get("transfer", {}).get("downloaded", {}).get("total", 0))
        ratio = round(up_total / dl_total, 2) if dl_total > 0 else 99.0
        dl_info = data.get("download", {})
        STATE["trackers"]["c411"].update({
            "status": "ok",
            "ratio": ratio,
            "uploaded_gb": round(up_total / (1024 ** 3), 2),
            "downloaded_gb": round(dl_total / (1024 ** 3), 2),
            "zen_balance": data.get("zen", {}).get("balance"),
            "download_allowed": dl_info.get("allowed", True),
            "blocked_reason": dl_info.get("blockedReason"),
            "last_check_iso": now_paris_iso(),
        })
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # Clé API actuelle limitée au scope torznab:read ; on s'appuie sur la télémétrie qBittorrent live
            STATE["trackers"]["c411"]["status"] = "torznab_key_only (scope profile:read optionnel)"
            STATE["trackers"]["c411"]["last_check_iso"] = now_paris_iso()
        else:
            STATE["trackers"]["c411"]["status"] = f"http_{e.code}"
    except Exception as e:
        STATE["trackers"]["c411"]["status"] = f"error: {e}"


# ─── Moteur de Cross-Seed & Pont VIP (G3MINI / YGG -> C411, 0 impact YGG) ────
def bdecode(data, idx=0):
    c = chr(data[idx])
    if c == "i":
        end = data.index(b"e", idx)
        return int(data[idx + 1 : end]), end + 1
    elif c == "l":
        idx += 1
        res = []
        while chr(data[idx]) != "e":
            val, idx = bdecode(data, idx)
            res.append(val)
        return res, idx + 1
    elif c == "d":
        idx += 1
        res = {}
        while chr(data[idx]) != "e":
            k, idx = bdecode(data, idx)
            v, idx = bdecode(data, idx)
            res[k] = v
        return res, idx + 1
    else:
        colon = data.index(b":", idx)
        length = int(data[idx:colon])
        start = colon + 1
        return data[start : start + length], start + length


def bencode(val):
    if isinstance(val, int):
        return f"i{val}e".encode("ascii")
    elif isinstance(val, bytes):
        return f"{len(val)}:".encode("ascii") + val
    elif isinstance(val, list):
        return b"l" + b"".join(bencode(x) for x in val) + b"e"
    elif isinstance(val, dict):
        items = sorted(val.items(), key=lambda kv: kv[0])
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    return b""


def qbit_upload_torrent_bytes(raw_torrent, save_path="/home/apone/downloads/qbittorrent/", paused=True, tags="cross-seed"):
    if not QBIT._logged_in:
        QBIT.login()
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    parts = []

    def _add_field(name, val):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode("utf-8")
        )

    _add_field("paused", "true" if paused else "false")
    _add_field("stopped", "true" if paused else "false")
    if save_path:
        _add_field("savepath", save_path)
    if tags:
        _add_field("tags", tags)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="seed.torrent"\r\n'
        f"Content-Type: application/x-bittorrent\r\n\r\n".encode("utf-8")
        + raw_torrent
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{QBIT_URL}/api/v2/torrents/add",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Referer": f"{QBIT_URL}/",
            "User-Agent": USER_AGENT,
        },
    )
    with QBIT.opener.open(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def inject_c411_cross_seed_from_existing(existing_torrent, c411_dl_url, c411_title=""):
    """
    Télécharge le .torrent C411, l'ajoute en mode PAUSED dans qBittorrent,
    aligne les chemins de fichiers avec le torrent existant (100% sur disque),
    puis lance un recheck local afin que la 1ère annonce à C411 soit à 100% (left=0).
    """
    req = urllib.request.Request(c411_dl_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
        raw_t = resp.read()
    t_dict, _ = bdecode(raw_t)
    info = t_dict.get(b"info")
    if not info:
        return None
    info_hash = hashlib.sha1(bencode(info)).hexdigest().lower()

    ehash = existing_torrent["hash"]
    save_path = existing_torrent.get("save_path") or "/home/apone/downloads/qbittorrent/"
    efiles = QBIT.request(f"/api/v2/torrents/files?hash={ehash}") or []

    qbit_upload_torrent_bytes(raw_t, save_path=save_path, paused=True, tags="cross-seed")
    time.sleep(1.2)

    nfiles = QBIT.request(f"/api/v2/torrents/files?hash={info_hash}") or []
    if len(efiles) == 1 and len(nfiles) == 1:
        old_p = nfiles[0]["name"]
        new_p = efiles[0]["name"]
        if old_p != new_p:
            QBIT.request("/api/v2/torrents/renameFile", {"hash": info_hash, "oldPath": old_p, "newPath": new_p})
    elif len(efiles) == len(nfiles) and len(efiles) > 1:
        e_by_sz = {f["size"]: f["name"] for f in efiles}
        for nf in nfiles:
            target_p = e_by_sz.get(nf["size"])
            if target_p and nf["name"] != target_p:
                QBIT.request("/api/v2/torrents/renameFile", {"hash": info_hash, "oldPath": nf["name"], "newPath": target_p})

    QBIT.request("/api/v2/torrents/recheck", {"hashes": info_hash})
    log_event(
        "CROSS_SEED",
        f"Cross-Seed C411 injecté en recheck (0 Mo DL) : '{c411_title or existing_torrent.get('name', '')[:50]}'",
        {"c411_hash": info_hash, "source_hash": ehash, "size_bytes": existing_torrent.get("size")},
    )
    return info_hash


def run_cross_seed_and_vip_bridge(torrents, now_ts):
    """
    Exécuté toutes les 15 minutes (0 impact supplémentaire sur YGGreborn) :
    1. Récupère les 50 dernières sorties C411 (1 requête / 15 min) + cache Autobrr.
    2. Tout torrent déjà à 100% sur le disque (YGG normal ou G3MINI VIP) qui existe sur C411
       est automatiquement injecté en Cross-Seed sur C411 (recheck -> 100% -> seed, 0 Mo DL).
    3. Pont VIP G3MINI -> C411 : si une nouveauté (300 Mo - 15 Go) sort simultanément sur
       G3MINI (statut VIP = DL illimité sans impact ratio) et C411, la télécharge via G3MINI
       pour la cross-seeder automatiquement à 100% sur C411 dès complétion.
    """
    if not C411_API_KEY:
        return
    try:
        cs_state = {"checked_sizes": [], "bridged_sizes": [], "c411_urls": {}}
        if os.path.exists(CROSS_SEED_FILE):
            try:
                with open(CROSS_SEED_FILE, "r", encoding="utf-8") as f:
                    cs_state.update(json.load(f))
            except Exception:
                pass
        checked_sizes = set(cs_state.get("checked_sizes", []))
        bridged_sizes = set(cs_state.get("bridged_sizes", []))
        c411_urls = dict(cs_state.get("c411_urls", {}))

        # Indexer les torrents présents dans qBittorrent par taille exacte (octets)
        c411_sizes_in_qbit = set()
        completed_non_c411_by_size = {}
        all_qbit_sizes = set()
        active_bridge_dl = 0

        for t in torrents:
            sz = int(t.get("size") or 0)
            if sz <= 0:
                continue
            all_qbit_sizes.add(sz)
            tags = t.get("tags", "")
            tr = (t.get("tracker") or "").lower()
            prog = float(t.get("progress", 0.0))
            if "c411" in tr or "cross-seed" in tags:
                c411_sizes_in_qbit.add(sz)
            elif prog >= 1.0:
                completed_non_c411_by_size[sz] = t
            if "g3mini-bridge" in tags and prog < 1.0:
                active_bridge_dl += 1

        # 1. Lire le flux Torznab C411 des 50 dernières sorties (1 seule requête / 15 min)
        c411_live_url = f"{C411_BASE_URL}/api?" + urllib.parse.urlencode({
            "t": "search",
            "apikey": C411_API_KEY,
            "limit": "50",
        })
        req_c = urllib.request.Request(c411_live_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req_c, context=SSL_CTX, timeout=15) as resp_c:
            root_c = ET.fromstring(resp_c.read().decode("utf-8", errors="replace"))
        for it in root_c.findall(".//item"):
            sz = int(it.findtext("size") or 0)
            enc = it.find("enclosure")
            if sz > 100_000_000 and enc is not None and enc.attrib.get("url"):
                c411_urls[str(sz)] = {
                    "url": enc.attrib["url"],
                    "name": it.findtext("title") or "",
                }

        # A. Cross-Seed Passif immédiat : Torrents déjà à 100% sur la Seedbox (YGG ou G3MINI) qui matchent C411
        injected_count = 0
        for sz, src_t in list(completed_non_c411_by_size.items()):
            if sz in c411_sizes_in_qbit:
                continue
            c_entry = c411_urls.get(str(sz))
            if c_entry and c_entry.get("url") and injected_count < 2:
                time.sleep(2.0)
                inject_c411_cross_seed_from_existing(src_t, c_entry["url"], c_entry.get("name", ""))
                c411_sizes_in_qbit.add(sz)
                injected_count += 1

        # B. Vérification polie C411 Torznab (MAX 1 seul torrent nouvellement complété par cycle de 15 min)
        if injected_count == 0:
            for sz, src_t in list(completed_non_c411_by_size.items()):
                if sz in c411_sizes_in_qbit or sz in checked_sizes:
                    continue
                checked_sizes.add(sz)
                clean = re.sub(r"\.(mkv|mp4|avi|ts)$", "", src_t.get("name", ""), flags=re.I)
                tokens = re.split(r"[._\s-]+", clean)
                q_words = []
                for tok in tokens:
                    if re.match(r"^(1080p|2160p|multi|french|vff|vfq|vfi|truefrench|web|webrip|bluray|custom|s\d+e\d+|s\d+)$", tok, re.I):
                        break
                    q_words.append(tok)
                q = " ".join(q_words[:4])
                if len(q) >= 3:
                    time.sleep(2.5)
                    c411_url = f"{C411_BASE_URL}/api?" + urllib.parse.urlencode({
                        "t": "search",
                        "apikey": C411_API_KEY,
                        "q": q,
                        "limit": "30",
                    })
                    req = urllib.request.Request(c411_url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
                        root = ET.fromstring(resp.read().decode("utf-8", errors="replace"))
                    for it in root.findall(".//item"):
                        if int(it.findtext("size") or 0) == sz:
                            enc = it.find("enclosure")
                            if enc is not None and enc.attrib.get("url"):
                                time.sleep(2.5)
                                inject_c411_cross_seed_from_existing(src_t, enc.attrib["url"], it.findtext("title") or "")
                                c411_sizes_in_qbit.add(sz)
                            break
                break  # Strictement 1 seule recherche par cycle de 15 min (Anti-ban)

        # C. Pont Actif VIP G3MINI -> C411 (0 impact YGGreborn, 0 impact G3MINI VIP, 0 Mo DL C411)
        if G3MINI_API_TOKEN and active_bridge_dl < 2 and STATE["quota"]["used_pct"] < 80.0:
            req_g = urllib.request.Request(
                f"{G3MINI_BASE_URL}/api/torrents?api_token={G3MINI_API_TOKEN}&perPage=25",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req_g, context=SSL_CTX, timeout=15) as resp:
                g_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for it in g_data.get("data", []):
                attr = it.get("attributes", {})
                sz = int(attr.get("size") or 0)
                seeders = int(attr.get("seeders") or 0)
                dl_link = attr.get("download_link")
                c_entry = c411_urls.get(str(sz))
                # Taille entre 300 Mo et 15 Go, au moins 1 seeder sur G3MINI, et présent à l'octet près sur C411
                if (
                    300_000_000 <= sz <= 15_000_000_000
                    and seeders >= 1
                    and dl_link
                    and c_entry
                    and sz not in all_qbit_sizes
                    and sz not in bridged_sizes
                ):
                    req_gt = urllib.request.Request(dl_link, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req_gt, context=SSL_CTX, timeout=15) as resp_gt:
                        raw_gt = resp_gt.read()
                    qbit_upload_torrent_bytes(raw_gt, paused=False, tags="g3mini-bridge")
                    bridged_sizes.add(sz)
                    log_event(
                        "VIP_BRIDGE",
                        f"Pont VIP G3MINI -> C411 lancé : '{attr.get('name', '')[:50]}' ({round(sz / (1024**3), 2)} Go, 0 impact ratio)",
                        {"size_bytes": sz, "c411_match": c_entry.get("name")},
                    )
                    break  # Max 1 nouveau téléchargement pont VIP par cycle de 15 min

        # Conserver uniquement les 300 dernières entrées c411_urls
        if len(c411_urls) > 300:
            keep_keys = list(c411_urls.keys())[-300:]
            c411_urls = {k: c411_urls[k] for k in keep_keys}

        with open(CROSS_SEED_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "checked_sizes": list(checked_sizes)[-500:],
                "bridged_sizes": list(bridged_sizes)[-500:],
                "c411_urls": c411_urls,
                "updated_iso": now_paris_iso(),
            }, f)
    except Exception as e:
        log_event("CROSS_SEED_WARN", f"Note cycle cross-seed : {e}")


# ─── Moteur Principal de Pilotage (Cycle de 5 min) ───────────────────────────
def classify_tracker(url):
    u = (url or "").lower()
    if "c411" in u:
        return "c411"
    if "yggreborn" in u:
        return "yggreborn"
    if "gemini-tracker" in u:
        return "g3mini"
    return "other"


def is_hnr_safe_to_delete(t_info, tracker_key):
    seed_hours = t_info["seeding_time"] / 3600.0
    ratio = t_info["ratio"]
    if tracker_key == "c411":
        return seed_hours >= C411_MIN_SEED_HOURS or ratio >= C411_EXEMPT_RATIO
    if tracker_key == "yggreborn":
        return seed_hours >= YGG_MIN_SEED_HOURS or ratio >= YGG_EXEMPT_RATIO
    return seed_hours >= 72.0


def compute_profitability_score(t_info, tracker_key="other"):
    """
    Score de rentabilité auto-adaptatif d'un torrent en seed :
    Intègre le rendement horaire par Go, la vitesse instantanée, les leechers,
    le poids appris de sa catégorie (RatioLearner) et un bonus de protection C411 (Zen).
    """
    size_gb = max(0.1, t_info["size"] / (1024 ** 3))
    up_gb = t_info["uploaded"] / (1024 ** 3)
    seed_hours = max(1.0, t_info["seeding_time"] / 3600.0)
    up_speed_kb = t_info.get("upspeed", 0) / 1024.0
    leechers = max(0, t_info.get("num_incomplete", 0))
    hourly_roi = (up_gb / size_gb) / seed_hours
    base_score = hourly_roi * 1000.0 + up_speed_kb * 0.5 + leechers * 2.5

    feats = extract_torrent_features(t_info.get("name", ""), size_gb, t_info.get("tags", ""))
    cat_stats = STATE.get("learning", {}).get("by_category", {}).get(feats["content_type"], {})
    cat_weight = float(cat_stats.get("learned_weight", 1.0))

    # Bonus de protection C411 (+150%) pour maximiser la génération de points Zen
    tracker_mult = 2.5 if tracker_key == "c411" else 1.0
    return round(base_score * cat_weight * tracker_mult, 3)


def run_pilot_cycle():
    now_ts = int(time.time())
    now_iso = now_paris_iso()

    # Nettoyage automatique des logs > 30 jours toutes les 6h (discipline telemetry-collector)
    if now_ts - STATE.get("last_retention_ts", 0) >= 6 * 3600:
        run_retention_cleanup()

    # 1. Vérifier/appliquer les préférences qBittorrent (Anti-sabotage)
    prefs = QBIT.request("/api/v2/app/preferences")
    if isinstance(prefs, dict):
        needed = {}
        if prefs.get("max_ratio_enabled"):
            needed["max_ratio_enabled"] = False
            needed["max_ratio"] = -1
        if prefs.get("max_seeding_time_enabled"):
            needed["max_seeding_time_enabled"] = False
            needed["max_seeding_time"] = -1
        for priv_flag in ("dht", "pex", "lsd"):
            if prefs.get(priv_flag):
                needed[priv_flag] = False
        if needed:
            QBIT.request("/api/v2/app/setPreferences", {"json": json.dumps(needed)})
            log_event("QBIT_CONFIG", "Préférences qBittorrent corrigées automatiquement", needed)

    # 2. Infos de transfert & Liste des torrents
    tx = QBIT.request("/api/v2/transfer/info") or {}
    torrents = QBIT.request("/api/v2/torrents/info") or []

    total_size_bytes = sum(t.get("size", 0) for t in torrents)
    fetch_ultra_quota(fallback_used_bytes=total_size_bytes)

    to_delete_hashes = []
    to_delete_keep_files_hashes = []
    to_resume_cross_seeds = []
    unlimited_hashes = []
    super_seed_enable = []
    super_seed_disable = []
    c411_forbidden_dl_detected = False

    by_tracker = {
        "yggreborn": {"count": 0, "size_gb": 0.0, "up_gb": 0.0, "dl_gb": 0.0},
        "c411": {"count": 0, "size_gb": 0.0, "up_gb": 0.0, "dl_gb": 0.0},
        "g3mini": {"count": 0, "size_gb": 0.0, "up_gb": 0.0, "dl_gb": 0.0},
    }
    size_counts = {}
    for t in torrents:
        sz_b = int(t.get("size") or 0)
        if sz_b > 0:
            size_counts[sz_b] = size_counts.get(sz_b, 0) + 1

    enriched_torrents = []

    for t in torrents:
        h = t["hash"]
        name = t["name"]
        state = t["state"]
        prog = t.get("progress", 0.0)
        tags = t.get("tags", "")
        added_on = t.get("added_on", now_ts)
        age_sec = max(0, now_ts - added_on)

        # Démarrer automatiquement les Cross-Seeds C411 vérifiés à 100% (left=0 garanti)
        if "cross-seed" in tags:
            if prog >= 1.0 and ("paused" in state.lower() or "stopped" in state.lower()):
                to_resume_cross_seeds.append((h, name))
            elif prog < 1.0 and ("paused" in state.lower() or "stopped" in state.lower()) and "checking" not in state.lower() and age_sec >= 180:
                to_delete_keep_files_hashes.append((h, name, f"Recheck cross-seed incomplet ({round(prog*100, 1)}%)"))
                continue

        # Vérifier les trackers du torrent
        tr_list = QBIT.request(f"/api/v2/torrents/trackers?hash={h}") or []
        http_tr = [x for x in tr_list if x.get("url", "").startswith("http")]
        primary_url = t.get("tracker") or (http_tr[0]["url"] if http_tr else "")
        t_key = classify_tracker(primary_url)
        if t_key == "other":
            for x in http_tr:
                cand = classify_tracker(x.get("url", ""))
                if cand != "other":
                    t_key = cand
                    break

        # Supprimer les trackers publics parasites
        bad_urls = [
            x["url"]
            for x in http_tr
            if classify_tracker(x["url"]) == "other"
        ]
        if bad_urls:
            QBIT.request(
                "/api/v2/torrents/removeTrackers",
                {"hash": h, "urls": "|".join(bad_urls)},
            )
            log_event("HYGIENE", f"Trackers publics supprimés sur '{name[:45]}'", {"urls": bad_urls})

        # Détecter les torrents bloqués en erreur tracker (Forbidden / Removed)
        msgs = " ".join(str(x.get("msg", "")) for x in http_tr).lower()
        if "forbidden" in msgs and prog < 1.0 and t_key == "c411":
            c411_forbidden_dl_detected = True

        if state == "missingFiles":
            to_delete_keep_files_hashes.append((h, name, "Fichiers manquants (missingFiles)"))
            continue
        if "removed from the tracker" in msgs and prog < 1.0:
            if size_counts.get(int(t.get("size") or 0), 1) > 1:
                to_delete_keep_files_hashes.append((h, name, "Supprimé du tracker avant complétion (fichier partagé conservé)"))
            else:
                to_delete_hashes.append((h, name, "Supprimé du tracker avant complétion"))
            continue
        if state == "stalledDL" and prog == 0.0 and age_sec >= STALLED_FORBIDDEN_MAX_AGE_SEC:
            if "forbidden" in msgs or age_sec >= 86400:
                if size_counts.get(int(t.get("size") or 0), 1) > 1:
                    to_delete_keep_files_hashes.append((h, name, f"Bloqué à 0% ({msgs.strip() or 'stalled 24h'})"))
                else:
                    to_delete_hashes.append((h, name, f"Bloqué à 0% ({msgs.strip() or 'stalled 24h'})"))
                continue

        # S'assurer qu'aucune limite de ratio/temps ne coupe ce torrent
        if t.get("ratio_limit", -2) != -2 or t.get("seeding_time_limit", -2) != -2:
            unlimited_hashes.append(h)

        # Swarm Boost : Super Seeding uniquement si seeder initial (<=2 seeds et >=2 leechers connectés)
        leechs_cnt = t.get("num_incomplete", 0)
        connected_leechs = t.get("num_leechs", 0)
        seeds_cnt = t.get("num_complete", 0)
        cur_super = bool(t.get("super_seeding", False))
        want_super = prog >= 1.0 and seeds_cnt <= 2 and connected_leechs >= 2 and t.get("ratio", 0.0) < 1.5
        if want_super and not cur_super:
            super_seed_enable.append(h)
        elif not want_super and cur_super:
            super_seed_disable.append(h)

        # Statistiques par tracker
        if t_key in by_tracker:
            by_tracker[t_key]["count"] += 1
            by_tracker[t_key]["size_gb"] = round(by_tracker[t_key]["size_gb"] + t["size"] / (1024 ** 3), 2)
            by_tracker[t_key]["up_gb"] = round(by_tracker[t_key]["up_gb"] + t["uploaded"] / (1024 ** 3), 2)
            by_tracker[t_key]["dl_gb"] = round(by_tracker[t_key]["dl_gb"] + t["downloaded"] / (1024 ** 3), 2)

        seed_h = round(t.get("seeding_time", 0) / 3600.0, 1)
        hnr_safe = is_hnr_safe_to_delete(t, t_key)
        score = compute_profitability_score(t, t_key)
        min_h = C411_MIN_SEED_HOURS if t_key == "c411" else YGG_MIN_SEED_HOURS

        enriched_torrents.append({
            "hash": h,
            "name": name,
            "tracker": t_key,
            "state": state,
            "progress_pct": round(prog * 100.0, 1),
            "size_gb": round(t["size"] / (1024 ** 3), 2),
            "size_bytes": int(t.get("size") or 0),
            "uploaded_gb": round(t["uploaded"] / (1024 ** 3), 2),
            "downloaded_gb": round(t["downloaded"] / (1024 ** 3), 2),
            "ratio": round(t.get("ratio", 0.0), 2),
            "up_speed_kb": round(t.get("upspeed", 0) / 1024.0, 1),
            "seed_hours": seed_h,
            "hnr_target_hours": min_h,
            "hnr_safe": hnr_safe,
            "score": score,
            "seeds": t.get("num_complete", 0),
            "leechs": leechs_cnt,
            "super_seeding": want_super,
            "tags": tags,
            "added_on": added_on,
        })

    # Activer les Cross-Seeds C411 vérifiés à 100%
    if to_resume_cross_seeds:
        r_hashes = "|".join(x[0] for x in to_resume_cross_seeds)
        for ep in ("/api/v2/torrents/resume", "/api/v2/torrents/start"):
            try:
                QBIT.request(ep, {"hashes": r_hashes})
            except Exception:
                pass
        for h, nm in to_resume_cross_seeds:
            log_event("CROSS_SEED_ACTIVE", f"Cross-Seed C411 100% vérifié et démarré en seed (0 Mo DL) : '{nm[:50]}'")

    # Appliquer les nettoyages et optimisations qBittorrent
    if to_delete_keep_files_hashes:
        hashes_str = "|".join(x[0] for x in to_delete_keep_files_hashes)
        QBIT.request("/api/v2/torrents/delete", {"hashes": hashes_str, "deleteFiles": "false"})
        for h, nm, reason in to_delete_keep_files_hashes:
            log_event("CLEANUP", f"Retrait entrée torrent (fichiers conservés) '{nm[:50]}' : {reason}")

    if to_delete_hashes:
        hashes_str = "|".join(x[0] for x in to_delete_hashes)
        QBIT.request("/api/v2/torrents/delete", {"hashes": hashes_str, "deleteFiles": "true"})
        for h, nm, reason in to_delete_hashes:
            log_event("CLEANUP", f"Suppression torrent mort '{nm[:50]}' : {reason}")

    if unlimited_hashes:
        QBIT.request(
            "/api/v2/torrents/setShareLimits",
            {
                "hashes": "|".join(unlimited_hashes),
                "ratioLimit": -2,
                "seedingTimeLimit": -2,
                "inactiveSeedingTimeLimit": -2,
            },
        )
        log_event("QBIT_LIMITS", f"Limites de ratio désactivées sur {len(unlimited_hashes)} torrents")

    if super_seed_enable:
        QBIT.request("/api/v2/torrents/setSuperSeeding", {"hashes": "|".join(super_seed_enable), "value": "true"})
    if super_seed_disable:
        QBIT.request("/api/v2/torrents/setSuperSeeding", {"hashes": "|".join(super_seed_disable), "value": "false"})
    STATE["learning"]["super_seed_active_count"] = sum(1 for x in enriched_torrents if x.get("super_seeding"))

    # 2b. Exécuter le moteur d'Auto-Apprentissage & calcul du ROI par catégorie/taille/team
    learned = update_learning_engine(enriched_torrents, now_ts)

    # 2c. Exécuter le moteur Cross-Seed & Pont VIP G3MINI -> C411 (toutes les 15 min)
    if now_ts - STATE.get("last_cross_seed_ts", 0) >= CROSS_SEED_INTERVAL_SEC:
        run_cross_seed_and_vip_bridge(torrents, now_ts)
        STATE["last_cross_seed_ts"] = now_ts

    # 3. Garbage Collection Intelligent si le disque dépasse DISK_HIGH_WATERMARK_PCT (82%)
    used_pct = STATE["quota"]["used_pct"]
    if used_pct >= DISK_HIGH_WATERMARK_PCT:
        target_free_gb = (DISK_HIGH_WATERMARK_PCT - DISK_LOW_WATERMARK_PCT) / 100.0 * STATE["quota"]["limit_gb"]
        freed_gb = 0.0
        # Ne jamais supprimer sur disque un fichier partagé avec un torrent C411 actif
        c411_active_sizes = {x["size_bytes"] for x in enriched_torrents if x["tracker"] == "c411"}
        evict_candidates = [
            x for x in enriched_torrents
            if x["hnr_safe"]
            and x["progress_pct"] >= 100.0
            and x["up_speed_kb"] < 50.0
            and (x["tracker"] == "c411" or x["size_bytes"] not in c411_active_sizes)
        ]
        evict_candidates.sort(key=lambda x: x["score"])
        evicted_hashes = []
        for cand in evict_candidates:
            if freed_gb >= target_free_gb:
                break
            if size_counts.get(cand["size_bytes"], 1) > 1:
                continue
            evicted_hashes.append(cand["hash"])
            freed_gb += cand["size_gb"]
            log_event(
                "SMART_EVICT",
                f"Éviction disque ({used_pct}%) : '{cand['name'][:45]}' "
                f"(Tracker={cand['tracker']}, Seed={cand['seed_hours']}h, Ratio={cand['ratio']}, Score={cand['score']})",
            )
        if evicted_hashes:
            QBIT.request("/api/v2/torrents/delete", {"hashes": "|".join(evicted_hashes), "deleteFiles": "true"})

    # Mettre à jour les métriques C411 et G3MINI observées dans qBittorrent
    c411_qb = by_tracker["c411"]
    STATE["trackers"]["c411"]["active_seeds_qbit"] = c411_qb["count"]
    STATE["trackers"]["c411"]["cross_seeded_count"] = sum(
        1 for x in enriched_torrents if x["tracker"] == "c411" and "cross-seed" in x.get("tags", "")
    )
    STATE["trackers"]["c411"]["qbit_up_gb"] = c411_qb["up_gb"]
    STATE["trackers"]["c411"]["qbit_dl_gb"] = c411_qb["dl_gb"]
    STATE["trackers"]["c411"]["qbit_ratio"] = (
        round(c411_qb["up_gb"] / c411_qb["dl_gb"], 2) if c411_qb["dl_gb"] > 0 else 0.0
    )

    g3_qb = by_tracker["g3mini"]
    STATE["trackers"]["g3mini"]["active_seeds_qbit"] = g3_qb["count"]
    STATE["trackers"]["g3mini"]["qbit_up_gb"] = g3_qb["up_gb"]
    STATE["trackers"]["g3mini"]["qbit_dl_gb"] = g3_qb["dl_gb"]
    STATE["trackers"]["g3mini"]["last_check_iso"] = now_iso

    # 4. Surveillance Polie des Trackers (toutes les 30 minutes max)
    if now_ts - STATE["last_tracker_ts"] >= TRACKER_POLL_INTERVAL_SEC:
        poll_yggreborn_account()
        time.sleep(2)
        poll_c411_account()
        STATE["last_tracker_ts"] = now_ts
        STATE["last_tracker_iso"] = now_iso

    # 5. Pilotage Dynamique & Auto-Adaptation des Filtres Autobrr
    if AUTOBRR_API_KEY:
        try:
            disk_emergency = STATE["quota"]["used_pct"] >= DISK_EMERGENCY_STOP_PCT
            ygg_ratio = STATE["trackers"]["yggreborn"].get("ratio") or 1.40

            # Construire la liste d'exclusions apprises (Base + Teams déficitaires détectées)
            extra_excepts = [f"*-{grp}*" for grp in learned.get("auto_blacklisted_groups", [])]
            adaptive_except = BASE_EXCEPT_RELEASES + ("," + ",".join(extra_excepts) if extra_excepts else "")
            adaptive_max_size = learned.get("recommended_max_size", "9GiB")
            adaptive_max_dl = learned.get("recommended_max_downloads", 2)
            debt_brake = learned.get("debt_brake_active", False)
            unrepaid_6h = learned.get("unrepaid_dl_debt_6h_gb", 0.0)

            c411_allowed = STATE["trackers"]["c411"].get("download_allowed")
            c411_ratio = STATE["trackers"]["c411"].get("ratio")
            # Tant que C411 bloque le téléchargement direct (Marge < 0 / ratio < 0.80),
            # on pause aussi le filtre Freeleech direct C411 (#7) et on alimente C411
            # à 100% par Cross-Seed (left=0, 0 Mo DL) depuis YGG normal et G3MINI VIP !
            c411_direct_dl_ok = (
                (not disk_emergency)
                and (not c411_forbidden_dl_detected)
                and (c411_allowed is True)
            )

            set_autobrr_filter_enabled(
                FILTER_ID_YGG_FREELEECH,
                not disk_emergency,
                "Urgence disque >92%" if disk_emergency else "Freeleech YGG actif (0 risque ratio)",
            )
            set_autobrr_filter_enabled(
                FILTER_ID_C411_FREELEECH,
                c411_direct_dl_ok,
                "C411 alimenté par Cross-Seed YGG/G3MINI (0 Mo DL)" if not c411_direct_dl_ok else "Freeleech C411 actif",
            )

            # Filtre YGG - 2026 Hits FR/MULTi : piloté par Ratio + Disque + Frein Anti-Dilution + Calibration Taille/Teams
            ygg_paid_ok = (
                (not disk_emergency)
                and (STATE["quota"]["used_pct"] < 88.0)
                and (ygg_ratio >= YGG_MIN_RATIO_FOR_PAID)
                and (not debt_brake)
            )
            ygg_reason = (
                f"Frein Anti-Dilution actif (dette 6h={unrepaid_6h}Go ≥ {UNREPAID_DEBT_BRAKE_GB}Go)"
                if debt_brake
                else f"Ratio YGG={ygg_ratio}, MaxSize={adaptive_max_size}, Excl={len(extra_excepts)} teams"
            )
            set_autobrr_filter_enabled(
                FILTER_ID_YGG_PAID,
                ygg_paid_ok,
                ygg_reason,
                desired_max_size=adaptive_max_size,
                desired_max_downloads=adaptive_max_dl,
                desired_except_releases=adaptive_except,
            )

            # Filtre C411 - 2026 Hits FR/MULTi : actif uniquement si C411 autorise le DL, pas de 403 et dette 6h OK
            c411_paid_ok = (
                c411_direct_dl_ok
                and (c411_ratio is not None and c411_ratio >= C411_MIN_RATIO_FOR_PAID)
                and (not debt_brake)
            )
            set_autobrr_filter_enabled(
                FILTER_ID_C411_PAID,
                c411_paid_ok,
                "Récupération C411 via Cross-Seed + Zen (0 Mo DL)" if not c411_paid_ok else f"Ratio C411={c411_ratio} OK",
                desired_max_size=adaptive_max_size,
                desired_max_downloads=adaptive_max_dl,
                desired_except_releases=adaptive_except,
            )

            filters_list = autobrr_request("/api/filters", "GET") or []
            STATE["autobrr"]["filters"] = [
                {
                    "id": fl.get("id"),
                    "name": fl.get("name"),
                    "enabled": fl.get("enabled"),
                    "priority": fl.get("priority"),
                    "max_size": fl.get("max_size"),
                    "max_downloads": f"{fl.get('max_downloads', 0)}/{fl.get('max_downloads_unit', 'HOUR')}",
                }
                for fl in filters_list
            ]
        except Exception as e:
            log_event("AUTOBRR_ERROR", f"Erreur synchronisation Autobrr : {e}")

    # Trier les torrents par upload total décroissant pour le dashboard
    enriched_torrents.sort(key=lambda x: (x["up_speed_kb"], x["uploaded_gb"]), reverse=True)

    STATE["qbit"] = {
        "connected": tx.get("connection_status") == "connected",
        "dl_speed_mb": round(tx.get("dl_info_speed", 0) / (1024 * 1024), 2),
        "up_speed_mb": round(tx.get("up_info_speed", 0) / (1024 * 1024), 2),
        "session_dl_gb": round(tx.get("dl_info_data", 0) / (1024 ** 3), 2),
        "session_up_gb": round(tx.get("up_info_data", 0) / (1024 ** 3), 2),
        "total_torrents": len(enriched_torrents),
        "seeding_count": sum(1 for x in enriched_torrents if x["progress_pct"] >= 100.0),
        "downloading_count": sum(1 for x in enriched_torrents if x["progress_pct"] < 100.0),
        "by_tracker": by_tracker,
        "torrents": enriched_torrents,
    }
    STATE["last_pilot_ts"] = now_ts
    STATE["last_pilot_iso"] = now_iso
    STATE["last_error"] = None

    # 6. Sauvegarder la télémétrie structurée (compatible telemetry-collector, rétention 30j)
    try:
        telemetry_point = {
            "ts": now_ts,
            "iso": now_iso,
            "ygg": {
                "ratio": STATE["trackers"]["yggreborn"].get("ratio"),
                "up_tb": STATE["trackers"]["yggreborn"].get("upload_tb"),
                "dl_tb": STATE["trackers"]["yggreborn"].get("download_tb"),
                "fl_tb": STATE["trackers"]["yggreborn"].get("freeleech_tb"),
                "buffer_gb": STATE["trackers"]["yggreborn"].get("buffer_gb"),
                "qbit_up_gb": by_tracker["yggreborn"]["up_gb"],
                "qbit_count": by_tracker["yggreborn"]["count"],
            },
            "c411": {
                "ratio": STATE["trackers"]["c411"].get("ratio") or STATE["trackers"]["c411"].get("qbit_ratio"),
                "qbit_up_gb": by_tracker["c411"]["up_gb"],
                "qbit_dl_gb": by_tracker["c411"]["dl_gb"],
                "qbit_count": by_tracker["c411"]["count"],
                "zen": STATE["trackers"]["c411"].get("zen_balance"),
            },
            "disk": STATE["quota"],
            "learning": {
                "mode": learned.get("strategy_mode"),
                "unrepaid_6h_gb": learned.get("unrepaid_dl_debt_6h_gb"),
                "max_size": learned.get("recommended_max_size"),
                "blacklisted_groups": learned.get("auto_blacklisted_groups"),
            },
        }
        with open(TELEMETRY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(telemetry_point, ensure_ascii=False) + "\n")
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now_ts,
                "iso": now_iso,
                "ygg_ratio": telemetry_point["ygg"]["ratio"],
                "ygg_up_tb": telemetry_point["ygg"]["up_tb"],
                "c411_qbit_up_gb": by_tracker["c411"]["up_gb"],
                "disk_used_gb": STATE["quota"]["used_gb"],
                "torrents_count": len(enriched_torrents),
            }) + "\n")
        STATE["logs_retention"]["telemetry_count"] += 1
        STATE["logs_retention"]["history_count"] += 1
    except Exception:
        pass


def pilot_loop():
    run_retention_cleanup()
    load_recent_events()
    log_event("STARTUP", "Seedbox Pilot démarré avec Auto-Apprentissage ROI & Rétention Logs 30j")
    while True:
        try:
            run_pilot_cycle()
        except Exception as e:
            STATE["last_error"] = f"{now_paris_iso()}: {e}"
            log_event("ERROR", f"Erreur cycle de pilotage : {e}")
        time.sleep(PILOT_INTERVAL_SEC)


# ─── Interface Web & Serveur HTTP ────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Seedbox Pilot — Ultra.cc (C411 & YGGreborn)</title>
<style>
  :root {
    --bg: #0b0f17;
    --card: #131b2e;
    --border: #1e293b;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --emerald: #10b981;
    --amber: #f59e0b;
    --rose: #f43f5e;
    --sky: #38bdf8;
    --violet: #a78bfa;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 20px;
    line-height: 1.45;
  }
  .container { max-width: 1400px; margin: 0 auto; }
  header {
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
    padding-bottom: 16px; margin-bottom: 20px; border-bottom: 1px solid var(--border);
  }
  h1 { font-size: 1.4rem; font-weight: 700; display: flex; align-items: center; gap: 10px; }
  .badge {
    display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.75rem; font-weight: 600;
  }
  .badge-ok { background: rgba(16,185,129,0.15); color: var(--emerald); border: 1px solid rgba(16,185,129,0.35); }
  .badge-warn { background: rgba(245,158,11,0.15); color: var(--amber); border: 1px solid rgba(245,158,11,0.35); }
  .badge-err { background: rgba(244,63,94,0.15); color: var(--rose); border: 1px solid rgba(244,63,94,0.35); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 16px; margin-bottom: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px;
  }
  .card h2 { font-size: 0.9rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin-bottom: 12px; }
  .metric-row { display: flex; justify-content: space-between; padding: 5px 0; font-size: 0.9rem; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .metric-row:last-child { border-bottom: none; }
  .big-val { font-size: 1.8rem; font-weight: 700; color: var(--emerald); margin-bottom: 6px; }
  .bar-bg { background: #1e293b; height: 8px; border-radius: 999px; overflow: hidden; margin: 8px 0; }
  .bar-fill { background: linear-gradient(90deg, var(--sky), var(--emerald)); height: 100%; }
  table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 0.72rem; }
  .right { text-align: right; font-variant-numeric: tabular-nums; }
  .log-list { max-height: 260px; overflow-y: auto; font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.78rem; }
  .log-item { padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  button {
    background: var(--sky); color: #0b0f17; border: none; border-radius: 8px;
    padding: 7px 14px; font-weight: 600; cursor: pointer; font-size: 0.85rem;
  }
  button:hover { opacity: 0.9; }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>🚀 Seedbox Pilot — Ultra.cc (C411 & YGGreborn)</h1>
      <div style="font-size:0.82rem;color:var(--muted)" id="sub-header">Chargement...</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;">
      <span id="qbit-badge" class="badge badge-ok">qBittorrent</span>
      <button onclick="triggerRun()">🔄 Forcer un cycle</button>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>🟢 YGGreborn (Compte & Seedbox)</h2>
      <div class="big-val" id="ygg-ratio">--</div>
      <div class="metric-row"><span>Upload Total (Compte)</span><strong id="ygg-up">--</strong></div>
      <div class="metric-row"><span>Download Comptabilisé</span><strong id="ygg-dl">--</strong></div>
      <div class="metric-row"><span>Téléchargé en Freeleech</span><strong id="ygg-fl" style="color:var(--sky)">--</strong></div>
      <div class="metric-row"><span>Buffer d'avance</span><strong id="ygg-buf" style="color:var(--emerald)">--</strong></div>
      <div class="metric-row"><span>Torrents actifs (Seedbox)</span><strong id="ygg-seeds">--</strong></div>
      <div class="metric-row"><span>Dernier relevé (Poli 30m)</span><span id="ygg-ts" style="color:var(--muted)">--</span></div>
    </div>

    <div class="card">
      <h2>🟣 C411 (Seed & Zen Recovery)</h2>
      <div class="big-val" id="c411-ratio" style="color:var(--violet)">--</div>
      <div class="metric-row"><span>Torrents en Seed (Seedbox)</span><strong id="c411-seeds">--</strong></div>
      <div class="metric-row"><span>Upload Seedbox actif</span><strong id="c411-up">--</strong></div>
      <div class="metric-row"><span>Download Seedbox actif</span><strong id="c411-dl">--</strong></div>
      <div class="metric-row"><span>Règle Anti-H&R appliquée</span><strong>74h Seed ou Ratio ≥ 2.1</strong></div>
      <div class="metric-row"><span>Mode Pilote C411</span><strong id="c411-mode" style="color:var(--amber)">--</strong></div>
    </div>

    <div class="card">
      <h2>💾 Seedbox Ultra.cc (Spirit-v2)</h2>
      <div class="big-val" id="disk-pct" style="color:var(--sky)">--%</div>
      <div class="bar-bg"><div class="bar-fill" id="disk-bar" style="width:10%"></div></div>
      <div class="metric-row"><span>Espace Disque Utilisé</span><strong id="disk-used">--</strong></div>
      <div class="metric-row"><span>Espace Disque Libre</span><strong id="disk-free">--</strong></div>
      <div class="metric-row"><span>Seuil d'éviction intelligente</span><strong>82% (H&R-safe uniquement)</strong></div>
      <div class="metric-row"><span>Vitesse Upload / Download</span><strong id="speeds">--</strong></div>
    </div>

    <div class="card">
      <h2>⚡ Filtres Autobrr Pilotés</h2>
      <div id="filters-list"></div>
    </div>
  </div>

  <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));">
    <div class="card">
      <h2>🧠 Moteur d'Auto-Apprentissage & Auto-Adaptation (RatioLearner)</h2>
      <div class="metric-row"><span>Mode Stratégique Actif</span><strong id="learn-mode" style="color:var(--emerald)">--</strong></div>
      <div class="metric-row"><span>Mémoire d'Apprentissage (Torrents)</span><strong id="learn-count">--</strong></div>
      <div class="metric-row"><span>Dette Download 6h (Frein ≥12 Go)</span><strong id="learn-debt">--</strong></div>
      <div class="metric-row"><span>Plafond Taille Auto-Calibré (Payant)</span><strong id="learn-maxsize" style="color:var(--sky)">--</strong></div>
      <div class="metric-row"><span>Teams Auto-Blacklistées (ROI &lt; 0.45)</span><strong id="learn-bl" style="color:var(--rose)">--</strong></div>
      <div class="metric-row"><span>Rétention Logs (30 jours)</span><span id="learn-ret" style="color:var(--muted)">--</span></div>
    </div>

    <div class="card">
      <h2>📈 Rentabilité Apprise par Catégorie & Tranche de Taille</h2>
      <div id="learn-buckets"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:20px;">
    <h2>📦 Torrents Actifs sur Ultra.cc (<span id="torrents-count">0</span>) — Triés par Rentabilité & Protection H&R</h2>
    <div style="overflow-x:auto;">
      <table>
        <thead>
          <tr>
            <th>Tracker</th>
            <th>Nom du Torrent</th>
            <th class="right">Taille</th>
            <th class="right">Uploadé</th>
            <th class="right">Ratio</th>
            <th class="right">Seed / Cible H&R</th>
            <th>Statut H&R</th>
            <th class="right">Score</th>
            <th class="right">Pairs (S/L)</th>
          </tr>
        </thead>
        <tbody id="torrents-tbody"></tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <h2>📜 Journal des Actions Automatiques (Seedbox Pilot)</h2>
    <div class="log-list" id="events-list"></div>
  </div>
</div>

<script>
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('sub-header').textContent =
      'Dernier cycle : ' + (s.last_pilot_iso || '--') + ' • Surveillance Trackers anti-ban : toutes les 30 min';

    const ygg = s.trackers.yggreborn || {};
    const yggQb = (s.qbit.by_tracker && s.qbit.by_tracker.yggreborn) || {};
    document.getElementById('ygg-ratio').textContent = ygg.ratio ? ('Ratio ' + ygg.ratio) : '--';
    document.getElementById('ygg-up').textContent = (ygg.upload_tb ?? '--') + ' To';
    document.getElementById('ygg-dl').textContent = (ygg.download_tb ?? '--') + ' To';
    document.getElementById('ygg-fl').textContent = (ygg.freeleech_tb ?? '--') + ' To (0 impact ratio)';
    document.getElementById('ygg-buf').textContent = (ygg.buffer_gb !== null ? ('+' + ygg.buffer_gb + ' Go') : '--');
    document.getElementById('ygg-seeds').textContent = (yggQb.count || 0) + ' torrents (' + (yggQb.up_gb || 0) + ' Go UP)';
    document.getElementById('ygg-ts').textContent = ygg.last_check_iso || '--';

    const c411 = s.trackers.c411 || {};
    const c411Ratio = c411.ratio || c411.qbit_ratio || 0;
    document.getElementById('c411-ratio').textContent = 'Ratio Seed ' + c411Ratio;
    document.getElementById('c411-seeds').textContent =
      (c411.active_seeds_qbit || 0) + ' torrents (dont ' + (c411.cross_seeded_count || 0) + ' Cross-Seeds 0 Mo DL)';
    document.getElementById('c411-up').textContent = (c411.qbit_up_gb || 0) + ' Go';
    document.getElementById('c411-dl').textContent = (c411.qbit_dl_gb || 0) + ' Go';
    document.getElementById('c411-mode').textContent =
      c411.download_allowed ? 'Nominal (2026 + Freeleech)' : 'Cross-Seed YGG/G3MINI VIP (0 Mo DL) + Zen';

    const q = s.quota || {};
    document.getElementById('disk-pct').textContent = (q.used_pct || 0) + ' %';
    document.getElementById('disk-bar').style.width = Math.min(100, q.used_pct || 0) + '%';
    document.getElementById('disk-used').textContent = (q.used_gb || 0) + ' / ' + (q.limit_gb || 2000) + ' Go';
    document.getElementById('disk-free').textContent = (q.free_gb || 0) + ' Go';
    document.getElementById('speeds').textContent =
      '⬆️ ' + (s.qbit.up_speed_mb || 0) + ' Mo/s  |  ⬇️ ' + (s.qbit.dl_speed_mb || 0) + ' Mo/s';

    const flHtml = (s.autobrr.filters || []).map(f =>
      '<div class="metric-row"><span>' + f.name + ' <small style="color:var(--muted)">(' + f.max_downloads + (f.max_size ? ', ≤' + f.max_size : '') + ')</small></span>' +
      '<span class="badge ' + (f.enabled ? 'badge-ok' : 'badge-warn') + '">' + (f.enabled ? 'ACTIF' : 'PAUSÉ') + '</span></div>'
    ).join('');
    document.getElementById('filters-list').innerHTML = flHtml || '<div style="color:var(--muted)">Aucun filtre</div>';

    const lrn = s.learning || {};
    const ret = s.logs_retention || {};
    document.getElementById('learn-mode').textContent = lrn.strategy_mode || '--';
    document.getElementById('learn-count').textContent =
      (lrn.mature_torrents_analyzed || 0) + ' analysés / ' + (lrn.ledger_total_torrents || 0) + ' en mémoire';
    document.getElementById('learn-debt').textContent =
      (lrn.unrepaid_dl_debt_6h_gb || 0) + ' Go ' + (lrn.debt_brake_active ? '🛑 FREIN ACTIF' : '✅ OK');
    document.getElementById('learn-maxsize').textContent =
      (lrn.recommended_max_size || '9GiB') + ' (' + (lrn.recommended_max_downloads || 2) + '/h)';
    document.getElementById('learn-bl').textContent =
      (lrn.auto_blacklisted_groups && lrn.auto_blacklisted_groups.length)
        ? lrn.auto_blacklisted_groups.join(', ')
        : 'Aucune (0)';
    document.getElementById('learn-ret').textContent =
      'Purge auto 30j (' + (ret.telemetry_count || 0) + ' pts télémétrie)';

    const catEntries = Object.entries(lrn.by_category || {});
    const szEntries = Object.entries(lrn.by_size_bucket || {});
    const bucketRows = [
      ...catEntries.map(([k, v]) => ({label: 'Cat: ' + k, ...v})),
      ...szEntries.map(([k, v]) => ({label: 'Taille: ' + k, ...v})),
    ];
    document.getElementById('learn-buckets').innerHTML = bucketRows.map(b => {
      const col = b.roi >= 0.85 ? 'var(--emerald)' : (b.roi >= 0.5 ? 'var(--amber)' : 'var(--rose)');
      const sign = b.net_buffer_gb >= 0 ? '+' : '';
      return '<div class="metric-row">' +
        '<span>' + b.label + ' <small style="color:var(--muted)">(' + b.count + ' torrents)</small></span>' +
        '<strong style="color:' + col + '">ROI ' + b.roi + 'x (' + sign + b.net_buffer_gb + ' Go)</strong>' +
      '</div>';
    }).join('') || '<div style="color:var(--muted)">Collecte en cours...</div>';

    const torrents = s.qbit.torrents || [];
    document.getElementById('torrents-count').textContent = torrents.length;
    document.getElementById('torrents-tbody').innerHTML = torrents.map(t => {
      const trBadge = t.tracker === 'yggreborn'
        ? '<span class="badge badge-ok">YGG</span>'
        : (t.tracker === 'c411'
            ? '<span class="badge badge-warn">C411' + ((t.tags || '').includes('cross-seed') ? ' ⚡CS' : '') + '</span>'
            : (t.tracker === 'g3mini' ? '<span class="badge badge-ok">G3MINI VIP</span>' : '<span class="badge">Autre</span>'));
      const hnrBadge = t.hnr_safe
        ? '<span class="badge badge-ok">✅ H&R Validé</span>'
        : '<span class="badge badge-warn">⏳ Protégé (' + t.seed_hours + 'h/' + t.hnr_target_hours + 'h)</span>';
      return '<tr>' +
        '<td>' + trBadge + '</td>' +
        '<td style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + t.name + '">' + t.name + '</td>' +
        '<td class="right">' + t.size_gb + ' Go</td>' +
        '<td class="right" style="color:var(--emerald);font-weight:600;">' + t.uploaded_gb + ' Go</td>' +
        '<td class="right">' + t.ratio + '</td>' +
        '<td class="right">' + t.seed_hours + ' h</td>' +
        '<td>' + hnrBadge + '</td>' +
        '<td class="right">' + t.score + '</td>' +
        '<td class="right">' + t.seeds + ' / ' + t.leechs + '</td>' +
      '</tr>';
    }).join('');

    document.getElementById('events-list').innerHTML = (s.recent_events || []).map(ev =>
      '<div class="log-item"><span style="color:var(--muted)">[' + ev.iso + ']</span> ' +
      '<strong style="color:var(--sky)">[' + ev.category + ']</strong> ' + ev.message + '</div>'
    ).join('');
  } catch (e) {
    console.error(e);
  }
}

async function triggerRun() {
  await fetch('/api/run', {method: 'POST'});
  await loadStatus();
}

loadStatus();
setInterval(loadStatus, 15000);
</script>
</body>
</html>
"""


class PilotHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send_json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/healthz":
            self._send_json(200, {
                "ok": True,
                "last_pilot_ts": STATE["last_pilot_ts"],
                "logs_retention": STATE["logs_retention"],
                "strategy_mode": STATE["learning"].get("strategy_mode"),
            })
        elif self.path.startswith("/api/learning"):
            self._send_json(200, STATE["learning"])
        elif self.path.startswith("/api/status"):
            self._send_json(200, STATE)
        else:
            raw = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def do_POST(self):
        if self.path == "/api/run":
            try:
                run_pilot_cycle()
                self._send_json(200, {"ok": True, "last_pilot_iso": STATE["last_pilot_iso"]})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
        else:
            self._send_json(404, {"error": "not_found"})


if __name__ == "__main__":
    t = threading.Thread(target=pilot_loop, daemon=True)
    t.start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), PilotHTTPHandler)
    print(f"Seedbox Pilot HTTP server listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()
