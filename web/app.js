/* ============================================================
   app.js — Moniteur de stress énergétique (l0g.fr)
   Lit snapshot.json (pré-calculé par builder.py sur zen) et complète
   avec deux flux live sans clé (EUR/USD via Frankfurter, intensité
   carbone UK). Zéro dépendance, zéro inline (CSP stricte).
   ============================================================ */
(function () {
  "use strict";

  var REGIME_COLOR = {
    "détendu": "var(--cool)", "normal": "var(--warm)",
    "tendu": "var(--hot)", "crise": "var(--crit)",
    "info": "var(--info)", "n/d": "var(--faint)"
  };

  // -- helpers ---------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function el(tag, cls, txt) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  }
  function fmt(v, dp) {
    if (v == null || isNaN(v)) return "—";
    var n = Number(v);
    if (Math.abs(n) >= 100000) return (n / 1000).toFixed(0) + "k";
    if (Math.abs(n) >= 1000) return n.toLocaleString("fr-FR", { maximumFractionDigits: 0 });
    return n.toFixed(dp == null ? (Math.abs(n) >= 10 ? 1 : 2) : dp);
  }
  function regimeColor(r) { return REGIME_COLOR[r] || "var(--faint)"; }

  // sparkline SVG path depuis un tableau de valeurs
  function sparkPath(vals) {
    var v = (vals || []).filter(function (x) { return x != null && !isNaN(x); });
    if (v.length < 2) return "";
    var min = Math.min.apply(null, v), max = Math.max.apply(null, v);
    var span = (max - min) || 1, n = v.length;
    var pts = v.map(function (y, i) {
      var x = (i / (n - 1)) * 100;
      var yy = 28 - ((y - min) / span) * 26;
      return x.toFixed(2) + "," + yy.toFixed(2);
    });
    return "M" + pts.join(" L");
  }

  // -- rendu d'une carte série ----------------------------------------------
  function renderCard(s, opts) {
    opts = opts || {};
    var tpl = document.getElementById("tpl-card");
    var node = tpl.content.firstElementChild.cloneNode(true);
    if (s.stale) node.classList.add("stale");
    if (opts.live) node.classList.add("live");

    node.querySelector(".lbl").textContent = s.label;
    var badge = node.querySelector(".badge");
    badge.textContent = s.regime || "info";
    badge.style.color = regimeColor(s.regime);

    node.querySelector(".value").textContent = fmt(s.value, opts.dp);
    node.querySelector(".unit").textContent = s.unit ? " " + s.unit : "";

    // sparkline colorée par régime
    var svg = node.querySelector(".spark");
    var d = sparkPath(s.spark);
    if (d) {
      var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("d", d);
      p.setAttribute("fill", "none");
      p.setAttribute("stroke", regimeColor(s.regime));
      p.setAttribute("stroke-width", "1.4");
      p.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(p);
    } else { svg.style.display = "none"; }

    // delta coloré selon qu'il accroît ou réduit le stress
    var chg = node.querySelector(".chg");
    var c = s.chg_1d;
    if (c == null) c = s.chg_20d;
    if (c != null && !isNaN(c)) {
      var dir = s.direction || 1;
      var stressUp = (c * dir) > 0;
      chg.classList.add(stressUp ? "up" : "down");
      var suffix = (s.chg_mode === "abs") ? (" " + (s.unit || "")) : "%";
      chg.textContent = (c > 0 ? "▲ +" : "▼ ") + Math.abs(c).toFixed(2) + suffix;
    } else { chg.textContent = ""; }

    var z = node.querySelector(".z");
    z.textContent = (s.z != null) ? ("z " + (s.z > 0 ? "+" : "") + s.z) : (s.score != null ? "idx " + s.score : "");

    // date de la donnée (rend tout lag visible et assumé)
    if (s.date) {
      var dnode = el("span", "dt");
      var dd = String(s.date).slice(0, 10);
      dnode.textContent = dd.split("-").reverse().join("/");
      node.querySelector(".meta").appendChild(dnode);
    }

    if (s.stale) {
      var tag = el("span", "stale-tag", "stale");
      node.appendChild(tag);
    }
    return node;
  }

  function section(title, ids, snap, opts) {
    var frag = document.createDocumentFragment();
    var h = el("div", "section-h");
    h.appendChild(el("h2", null, title));
    h.appendChild(el("div", "hr"));
    frag.appendChild(h);
    var grid = el("div", "grid");
    ids.forEach(function (id) {
      var s = snap.series[id];
      if (s) grid.appendChild(renderCard(s, opts));
    });
    frag.appendChild(grid);
    return frag;
  }

  // -- hero : jauge composite + rails sous-indices --------------------------
  function renderHero(snap) {
    var comp = snap.composite || {};
    var sub = snap.subindices || {};
    var score = comp.score;

    var hero = el("div", "hero");

    // carte jauge
    var g = el("div", "gaugecard");
    g.appendChild(el("div", "eyebrow", "Indice composite de stress"));
    var num = el("div", "composite-num");
    var sc = el("span", "score", score == null ? "—" : score);
    var rg = el("span", "regime", (comp.regime || "n/d"));
    rg.style.color = regimeColor(comp.regime);
    num.appendChild(sc); num.appendChild(rg);
    g.appendChild(num);

    g.appendChild(el("div", "driver", buildDriver(sub)));

    var th = el("div", "thermal");
    var ticks = el("div", "ticks");
    for (var i = 0; i < 11; i++) ticks.appendChild(el("i"));
    th.appendChild(ticks);
    var needle = el("div", "needle");
    needle.style.left = (score == null ? 50 : Math.max(1, Math.min(99, score))) + "%";
    th.appendChild(needle);
    g.appendChild(th);
    var labels = el("div", "thermal-labels");
    ["détendu", "normal", "tendu", "crise"].forEach(function (t) { labels.appendChild(el("span", null, t)); });
    g.appendChild(labels);
    hero.appendChild(g);

    // carte rails
    var d = el("div", "drivers");
    d.appendChild(el("h3", null, "Sous-indices"));
    var order = ["petrole", "gaz", "electricite", "positionnement", "contexte"];
    var names = { petrole: "Pétrole", gaz: "Gaz", electricite: "Électricité", positionnement: "Positionnement", contexte: "Contexte FX" };
    order.forEach(function (k) {
      var v = sub[k] || {};
      var rail = el("div", "rail");
      rail.appendChild(el("div", "name", names[k]));
      var track = el("div", "track");
      var fill = el("div", "fill");
      var sv = v.score;
      fill.style.width = (sv == null ? 0 : Math.max(2, Math.min(100, sv))) + "%";
      fill.style.background = regimeColor(v.regime);
      track.appendChild(fill);
      rail.appendChild(track);
      rail.appendChild(el("div", "val", sv == null ? "—" : sv));
      d.appendChild(rail);
    });
    hero.appendChild(d);
    return hero;
  }

  function buildDriver(sub) {
    var entries = Object.keys(sub).map(function (k) {
      return { k: k, s: sub[k] && sub[k].score, r: sub[k] && sub[k].regime };
    }).filter(function (e) { return e.s != null; });
    if (!entries.length) return "En attente de données.";
    entries.sort(function (a, b) { return b.s - a.s; });
    var names = { petrole: "le pétrole", gaz: "le gaz", electricite: "l'électricité", positionnement: "le positionnement", contexte: "le change" };
    var top = entries[0], low = entries[entries.length - 1];
    var s = "Tension menée par " + names[top.k] + " (" + top.r + ", " + top.s + ")";
    if (low.k !== top.k) s += " ; " + names[low.k] + " " + low.r + " (" + low.s + ").";
    return s;
  }

  // -- flux live (sans clé) --------------------------------------------------
  function fetchLive(snap) {
    // EUR/USD via Frankfurter (BCE)
    fetch("https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var eur = j && j.rates && j.rates.EUR;
        if (!eur) return;
        var usdEur = 1 / eur; // EUR/USD
        snap.series.eur_usd = {
          label: "EUR / USD", unit: "", value: usdEur, regime: "info",
          spark: [], direction: 1, _live: true
        };
        addLiveCard(snap.series.eur_usd);
      })
      .catch(function () {});

    // intensité carbone UK (gCO2/kWh)
    fetch("https://api.carbonintensity.org.uk/intensity")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var d = j && j.data && j.data[0] && j.data[0].intensity;
        if (!d) return;
        snap.series.carbon_uk = {
          label: "Intensité carbone UK", unit: "gCO2/kWh",
          value: d.actual != null ? d.actual : d.forecast,
          regime: "info", spark: [], _live: true
        };
        addLiveCard(snap.series.carbon_uk);
      })
      .catch(function () {});
  }

  function addLiveCard(s) {
    var grid = document.getElementById("livegrid");
    if (!grid) return;
    grid.appendChild(renderCard(s, { live: true, dp: s.label.indexOf("EUR") === 0 ? 4 : 0 }));
  }

  // -- assemblage ------------------------------------------------------------
  function render(snap) {
    var root = document.getElementById("root");
    root.innerHTML = "";
    root.appendChild(renderHero(snap));

    root.appendChild(section("Pétrole", ["brent", "wti", "brent_wti_spread", "crude_stocks_us"], snap));
    root.appendChild(section("Gaz", ["henry_hub", "gas_storage_eu"], snap));
    root.appendChild(section("Électricité", ["elec_fr", "elec_de", "fossil_share_fr", "co2_fr"], snap));
    root.appendChild(section("Positionnement (CFTC)", ["cftc_wti_net", "cftc_natgas_net"], snap));

    // section live
    var h = el("div", "section-h");
    h.appendChild(el("h2", null, "Live · sans clé"));
    h.appendChild(el("div", "hr"));
    root.appendChild(h);
    var lg = el("div", "grid"); lg.id = "livegrid";
    root.appendChild(lg);

    root.appendChild(renderFooter(snap));

    // horodatage
    var t = document.getElementById("snaptime");
    if (snap.generated) {
      var dt = new Date(snap.generated);
      t.textContent = "snapshot " + dt.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) + (snap.sample ? " · exemple" : "");
    }

    fetchLive(snap);
  }

  function renderFooter(snap) {
    var f = el("div", "foot");
    var left = el("div");
    left.appendChild(el("h4", null, "Méthodologie"));
    var m = snap.methodology || {};
    var p1 = el("p");
    p1.textContent = "Chaque série est normalisée en z-score sur une fenêtre glissante d'environ "
      + (m.zscore_window || 252) + " points, combinée à un momentum (variation sur "
      + (m.momentum_lag || 20) + " points). " + (m.blend || "")
      + " Les séries de stockage et de stocks sont inversées (un niveau bas accroît le stress).";
    left.appendChild(p1);
    var p2 = el("p");
    p2.appendChild(document.createTextNode("Régimes : "));
    [["détendu", "var(--cool)", " <30 · "], ["normal", "var(--warm)", " 30–55 · "],
     ["tendu", "var(--hot)", " 55–75 · "], ["crise", "var(--crit)", " >75."]]
      .forEach(function (r) {
        var b = el("b", null, r[0]); b.style.color = r[1];
        p2.appendChild(b); p2.appendChild(document.createTextNode(r[2]));
      });
    p2.appendChild(document.createTextNode(
      " Composite = moyenne pondérée des sous-indices (pétrole 30, gaz 25, élec 20, positionnement 15, contexte 10)."));
    left.appendChild(p2);
    if (snap.notes && snap.notes.length) {
      var n = el("p", "note");
      n.textContent = "⚠ " + snap.notes.join(" · ");
      left.appendChild(n);
    }
    f.appendChild(left);

    var right = el("div");
    right.appendChild(el("h4", null, "Sources primaires"));
    var ul = el("ul");
    (snap.sources || []).forEach(function (s) {
      var li = el("li");
      var a = el("a", null, s.name);
      a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      li.appendChild(a);
      li.appendChild(el("span", null, s.series));
      ul.appendChild(li);
    });
    right.appendChild(ul);
    f.appendChild(right);
    return f;
  }

  // -- bootstrap -------------------------------------------------------------
  fetch("snapshot.json", { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(render)
    .catch(function (e) {
      var root = document.getElementById("root");
      root.innerHTML = "";
      var d = el("div", "err");
      d.textContent = "Snapshot indisponible (" + e.message + "). Le builder n'a pas encore produit snapshot.json, ou le fichier n'est pas servi à la racine.";
      root.appendChild(d);
      document.getElementById("snaptime").textContent = "hors-ligne";
      document.getElementById("livedot").classList.remove("live");
    });
})();
