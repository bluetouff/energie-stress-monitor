#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
builder.py — Moniteur de stress énergétique multi-actifs (l0g.fr)

Construit un snapshot statique (snapshot.json) à partir de sources primaires.
Tout en stdlib : aucune dépendance externe, aucune clé exposée au navigateur.

Philosophie "zen" : tout le travail (clés, fetch, calcul des z-scores et de
l'indice composite) se fait ici, côté serveur, lancé par un timer systemd.
Le front ne lit que du JSON pré-calculé + deux flux live sans clé.

Sources (toutes vérifiées) :
  - EIA  v2 petroleum    : Brent et WTI spot quotidiens              (clé EIA_KEY)
  - EIA  v2 /seriesid/   : Henry Hub et stocks brut US               (clé EIA_KEY)
  - GIE  AGSI+           : stockage gaz Europe (% full)               (clé GIE_KEY, header x-key)
  - ENTSO-E              : prix day-ahead FR + DE-LU (A44)            (token ENTSOE_TOKEN)
  - CFTC  Socrata legacy : positionnement non-commercial WTI/NatGas  (sans clé)
  - ODRE  éCO2mix        : mix France + intensité CO2                 (sans clé)

Robustesse : chaque source est isolée dans un try/except. Si une source tombe,
on conserve la valeur du snapshot précédent. Elle reste exploitable tant que sa
date respecte la cadence normale de publication, puis passe "stale". Le build
ne plante jamais sur une seule source en échec.
"""

import os
import sys
import json
import math
import statistics
import urllib.request
import urllib.parse
import urllib.error
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.environ.get("ENERGIE_OUT", os.path.join(HERE, "web", "snapshot.json"))
HTTP_TIMEOUT = int(os.environ.get("ENERGIE_TIMEOUT", "25"))
USER_AGENT = "l0g-energie-monitor/1.0 (+https://l0g.fr)"

# Clés (jamais loggées, jamais écrites dans le snapshot)
EIA_KEY = os.environ.get("EIA_KEY", "").strip()
GIE_KEY = os.environ.get("GIE_KEY", "").strip()
ENTSOE_TOKEN = os.environ.get("ENTSOE_TOKEN", "").strip()

# Le timer général reste à 30 min pour les sources intrajournalières, mais
# l'EIA n'est jamais interrogée plus souvent que cette fenêtre. Les réponses
# publiques validées sont mises en cache côté serveur, sans aucune clé.
EIA_MIN_REFRESH_SECONDS = int(os.environ.get("ENERGIE_EIA_MIN_REFRESH_SECONDS", "14400"))
EIA_CACHE_DIR = os.environ.get(
    "ENERGIE_EIA_CACHE_DIR", os.path.join(HERE, ".cache", "eia"))
EIA_FETCH_META = {}

# Fenêtre d'historique pour les z-scores (jours calendaires)
HIST_DAYS = 365 * 3            # on tire ~3 ans
ZSCORE_WINDOW = 252            # z calculé sur ~1 an glissant (jours ouvrés)
MOMENTUM_LAG = 20             # momentum = variation sur 20 points

# Codes contrat CFTC legacy futures-only
CFTC_WTI = "067651"            # WTI Crude Oil — NYMEX
CFTC_NATGAS = "023651"         # Natural Gas — NYMEX

# EIC zones ENTSO-E
EIC_FR = "10YFR-RTE------C"
EIC_DE = "10Y1001A1001A82H"    # DE-LU bidding zone

# Pondérations de l'indice composite (documentées dans le README)
WEIGHTS = {
    "petrole": 0.30,
    "gaz": 0.25,
    "electricite": 0.20,
    "positionnement": 0.15,
    "contexte": 0.10,
}


# ---------------------------------------------------------------------------
# Utilitaires HTTP / parsing
# ---------------------------------------------------------------------------

def _safe_err(e):
    """Message d'erreur sans jamais divulguer une clé."""
    s = str(e)
    for secret in (EIA_KEY, GIE_KEY, ENTSOE_TOKEN):
        if secret:
            s = s.replace(secret, "***")
    return s


MAX_BYTES = 24 * 1024 * 1024  # plafond de lecture (anti-DoS ; XML ENTSO-E DE-LU volumineux)


class _HTTPSRedirectOnly(urllib.request.HTTPRedirectHandler):
    """Refuse toute redirection qui quitterait HTTPS (anti-downgrade)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.URLError("redirection non-HTTPS refusée: %s" % newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HTTPSRedirectOnly())


def http_get(url, headers=None, timeout=HTTP_TIMEOUT):
    if not url.lower().startswith("https://"):
        raise urllib.error.URLError("URL non-HTTPS refusée: %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise urllib.error.URLError("réponse trop volumineuse (> %d octets)" % MAX_BYTES)
        return data


def http_json(url, headers=None):
    return json.loads(http_get(url, headers=headers).decode("utf-8"))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def iso_to_date(s):
    s = (s or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def age_days(iso_date):
    """Ancienneté en jours du point le plus récent d'une série (None si illisible)."""
    d = iso_to_date(iso_date) if iso_date else None
    if not d:
        return None
    return (datetime.now(timezone.utc).date() - d).days


# ---------------------------------------------------------------------------
# Statistiques : z-score directionnel + momentum -> score de stress 0..100
# ---------------------------------------------------------------------------

def zscore(values, window=ZSCORE_WINDOW):
    """z du dernier point vs fenêtre glissante. values: liste ordonnée ancien->récent."""
    vals = [v for v in values if v is not None]
    if len(vals) < 20:
        return None
    w = vals[-window:] if len(vals) > window else vals
    mu = statistics.fmean(w)
    sd = statistics.pstdev(w)
    if sd == 0:
        return 0.0
    return (vals[-1] - mu) / sd


def momentum_z(values, lag=MOMENTUM_LAG, window=ZSCORE_WINDOW):
    """z de la variation sur `lag` points."""
    vals = [v for v in values if v is not None]
    if len(vals) < lag + 20:
        return None
    diffs = [vals[i] - vals[i - lag] for i in range(lag, len(vals))]
    w = diffs[-window:] if len(diffs) > window else diffs
    mu = statistics.fmean(w)
    sd = statistics.pstdev(w)
    if sd == 0:
        return 0.0
    return (diffs[-1] - mu) / sd


def stress_from_z(z, direction=1.0):
    """Mappe un z-score signé vers 0..100 via logistique.
    direction=+1 : valeur haute = stress haut (prix, spread, positionnement net).
    direction=-1 : valeur haute = stress bas (stockage gaz, marge confortable)."""
    if z is None:
        return None
    x = direction * z
    return round(100.0 / (1.0 + math.exp(-0.9 * x)), 1)


def regime(score):
    if score is None:
        return "n/d"
    if score < 30:
        return "détendu"
    if score < 55:
        return "normal"
    if score < 75:
        return "tendu"
    return "crise"


def pct_change(values, lag):
    vals = [v for v in values if v is not None]
    if len(vals) <= lag or vals[-1 - lag] == 0:
        return None
    return round((vals[-1] / vals[-1 - lag] - 1.0) * 100.0, 2)


def abs_change(values, lag):
    """Variation absolue (pour les spreads : un % sur une base proche de zéro explose)."""
    vals = [v for v in values if v is not None]
    if len(vals) <= lag:
        return None
    return round(vals[-1] - vals[-1 - lag], 2)


def drop_outliers(pairs, k=6.0):
    """Retire les points aberrants via médiane ± k*MAD (robuste).
    Vise les vraies erreurs de flux (Henry Hub à 30 $/MMBtu, prints fantômes),
    pas les vrais mouvements : k élevé pour ne pas écrêter une crise réelle."""
    vals = [v for _, v in pairs]
    if len(vals) < 10:
        return pairs
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals])
    if mad == 0:
        return pairs
    return [(d, v) for d, v in pairs if abs(v - med) <= k * mad]


# ---------------------------------------------------------------------------
# Collecteurs (un par source). Chacun renvoie une liste (date, valeur) ancien->récent
# ---------------------------------------------------------------------------

EIA_CACHE_SCHEMA = "l0g-energie/eia-cache/v1"


def _eia_cache_path(series_id):
    """Chemin de cache déterministe, limité aux identifiants EIA attendus."""
    if not series_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for char in series_id):
        raise ValueError("identifiant de série EIA invalide")
    return os.path.join(EIA_CACHE_DIR, series_id + ".json")


def _read_eia_cache(series_id):
    """Lit et valide un cache public EIA; ignore tout fichier incomplet ou altéré."""
    try:
        with open(_eia_cache_path(series_id), "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        if cached.get("schema") != EIA_CACHE_SCHEMA or cached.get("series") != series_id:
            return None
        checked_at = datetime.fromisoformat(
            str(cached.get("checked_at", "")).replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            return None
        pairs = []
        for item in cached.get("pairs") or []:
            if not isinstance(item, list) or len(item) != 2:
                return None
            day = iso_to_date(item[0])
            value = fnum(item[1])
            if not day or value is None or not math.isfinite(value):
                return None
            pairs.append((day, value))
        if not pairs:
            return None
        pairs.sort(key=lambda item: item[0])
        return checked_at.astimezone(timezone.utc), pairs
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_eia_cache(series_id, pairs, checked_at):
    """Écrit atomiquement des données publiques EIA, sans URL ni clé API."""
    os.makedirs(EIA_CACHE_DIR, mode=0o750, exist_ok=True)
    payload = {
        "schema": EIA_CACHE_SCHEMA,
        "series": series_id,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "pairs": [[day.isoformat(), value] for day, value in pairs],
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=EIA_CACHE_DIR,
                prefix=".eia-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, _eia_cache_path(series_id))
    except Exception:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _fetch_eia_cached(series_id, fetch_uncached):
    """Plafonne les appels EIA et conserve la date du dernier contrôle réseau."""
    cached = _read_eia_cache(series_id)
    now = datetime.now(timezone.utc)
    if cached:
        checked_at, pairs = cached
        cache_age = (now - checked_at).total_seconds()
        if EIA_MIN_REFRESH_SECONDS > 0 and 0 <= cache_age < EIA_MIN_REFRESH_SECONDS:
            EIA_FETCH_META[series_id] = {
                "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
                "refresh_mode": "cache",
            }
            return pairs

    pairs = fetch_uncached()
    if not pairs:
        raise RuntimeError("réponse EIA vide pour %s" % series_id)
    checked_at = datetime.now(timezone.utc)
    EIA_FETCH_META[series_id] = {
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "refresh_mode": "network",
    }
    try:
        _write_eia_cache(series_id, pairs, checked_at)
    except OSError as error:
        sys.stderr.write("[warn] cache EIA %s non écrit: %s\n"
                         % (series_id, _safe_err(error)))
    return pairs


def _fetch_eia_series_uncached(series_id, length=900):
    """Appel EIA /seriesid/ sans cache, réservé au wrapper plafonné."""
    if not EIA_KEY:
        raise RuntimeError("EIA_KEY absente")
    qs = urllib.parse.urlencode({
        "api_key": EIA_KEY,
        "length": str(length),
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
    })
    url = "https://api.eia.gov/v2/seriesid/%s?%s" % (urllib.parse.quote(series_id), qs)
    data = http_json(url)
    rows = data.get("response", {}).get("data", [])
    out = []
    for row in rows:
        d = iso_to_date(row.get("period"))
        v = fnum(row.get("value"))
        if d and v is not None:
            out.append((d, v))
    out.sort(key=lambda t: t[0])
    return drop_outliers(out)


def fetch_eia_series(series_id, length=900):
    """EIA v2 /seriesid/, avec au plus un contrôle réseau par fenêtre de cache."""
    return _fetch_eia_cached(
        series_id,
        lambda: _fetch_eia_series_uncached(series_id, length=length),
    )


# Seuil d'ancienneté (jours) au-delà duquel une carte pétrole est marquée stale.
# Il signale une donnée qui cesse vraiment d'avancer (flux EIA cassé, clé morte),
# pas son délai de publication normal. Défaut 10 j = marge au-dessus du lag EIA.
STALE_MAX_AGE_DAYS = int(os.environ.get("ENERGIE_STALE_MAX_AGE", "10"))

# Une observation EIA de moins de quatre jours est nominale (week-end inclus).
# Au-delà, elle reste valide mais son retard est publié explicitement.
EIA_OIL_DELAY_DAYS = int(os.environ.get("ENERGIE_EIA_OIL_DELAY_DAYS", "3"))

# Ancienneté maximale d'un point déjà publié pour qu'un échec de collecte
# ponctuel ne l'exclue pas à tort du composite. Ces seuils suivent la cadence
# normale de chaque source, pas la fréquence du timer (30 min).
FALLBACK_MAX_AGE_DAYS = {
    "brent": STALE_MAX_AGE_DAYS,
    "wti": STALE_MAX_AGE_DAYS,
    "crude_stocks_us": 10,   # EIA hebdomadaire
    "henry_hub": 7,          # EIA quotidienne, avec week-ends et délai de publication
    "gas_storage_eu": 2,     # GIE quotidienne, deux publications le soir
    "elec_fr": 1,            # ENTSO-E day-ahead
    "elec_de": 1,
    "fossil_share_fr": 1,    # ODRE quasi temps réel
    "co2_fr": 1,
    "cftc_wti_net": 12,      # données du mardi publiées le vendredi
    "cftc_natgas_net": 12,
}


EIA_OIL_SERIES = {
    "brent": "RBRTE",
    "wti": "RWTC",
}


def _fetch_eia_oil_spot_uncached(series, length=900):
    """Prix spot quotidien EIA v2 natif, ancien vers récent.

    La route, la fréquence, la série et l'unité sont validées avant qu'un
    point puisse entrer dans le score.
    """
    if not EIA_KEY:
        raise RuntimeError("EIA_KEY absente")
    if series not in EIA_OIL_SERIES.values():
        raise ValueError("série pétrole EIA non autorisée: %s" % series)
    qs = urllib.parse.urlencode([
        ("api_key", EIA_KEY),
        ("frequency", "daily"),
        ("data[0]", "value"),
        ("facets[series][]", series),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "desc"),
        ("length", str(length)),
    ])
    data = http_json("https://api.eia.gov/v2/petroleum/pri/spt/data/?" + qs)
    response = data.get("response") or {}
    if response.get("frequency") != "daily":
        raise RuntimeError("fréquence EIA pétrole inattendue")
    out = []
    for row in response.get("data") or []:
        if row.get("series") != series or row.get("units") != "$/BBL":
            continue
        day = iso_to_date(row.get("period"))
        value = fnum(row.get("value"))
        if day and value is not None and math.isfinite(value):
            out.append((day, value))
    if not out:
        raise RuntimeError("aucun prix spot EIA exploitable pour %s" % series)
    out.sort(key=lambda item: item[0])
    return drop_outliers(out)


def fetch_eia_oil_spot(series, length=900):
    """Prix spot quotidien EIA, avec contrôle réseau plafonné et cache atomique."""
    return _fetch_eia_cached(
        series,
        lambda: _fetch_eia_oil_spot_uncached(series, length=length),
    )


def fetch_brent():
    """Brent Europe spot EIA quotidien (RBRTE)."""
    return fetch_eia_oil_spot(EIA_OIL_SERIES["brent"])


def fetch_wti():
    """WTI Cushing spot EIA quotidien (RWTC)."""
    return fetch_eia_oil_spot(EIA_OIL_SERIES["wti"])


def fetch_gie_eu_storage():
    """GIE AGSI+ agrégat EU : (date, % full). header x-key."""
    if not GIE_KEY:
        raise RuntimeError("GIE_KEY absente")
    url = "https://agsi.gie.eu/api?continent=eu&size=400"
    data = http_json(url, headers={"x-key": GIE_KEY})
    rows = data.get("data", [])
    out = []
    for row in rows:
        d = iso_to_date(row.get("gasDayStart"))
        v = fnum(row.get("full"))   # % de remplissage
        if d and v is not None:
            out.append((d, v))
    out.sort(key=lambda t: t[0])
    return out


def fetch_entsoe_dayahead(eic, days=360):
    """ENTSO-E A44 day-ahead. Renvoie [(date, moyenne journalière €/MWh), ...]."""
    if not ENTSOE_TOKEN:
        raise RuntimeError("ENTSOE_TOKEN absent")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    params = {
        "securityToken": ENTSOE_TOKEN,
        "documentType": "A44",
        "businessType": "A62",          # contournement bug REST 12.1.D (depuis 01/2026)
        "in_Domain": eic,
        "out_Domain": eic,
        "periodStart": start.strftime("%Y%m%d") + "0000",
        "periodEnd": end.strftime("%Y%m%d") + "2300",
    }
    url = "https://web-api.tp.entsoe.eu/api?" + urllib.parse.urlencode(params)
    raw = http_get(url)
    # Parsing namespace-agnostique
    root = ET.fromstring(raw)
    def local(tag):
        return tag.split("}", 1)[-1]
    daily = {}
    for ts in root.iter():
        if local(ts.tag) != "TimeSeries":
            continue
        for period in ts:
            if local(period.tag) != "Period":
                continue
            day = None
            for child in period:
                if local(child.tag) == "timeInterval":
                    for c in child:
                        if local(c.tag) == "start":
                            day = iso_to_date(c.text)
            if not day:
                continue
            for pt in period:
                if local(pt.tag) != "Point":
                    continue
                price = None
                for c in pt:
                    if local(c.tag) == "price.amount":
                        price = fnum(c.text)
                if price is not None:
                    daily.setdefault(day, []).append(price)
    out = [(d, statistics.fmean(vs)) for d, vs in daily.items() if vs]
    out.sort(key=lambda t: t[0])
    return out


def fetch_cftc_net(contract_code, limit=200):
    """CFTC legacy futures-only : net non-commercial (long - short). Socrata, sans clé."""
    where = "cftc_contract_market_code='%s'" % contract_code
    qs = urllib.parse.urlencode({
        "$where": where,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(limit),
    })
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json?" + qs
    rows = http_json(url)
    out = []
    for row in rows:
        d = iso_to_date(row.get("report_date_as_yyyy_mm_dd"))
        lo = fnum(row.get("noncomm_positions_long_all"))
        sh = fnum(row.get("noncomm_positions_short_all"))
        if d and lo is not None and sh is not None:
            out.append((d, lo - sh))
    out.sort(key=lambda t: t[0])
    return out


def fetch_odre_france():
    """ODRE éCO2mix national temps réel : dernier point (mix + CO2). Sans clé.
    Renvoie un dict {fossil_share, co2, conso, gen_total, date}."""
    base = "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/eco2mix-national-tr/records"
    recs = []
    for page in range(5):  # 5 x 100 = ~5 jours au pas 15 min : capte le cycle jour/nuit
        qs = urllib.parse.urlencode({
            "order_by": "date_heure DESC",
            "where": "nucleaire IS NOT NULL",
            "limit": "100",
            "offset": str(page * 100),
        }, quote_via=urllib.parse.quote)
        batch = http_json(base + "?" + qs).get("results", [])
        if not batch:
            break
        recs.extend(batch)
    # historique de la part fossile pour z-score
    hist = []
    latest = None
    for rec in recs:
        gaz = fnum(rec.get("gaz")) or 0.0
        charbon = fnum(rec.get("charbon")) or 0.0
        fioul = fnum(rec.get("fioul")) or 0.0
        nuc = fnum(rec.get("nucleaire")) or 0.0
        hydro = fnum(rec.get("hydraulique")) or 0.0
        eol = fnum(rec.get("eolien")) or 0.0
        sol = fnum(rec.get("solaire")) or 0.0
        bio = fnum(rec.get("bioenergies")) or 0.0
        gen = gaz + charbon + fioul + nuc + hydro + eol + sol + bio
        if gen <= 0:
            continue
        fossil = (gaz + charbon + fioul) / gen * 100.0
        d = rec.get("date_heure")
        hist.append(fossil)
        if latest is None:
            latest = {
                "date": d,
                "fossil_share": round(fossil, 2),
                "co2": fnum(rec.get("taux_co2")),
                "conso": fnum(rec.get("consommation")),
                "gen_total": round(gen, 0),
            }
    hist.reverse()  # ancien->récent
    if latest is not None:
        latest["fossil_hist"] = hist
    return latest


# ---------------------------------------------------------------------------
# Construction d'une série standardisée
# ---------------------------------------------------------------------------

def build_series(pairs, unit, direction, label, change_mode="pct"):
    """pairs: [(date, value)] ancien->récent. Renvoie le dict série + métriques.
    change_mode='abs' pour les spreads (variation en unité, pas en %)."""
    values = [v for _, v in pairs]
    last_date = pairs[-1][0].isoformat() if pairs else None
    last = values[-1] if values else None
    z = zscore(values)
    mz = momentum_z(values)
    # score de stress: 70% niveau, 30% momentum
    comps = [s for s in (stress_from_z(z, direction), stress_from_z(mz, direction)) if s is not None]
    if stress_from_z(z, direction) is not None and stress_from_z(mz, direction) is not None:
        score = round(0.7 * stress_from_z(z, direction) + 0.3 * stress_from_z(mz, direction), 1)
    elif comps:
        score = comps[0]
    else:
        score = None
    # variation : absolue pour les spreads, sinon en %
    chg = abs_change if change_mode == "abs" else pct_change
    # mini-historique (90 derniers points) pour sparkline front
    spark = [round(v, 4) for v in values[-90:]]
    return {
        "label": label,
        "unit": unit,
        "value": round(last, 4) if last is not None else None,
        "date": last_date,
        "chg_1d": chg(values, 1),
        "chg_20d": chg(values, MOMENTUM_LAG),
        "chg_mode": change_mode,
        "z": round(z, 2) if z is not None else None,
        "momentum_z": round(mz, 2) if mz is not None else None,
        "score": score,
        "regime": regime(score),
        "direction": direction,
        "spark": spark,
        "stale": False,
    }


def derived_spread(a_pairs, b_pairs, unit, label):
    """Spread = série A - série B sur dates communes. Variation en absolu."""
    bmap = {d: v for d, v in b_pairs}
    pairs = [(d, v - bmap[d]) for d, v in a_pairs if d in bmap]
    pairs.sort(key=lambda t: t[0])
    return build_series(pairs, unit, direction=1.0, label=label, change_mode="abs")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def load_previous():
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cached_fallback(previous, source_error, max_age_days=None):
    """Qualifie un point conservé après un échec ponctuel de collecte.

    Une valeur encore dans la fenêtre normale de publication reste exploitable
    dans le composite et est signalée ``cached-current``. Elle ne devient
    ``stale`` qu'une fois cette fenêtre dépassée.
    """
    cached = dict(previous)
    cached_age = age_days(cached.get("date"))
    still_current = (
        max_age_days is not None
        and cached_age is not None
        and cached_age <= max_age_days
    )
    cached["stale"] = not still_current
    cached["age_days"] = cached_age
    cached["quality_status"] = "cached-current" if still_current else "stale"
    cached["source_warning"] = source_error
    return cached


def annotate_oil_provenance(series, notes):
    """Expose la source et le retard du dernier point Brent/WTI.

    Le seuil ``stale`` reste réservé à une vraie rupture EIA. Un retard de
    publication normal est visible comme ``official-delayed``. Un point du
    snapshot précédent conservé après erreur reste explicitement
    ``cached-current`` et ne se fait pas passer pour une collecte réussie.
    """
    for oil in ("brent", "wti"):
        s = series.get(oil)
        if not s:
            continue
        source_series = EIA_OIL_SERIES[oil]
        meta = EIA_FETCH_META.get(source_series) or {}
        s["tip_source"] = "eia"
        s["source_series"] = source_series
        if meta:
            s["source_checked_at"] = meta.get("checked_at")
            s["source_refresh_mode"] = meta.get("refresh_mode")
        s["age_days"] = age_days(s.get("date"))

        # Le fallback a déjà reçu sa qualification et son erreur source dans
        # attempt(); on conserve cette vérité au lieu de la masquer.
        if s.get("quality_status") == "cached-current":
            continue

        if s.get("stale") or (
                s["age_days"] is not None
                and s["age_days"] > STALE_MAX_AGE_DAYS):
            s["stale"] = True
            s["quality_status"] = "stale"
            warning = "%s: donnee figee, dernier point %s (%s j, > %s j)" % (
                oil, s.get("date"), s.get("age_days"), STALE_MAX_AGE_DAYS)
            if not s.get("source_warning"):
                s["source_warning"] = warning
            notes.append(warning)
        elif s["age_days"] is not None and s["age_days"] > EIA_OIL_DELAY_DAYS:
            s["quality_status"] = "official-delayed"
            warning = (
                "%s: source EIA quotidienne officielle différée, dernier point %s (%s j)"
                % (oil, s.get("date"), s.get("age_days"))
            )
            s["source_warning"] = warning
            notes.append(warning)
        else:
            s["quality_status"] = "nominal"
            s["source_warning"] = None


def collect():
    prev = load_previous()
    prev_series = prev.get("series", {})
    series = {}
    notes = []

    def attempt(name, fn, **build_kwargs):
        """Tente une collecte; sinon qualifie puis réutilise le point précédent."""
        try:
            pairs = fn()
            if not pairs:
                raise RuntimeError("réponse vide")
            series[name] = build_series(pairs, **build_kwargs)
            return pairs
        except Exception as e:  # noqa: BLE001 — on isole chaque source
            msg = "%s indisponible: %s" % (name, _safe_err(e))
            if name in prev_series:
                fallback = cached_fallback(
                    prev_series[name],
                    msg,
                    FALLBACK_MAX_AGE_DAYS.get(name),
                )
                series[name] = fallback
                if not fallback["stale"]:
                    msg += (
                        "; dernier point %s réutilisé comme encore courant "
                        "(%s j, seuil %s j)"
                        % (
                            fallback.get("date"),
                            fallback.get("age_days"),
                            FALLBACK_MAX_AGE_DAYS[name],
                        )
                    )
                    fallback["source_warning"] = msg
            notes.append(msg)
            sys.stderr.write("[warn] " + msg + "\n")
            return None

    # --- Pétrole ---
    brent = attempt("brent", fetch_brent,
                    unit="$/bbl", direction=1.0, label="Brent · spot EIA")
    wti = attempt("wti", fetch_wti,
                  unit="$/bbl", direction=1.0, label="WTI · spot EIA")
    attempt("crude_stocks_us", lambda: fetch_eia_series("PET.WCESTUS1.W"),
            unit="kb", direction=-1.0, label="Stocks brut US (hors SPR)")
    # Transparence + fraîcheur des cartes pétrole. La source EIA, la série, le
    # dernier contrôle réseau/cache et l'ancienneté du point sont publiés. Le
    # badge stale ne se déclenche que si la donnée cesse réellement d'avancer.
    annotate_oil_provenance(series, notes)

    if brent and wti:
        series["brent_wti_spread"] = derived_spread(
            brent, wti, unit="$/bbl", label="Spread Brent-WTI")

    # --- Gaz ---
    attempt("henry_hub", lambda: fetch_eia_series("NG.RNGWHHD.D"),
            unit="$/MMBtu", direction=1.0, label="Henry Hub · spot EIA")
    attempt("gas_storage_eu", fetch_gie_eu_storage,
            unit="% plein", direction=-1.0, label="Stockage gaz Europe")

    # --- Électricité ---
    attempt("elec_fr", lambda: fetch_entsoe_dayahead(EIC_FR),
            unit="€/MWh", direction=1.0, label="Day-ahead France")
    attempt("elec_de", lambda: fetch_entsoe_dayahead(EIC_DE),
            unit="€/MWh", direction=1.0, label="Day-ahead Allemagne (DE-LU)")

    # France mix + CO2 (ODRE)
    try:
        fr = fetch_odre_france()
        if fr:
            fh = fr.get("fossil_hist", [])
            z = zscore(fh)
            series["fossil_share_fr"] = {
                "label": "Part fossile mix France", "unit": "%",
                "value": fr.get("fossil_share"), "date": fr.get("date"),
                "z": round(z, 2) if z is not None else None,
                # info seulement : la part fossile France est structurellement <1%,
                # un z-score dessus produit un faux signal de stress. N'entre pas
                # dans le composite (l'électricité y revient avec ENTSO-E).
                "score": None, "regime": "info",
                "spark": [round(x, 2) for x in fh[-90:]], "stale": False,
            }
            series["co2_fr"] = {
                "label": "Intensité CO2 France", "unit": "gCO2/kWh",
                "value": fr.get("co2"), "date": fr.get("date"),
                "score": None, "regime": "info", "spark": [], "stale": False,
            }
    except Exception as e:  # noqa: BLE001
        msg = "odre_france indisponible: %s" % _safe_err(e)
        notes.append(msg)
        sys.stderr.write("[warn] " + msg + "\n")
        for k in ("fossil_share_fr", "co2_fr"):
            if k in prev_series:
                fallback = cached_fallback(
                    prev_series[k],
                    msg,
                    FALLBACK_MAX_AGE_DAYS[k],
                )
                series[k] = fallback
                if not fallback["stale"]:
                    fallback["source_warning"] = (
                        "%s; dernier point %s réutilisé comme encore courant "
                        "(%s j, seuil %s j)"
                        % (
                            msg,
                            fallback.get("date"),
                            fallback.get("age_days"),
                            FALLBACK_MAX_AGE_DAYS[k],
                        )
                    )

    # --- Positionnement (CFTC) ---
    attempt("cftc_wti_net", lambda: fetch_cftc_net(CFTC_WTI),
            unit="contrats", direction=1.0, label="Net spéculatif WTI")
    attempt("cftc_natgas_net", lambda: fetch_cftc_net(CFTC_NATGAS),
            unit="contrats", direction=1.0, label="Net spéculatif NatGas")

    return series, notes


def composite(series):
    """Calcule les 5 sous-indices et l'indice composite global."""
    def avg(scores):
        s = [x for x in scores if x is not None]
        return round(statistics.fmean(s), 1) if s else None

    def sc(name):
        """Score d'une série, SAUF si elle est stale : une donnée morte
        (ex. ENTSO-E figé sur l'exemple) ne doit pas alimenter le composite."""
        s = series.get(name, {})
        if not s or s.get("stale"):
            return None
        return s.get("score")

    sub = {}
    sub["petrole"] = avg([
        sc("brent"), sc("wti"), sc("brent_wti_spread"), sc("crude_stocks_us"),
    ])
    sub["gaz"] = avg([
        sc("henry_hub"), sc("gas_storage_eu"),
    ])
    sub["electricite"] = avg([
        sc("elec_fr"), sc("elec_de"), sc("fossil_share_fr"),
    ])
    sub["positionnement"] = avg([
        sc("cftc_wti_net"), sc("cftc_natgas_net"),
    ])
    # contexte (FX) est ajouté côté front (live) ; placeholder neutre ici
    sub["contexte"] = None

    # composite : moyenne pondérée des sous-indices disponibles, renormalisée
    num = 0.0
    den = 0.0
    for k, w in WEIGHTS.items():
        v = sub.get(k)
        if v is not None:
            num += w * v
            den += w
    score = round(num / den, 1) if den > 0 else None

    sub_out = {}
    for k, v in sub.items():
        sub_out[k] = {"score": v, "regime": regime(v), "weight": WEIGHTS[k]}
    return {"score": score, "regime": regime(score)}, sub_out


def main():
    series, notes = collect()
    comp, sub = composite(series)
    snapshot = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "composite": comp,
        "subindices": sub,
        "series": series,
        "notes": notes,
        "methodology": {
            "zscore_window": ZSCORE_WINDOW,
            "momentum_lag": MOMENTUM_LAG,
            "weights": WEIGHTS,
            "regimes": {"détendu": "<30", "normal": "30-55", "tendu": "55-75", "crise": ">75"},
            "blend": "score série = 70% niveau (z) + 30% momentum (z), mappés en 0-100 (logistique)",
        },
        "sources": [
            {"name": "EIA v2", "url": "https://api.eia.gov", "series": "Brent, WTI, Henry Hub, stocks brut US"},
            {"name": "GIE AGSI+", "url": "https://agsi.gie.eu", "series": "stockage gaz Europe"},
            {"name": "ENTSO-E", "url": "https://transparency.entsoe.eu", "series": "prix day-ahead FR/DE"},
            {"name": "CFTC CoT", "url": "https://publicreporting.cftc.gov", "series": "positionnement WTI/NatGas"},
            {"name": "ODRE éCO2mix", "url": "https://opendata.reseaux-energies.fr", "series": "mix + CO2 France"},
            {"name": "Frankfurter (BCE)", "url": "https://frankfurter.dev", "series": "EUR/USD (live front)"},
            {"name": "UK Carbon Intensity", "url": "https://carbonintensity.org.uk", "series": "gCO2/kWh (live front)"},
        ],
    }
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_PATH)  # écriture atomique
    sys.stderr.write("[ok] snapshot écrit: %s (composite=%s, %d séries, %d warnings)\n"
                     % (OUT_PATH, comp.get("score"), len(series), len(notes)))


if __name__ == "__main__":
    main()
