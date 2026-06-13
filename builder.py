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
  - EIA  v2 /seriesid/   : Brent, WTI, Henry Hub, stocks brut US      (clé EIA_KEY)
  - GIE  AGSI+           : stockage gaz Europe (% full)               (clé GIE_KEY, header x-key)
  - ENTSO-E              : prix day-ahead FR + DE-LU (A44)            (token ENTSOE_TOKEN)
  - CFTC  Socrata legacy : positionnement non-commercial WTI/NatGas  (sans clé)
  - ODRE  éCO2mix        : mix France + intensité CO2                 (sans clé)

Robustesse : chaque source est isolée dans un try/except. Si une source tombe,
on conserve la valeur du snapshot précédent (serve-stale) et on marque la série
"stale". Le build ne plante jamais sur une seule source en échec.
"""

import os
import sys
import json
import math
import time
import statistics
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error
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

def fetch_eia_series(series_id, length=900):
    """EIA v2 via /seriesid/ (compat APIv1). Renvoie [(date, value), ...] ancien->récent."""
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


_YAHOO_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
_YAHOO_HEADERS = {
    "User-Agent": _YAHOO_UA,
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Referer": "https://finance.yahoo.com/",
    "Connection": "keep-alive",
}
_YAHOO_CJ = http.cookiejar.CookieJar()
_YAHOO_OPENER = urllib.request.build_opener(
    _HTTPSRedirectOnly(), urllib.request.HTTPCookieProcessor(_YAHOO_CJ))
_yahoo_primed = False


def _yahoo_prime():
    """Récupère un cookie de session Yahoo (réduit fortement les 429)."""
    global _yahoo_primed
    if _yahoo_primed:
        return
    for u in ("https://fc.yahoo.com/", "https://finance.yahoo.com/quote/CL=F"):
        try:
            _YAHOO_OPENER.open(
                urllib.request.Request(u, headers=_YAHOO_HEADERS),
                timeout=HTTP_TIMEOUT).read(4096)
            break
        except Exception:  # noqa: BLE001 — un 404 dépose souvent le cookie quand même
            continue
    _yahoo_primed = True


def _yahoo_get(symbol, rng):
    """GET chart Yahoo avec cookie, bascule query1/query2 et retry sur 429."""
    _yahoo_prime()
    last = None
    for host in ("query1", "query2"):
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=%s"
               % (host, urllib.parse.quote(symbol, safe="=^"), rng))
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=_YAHOO_HEADERS)
                with _YAHOO_OPENER.open(req, timeout=HTTP_TIMEOUT) as r:
                    raw = r.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise urllib.error.URLError("réponse trop volumineuse")
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:
                    time.sleep(1.5 * (attempt + 1))  # backoff puis retry
                    continue
                break  # autre code : on tente l'autre host
            except Exception as e:  # noqa: BLE001
                last = e
                break
    raise last or RuntimeError("Yahoo indisponible")


def fetch_yahoo(symbol, rng="2y"):
    """Yahoo Finance chart : [(date, close)] ancien->récent, dernier point = prix
    temps réel (meta.regularMarketPrice). Robuste : cookie + query1/2 + retry 429."""
    data = _yahoo_get(symbol, rng)
    res = data.get("chart", {}).get("result")
    if not res:
        raise RuntimeError("réponse Yahoo vide")
    r = res[0]
    ts = r.get("timestamp") or []
    quote = (r.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(t, tz=timezone.utc).date(), float(c)))
    # dernier point : prix temps réel (remplace la barre du jour si présente)
    meta = r.get("meta", {})
    rmp, rmt = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if rmp is not None and rmt:
        d = datetime.fromtimestamp(rmt, tz=timezone.utc).date()
        out = [(dd, vv) for dd, vv in out if dd != d]
        out.append((d, float(rmp)))
    out.sort(key=lambda t: t[0])
    if not out:
        raise RuntimeError("aucune cotation Yahoo exploitable")
    return drop_outliers(out)


# Yahoo désactivé par défaut : l'API rate-limite (429) les IP de serveur.
# Mettre ENERGIE_YAHOO_OIL=1 dans l'env pour tenter Yahoo (frais) si l'IP n'est
# pas bannie ; sinon on reste sur le spot EIA officiel (fiable, laggé de quelques jours).
USE_YAHOO_OIL = os.environ.get("ENERGIE_YAHOO_OIL", "").strip().lower() in ("1", "true", "yes")

OILPRICE_KEY = os.environ.get("OILPRICE_KEY", "").strip()


def fetch_oilprice_latest(code):
    """oilpriceapi.com : (date, prix) du dernier spot. None si indisponible.
    Sert à rafraîchir la tête de série EIA avec un prix temps réel."""
    if not OILPRICE_KEY:
        return None
    try:
        url = "https://api.oilpriceapi.com/v1/prices/latest?by_code=" + urllib.parse.quote(code)
        raw = http_get(url, headers={"Authorization": "Token " + OILPRICE_KEY})
        d = json.loads(raw.decode("utf-8"))
        if d.get("status") != "success":
            return None
        node = d.get("data", {})
        price = node.get("price")
        ts = node.get("created_at") or node.get("updated_at")
        if price is None or not ts:
            return None
        day = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).date()
        return (day, float(price))
    except Exception as e:  # noqa: BLE001
        sys.stderr.write("[info] oilpriceapi %s KO (%s)\n" % (code, _safe_err(e)))
        return None


def _freshen_tip(series, code):
    """Remplace/ajoute le dernier point d'une série EIA par le spot frais oilpriceapi."""
    tip = fetch_oilprice_latest(code)
    if not tip:
        return series
    day, price = tip
    series = [(d, v) for (d, v) in series if d < day]
    series.append((day, price))
    series.sort(key=lambda t: t[0])
    return series


def fetch_brent():
    """Brent : historique EIA + tête de série fraîche oilpriceapi (repli : EIA seul)."""
    if USE_YAHOO_OIL:
        try:
            return fetch_yahoo("BZ=F")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("[info] Yahoo Brent KO (%s), repli EIA\n" % _safe_err(e))
    return _freshen_tip(fetch_eia_series("PET.RBRTE.D"), "BRENT_CRUDE_USD")


def fetch_wti():
    """WTI : historique EIA + tête de série fraîche oilpriceapi (repli : EIA seul)."""
    if USE_YAHOO_OIL:
        try:
            return fetch_yahoo("CL=F")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("[info] Yahoo WTI KO (%s), repli EIA\n" % _safe_err(e))
    return _freshen_tip(fetch_eia_series("PET.RWTC.D"), "WTI_USD")


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


def collect():
    prev = load_previous()
    prev_series = prev.get("series", {})
    series = {}
    notes = []

    def attempt(name, fn, **build_kwargs):
        """Tente une collecte; sinon réutilise la valeur précédente (serve-stale)."""
        try:
            pairs = fn()
            if not pairs:
                raise RuntimeError("réponse vide")
            series[name] = build_series(pairs, **build_kwargs)
            return pairs
        except Exception as e:  # noqa: BLE001 — on isole chaque source
            msg = "%s indisponible: %s" % (name, _safe_err(e))
            notes.append(msg)
            sys.stderr.write("[warn] " + msg + "\n")
            if name in prev_series:
                stale = dict(prev_series[name])
                stale["stale"] = True
                series[name] = stale
            return None

    # --- Pétrole ---
    brent = attempt("brent", fetch_brent,
                    unit="$/bbl", direction=1.0, label="Brent · spot")
    wti = attempt("wti", fetch_wti,
                  unit="$/bbl", direction=1.0, label="WTI · spot")
    attempt("crude_stocks_us", lambda: fetch_eia_series("PET.WCESTUS1.W"),
            unit="kb", direction=-1.0, label="Stocks brut US (hors SPR)")
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
                s = dict(prev_series[k]); s["stale"] = True; series[k] = s

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
