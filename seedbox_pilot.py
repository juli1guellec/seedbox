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
YGG_COOKIE_FILE = os.path.join(DATA_DIR, "ygg_cookies.txt")

STATE = {
    "started_at": int(time.time()),
    "last_pilot_ts": None,
    "last_pilot_iso": None,
    "last_tracker_ts": 0,
    "last_tracker_iso": None,
    "last_error": None,
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
            "qbit_up_gb": 0.0,
            "qbit_dl_gb": 0.0,
            "qbit_ratio": 0.0,
            "last_check_iso": None,
        },
    },
    "autobrr": {
        "filters": [],
    },
    "recent_events": [],
}


def now_paris_iso():
    return datetime.datetime.now(TZ_PARIS).isoformat(timespec="seconds")


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


def set_autobrr_filter_enabled(filter_id, enabled, reason):
    f = autobrr_request(f"/api/filters/{filter_id}", "GET")
    if not f or bool(f.get("enabled")) == bool(enabled):
        return False
    for k in AUTOBRR_ARRAY_FIELDS:
        if f.get(k) is None:
            f[k] = []
    f["enabled"] = bool(enabled)
    autobrr_request(f"/api/filters/{filter_id}", "PUT", f)
    state_str = "ACTIVÉ" if enabled else "PAUSÉ"
    log_event(
        "AUTOBRR",
        f"Filtre '{f.get('name')}' (#{filter_id}) {state_str} — {reason}",
        {"filter_id": filter_id, "enabled": enabled, "reason": reason},
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


# ─── Moteur Principal de Pilotage (Cycle de 5 min) ───────────────────────────
def classify_tracker(url):
    u = (url or "").lower()
    if "c411" in u:
        return "c411"
    if "yggreborn" in u:
        return "yggreborn"
    return "other"


def is_hnr_safe_to_delete(t_info, tracker_key):
    seed_hours = t_info["seeding_time"] / 3600.0
    ratio = t_info["ratio"]
    if tracker_key == "c411":
        return seed_hours >= C411_MIN_SEED_HOURS or ratio >= C411_EXEMPT_RATIO
    if tracker_key == "yggreborn":
        return seed_hours >= YGG_MIN_SEED_HOURS or ratio >= YGG_EXEMPT_RATIO
    return seed_hours >= 72.0


def compute_profitability_score(t_info):
    """
    Score de rentabilité d'un torrent en seed :
    Plus le score est bas, plus le torrent est candidat à l'éviction si le disque est plein.
    """
    size_gb = max(0.1, t_info["size"] / (1024 ** 3))
    up_gb = t_info["uploaded"] / (1024 ** 3)
    seed_hours = max(1.0, t_info["seeding_time"] / 3600.0)
    up_speed_kb = t_info.get("upspeed", 0) / 1024.0
    leechers = max(0, t_info.get("num_incomplete", 0))
    # Upload horaire par Go de disque occupé + bonus vitesse active + bonus leechers
    hourly_roi = (up_gb / size_gb) / seed_hours
    return round(hourly_roi * 1000.0 + up_speed_kb * 0.5 + leechers * 2.0, 3)


def run_pilot_cycle():
    now_ts = int(time.time())
    now_iso = now_paris_iso()

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
    unlimited_hashes = []
    c411_forbidden_dl_detected = False

    by_tracker = {
        "yggreborn": {"count": 0, "size_gb": 0.0, "up_gb": 0.0, "dl_gb": 0.0},
        "c411": {"count": 0, "size_gb": 0.0, "up_gb": 0.0, "dl_gb": 0.0},
    }
    enriched_torrents = []

    for t in torrents:
        h = t["hash"]
        name = t["name"]
        state = t["state"]
        prog = t.get("progress", 0.0)
        added_on = t.get("added_on", now_ts)
        age_sec = max(0, now_ts - added_on)

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
            to_delete_hashes.append((h, name, "Fichiers manquants (missingFiles)"))
            continue
        if "removed from the tracker" in msgs and prog < 1.0:
            to_delete_hashes.append((h, name, "Supprimé du tracker avant complétion"))
            continue
        if state == "stalledDL" and prog == 0.0 and age_sec >= STALLED_FORBIDDEN_MAX_AGE_SEC:
            if "forbidden" in msgs or age_sec >= 86400:
                to_delete_hashes.append((h, name, f"Bloqué à 0% ({msgs.strip() or 'stalled 24h'})"))
                continue

        # S'assurer qu'aucune limite de ratio/temps ne coupe ce torrent
        if t.get("ratio_limit", -2) != -2 or t.get("seeding_time_limit", -2) != -2:
            unlimited_hashes.append(h)

        # Statistiques par tracker
        if t_key in by_tracker:
            by_tracker[t_key]["count"] += 1
            by_tracker[t_key]["size_gb"] = round(by_tracker[t_key]["size_gb"] + t["size"] / (1024 ** 3), 2)
            by_tracker[t_key]["up_gb"] = round(by_tracker[t_key]["up_gb"] + t["uploaded"] / (1024 ** 3), 2)
            by_tracker[t_key]["dl_gb"] = round(by_tracker[t_key]["dl_gb"] + t["downloaded"] / (1024 ** 3), 2)

        seed_h = round(t.get("seeding_time", 0) / 3600.0, 1)
        hnr_safe = is_hnr_safe_to_delete(t, t_key)
        score = compute_profitability_score(t)
        min_h = C411_MIN_SEED_HOURS if t_key == "c411" else YGG_MIN_SEED_HOURS

        enriched_torrents.append({
            "hash": h,
            "name": name,
            "tracker": t_key,
            "state": state,
            "progress_pct": round(prog * 100.0, 1),
            "size_gb": round(t["size"] / (1024 ** 3), 2),
            "uploaded_gb": round(t["uploaded"] / (1024 ** 3), 2),
            "downloaded_gb": round(t["downloaded"] / (1024 ** 3), 2),
            "ratio": round(t.get("ratio", 0.0), 2),
            "up_speed_kb": round(t.get("upspeed", 0) / 1024.0, 1),
            "seed_hours": seed_h,
            "hnr_target_hours": min_h,
            "hnr_safe": hnr_safe,
            "score": score,
            "seeds": t.get("num_complete", 0),
            "leechs": t.get("num_incomplete", 0),
            "tags": t.get("tags", ""),
            "added_on": added_on,
        })

    # Appliquer les nettoyages qBittorrent
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

    # 3. Garbage Collection Intelligent si le disque dépasse DISK_HIGH_WATERMARK_PCT (82%)
    used_pct = STATE["quota"]["used_pct"]
    if used_pct >= DISK_HIGH_WATERMARK_PCT:
        target_free_gb = (DISK_HIGH_WATERMARK_PCT - DISK_LOW_WATERMARK_PCT) / 100.0 * STATE["quota"]["limit_gb"]
        freed_gb = 0.0
        evict_candidates = [
            x for x in enriched_torrents
            if x["hnr_safe"] and x["progress_pct"] >= 100.0 and x["up_speed_kb"] < 50.0
        ]
        evict_candidates.sort(key=lambda x: x["score"])
        evicted_hashes = []
        for cand in evict_candidates:
            if freed_gb >= target_free_gb:
                break
            evicted_hashes.append(cand["hash"])
            freed_gb += cand["size_gb"]
            log_event(
                "SMART_EVICT",
                f"Éviction disque ({used_pct}%) : '{cand['name'][:45]}' "
                f"(Tracker={cand['tracker']}, Seed={cand['seed_hours']}h, Ratio={cand['ratio']}, Score={cand['score']})",
            )
        if evicted_hashes:
            QBIT.request("/api/v2/torrents/delete", {"hashes": "|".join(evicted_hashes), "deleteFiles": "true"})

    # Mettre à jour les métriques C411 observées dans qBittorrent
    c411_qb = by_tracker["c411"]
    STATE["trackers"]["c411"]["active_seeds_qbit"] = c411_qb["count"]
    STATE["trackers"]["c411"]["qbit_up_gb"] = c411_qb["up_gb"]
    STATE["trackers"]["c411"]["qbit_dl_gb"] = c411_qb["dl_gb"]
    STATE["trackers"]["c411"]["qbit_ratio"] = (
        round(c411_qb["up_gb"] / c411_qb["dl_gb"], 2) if c411_qb["dl_gb"] > 0 else 0.0
    )

    # 4. Surveillance Polie des Trackers (toutes les 30 minutes max)
    if now_ts - STATE["last_tracker_ts"] >= TRACKER_POLL_INTERVAL_SEC:
        poll_yggreborn_account()
        time.sleep(2)
        poll_c411_account()
        STATE["last_tracker_ts"] = now_ts
        STATE["last_tracker_iso"] = now_iso

    # 5. Pilotage Dynamique des Filtres Autobrr
    if AUTOBRR_API_KEY:
        try:
            disk_emergency = STATE["quota"]["used_pct"] >= DISK_EMERGENCY_STOP_PCT
            ygg_ratio = STATE["trackers"]["yggreborn"].get("ratio") or 1.39

            # Filtres Freeleech : toujours actifs sauf urgence disque (>92%)
            set_autobrr_filter_enabled(
                FILTER_ID_YGG_FREELEECH,
                not disk_emergency,
                "Urgence disque >92%" if disk_emergency else "Freeleech YGG actif (0 risque ratio)",
            )
            set_autobrr_filter_enabled(
                FILTER_ID_C411_FREELEECH,
                not disk_emergency,
                "Urgence disque >92%" if disk_emergency else "Freeleech C411 actif (0 risque ratio)",
            )

            # Filtre YGG - 2026 Hits FR/MULTi : actif si ratio YGG >= 1.10 et disque < 88%
            ygg_paid_ok = (not disk_emergency) and (STATE["quota"]["used_pct"] < 88.0) and (ygg_ratio >= YGG_MIN_RATIO_FOR_PAID)
            set_autobrr_filter_enabled(
                FILTER_ID_YGG_PAID,
                ygg_paid_ok,
                f"Ratio YGG={ygg_ratio} (seuil {YGG_MIN_RATIO_FOR_PAID}), Disque={STATE['quota']['used_pct']}%",
            )

            # Filtre C411 - 2026 Hits FR/MULTi : actif uniquement si C411 autorise le DL et pas de 403 Forbidden récent
            c411_allowed = STATE["trackers"]["c411"].get("download_allowed")
            c411_ratio = STATE["trackers"]["c411"].get("ratio")
            c411_paid_ok = (
                (not disk_emergency)
                and (not c411_forbidden_dl_detected)
                and (c411_allowed is True)
                and (c411_ratio is not None and c411_ratio >= C411_MIN_RATIO_FOR_PAID)
            )
            set_autobrr_filter_enabled(
                FILTER_ID_C411_PAID,
                c411_paid_ok,
                "Récupération ratio C411 en cours (seed + Zen)" if not c411_paid_ok else f"Ratio C411={c411_ratio} OK",
            )

            filters_list = autobrr_request("/api/filters", "GET") or []
            STATE["autobrr"]["filters"] = [
                {
                    "id": fl.get("id"),
                    "name": fl.get("name"),
                    "enabled": fl.get("enabled"),
                    "priority": fl.get("priority"),
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

    # Sauvegarder un point d'historique toutes les heures
    try:
        hist_point = {
            "ts": now_ts,
            "iso": now_iso,
            "ygg_ratio": STATE["trackers"]["yggreborn"].get("ratio"),
            "ygg_up_tb": STATE["trackers"]["yggreborn"].get("upload_tb"),
            "c411_qbit_up_gb": by_tracker["c411"]["up_gb"],
            "disk_used_gb": STATE["quota"]["used_gb"],
            "torrents_count": len(enriched_torrents),
        }
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(hist_point) + "\n")
    except Exception:
        pass


def pilot_loop():
    load_recent_events()
    log_event("STARTUP", "Seedbox Pilot démarré (Cycle qBittorrent=5m, Trackers=30m anti-ban)")
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
    document.getElementById('c411-seeds').textContent = (c411.active_seeds_qbit || 0) + ' torrents en seed';
    document.getElementById('c411-up').textContent = (c411.qbit_up_gb || 0) + ' Go';
    document.getElementById('c411-dl').textContent = (c411.qbit_dl_gb || 0) + ' Go';
    document.getElementById('c411-mode').textContent =
      c411.download_allowed ? 'Nominal (2026 + Freeleech)' : 'Seed & Zen Recovery + Freeleech';

    const q = s.quota || {};
    document.getElementById('disk-pct').textContent = (q.used_pct || 0) + ' %';
    document.getElementById('disk-bar').style.width = Math.min(100, q.used_pct || 0) + '%';
    document.getElementById('disk-used').textContent = (q.used_gb || 0) + ' / ' + (q.limit_gb || 2000) + ' Go';
    document.getElementById('disk-free').textContent = (q.free_gb || 0) + ' Go';
    document.getElementById('speeds').textContent =
      '⬆️ ' + (s.qbit.up_speed_mb || 0) + ' Mo/s  |  ⬇️ ' + (s.qbit.dl_speed_mb || 0) + ' Mo/s';

    const flHtml = (s.autobrr.filters || []).map(f =>
      '<div class="metric-row"><span>' + f.name + ' <small style="color:var(--muted)">(' + f.max_downloads + ')</small></span>' +
      '<span class="badge ' + (f.enabled ? 'badge-ok' : 'badge-warn') + '">' + (f.enabled ? 'ACTIF' : 'PAUSÉ') + '</span></div>'
    ).join('');
    document.getElementById('filters-list').innerHTML = flHtml || '<div style="color:var(--muted)">Aucun filtre</div>';

    const torrents = s.qbit.torrents || [];
    document.getElementById('torrents-count').textContent = torrents.length;
    document.getElementById('torrents-tbody').innerHTML = torrents.map(t => {
      const trBadge = t.tracker === 'yggreborn'
        ? '<span class="badge badge-ok">YGG</span>'
        : (t.tracker === 'c411' ? '<span class="badge badge-warn">C411</span>' : '<span class="badge">Autre</span>');
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
            self._send_json(200, {"ok": True, "last_pilot_ts": STATE["last_pilot_ts"]})
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
