# Moniteur de stress énergétique

Un tableau de bord qui mesure, en un seul chiffre de 0 à 100, le niveau de tension
des marchés de l'énergie (pétrole, gaz, électricité), à partir de sources de données
officielles. En ligne : [energie.l0g.fr](https://energie.l0g.fr).

---

## C'est quoi, en clair ?

Les prix de l'énergie bougent tout le temps, sur plein de marchés différents : le
baril de pétrole, le gaz, l'électricité, sans compter les paris des financiers. Difficile
de savoir, d'un coup d'œil, si la situation est calme ou si ça chauffe.

Cette appli répond à une question simple : **est-ce que le marché de l'énergie est tendu
en ce moment, oui ou non ?**

Elle agrège une douzaine d'indicateurs officiels en un **indice unique de 0 à 100** et
un code couleur en quatre niveaux :

- **Détendu** (cyan) — tout va bien, prix bas et stables.
- **Normal** (ambre) — rien d'anormal.
- **Tendu** (orange) — ça commence à chauffer.
- **Crise** (rose) — forte tension, à surveiller de près.

Sous ce chiffre global, cinq « jauges » détaillent d'où vient la tension :

1. **Pétrole** — prix du Brent et du WTI, écart entre les deux, stocks américains.
2. **Gaz** — prix de référence US (Henry Hub), niveau de remplissage des stockages européens.
3. **Électricité** — prix de gros France et Allemagne, part des énergies fossiles dans le mix français.
4. **Positionnement** — ce que parient les gros fonds spéculatifs (sont-ils massivement
   acheteurs ou vendeurs ?).
5. **Contexte** — le taux de change euro/dollar, qui pèse sur les prix.

Chaque indicateur est comparé à son propre passé : on ne regarde pas juste « le prix est
haut », mais « le prix est-il anormalement haut **par rapport à d'habitude**, et dans
quelle direction il bouge ». C'est ça qui transforme une douzaine de chiffres bruts en
une lecture de tension compréhensible.

> Important : l'indice mesure la **dynamique de stress de marché** (écart à la normale +
> momentum), pas le niveau de risque géopolitique brut. En période de crise qui se calme,
> les prix peuvent rester élevés mais l'indice redescendre, parce que la tension reflue.

L'outil est une aide à la surveillance, pas un conseil en investissement.

---

## Architecture

Conception « snapshot » : tout le travail se fait côté serveur, le navigateur ne lit qu'un
fichier statique. Aucune clé d'API n'atteint jamais le navigateur, et les visiteurs ne
contactent aucun service tiers (leurs adresses IP ne fuitent pas vers les fournisseurs de
données).

```
            Serveur (Debian + Apache)
  +-------------------------------------------+
  |  timer systemd (toutes les 30 min)        |
  |     -> builder.py  (venv, stdlib only)    |
  |          EIA, GIE, ENTSO-E (avec clés)    |
  |          CFTC, ODRE (sans clé)            |
  |          calcule z-scores + composite     |
  |     -> /var/www/html/energie/snapshot.json|
  +----------------------+--------------------+
                         | Apache (statique, CSP stricte)
                         v
        navigateur : index.html + app.js
            - lit snapshot.json (même origine)
            - 2 flux live sans clé : Frankfurter (EUR/USD), Carbon Intensity UK
```

Le builder n'a **aucune dépendance externe** (bibliothèque standard Python uniquement),
ce qui élimine tout risque de chaîne d'approvisionnement. Il dégrade gracieusement : si
une source tombe, la dernière valeur connue est conservée. Elle reste dans le calcul avec
le statut `cached-current` tant que sa date respecte la cadence normale de publication de
la source ; elle passe ensuite `stale` et sort du score. Le build ne plante jamais sur une
source en échec.

---

## Sources et indicateurs

Toutes primaires, toutes gratuites.

| Indicateur | Source | Clé | Voie |
|---|---|---|---|
| Brent, WTI | historique EIA `PET.RBRTE.D`/`PET.RWTC.D` + tête de série spot temps réel (chaîne oilpriceapi → Twelve Data → Yahoo → EIA) | oui (EIA ; sources spot facultatives) | builder |
| Stocks bruts US (hors SPR) | EIA `PET.WCESTUS1.W` | oui | builder |
| Henry Hub (spot) | EIA `NG.RNGWHHD.D` | oui | builder |
| Stockage gaz Europe (% plein) | GIE AGSI+ `?continent=eu` (champ `full`) | oui (`x-key`) | builder |
| Prix day-ahead FR / DE-LU | ENTSO-E A44 (+ `BusinessType=A62`) | token | builder |
| Net spéculatif WTI / NatGas | CFTC legacy `6dca-aqww` (067651 / 023651) | non | builder |
| Part fossile + CO2 France | ODRE `eco2mix-national-tr` | non | builder |
| EUR/USD | Frankfurter (BCE) | non | navigateur (live) |
| Intensité carbone UK | National Grid Carbon Intensity | non | navigateur (live) |

---

## Méthodologie

- **z-score** de chaque série sur une fenêtre glissante (~252 points) : combien d'écarts-types
  la valeur actuelle est au-dessus/au-dessous de sa moyenne récente.
- **momentum** : même logique sur la variation à 20 points (la tendance récente).
- **score de série** = 70 % niveau (z) + 30 % momentum, ramené sur une échelle 0-100 (logistique).
- **Séries inversées** (`direction = -1`) : stockage gaz et stocks de brut. Un niveau bas
  accroît le stress (moins de coussin de sécurité).
- **Sous-indices** = moyenne des scores de leurs composants, **hors séries `stale`**.
- **Composite** = moyenne pondérée des sous-indices disponibles, renormalisée :
  pétrole 30 %, gaz 25 %, électricité 20 %, positionnement 15 %, contexte 10 %.
- **Régimes** : détendu < 30, normal 30-55, tendu 55-75, crise > 75.

Robustesse statistique : un **filtre d'outliers** (médiane ± k·MAD, k = 6) retire les prints
de données aberrants (ex. un Henry Hub fantôme à 30 $/MMBtu) sans écrêter les vrais
mouvements de marché. La part fossile France utilise une **baseline de ~5 jours** (pagination
ODRE) pour capter le cycle jour/nuit au lieu de subir le bruit intraday.

---

## Sécurité

Revue des points sensibles et des mesures en place.

**Secrets.** Les clés d'API vivent dans `/etc/energie/env` (`chmod 640`, `root:energie`),
lisibles par le seul utilisateur de service, jamais par Apache (`www-data`) ni par le
navigateur. Le snapshot publié ne contient que des valeurs dérivées, aucun secret. Le dépôt
ne contient aucune clé (`.gitignore` couvre `env`, `*.key`, `*.pem`) ; seul `env.example`
vide est versionné.

**Surface d'exécution.** Le builder est en bibliothèque standard pure (aucune dépendance,
donc aucun risque de chaîne d'approvisionnement). Il tourne sous un utilisateur système
dédié sans shell ni home, dans un bac à sable systemd durci : `NoNewPrivileges`,
`ProtectSystem=strict`, `SystemCallFilter=@system-service`, `MemoryDenyWriteExecute`,
`CapabilityBoundingSet=` (vide), `RestrictAddressFamilies` limité, et un seul chemin
accessible en écriture (`ReadWritePaths=/var/www/html/energie`).

**Réseau.** `http_get` refuse toute URL non-HTTPS et toute redirection qui quitterait HTTPS
(anti-downgrade), et borne la lecture à 8 Mo par requête (anti-DoS mémoire).

**Navigateur.** CSP stricte servie par Apache : `default-src 'none'`, `script-src 'self'`,
`style-src 'self'` (aucun inline, ni JS ni CSS), `connect-src` limité aux deux seuls flux
live légitimes. Plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, et HSTS sur le vhost TLS. Zéro CDN, zéro police externe,
zéro traceur.

**Intégrité.** Écriture atomique du snapshot (`os.replace`) : le fichier servi n'est jamais
partiellement écrit. Les messages d'erreur masquent les clés (`_safe_err`).

---

## Déploiement pas à pas (Debian + Apache)

### 0. Clés d'API

- **EIA** (immédiat) : https://www.eia.gov/opendata/register.php
- **GIE AGSI+** (immédiat) : https://agsi.gie.eu/account
- **ENTSO-E** (délai ~1-2 j) : compte sur transparency.entsoe.eu, puis mail à
  `transparency@entsoe.eu`, objet « Restful API access », en précisant l'e-mail du compte.
  Optionnel : l'appli tourne sans, en marquant les prix élec indisponibles.

### 1. Utilisateur dédié et arborescence

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin energie
sudo mkdir -p /opt/energie /var/www/html/energie /etc/energie

# code : root propriétaire, lisible par le groupe energie
sudo cp builder.py /opt/energie/
sudo chown -R root:energie /opt/energie
sudo chmod 750 /opt/energie && sudo chmod 640 /opt/energie/builder.py

# venv (aucune dépendance, mais isole l'interpréteur)
sudo python3 -m venv /opt/energie/venv
sudo chown -R root:energie /opt/energie/venv

# web : energie écrit, www-data lit ; setgid pour l'héritage de groupe
sudo cp web/index.html web/app.css web/app.js web/snapshot.json /var/www/html/energie/
sudo chown -R energie:www-data /var/www/html/energie
sudo chmod 2750 /var/www/html/energie
sudo find /var/www/html/energie -type f -exec chmod 640 {} \;
```

### 2. Clés

```bash
sudo cp env.example /etc/energie/env
sudo nano /etc/energie/env            # renseigner EIA_KEY et GIE_KEY (ENTSOE_TOKEN si dispo)
sudo chown root:energie /etc/energie/env /etc/energie
sudo chmod 640 /etc/energie/env && sudo chmod 750 /etc/energie
```

Vérifier que le service peut lire et pas Apache :

```bash
sudo runuser -u energie -- cat /etc/energie/env >/dev/null && echo "energie lit OK"
sudo runuser -u www-data -- cat /etc/energie/env 2>/dev/null && echo "PROBLEME" || echo "www-data bloqué OK"
```

### 3. Premier build en test (avant tout service)

```bash
sudo runuser -u energie -- bash -c 'set -a; . /etc/energie/env; set +a; exec /opt/energie/venv/bin/python3 /opt/energie/builder.py'
```

Attendu : `[ok] snapshot écrit ... composite=NN`. Les `[warn]` listent les sources
momentanément indisponibles. Un point déjà publié reste `cached-current` dans sa fenêtre
normale de fraîcheur, puis passe automatiquement `stale` ; `elec_fr`/`elec_de` sans token
ENTSO-E sont donc absents au premier build, ce qui est normal.

### 4. systemd

```bash
sudo cp deploy/energie-snapshot.service deploy/energie-snapshot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start energie-snapshot.service      # build via systemd (durcissement actif)
sudo journalctl -u energie-snapshot.service -n 10 --no-pager
sudo systemctl enable --now energie-snapshot.timer
systemctl list-timers energie-snapshot.timer
```

### 5. Apache + TLS

Important : déployer d'abord le vhost **HTTP seul**, puis laisser certbot générer le vhost
TLS. Ne jamais activer un `<VirtualHost *:443>` avec `SSLEngine on` sans certificat, Apache
refuserait de démarrer (et ferait tomber les autres sites).

```bash
sudo a2enmod headers ssl rewrite
sudo cp deploy/energie.l0g.fr.conf /etc/apache2/sites-available/
sudo a2ensite energie.l0g.fr
sudo apache2ctl configtest && sudo systemctl reload apache2

# vérifier que la page sort en HTTP avant le TLS
curl -sI http://energie.l0g.fr/ | head -1
curl -s  http://energie.l0g.fr/snapshot.json | head -c 80; echo

# certbot crée le vhost :443 avec cert + redirection
sudo certbot --apache -d energie.l0g.fr      # à l'invite : choisir "Redirect"
```

Puis ajouter HSTS au vhost SSL généré (`energie.l0g.fr-le-ssl.conf`) :

```apache
Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains"
```

### 6. Vérification finale

```bash
curl -sI https://energie.l0g.fr/ | grep -i content-security-policy
curl -s  "https://energie.l0g.fr/snapshot.json?$(date +%s)" | python3 -m json.tool | head
```

Ouvrir https://energie.l0g.fr/ : jauge composite, cinq rails, cartes par actif, deux cartes live.

---

## Activer ENTSO-E plus tard

Quand le token arrive :

```bash
sudo sed -i 's/^ENTSOE_TOKEN=$/ENTSOE_TOKEN=COLLE_LE_TOKEN/' /etc/energie/env
sudo systemctl start energie-snapshot.service
sudo journalctl -u energie-snapshot.service -n 5 --no-pager
```

Les avertissements `elec_fr`/`elec_de` disparaissent, l'électricité repart sur deux vraies
jambes (prix day-ahead FR + DE-LU) et entre pleinement dans le composite.

---

## Dépannage (pièges rencontrés en production)

- **ODRE renvoie HTTP 400** : le mirror `odre.opendatasoft.com` plafonne `limit` à 100. Le
  builder pagine par lots de 100 (`offset`). Ne pas dépasser.
- **GIE renvoie `total:0`** : l'agrégat Europe est `continent=eu`, pas `country=eu`.
- **ENTSO-E HTTP 400 sur les prix** : bug REST connu sur 12.1.D depuis janvier 2026,
  contourné par `BusinessType=A62` (déjà dans le builder).
- **certbot casse Apache** : un vhost `:443` avec `SSLEngine on` sans certificat empêche le
  démarrage. Déployer en HTTP seul, laisser certbot créer le bloc TLS.
- **Permission refusée sur `/etc/energie/env`** : vérifier que le **dossier** `/etc/energie`
  est bien `root:energie 750` (sinon le service ne peut pas traverser jusqu'au fichier).
- **Pétrole figé au même prix pendant plusieurs jours** : les deux sources spot temps réel
  sont tombées. La tête de série suit la chaîne `oilpriceapi → Twelve Data → Yahoo → EIA` ;
  quand toutes les sources temps réel échouent, le WTI/Brent reste sur le **spot EIA
  officiel**, qui est valide mais publié avec ~1 semaine de lag (donc figé tant que l'EIA
  n'a pas publié le point suivant). Ce n'est pas un bug : `tip_source` vaut alors `eia`,
  `quality_status` vaut `official-delayed`, `source_warning` et `notes` exposent la date
  effective dans `snapshot.json`, et la carte n'est marquée `stale` que si la donnée dépasse
  `STALE_MAX_AGE_DAYS` (défaut 10 j = l'EIA lui-même est cassé). Causes fréquentes des
  sources spot : `OILPRICE_KEY` sans crédit (`HTTP 402`), `TWELVEDATA_KEY` sur plan gratuit
  (le brut exige un plan Grow/Venture, `HTTP 404`), Yahoo qui bannit l'IP serveur (`429`).
  Diagnostic : `journalctl -u energie-snapshot.service | grep -iE 'oilprice|twelvedata|yahoo|donnee figee'`.
- **Avoir un prix frais** : aucune source de brut **temps réel gratuite** ne passe depuis un
  serveur (Yahoo bannit l'IP, Stooq oppose un défi anti-bot, Twelve Data/FMP réservent le
  brut aux plans payants, CME renvoie 403). Le seul daily gratuit fiable est l'EIA/FRED,
  laggé de ~1 semaine. Pour du temps réel : recréditer `OILPRICE_KEY`, ou passer un plan
  payant Twelve Data (le code le prend alors automatiquement).
- **ENTSO-E HTTP 400 « larger than maximum allowed period 'P1Y' »** : la fenêtre demandée
  dépasse 1 an. Le builder borne à 360 jours (`fetch_entsoe_dayahead(days=360)`).
- **« réponse trop volumineuse »** : `MAX_BYTES` (24 Mo) trop bas pour un gros XML ENTSO-E.
  Le XML day-ahead DE-LU sur ~1 an est volumineux ; ne pas redescendre sous ~16 Mo.
- **Page bloquée sur « chargement »** : souvent le cache navigateur ou une extension qui se
  cogne à la CSP. Forcer le rechargement (Ctrl+Maj+R) ; tester en navigation privée.

---

## Structure du dépôt

```
energie-stress-monitor/
├── builder.py                 # collecte + scoring + snapshot (stdlib only)
├── gen_sample.py              # génère un snapshot d'exemple pour l'aperçu local
├── env.example                # gabarit des clés (à copier vers /etc/energie/env)
├── deploy/
│   ├── energie-snapshot.service
│   ├── energie-snapshot.timer
│   └── energie.l0g.fr.conf
└── web/
    ├── index.html
    ├── app.css                # identité "terminal éditorial" (l0g.fr)
    ├── app.js
    └── snapshot.json          # échantillon synthétique (remplacé en prod)
```

## Aperçu local sans serveur

```bash
python3 gen_sample.py          # (re)génère web/snapshot.json synthétique
cd web && python3 -m http.server 8000
# ouvrir http://localhost:8000/
```

## Limites connues (v1)

- Pas de prix **TTF** propre en API gratuite : la tension gaz européenne passe par le
  stockage GIE. Carbone **EUA** non inclus (pas de flux gratuit fiable).
- **Pétrole** : l'historique (z-score) vient du spot EIA officiel ; la tête de série (prix
  affiché) est rafraîchie en temps réel par une chaîne de repli `oilpriceapi → Twelve Data →
  Yahoo`, sinon on reste sur le spot EIA seul (officiel, laggé de ~1 semaine). Le champ
  `tip_source` de chaque carte indique la source effective du dernier point, et `age_days`
  son ancienneté ; le badge `stale` ne s'allume qu'au-delà de `STALE_MAX_AGE_DAYS` (défaut
  10 j), c.-à-d. quand l'EIA lui-même cesse d'avancer — le lag normal de l'EIA n'est donc
  pas signalé comme une anomalie. Chaque carte affiche sa date. **Il n'existe pas de source
  de brut temps réel gratuite exploitable depuis un serveur** (Yahoo bannit l'IP, Stooq/CME
  opposent des murs anti-bot, Twelve Data/FMP réservent le brut au payant) : pour du frais
  durable, prévoir un `OILPRICE_KEY` crédité ou un plan payant Twelve Data.
  Yahoo Finance reste forçable en source primaire (`ENERGIE_YAHOO_OIL=1`) mais rate-limite les IP serveur.
- `contexte` (EUR/USD) est fetché live côté navigateur, donc absent du composite serveur
  (renormalisé sur les autres sous-indices).

## Licence

MIT — Olivier Laurelli, 2026. Voir [LICENSE](LICENSE).
