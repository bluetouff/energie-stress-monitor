#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_sample.py — produit un snapshot.json d'exemple en utilisant le VRAI code
de scoring de builder.py (build_series, derived_spread, composite). Les séries
sont synthétiques mais calées sur le régime de juin 2026 (prime géopolitique
Hormuz, Brent ~95, stockage gaz EU ~83%). Sert uniquement à l'aperçu du front ;
sur zen, builder.py régénère le vrai snapshot."""

import json, math, random
from datetime import date, timedelta, datetime, timezone
import builder

random.seed(42)
TODAY = date(2026, 6, 9)


def walk(n, start, drift, vol, last=None, floor=None):
    """Marche aléatoire douce ancien->récent, dernier point forcé à `last`."""
    out, v = [], start
    for i in range(n):
        v = v + drift + random.gauss(0, vol)
        if floor is not None:
            v = max(floor, v)
        out.append(v)
    if last is not None:
        out[-1] = last
    return out


def shock(n, base, base_vol, spike_to, spike_len=25, floor=None):
    """Base plate sur (n-spike_len) points, puis rampe brutale vers spike_to.
    Reproduit un choc géopolitique récent -> z-score et momentum élevés."""
    out, v = [], base
    flat = n - spike_len
    for i in range(flat):
        v = base + random.gauss(0, base_vol)
        if floor is not None:
            v = max(floor, v)
        out.append(v)
    start = out[-1]
    for j in range(spike_len):
        frac = (j + 1) / spike_len
        v = start + (spike_to - start) * (frac ** 1.6) + random.gauss(0, base_vol * 0.6)
        if floor is not None:
            v = max(floor, v)
        out.append(v)
    out[-1] = spike_to
    return out


def pairs(values, step_days=1):
    n = len(values)
    return [(TODAY - timedelta(days=(n - 1 - i) * step_days), values[i]) for i in range(n)]


# --- Pétrole : remontée violente sur la prime Hormuz (régime crise) ---
brent = pairs(shock(420, base=64, base_vol=1.4, spike_to=95.03, spike_len=28, floor=40))
wti = pairs(shock(420, base=60, base_vol=1.4, spike_to=93.04, spike_len=28, floor=38))
stocks = pairs(shock(160, base=440000, base_vol=2500, spike_to=408000, spike_len=6), step_days=7)

# --- Gaz ---
hh = pairs(walk(420, 3.4, -0.0008, 0.12, last=3.08, floor=1.5))           # HH calme
storage = pairs(walk(400, 60, 0.06, 0.7, last=83.1, floor=10))           # EU 83% (confortable -> stress bas)

# --- Électricité : tension importée via gaz/géopo ---
elec_fr = pairs(shock(380, base=58, base_vol=8, spike_to=88.5, spike_len=22, floor=-20))
elec_de = pairs(shock(380, base=70, base_vol=11, spike_to=104.2, spike_len=22, floor=-50))

# --- Positionnement CFTC : net spéculatif qui se recharge long (stress) ---
wti_net = pairs(shock(160, base=110000, base_vol=8000, spike_to=205000, spike_len=6), step_days=7)
ng_net = pairs(walk(160, -40000, -120, 7000, last=-78000), step_days=7)

series = {}
series["brent"] = builder.build_series(brent, "$/bbl", 1.0, "Brent")
series["wti"] = builder.build_series(wti, "$/bbl", 1.0, "WTI")
series["crude_stocks_us"] = builder.build_series(stocks, "kb", -1.0, "Stocks brut US (hors SPR)")
series["brent_wti_spread"] = builder.derived_spread(brent, wti, "$/bbl", "Spread Brent-WTI")
series["henry_hub"] = builder.build_series(hh, "$/MMBtu", 1.0, "Henry Hub (spot)")
series["gas_storage_eu"] = builder.build_series(storage, "% plein", -1.0, "Stockage gaz Europe")
series["elec_fr"] = builder.build_series(elec_fr, "€/MWh", 1.0, "Day-ahead France")
series["elec_de"] = builder.build_series(elec_de, "€/MWh", 1.0, "Day-ahead Allemagne (DE-LU)")
series["cftc_wti_net"] = builder.build_series(wti_net, "contrats", 1.0, "Net spéculatif WTI")
series["cftc_natgas_net"] = builder.build_series(ng_net, "contrats", 1.0, "Net spéculatif NatGas")

# France mix (nucléaire dominant -> part fossile basse, CO2 bas)
fossil_hist = walk(90, 9, -0.002, 1.1, last=7.8, floor=2)
fz = builder.zscore(fossil_hist)
series["fossil_share_fr"] = {
    "label": "Part fossile mix France", "unit": "%", "value": round(7.8, 2),
    "date": "2026-06-09T11:30:00+02:00", "z": round(fz, 2) if fz is not None else None,
    "score": None, "regime": "info",
    "spark": [round(x, 2) for x in fossil_hist[-90:]], "stale": False,
}
series["co2_fr"] = {
    "label": "Intensité CO2 France", "unit": "gCO2/kWh", "value": 34.0,
    "date": "2026-06-09T11:30:00+02:00", "score": None, "regime": "info",
    "spark": [], "stale": False,
}

comp, sub = builder.composite(series)
snapshot = {
    "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "sample": True,
    "composite": comp,
    "subindices": sub,
    "series": series,
    "notes": ["snapshot d'exemple (données synthétiques) — remplacé par builder.py sur zen"],
    "methodology": {
        "zscore_window": builder.ZSCORE_WINDOW, "momentum_lag": builder.MOMENTUM_LAG,
        "weights": builder.WEIGHTS,
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

with open("web/snapshot.json", "w", encoding="utf-8") as f:
    json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
print("composite =", comp)
for k, v in sub.items():
    print("  %-16s %s (%s)" % (k, v["score"], v["regime"]))
