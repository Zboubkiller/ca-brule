"""ça brûle — carte des contestations en France d'après la presse (Google Actualités RSS).
Lancer : python app.py  puis ouvrir http://localhost:8765
"""
import urllib.error
import base64, hashlib, html, json, os, re, threading, time, unicodedata, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8765))
REFRESH = 600  # secondes entre deux cycles de collecte
CITY_CHUNK = 100  # villes interrogées par cycle (rotation : les 300 sont couvertes en 3 cycles)
TOP_CITIES = 300  # villes interrogées individuellement
CITY_TERMS = '(blocus OR blocage OR manifestation OR grève OR rassemblement OR mobilisation OR manifestants)'
MASTO_TAGS = ["blocus", "manifestation", "blocage", "grève", "mobilisation"]
UA = {"User-Agent": "Mozilla/5.0 (ca-brule prototype)"}

QUERIES = ["manifestation", "manifestants", "blocage", "grève", "rassemblement",
           "mobilisation", "agriculteurs colère", "émeutes", "piquet de grève", "occupation lycée"]
KEYWORDS = re.compile(r"blocus|heurts|mouvement lyc[ée]en|lyc[ée]ens? (?:mobilis|bloqu)|manifest|manif\b|blocage|bloqu|gr[eè]ve|rassembl|mobilis|piquet|[eé]meute|"
                      r"barrages? (?:filtrants?|routiers?|de (?:manifestants|tracteurs|agriculteurs|gendarmes|police|cars))|occupation|occup[ée]s? (?:par|depuis)|march[ea] (?:de|pour|contre)|colère|syndicat|intersyndicale", re.I)
EXCLUDE = re.compile(r"\b(?:football|ligue 1|rugby|top 14|mercato|horoscope|salon|exposition|festival|octobre rose|concert|spectacle)\b", re.I)
STOP = {"pour", "bon", "meme", "ainsi", "plaine", "sur", "mars", "juin", "pres", "tout", "ceux",
        "avant", "apres", "gare", "lors", "est", "les", "des", "une", "grand", "saint", "ville",
        "vienne", "force", "loire", "france", "mobilisation", "colere", "manifestation", "gréve",
        "retraites", "budget", "paris 2024", "ici", "eux", "sens", "ligne", "tours de", "bar", "fort",
        "matignon", "syndicat", "le syndicat", "leon", "caux", "bercy", "assemblee", "senat", "elysee", "doubs", "jura", "moselle", "var", "gard", "aisne", "marne", "somme", "nord", "meuse", "orne", "indre", "cher", "lot", "gers", "ain", "eure", "oise", "tarn", "aude", "cote d or", "le", "la", "ce", "va", "ay", "do", "bu", "ste", "cour", "pont", "port", "sète"} - {"sète"}


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[-'’]", " ", s).strip()


FULL = []  # toutes les communes (toutes tailles) : recherche et scan de près


def load_communes():
    path = os.path.join(HERE, "communes.json")
    if not os.path.exists(path):
        url = "https://geo.api.gouv.fr/communes?fields=nom,centre,population,codeDepartement&format=json&geometry=centre"
        data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
        open(path, "wb").write(data)
    idx = {}
    for c in json.load(open(path, encoding="utf-8")):
        pop = c.get("population") or 0
        if c.get("centre"):
            FULL.append({"nom": c["nom"], "lon": c["centre"]["coordinates"][0], "lat": c["centre"]["coordinates"][1],
                         "pop": pop, "dep": c.get("codeDepartement"), "code": c.get("code"), "n": norm(c["nom"])})
        if pop < 1500 or not c.get("centre"):
            continue
        k = norm(c["nom"])
        if k in STOP:
            continue
        rec = {"nom": c["nom"], "lon": c["centre"]["coordinates"][0],
               "lat": c["centre"]["coordinates"][1], "pop": pop, "dep": c.get("codeDepartement")}
        if k not in idx or pop > idx[k]["pop"]:
            idx[k] = rec
        # « au Mans », « du Havre », « à La Rochelle » : alias sans l'article
        m = re.match(r"(?:le|la|les|l) (.+)", k)
        if m and pop >= 5000 and m.group(1) not in STOP:
            a = m.group(1)
            if a not in idx or pop > idx[a]["pop"]:
                idx[a] = rec
    return idx


COMMUNES = {}
STATE = {"events": [], "updated": None, "status": "démarrage…"}
LOCK = threading.Lock()


def geocode(title):
    """Cherche la plus longue suite de mots à majuscule correspondant à une commune."""
    # on tente toutes les fenêtres de 1 à 5 tokens commençant par une majuscule
    toks = re.findall(r"[\wÀ-ÿ'’-]+", title)
    best = None
    for i, t in enumerate(toks):
        if not t[0].isupper():
            continue
        for n in range(min(5, len(toks) - i), 0, -1):
            cand = " ".join(toks[i:i + n])
            c = COMMUNES.get(norm(cand))
            if c:
                # un mot unique en début de titre est peu fiable (majuscule de phrase)
                if n == 1 and i == 0 and c["pop"] < 30000:
                    continue
                if not best or n > best[0] or (n == best[0] and c["pop"] > best[1]["pop"]):
                    best = (n, c)
                break
    return best[1] if best else None


def _items(url, ua=None):
    req = urllib.request.Request(url, headers={"User-Agent": ua or UA["User-Agent"]})
    return ET.fromstring(urllib.request.urlopen(req, timeout=30).read()).iter("item")


def fetch_google(q, when="1d"):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:{when}" if when else q, "hl": "fr", "gl": "FR", "ceid": "FR:fr"})
    for it in _items(url):
        title = (it.findtext("title") or "").strip()
        src = it.findtext("source") or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            continue
        yield {"title": title, "src": src, "link": it.findtext("link"), "ts": ts}


def fetch_bing(q):
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "rss", "setmkt": "fr-FR", "qft": 'interval="7"'})
    for it in _items(url):
        title = (it.findtext("title") or "").strip()
        link = it.findtext("link") or ""
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            continue
        if time.time() - ts > 86400 or ts > time.time() + 3600:
            continue
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        src = urllib.parse.urlparse(real).netloc.replace("www.", "")
        yield {"title": title, "src": src, "link": real, "ts": ts}


def fetch_mastodon(tag):
    url = f"https://mastodon.social/api/v1/timelines/tag/{urllib.parse.quote(tag)}?limit=40"
    for st in json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30)):
        if st.get("language") not in ("fr", None):
            continue
        text = html.unescape(re.sub(r"<[^>]+>", " ", st.get("content", ""))).strip()
        ts = time.mktime(time.strptime(st["created_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        if time.time() - ts > 86400:
            continue
        yield {"title": re.sub(r"\s+", " ", text)[:200], "src": "Mastodon", "link": st["url"], "ts": ts}



# --- Sources de secours / complémentaires : flux presse directs (sans moteur de recherche) et GDELT ---
ICI_STATIONS = ("107-1 alsace armorique auxerre azur bearn belfort-montbeliard berry besancon bourgogne breizh-izel "
                "champagne-ardenne cotentin creuse drome-ardeche elsass gard-lozere gascogne gironde herault isere "
                "la-rochelle limousin loire-ocean lorraine-nord maine mayenne nord normandie-caen normandie-rouen "
                "orleans pays-basque pays-d-auvergne pays-de-savoie perigord picardie poitou provence rcfm roussillon "
                "saint-etienne-loire sud-lorraine toulouse touraine vaucluse").split()
FEEDS = ([f"https://www.ici.fr/rss/{st}/a-la-une.xml" for st in ICI_STATIONS] + [
    "https://www.ici.fr/rss/a-la-une.xml", "https://www.ladepeche.fr/rss.xml", "https://www.sudouest.fr/rss.xml",
    "https://www.letelegramme.fr/rss.xml", "https://www.ouest-france.fr/rss/une", "https://actu.fr/rss.xml",
    "https://www.lemonde.fr/rss/en_continu.xml", "https://www.francetvinfo.fr/titres.rss",
    "https://www.20minutes.fr/feeds/rss-une.xml", "https://www.bfmtv.com/rss/news-24-7/",
    "https://www.lefigaro.fr/rss/figaro_actualites.xml", "https://www.lexpress.fr/rss/alaune.xml",
    "https://www.liberation.fr/arc/outboundfeeds/rss-all/collection/accueil-une/?outputType=xml"])
FEED_NAMES = {"www.ici.fr": "ici", "www.ladepeche.fr": "La Dépêche", "www.sudouest.fr": "Sud Ouest",
              "www.letelegramme.fr": "Le Télégramme", "www.ouest-france.fr": "Ouest-France", "actu.fr": "actu.fr",
              "www.lemonde.fr": "Le Monde", "www.francetvinfo.fr": "franceinfo", "www.20minutes.fr": "20 Minutes",
              "www.bfmtv.com": "BFMTV", "www.lefigaro.fr": "Le Figaro", "www.lexpress.fr": "L'Express",
              "www.liberation.fr": "Libération"}
GDELT_QUERIES = ["blocus OR blocage OR manifestation OR grève", "manifestants OR rassemblement OR mobilisation OR émeutes"]
GDELT_LOCK = threading.Lock()
GDELT_LAST = [0.0]
GDELT_BLOCK = [0.0]  # suspension après un 429


def fetch_feed(url):
    host = urllib.parse.urlparse(url).netloc
    src = FEED_NAMES.get(host, host)
    for it in _items(url, ua="Mozilla/5.0"):
        title = html.unescape((it.findtext("title") or "").strip())
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            continue
        if time.time() - ts > 86400 or ts > time.time() + 3600:
            continue
        yield {"title": title, "src": src, "link": it.findtext("link") or "", "ts": ts}


def fetch_gdelt(q):
    with GDELT_LOCK:  # GDELT n'accepte qu'une requête toutes les 5 s
        if time.time() < GDELT_BLOCK[0]:
            return
        wait = 6.5 - (time.time() - GDELT_LAST[0])
        if wait > 0:
            time.sleep(wait)
        GDELT_LAST[0] = time.time()
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(
            {"query": f"({q}) sourcecountry:france sourcelang:french", "mode": "artlist", "format": "json",
             "maxrecords": "100", "timespan": "1d", "sort": "datedesc"})
        try:
            data = json.loads(http_text(url, 40))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                GDELT_BLOCK[0] = time.time() + 1800
                print("GDELT limite les requêtes : pause de 30 min")
                return
            raise
    for a in data.get("articles", []):
        try:
            ts = time.mktime(time.strptime(a["seendate"], "%Y%m%dT%H%M%SZ")) - time.timezone
        except Exception:
            continue
        yield {"title": a.get("title", ""), "src": a.get("domain", "gdelt"), "link": a.get("url", ""), "ts": ts}


NEW_FEEDS = {
    # presse régionale
    "https://www.dna.fr/rss": "Dernières Nouvelles d'Alsace", "https://www.lejsl.com/rss": "Le Journal de Saône-et-Loire",
    "https://www.lalsace.fr/rss": "L'Alsace", "https://www.leprogres.fr/rss": "Le Progrès",
    "https://www.ledauphine.com/rss": "Le Dauphiné libéré", "https://www.nicematin.com/rss": "Nice-Matin",
    "https://www.midilibre.fr/rss.xml": "Midi Libre", "https://www.bienpublic.com/rss": "Le Bien Public",
    "https://www.republicain-lorrain.fr/rss": "Le Républicain Lorrain", "https://www.vosgesmatin.fr/rss": "Vosges Matin",
    "https://www.lindependant.fr/rss.xml": "L'Indépendant", "https://www.charentelibre.fr/rss.xml": "Charente Libre",
    "https://www.estrepublicain.fr/rss": "L'Est Républicain",
    "https://france3-regions.franceinfo.fr/paris-ile-de-france/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/provence-alpes-cote-d-azur/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/bretagne/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/pays-de-la-loire/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/nouvelle-aquitaine/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/occitanie/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/auvergne-rhone-alpes/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/hauts-de-france/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/grand-est/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/normandie/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/bourgogne-franche-comte/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/centre-val-de-loire/rss": "France 3 Régions",
    "https://marsactu.fr/feed/": "Marsactu", "https://www.rue89lyon.fr/feed/": "Rue89 Lyon",
    "https://www.rue89strasbourg.com/feed": "Rue89 Strasbourg", "https://rue89bordeaux.com/feed/": "Rue89 Bordeaux",
    "https://www.mediacites.fr/feed/": "Médiacités",
    # presse indépendante et militante
    "https://reporterre.net/spip.php?page=backend": "Reporterre", "https://basta.media/spip.php?page=backend": "Basta!",
    "https://rapportsdeforce.fr/feed": "Rapports de Force", "https://contre-attaque.net/feed/": "Contre-Attaque",
    "https://www.humanite.fr/rss/actu.rss": "L'Humanité", "https://www.mediapart.fr/articles/feed": "Mediapart",
    # national / international
    "https://www.francetvinfo.fr/societe.rss": "franceinfo", "https://www.francetvinfo.fr/politique.rss": "franceinfo",
    "https://www.francetvinfo.fr/economie.rss": "franceinfo", "https://www.rfi.fr/fr/france/rss": "RFI",
    "https://www.france24.com/fr/france/rss": "France 24", "https://www.europe1.fr/rss.xml": "Europe 1",
    "https://www.cnews.fr/rss.xml": "CNews", "https://fr.euronews.com/rss": "Euronews",
}
for _u, _n in NEW_FEEDS.items():
    FEEDS.append(_u)
    FEED_NAMES[urllib.parse.urlparse(_u).netloc] = _n
FEED_NAMES["france3-regions.franceinfo.fr"] = "France 3 Régions"

CURSOR = [0]
# Disjoncteur : si Google répond 429/503, on suspend ses requêtes (30 min, puis 1 h, 2 h… max 4 h)
BLOCK = {"until": 0, "step": 1800}


def blocked():
    return time.time() < BLOCK["until"]


def jobs():
    j = [(fetch_feed, u) for u in FEEDS]
    j += [(fetch_gdelt, q) for q in GDELT_QUERIES]
    j += [(fetch_google, q) for q in QUERIES]
    j += [(fetch_bing, q) for q in ("blocus lycées", "manifestation", "blocage", "grève", "rassemblement", "mobilisation")]
    j += [(fetch_mastodon, t) for t in MASTO_TAGS]
    top = sorted({c["nom"]: c for c in COMMUNES.values()}.values(), key=lambda c: -c["pop"])[:TOP_CITIES]
    start = CURSOR[0] % len(top)
    chunk = (top + top)[start:start + CITY_CHUNK]
    CURSOR[0] = (start + CITY_CHUNK) % len(top)
    j += [(fetch_google, f'{CITY_TERMS} "{c["nom"]}"') for c in chunk]
    return j


def run_job(job):
    fn, q = job
    if fn is fetch_google and blocked():
        return []
    BUDGET.append(time.time())
    try:
        out = list(fn(q))
        if fn is fetch_google:
            BLOCK["step"] = 1800
        return out
    except urllib.error.HTTPError as e:
        if fn is fetch_bing and e.code in (403, 429, 503):
            BLOCK_BING["until"] = time.time() + BLOCK_BING["step"]
            print("Bing limite les requêtes (HTTP %d) : pause de %d min" % (e.code, BLOCK_BING["step"] // 60))
            BLOCK_BING["step"] = min(BLOCK_BING["step"] * 2, 3 * 3600)
        elif fn is fetch_google and e.code in (429, 503):
            BLOCK["until"] = time.time() + BLOCK["step"]
            print("Google limite les requêtes (HTTP %d) : pause de %d min" % (e.code, BLOCK["step"] // 60))
            BLOCK["step"] = min(BLOCK["step"] * 2, 4 * 3600)
        else:
            print("erreur", fn.__name__, q[:40], e)
        return []
    except Exception as e:
        print("erreur", fn.__name__, q[:40], e)
        return []
    finally:
        time.sleep(1.5)


THEMES = {  # clé -> motifs sur titre normalisé (minuscules, sans accents)
    "lyceens": r"lycee|lyceen|etudiant|universit|fac|blocus|parcoursup|college|eleves?|campus|scolaire|enseignant|professeur|profs?|education nationale|rectorat",
    "agri": r"agricult|paysan|tracteur|eleveur|fnsea|coordination rurale|mercosur|viticult|pecheur|ferme|exploitant",
    "travail": r"greve|syndicat|intersyndicale|salarie|licenci|usine|cgt|cfdt|force ouvriere|fonctionnaire|plan social|piquet|ouvrier|emploi|intermittent|chomage|salaire",
    "transports": r"sncf|ratp|cheminot|controleur|aiguilleur|aerien|aeroport|bus|tram|tramway|metro|transports?|conducteur|trafic|peage|autoroute",
    "sante": r"hopital|hospitalier|soignant|infirmi|medecin|urgences|sante|ehpad|pharmac|chu",
    "ecologie": r"climat|ecolog|megabassine|bassines?|a69|extinction rebellion|soulevements|nucleaire|biodiversite|pesticide|animaux|anti.?chasse",
    "international": r"palestin|gaza|israel|ukrain|iran|liban|syrie|solidarite avec|contre la guerre|trump",
    "politique": r"budget|reforme|gouvernement|retraite|49\.?3|premier ministre|macron|matignon|assemblee|projet de loi|loi|lfi|rassemblement national|ultragauche|ultradroite|pnf",
    "violences": r"brul|poubelle|pompier|caillass|jets? de|violence|emeute|heurts?|affrontement|lbd|lacrymo|interpell|incendi|feux?|mortier|degradation|blesse|police|gendarm|casseur|tension",
    "securite_logement": r"logement|loyer|expulsion|squat|insecurite|narcotrafic|deal|quartier|ordures|dechets",
}
THEME_RE = {k: re.compile(v) for k, v in THEMES.items()}


def themes_of(title):
    t = norm(title)
    return [k for k, r in THEME_RE.items() if r.search(t)] or ["autre"]


STORE = {}        # clé de titre -> {"a": article, "c": commune}
LAST_FETCH = {}   # (nom, dep) -> timestamp de la dernière requête dédiée
ALL = []          # toutes les communes indexées (uniques)
BUDGET = []       # horodatages de toutes les requêtes sortantes (fenêtre glissante 10 min)
AREA_LOCK = threading.Lock()
AREA_TTL = 900
AREA_MAX_PER_CALL = 15
TOTAL_BUDGET = 450  # requêtes sortantes max par 10 min (collecte de base + à la demande)


RAW = {}  # articles de protestation sans commune reconnue (24 h) : réservoir pour la recherche locale hors ligne
RAW_MAX = 6000


def ingest(arts, hint=None):
    for a in arts:
        key = norm(a["title"])[:80]
        if key in STORE or not KEYWORDS.search(a["title"]) or EXCLUDE.search(a["title"]):
            continue
        c = geocode(a["title"])
        if hint and hint["n"] in norm(a["title"]):
            c = hint  # requête dédiée à cette commune : on lui attribue l'article si elle est citée
        if c:
            a["themes"] = themes_of(a["title"])
            STORE[key] = {"a": a, "c": c}
            RAW.pop(key, None)
        elif len(RAW) < RAW_MAX:
            RAW[key] = a


def scan_raw(c):
    """Repli sans requête externe : cherche la commune dans les articles déjà collectés mais non localisés.
    On exige le nom avec sa majuscule (« Barre » commune ≠ « barré » verbe)."""
    if len(c["n"]) < 4:
        return 0
    pat = re.compile(r"(?<![\wÀ-ÿ])%s(?![\wÀ-ÿ])" % re.escape(c["nom"]))
    pat_n = re.compile(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(c["n"])) if len(c["n"]) >= 8 else None
    found = 0
    for key, a in list(RAW.items()):
        if key in STORE:
            continue
        if pat.search(a["title"]) or (pat_n and pat_n.search(norm(a["title"]))):
            a["themes"] = themes_of(a["title"])
            STORE[key] = {"a": a, "c": c}
            RAW.pop(key, None)
            found += 1
    return found


BLOCK_BING = {"until": 0, "step": 900}


def fetch_local(c):
    """Cascade pour une commune : Google Actualités -> Bing Actualités -> flux déjà collectés (sans requête)."""
    q = '%s "%s"' % (CITY_TERMS, c["nom"])
    arts, via = [], "cache"
    if not blocked():
        arts = run_job((fetch_google, q))
        via = "google" if arts else via
    if not arts and time.time() >= BLOCK_BING["until"]:
        arts = run_job((fetch_bing, q))
        via = "bing" if arts else via
    ingest(arts, c)
    n = scan_raw(c)
    return via if (arts or not n) else "cache"


STORE_PATH = os.path.join(HERE, "store.json")


def save_store():
    try:
        with LOCK:
            blob = json.dumps(list(STORE.values()), ensure_ascii=False)
        tmp = STORE_PATH + ".tmp"
        open(tmp, "w", encoding="utf-8").write(blob)
        os.replace(tmp, STORE_PATH)
    except Exception as e:
        print("sauvegarde impossible:", e)


def load_store():
    try:
        cutoff = time.time() - 86400
        for v in json.load(open(STORE_PATH, encoding="utf-8")):
            if v["a"]["ts"] >= cutoff:
                STORE[norm(v["a"]["title"])[:80]] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print("lecture sauvegarde impossible:", e)


LINKMAP = {}  # id court -> URL de l'article (les liens ne sont plus envoyés dans la charge initiale)


def link_id(url):
    return base64.urlsafe_b64encode(hashlib.sha1(url.encode("utf-8")).digest())[:10].decode()


def slim_events(state):
    """Version allégée pour le navigateur : titres, sources, dates, thèmes et id de lien (≈ -90 % de poids)."""
    out = dict(state)
    out["events"] = [{**e, "articles": [{"title": a["title"], "src": a.get("src", ""), "ts": a["ts"], "themes": a.get("themes"), "h": a["h"]}
                                        for a in e["articles"] if a.get("h")]} for e in state["events"]]
    return out


def rebuild():
    cutoff = time.time() - 86400
    for k in [k for k, a in RAW.items() if a["ts"] < cutoff]:
        RAW.pop(k, None)
    places = {}
    with LOCK:
        for k in [k for k, v in STORE.items() if v["a"]["ts"] < cutoff]:
            del STORE[k]
        LINKMAP.clear()
        for v in STORE.values():
            c = v["c"]
            a = v["a"]
            a["h"] = link_id(a["link"] or "")
            if a["link"]:
                LINKMAP[a["h"]] = a["link"]
            places.setdefault(c["nom"] + c["dep"], {**c, "articles": []})["articles"].append(a)
        events = []
        for p in places.values():
            p["articles"].sort(key=lambda a: -a["ts"])
            events.append({"nom": p["nom"], "dep": p["dep"], "lat": p["lat"], "lon": p["lon"], "pop": p["pop"],
                           "last": p["articles"][0]["ts"],
                           "sources": len({a["src"] for a in p["articles"]}),
                           "articles": p["articles"][:8], "n": len(p["articles"])})
        STATE.update(events=events, updated=time.time(), status="ok")
    save_store()
    return events


def refresh():
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(run_job, jobs()))
    for arts in results:
        ingest(arts)
    now = time.time()
    for c in sorted(ALL, key=lambda c: -c["pop"])[:TOP_CITIES]:
        LAST_FETCH[(c["nom"], c["dep"])] = now
    ev = rebuild()
    print(f"{len(ev)} lieux, {sum(e['n'] for e in ev)} articles")


def minpop_for_zoom(z):
    """Séquençage par taille de commune : plus on zoome, plus on descend vers les petites."""
    return None if z < 10 else 5000 if z == 10 else 2000 if z == 11 else 0


def area(s, w, n, e, z=9):
    """Interroge à la demande les communes visibles dont les données sont périmées."""
    if not AREA_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        now = time.time()
        BUDGET[:] = [t for t in BUDGET if now - t < 600]
        mp = minpop_for_zoom(z)
        if mp is None:
            return {"queried": 0, "remaining": 0, "tooFar": True}
        inside = sorted((c for c in FULL if c["pop"] >= mp and s <= c["lat"] <= n and w <= c["lon"] <= e), key=lambda c: -c["pop"])
        stale = [c for c in inside if now - LAST_FETCH.get((c["nom"], c["dep"]), 0) > AREA_TTL]
        room = max(0, TOTAL_BUDGET - len(BUDGET))
        batch = stale[:min(AREA_MAX_PER_CALL, room)]
        if batch:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(fetch_local, batch))
            for c in batch:
                LAST_FETCH[(c["nom"], c["dep"])] = now
            rebuild()
        return {"queried": len(batch), "remaining": len(stale) - len(batch), "limited": room == 0}
    finally:
        AREA_LOCK.release()


def search(q):
    q = norm(q)
    if len(q) < 2:
        return []
    hits = [c for c in FULL if q in c["n"]]
    hits.sort(key=lambda c: (not c["n"].startswith(q), -c["pop"]))
    return [{k: c[k] for k in ("nom", "dep", "lat", "lon", "pop", "code")} for c in hits[:8]]


def city(code):
    """Interroge à la demande une commune précise (recherche utilisateur), même petite."""
    c = next((c for c in FULL if c["code"] == code), None)
    if not c:
        return {"error": "inconnue"}
    info = {k: c[k] for k in ("nom", "dep", "lat", "lon", "pop", "code")}
    if not AREA_LOCK.acquire(timeout=10):
        return {"busy": True, "commune": info}
    try:
        now = time.time()
        BUDGET[:] = [t for t in BUDGET if now - t < 600]
        fresh = now - LAST_FETCH.get((c["nom"], c["dep"]), 0) <= AREA_TTL
        if not fresh and len(BUDGET) < TOTAL_BUDGET:
            via = fetch_local(c)
            LAST_FETCH[(c["nom"], c["dep"])] = now
            rebuild()
            return {"queried": 1, "via": via, "commune": info}
        return {"queried": 0, "limited": not fresh, "commune": info}
    finally:
        AREA_LOCK.release()


def loop():
    try:  # redémarrage récent : on reprend les données sauvegardées sans relancer 100+ requêtes
        age = time.time() - os.path.getmtime(STORE_PATH)
        if STORE and age < REFRESH:
            time.sleep(REFRESH - age)
    except OSError:
        pass
    while True:
        try:
            refresh()
        except Exception as e:
            STATE["status"] = f"erreur: {e}"
        time.sleep(REFRESH)



# ---------------------------------------------------------------------------
# Jeux de données « add-ons » : sources structurées, mises à jour 1 fois par jour
#  - féminicides : carte publique du collectif « Féminicides par compagnons ou ex » (export KML)
#  - décès liés à la police : liste publique de « Désarmons-les »
# ---------------------------------------------------------------------------
import math

YEAR = time.localtime().tm_year
MONTHS = {m: i + 1 for i, m in enumerate("janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split())}
MONTHS_RE = "|".join(MONTHS)
DATASET_TTL = 23 * 3600
DATASETS = {
    "feminicides": {"title": "Féminicides conjugaux", "path": os.path.join(HERE, "fem.json"),
                    "source": "Collectif Féminicides par compagnons ou ex", "url": "https://www.feminicides.fr/",
                    "note": "Décompte a minima établi par le collectif à partir de la presse ; il se limite aux féminicides conjugaux. Les noms sont retirés et la position est ramenée au centre de la commune."},
    "police": {"title": "Décès liés à la police", "path": os.path.join(HERE, "police.json"),
               "source": "Désarmons-les", "url": "https://desarmons.net/listes-des-victimes/personnes-tuees-par-les-forces-de-lordre/",
               "note": "Décès survenus lors ou à la suite d'interventions des forces de l'ordre, recensés par un collectif militant. Il ne s'agit pas de toutes les violences policières."},
}
DS = {k: {"items": [], "updated": None} for k in DATASETS}
FEM_MAP = "1X-eWA3Es57ts50GRiKbKXSYp9FKcmnM"  # carte 2026 liée depuis feminicides.fr
DEPS = {}


def http_text(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode("utf-8", "replace")


def nearest_commune(lat, lon, max_km=25):
    best, bd = None, 1e9
    k = math.cos(math.radians(lat))
    for c in FULL:
        d = (c["lat"] - lat) ** 2 + ((c["lon"] - lon) * k) ** 2
        if d < bd:
            best, bd = c, d
    return best if best and bd ** 0.5 * 111 <= max_km else None


def parse_date(text, year=None):
    m = re.search(r"\b(\d{1,2})(?:er)? (%s)\b" % MONTHS_RE, text, re.I)
    if not m:
        return None
    try:
        return time.mktime((year or YEAR, MONTHS[m.group(2).lower()], int(m.group(1)), 12, 0, 0, 0, 0, -1))
    except Exception:
        return None


def mask_names(text):
    """Retire les prénoms/noms des victimes : on ne garde que l'âge (« une femme (63 ans) »)."""
    return re.sub(r"(?<=, )[A-ZÀ-Ý][\wÀ-ÿ'’-]+(?: [A-ZÀ-Ý][\wÀ-ÿ'’-]+){0,3} (\(\d+ ans\))", r"une femme \1", text)


def fetch_feminicides():
    if not DEPS:
        for d in json.loads(http_text("https://geo.api.gouv.fr/departements?fields=nom,code")):
            DEPS[norm(d["nom"])] = d["code"]
    kml = http_text("https://www.google.com/maps/d/kml?mid=%s&forcekml=1" % FEM_MAP)
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    items = []
    for p in ET.fromstring(kml.encode("utf-8")).iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name = (p.findtext("k:name", namespaces=ns) or "").strip()
        if not re.match(r"\d+\s*-", name):
            continue  # on ignore la fiche « STATS »
        coords = (p.findtext(".//k:coordinates", namespaces=ns) or "").strip().split(",")
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        desc = html.unescape(re.sub(r"<[^>]+>", " ", p.findtext("k:description", namespaces=ns) or ""))
        m = re.search(r"https://www\.feminicides\.fr/\S+", desc)
        link = m.group(0) if m else DATASETS["feminicides"]["url"]
        text = re.split(r"➡|Lire la publication", desc)[0]
        text = re.sub(r"#\w+", "", text)
        text = mask_names(re.sub(r"\s+", " ", text)).strip()
        ts = parse_date(text)
        if not ts:
            continue
        c = locate_police(text[:300])  # commune citée dans le texte (plus fiable que le point)
        if not c or (c["lat"] - lat) ** 2 + ((c["lon"] - lon) * 0.7) ** 2 > 0.35 ** 2:
            c = nearest_commune(lat, lon)  # sinon, commune la plus proche : position ramenée au centre de la commune
        items.append({"ts": ts, "lat": c["lat"] if c else round(lat, 2), "lon": c["lon"] if c else round(lon, 2),
                      "place": c["nom"] if c else "", "dep": c["dep"] if c else "", "text": text[:420], "url": link})
    return items


def locate_police(text):
    """Trouve « Commune (Département) » dans le texte ; repli sur la détection de communes par majuscules."""
    for m in re.finditer(r"\(([^()]{2,40})\)", text):
        inner = norm(m.group(1))
        dm = re.match(r"(\d{2,3}|2[ab])\s*[-–]", inner) or re.match(r"(\d{2,3})$", inner)
        code = DEPS.get(inner) or (dm.group(1).upper() if dm else None) or ("75" if re.match(r"\d+(?:e|er|eme)\b", inner) else None)
        if not code:
            continue
        toks = re.findall(r"[\wÀ-ÿ-]+", text[:m.start()])[-6:]
        for n in range(min(5, len(toks)), 0, -1):
            cand = norm(" ".join(toks[-n:]))
            hit = next((c for c in FULL if c["dep"] == code and c["n"] == cand), None)
            if hit:
                return hit
        if code == "75":
            return next((c for c in FULL if c["code"] == "75056"), None)
    return geocode(text)


def fetch_police():
    if not DEPS:
        for d in json.loads(http_text("https://geo.api.gouv.fr/departements?fields=nom,code")):
            DEPS[norm(d["nom"])] = d["code"]
    page = http_text(DATASETS["police"]["url"])
    i = page.find(">%d<" % YEAR)
    section = page[i:] if i >= 0 else page
    nxt = re.search(r">%d<" % (YEAR - 1), section)
    if nxt:
        section = section[:nxt.start()]
    items = []
    for li in re.findall(r"<li>(.*?)</li>", section, re.S):
        link = re.search(r'href="([^"]+)"', li)
        t = html.unescape(re.sub(r"<[^>]+>", "", li))
        t = re.sub(r"\s+", " ", t).strip()
        m = re.match(r"Le (\d\d)/(\d\d)/(\d{4})\s*,\s*(.*)", t)
        if not m or int(m.group(3)) != YEAR:
            continue
        rest = m.group(4)
        a = re.search(r"(?:environ |entre )?\d{1,2} ans", rest)
        after = rest[a.end():] if a else rest.split(",", 1)[-1]
        desc = after.lstrip(" ,")
        loc = locate_police(desc[:360])
        items.append({"ts": time.mktime((YEAR, int(m.group(2)), int(m.group(1)), 12, 0, 0, 0, 0, -1)),
                      "lat": loc["lat"] if loc else None, "lon": loc["lon"] if loc else None,
                      "place": loc["nom"] if loc else "", "dep": loc["dep"] if loc else "",
                      "age": a.group(0) if a else "", "text": desc[:420],
                      "url": link.group(1) if link else DATASETS["police"]["url"]})
    return items


def refresh_dataset(name):
    ds = DATASETS[name]
    try:
        items = fetch_feminicides() if name == "feminicides" else fetch_police()
    except Exception as e:
        print("add-on", name, "erreur:", e)
        return False
    items.sort(key=lambda i: -i["ts"])
    DS[name] = {"items": items, "updated": time.time()}
    try:
        tmp = ds["path"] + ".tmp"
        open(tmp, "w", encoding="utf-8").write(json.dumps(DS[name], ensure_ascii=False))
        os.replace(tmp, ds["path"])
    except Exception as e:
        print("sauvegarde add-on impossible:", e)
    print("add-on %s: %d entrées, %d localisées" % (name, len(items), sum(1 for i in items if i.get("lat") is not None)))
    return True


def datasets_loop():
    for name, ds in DATASETS.items():
        try:
            DS[name] = json.load(open(ds["path"], encoding="utf-8"))
        except Exception:
            pass
    while True:
        for name in DATASETS:
            if not DS[name]["updated"] or time.time() - DS[name]["updated"] > DATASET_TTL:
                refresh_dataset(name)
                time.sleep(3)
        time.sleep(1800)


def dataset_payload(name):
    ds = DATASETS.get(name)
    if not ds:
        return {"error": "inconnu"}
    items = DS[name]["items"]
    return {"name": name, "title": ds["title"], "source": ds["source"], "url": ds["url"], "note": ds["note"],
            "year": YEAR, "updated": DS[name]["updated"], "total": len(items),
            "unlocated": sum(1 for i in items if i.get("lat") is None), "items": items}



# ---------------------------------------------------------------------------
# SEO / GEO : pages rendues côté serveur (indexables), sitemap, FAQ + données structurées
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL", "https://oraclemarin.fr/cabrule")
BASE_PATH = os.environ.get("BASE_PATH", "/cabrule")
SITE_NAME = "ça brûle"
ICON_LINKS = ('<link rel="icon" href="%(b)s/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="192x192" href="%(b)s/icon-192.png">'
              '<link rel="apple-touch-icon" href="%(b)s/apple-touch-icon.png"><link rel="manifest" href="%(b)s/manifest.webmanifest">') % {"b": BASE_PATH}
HOME_TITLE = "Carte des manifestations aujourd'hui en France, en direct | ça brûle"
HOME_DESC = ("Carte en direct des manifestations, grèves, blocages et mobilisations en France aujourd'hui, ville par ville, "
             "d'après la presse locale. Recherchez votre ville, filtrez par thème. Mise à jour toutes les 10 minutes.")

FAQ = [
    ("Où ont lieu les manifestations aujourd'hui en France ?",
     "La carte « ça brûle » affiche les villes où la presse signale une manifestation, une grève, un blocage ou un rassemblement "
     "dans les dernières 24 heures. Plus la bulle est grande, plus il y a d'articles. Cliquez sur une ville ou cherchez la vôtre "
     "avec la barre de recherche pour lire les articles."),
    ("D'où viennent les données de la carte ?",
     "Des titres d'articles de la presse en ligne : flux de ici (ex-France Bleu), La Dépêche, Ouest-France, Le Monde, franceinfo, "
     "Le Télégramme, actu.fr et d'autres, ainsi que Google Actualités, Bing Actualités, GDELT et Mastodon. La commune citée dans "
     "le titre est détectée automatiquement grâce à la base officielle des communes (geo.api.gouv.fr)."),
    ("À quelle fréquence la carte est-elle mise à jour ?",
     "Toutes les 10 minutes pour les mobilisations en direct. En zoomant sur un secteur, ou en cherchant une ville, la carte interroge "
     "en plus les communes visibles. Les deux jeux de données complémentaires (féminicides conjugaux, décès liés à la police) sont actualisés une fois par jour."),
    ("La carte est-elle exhaustive et fiable ?",
     "Non, elle est indicative. Elle ne montre que ce que la presse en ligne relaie, avec des erreurs de localisation possibles. "
     "Une mention dans un titre n'est pas une confirmation : ouvrez l'article source pour vérifier. Pour une information officielle, "
     "consultez la préfecture ou le ministère de l'Intérieur."),
    ("Comment savoir s'il y a une manifestation ou un blocage dans ma ville ?",
     "Tapez le nom de votre ville dans la barre de recherche : la carte s'y place et interroge la presse pour cette commune, même petite. "
     "Si aucun signal n'apparaît, aucune mobilisation n'a été relayée par la presse ces dernières 24 heures."),
    ("Que sont les modes « Féminicides » et « Décès police » ?",
     "Ce sont deux cartes complémentaires, basées sur des recensements associatifs publics : les féminicides conjugaux comptés par le collectif "
     "Féminicides par compagnons ou ex, et les décès liés à des interventions des forces de l'ordre recensés par Désarmons-les. "
     "Les noms des victimes sont retirés et les positions sont ramenées au centre de la commune."),
]

FKEY = {}
PAGE_CSS = ("""
:root{--bg:#0f1114;--panel:#171a1f;--line:#2a2f37;--fg:#e8eaed;--mute:#8b929c;--fire:#ff5a1f;--fire2:#ffb020}
*{box-sizing:border-box}html{background:var(--bg)}
body{margin:0;color:var(--fg);font:16px/1.6 system-ui,sans-serif;background:var(--bg)}
header,main,footer{max-width:820px;margin:0 auto;padding:0 18px}
header{padding-top:18px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-size:24px;font-weight:700;letter-spacing:-.5px;color:var(--fg);text-decoration:none}.brand span{color:var(--fire)}
.btn{background:var(--fire);color:#fff;text-decoration:none;padding:9px 16px;border-radius:99px;font-size:14px}
h1{font-size:28px;line-height:1.25;margin:22px 0 8px}h2{font-size:19px;margin:28px 0 8px}
a{color:var(--fire2)}.meta{color:var(--mute);font-size:13px}
ul{padding-left:20px}li{margin:8px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:14px 0}
footer{color:var(--mute);font-size:13px;padding-bottom:36px;margin-top:30px;border-top:1px solid var(--line);padding-top:16px}
""").strip()


def esc(t):
    return html.escape(str(t), quote=True)


def fkey():
    if not FKEY:
        for c in FULL:
            FKEY[(c["nom"], c["dep"])] = c
    return FKEY


def slug(c):
    return "%s-%s" % (norm(c["nom"]).replace(" ", "-"), c["code"])


def city_url(c, absolute=False):
    return ("%s/ville/%s" % (BASE_URL if absolute else BASE_PATH, slug(c)))


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def ago_txt(ts):
    m = max(0, int((time.time() - ts) / 60))
    return "il y a %d min" % m if m < 60 else "il y a %d h" % round(m / 60)


def active_cities():
    """Villes avec au moins un article de moins de 24 h, triées par nombre d'articles."""
    cutoff = time.time() - 86400
    out = []
    with LOCK:
        events = list(STATE["events"])
    for e in events:
        arts = [a for a in e["articles"] if a["ts"] >= cutoff]
        c = fkey().get((e["nom"], e["dep"]))
        if arts and c:
            out.append((c, arts, e["n"]))
    out.sort(key=lambda t: (-t[2], -t[1][0]["ts"]))
    return out


def indexable(c, n):
    """Évite les pages minces : 2 articles minimum, ou une grande ville."""
    return n >= 2 or c["pop"] >= 20000


def faq_ld():
    return {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ]}


def home_ld():
    graph = [
        {"@type": "WebSite", "@id": BASE_URL + "/#site", "name": SITE_NAME, "url": BASE_URL + "/", "inLanguage": "fr-FR",
         "description": HOME_DESC},
        {"@type": "WebApplication", "@id": BASE_URL + "/#app", "name": "ça brûle — carte des manifestations", "url": BASE_URL + "/",
         "applicationCategory": "NewsApplication", "operatingSystem": "Web", "inLanguage": "fr-FR", "isAccessibleForFree": True,
         "areaServed": {"@type": "Country", "name": "France"}, "description": HOME_DESC,
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"}, "dateModified": iso_now()},
        faq_ld(),
    ]
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)


def about_html():
    act = active_cities()[:24]
    links = " · ".join('<a href="%s">%s</a> <span class="meta">(%d)</span>' % (city_url(c), esc(c["nom"]), n) for c, arts, n in act)
    faq = "".join("<h3>%s</h3><p>%s</p>" % (esc(q), esc(a)) for q, a in FAQ)
    return ('<details id="about"><summary>À propos · questions fréquentes</summary><div class="aboutin">'
            "<h2>Carte des manifestations aujourd'hui en France</h2>"
            "<p>« ça brûle » recense en direct les manifestations, grèves, blocages et rassemblements signalés par la presse, ville par ville, "
            "avec filtres par thème (lycéens, agriculteurs, travail, transports, santé, écologie…).</p>"
            + ("<h3>Mobilisations signalées aujourd'hui</h3><p>%s</p>" % links if links else "")
            + sources_blurb() + "<h3>Cartes complémentaires</h3><p>"
            '<a href="%s/feminicides">Féminicides conjugaux en 2026</a> · <a href="%s/deces-police">Décès liés à la police en 2026</a></p>'
              % (BASE_PATH, BASE_PATH)
            + faq + '<p class="meta">Pour les modèles de langage : <a href="%s/llms.txt">llms.txt</a>.</p></div></details>' % BASE_PATH)


def render_index():
    t = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    t = t.replace("<!--LDJSON-->", '<script type="application/ld+json">%s</script>' % home_ld().replace("</", "<\\/"))
    return t.replace("<!--ABOUT-->", about_html())


def _shell(title, desc, canonical, body, robots="index,follow", ld=None):
    ldtag = '<script type="application/ld+json">%s</script>' % json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") if ld else ""
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>%s</title><meta name=\"description\" content=\"%s\">"
            '<link rel="canonical" href="%s"><meta name="robots" content="%s,max-image-preview:large">'
            '<meta name="theme-color" content="#0f1114">%s'
            '<meta property="og:type" content="website"><meta property="og:locale" content="fr_FR"><meta property="og:site_name" content="ça brûle">'
            '<meta property="og:title" content="%s"><meta property="og:description" content="%s"><meta property="og:url" content="%s">'
            '<meta property="og:image" content="%s/og.png"><meta name="twitter:card" content="summary_large_image">'
            "<style>%s</style>%s</head><body>"
            '<header><a class="brand" href="%s/">ça <span>brûle</span></a><a class="btn" href="%s/">Ouvrir la carte</a></header>'
            "<main>%s</main>"
            '<footer>Carte indicative construite à partir des titres de la presse en ligne : une mention n\'est pas une confirmation, vérifiez à la source. '
            '<a href="%s/">Carte des manifestations</a> · <a href="%s/feminicides">Féminicides</a> · <a href="%s/deces-police">Décès liés à la police</a> · '
            '<a href="%s/llms.txt">llms.txt</a></footer></body></html>') % (
        esc(title), esc(desc), esc(canonical), robots, ICON_LINKS, esc(title), esc(desc), esc(canonical), BASE_URL,
        PAGE_CSS, ldtag, BASE_PATH, BASE_PATH, body, BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH)


EDITOR_FOOTER = ('<br><br><strong>Édité par Quentin Closset</strong>, développeur solo à Perros-Guirec · '
                 '<a href="https://oraclemarin.fr/apropos/">À propos</a> · '
                 '<a href="mailto:quentinclosset@gmail.com">Contact</a> · '
                 '<a href="https://oraclemarin.fr/2minWebsite/rgpd/?lang=fr">Mentions légales</a> · '
                 '<a href="https://oraclemarin.fr/">Oracle Marin</a>')


def shell(*a, extra_footer="", **k):
    out = _shell(*a, **k)
    old = '<a href="%s/llms.txt">llms.txt</a></footer>' % BASE_PATH
    out = out.replace(old, '<a href="%s/sources">Sources et méthode</a> · ' % BASE_PATH + old)
    return out.replace("</footer></body>", extra_footer + "</footer></body>") if extra_footer else out


def city_page(slug_str):
    code = slug_str.rsplit("-", 1)[-1]
    c = next((x for x in FULL if x["code"] == code), None)
    if not c or slug(c) != slug_str:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,follow")
    mine = next(((arts, n) for cc, arts, n in active_cities() if cc["code"] == code), None)
    url = city_url(c, True)
    label = "%s (%s)" % (c["nom"], c["dep"]) if c.get("dep") else c["nom"]
    others = [(cc, n) for cc, arts, n in active_cities() if cc["code"] != code][:10]
    other_html = ("<h2>Autres villes où une mobilisation est signalée</h2><p>" + " · ".join(
        '<a href="%s">%s</a> <span class="meta">(%d)</span>' % (city_url(cc), esc(cc["nom"]), n) for cc, n in others) + "</p>") if others else ""
    cta = '<p><a class="btn" href="%s/?ville=%s">Voir %s sur la carte interactive</a></p>' % (BASE_PATH, code, esc(c["nom"]))
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "ça brûle", "item": BASE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Manifestations à %s" % c["nom"], "item": url}]},
        {"@type": "WebPage", "name": "Manifestations à %s aujourd'hui" % c["nom"], "url": url, "inLanguage": "fr-FR",
         "dateModified": iso_now(), "about": {"@type": "Place", "name": c["nom"], "address": {"@type": "PostalAddress", "addressCountry": "FR"},
                                             "geo": {"@type": "GeoCoordinates", "latitude": c["lat"], "longitude": c["lon"]}}}]}
    if mine:
        arts, n = mine
        items = "".join('<li><a href="%s" rel="noopener" target="_blank">%s</a> <span class="meta">— %s, %s</span></li>' % (
            esc(a["link"]), esc(a["title"]), esc(a.get("src", "")), ago_txt(a["ts"])) for a in arts[:15])
        body = ("<h1>Manifestations et mobilisations à %s aujourd'hui</h1>"
                '<p class="meta">Mis à jour %s · carte actualisée toutes les 10 minutes</p>'
                '<div class="card"><p><strong>%d article%s de presse</strong> signale%s une mobilisation (manifestation, grève, blocage ou rassemblement) '
                "à %s au cours des dernières 24 heures.</p></div><ul>%s</ul>%s%s") % (
            esc(label), time.strftime("%d/%m/%Y à %H:%M UTC", time.gmtime()), n, "s" if n > 1 else "", "nt" if n > 1 else "",
            esc(c["nom"]), items, cta, other_html)
        title = "Manifestation aujourd'hui à %s : blocages, grèves, rassemblements | ça brûle" % c["nom"]
        desc = "%d article%s de presse signale%s une mobilisation à %s (%s) ces dernières 24 h. Carte en direct des manifestations en France." % (
            n, "s" if n > 1 else "", "nt" if n > 1 else "", c["nom"], c.get("dep", ""))
        return 200, shell(title, desc, url, body, "index,follow" if indexable(c, n) else "noindex,follow", ld)
    body = ("<h1>Manifestations à %s aujourd'hui</h1><div class=\"card\"><p>Aucune manifestation, grève ou blocage n'est signalé à %s "
            "par la presse en ligne au cours des dernières 24 heures.</p></div>%s%s") % (esc(label), esc(c["nom"]), cta, other_html)
    return 200, shell("Manifestation à %s aujourd'hui | ça brûle" % c["nom"], "Aucune mobilisation signalée à %s ces dernières 24 h." % c["nom"],
                      url, body, "noindex,follow", ld)


def dataset_page(kind):
    name = "feminicides" if kind == "feminicides" else "police"
    d = dataset_payload(name)
    items = d["items"]
    year = d["year"]
    title = ("Féminicides conjugaux en France en %d : carte et décompte | ça brûle" if name == "feminicides"
             else "Décès liés à des interventions policières en France en %d : carte | ça brûle") % year
    if name == "feminicides":
        desc = ("%d féminicides conjugaux recensés en France depuis le 1er janvier %d par le collectif Féminicides par compagnons ou ex. "
                "Carte par commune et liste, actualisées une fois par jour.") % (d["total"], year)
    else:
        desc = ("%d décès survenus lors ou à la suite d'interventions des forces de l'ordre en France depuis le 1er janvier %d, "
                "recensés par Désarmons-les. Carte par commune et liste, actualisées une fois par jour.") % (d["total"], year)
    url = "%s/%s" % (BASE_URL, "feminicides" if name == "feminicides" else "deces-police")
    lis = []
    for i in items:
        dt = time.strftime("%Y-%m-%d", time.localtime(i["ts"]))
        lieu = ("%s (%s)" % (i["place"], i["dep"])) if i.get("place") else "lieu non localisé"
        txt = (i.get("age", "") + " " if i.get("age") else "") + i["text"]
        lis.append('<li><time datetime="%s">%s</time> — <strong>%s</strong> — %s <a href="%s" rel="noopener" target="_blank">source</a></li>' % (
            dt, time.strftime("%d/%m/%Y", time.localtime(i["ts"])), esc(lieu), esc(txt), esc(i["url"])))
    help_ = ('<div class="card"><strong>Besoin d\'aide ?</strong> En cas de violences conjugales : 3919 (gratuit, anonyme) ; en danger immédiat : 17 ou 112, '
             'par SMS au 114.</div>') if name == "feminicides" else ""
    body = ("<h1>%s</h1><p class=\"meta\">Source : <a href=\"%s\" rel=\"noopener\" target=\"_blank\">%s</a> · actualisé %s</p>"
            '<div class="card"><p><strong>%d cas</strong> depuis le 1er janvier %d. %s</p></div>%s'
            '<p><a class="btn" href="%s/?mode=%s">Voir la carte interactive</a></p><ul>%s</ul>') % (
        esc(title.split(" | ")[0]), esc(d["url"]), esc(d["source"]), ago_txt(d["updated"]) if d["updated"] else "—", d["total"], year,
        esc(d["note"]), help_, BASE_PATH, name, "".join(lis))
    ld = {"@context": "https://schema.org", "@type": "Dataset", "name": title.split(" | ")[0], "description": desc, "url": url,
          "inLanguage": "fr-FR", "isBasedOn": d["url"], "creator": {"@type": "Organization", "name": d["source"], "url": d["url"]},
          "temporalCoverage": "%d-01-01/%d-12-31" % (year, year), "spatialCoverage": {"@type": "Country", "name": "France"},
          "dateModified": iso_now(), "isAccessibleForFree": True}
    return 200, shell(title, desc, url, body, "index,follow", ld)


def sitemap_xml():
    now = time.strftime("%Y-%m-%d", time.gmtime())
    urls = [(BASE_URL + "/", "hourly", "1.0"), (BASE_URL + "/feminicides", "daily", "0.6"), (BASE_URL + "/deces-police", "daily", "0.6"),
            (BASE_URL + "/sources", "daily", "0.5")]
    urls += [(city_url(c, True), "hourly", "0.7") for c, arts, n in active_cities() if indexable(c, n)]
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join("<url><loc>%s</loc><lastmod>%s</lastmod><changefreq>%s</changefreq><priority>%s</priority></url>" % (esc(u), now, f, p)
                      for u, f, p in urls) + "</urlset>")



SOURCE_CHANNELS = [
    ("Flux de presse lus directement (RSS)", [
        ("ici (ex-France Bleu) : 45 radios locales", "https://www.ici.fr/"),
        ("La Dépêche", "https://www.ladepeche.fr/"), ("Sud Ouest", "https://www.sudouest.fr/"),
        ("Le Télégramme", "https://www.letelegramme.fr/"), ("Ouest-France", "https://www.ouest-france.fr/"),
        ("actu.fr", "https://actu.fr/"), ("Le Monde", "https://www.lemonde.fr/"),
        ("franceinfo", "https://www.francetvinfo.fr/"), ("20 Minutes", "https://www.20minutes.fr/"),
        ("BFMTV", "https://www.bfmtv.com/"), ("Le Figaro", "https://www.lefigaro.fr/"),
        ("L'Express", "https://www.lexpress.fr/"), ("Libération", "https://www.liberation.fr/")]),
    ("Agrégateurs d'actualités interrogés par mots-clés", [
        ("Google Actualités (recherche générale et ville par ville)", "https://news.google.com/"),
        ("Bing Actualités", "https://www.bing.com/news"),
        ("GDELT (base mondiale d'articles de presse)", "https://www.gdeltproject.org/")]),
    ("Réseaux sociaux ouverts", [
        ("Mastodon : messages publics sous les hashtags blocus, manifestation, blocage, grève, mobilisation", "https://mastodon.social/")]),
    ("Cartes complémentaires (recensements associatifs publics)", [
        ("Collectif Féminicides par compagnons ou ex", "https://www.feminicides.fr/"),
        ("Désarmons-les", "https://desarmons.net/")]),
    ("Référentiels", [
        ("Base officielle des communes (geo.api.gouv.fr)", "https://geo.api.gouv.fr/"),
        ("Fond de carte © OpenStreetMap", "https://www.openstreetmap.org/copyright")]),
]


SOURCE_CHANNELS.insert(1, ("Presse régionale (RSS)", [
    ("Le Dauphiné libéré", "https://www.ledauphine.com/"), ("Le Progrès", "https://www.leprogres.fr/"),
    ("Midi Libre", "https://www.midilibre.fr/"), ("Nice-Matin", "https://www.nicematin.com/"),
    ("L'Indépendant", "https://www.lindependant.fr/"), ("Dernières Nouvelles d'Alsace", "https://www.dna.fr/"),
    ("L'Alsace", "https://www.lalsace.fr/"), ("L'Est Républicain", "https://www.estrepublicain.fr/"),
    ("Le Républicain Lorrain", "https://www.republicain-lorrain.fr/"), ("Vosges Matin", "https://www.vosgesmatin.fr/"),
    ("Le Bien Public", "https://www.bienpublic.com/"), ("Le Journal de Saône-et-Loire", "https://www.lejsl.com/"),
    ("Charente Libre", "https://www.charentelibre.fr/"), ("France 3 Régions", "https://france3-regions.franceinfo.fr/"),
    ("Marsactu", "https://marsactu.fr/"), ("Rue89 Lyon", "https://www.rue89lyon.fr/"),
    ("Rue89 Strasbourg", "https://www.rue89strasbourg.com/"), ("Rue89 Bordeaux", "https://rue89bordeaux.com/"),
    ("Médiacités", "https://www.mediacites.fr/")]))
SOURCE_CHANNELS.insert(2, ("Presse indépendante et militante (RSS)", [
    ("Reporterre", "https://reporterre.net/"), ("Basta!", "https://basta.media/"), ("Rapports de Force", "https://rapportsdeforce.fr/"),
    ("Contre-Attaque", "https://contre-attaque.net/"), ("L'Humanité", "https://www.humanite.fr/"), ("Mediapart (titres publics)", "https://www.mediapart.fr/")]))
SOURCE_CHANNELS.insert(3, ("Médias nationaux et internationaux (RSS supplémentaires)", [
    ("RFI", "https://www.rfi.fr/"), ("France 24", "https://www.france24.com/fr/"), ("Europe 1", "https://www.europe1.fr/"),
    ("CNews", "https://www.cnews.fr/"), ("Euronews", "https://fr.euronews.com/")]))


def source_counts():
    """Médias dont au moins un article de moins de 24 h est affiché, avec le nombre d'articles."""
    cutoff = time.time() - 86400
    groups = {}
    with LOCK:
        vals = list(STORE.values())
    for v in vals:
        a = v["a"]
        if a["ts"] < cutoff:
            continue
        name = (a.get("src") or "").strip() or "Source non précisée"
        g = groups.setdefault(norm(name).replace(".fr", "").replace(".com", ""), {"n": 0, "names": {}})
        g["n"] += 1
        g["names"][name] = g["names"].get(name, 0) + 1
    out = [(max(g["names"], key=g["names"].get), g["n"]) for g in groups.values()]
    return sorted(out, key=lambda kv: (-kv[1], kv[0].lower()))


def sources_blurb():
    sc = source_counts()
    if not sc:
        return ""
    top = ", ".join("%s (%d)" % (esc(n), c) for n, c in sc[:12])
    return ("<h3>Sources agrégées</h3><p>%d médias ces dernières 24 h, dont : %s. "
            '<a href="%s/sources">Voir toutes les sources et la méthode</a>.</p>') % (len(sc), top, BASE_PATH)


def sources_page():
    sc = source_counts()
    total = sum(c for _, c in sc)
    url = BASE_URL + "/sources"
    chan = "".join("<h2>%s</h2><ul>%s</ul>" % (esc(t), "".join(
        '<li><a href="%s" rel="noopener" target="_blank">%s</a></li>' % (esc(u), esc(n)) for n, u in items)) for t, items in SOURCE_CHANNELS)
    rows = "".join("<li>%s <span class=\"meta\">— %d article%s</span></li>" % (esc(n), c, "s" if c > 1 else "") for n, c in sc[:120])
    body = (
        "<h1>Sources et méthode : d'où viennent les données de la carte</h1>"
        "<p>Par transparence, voici tout ce que « ça brûle » agrège, comment les articles sont retenus et ce que la carte ne fait pas.</p>"
        "<h2>Médias dont des articles sont affichés en ce moment</h2>"
        '<p class="meta">%d articles de %d médias sur les dernières 24 heures · mis à jour %s</p><ul>%s</ul>'
        "%s"
        "<h2>Méthode</h2>"
        "<p>Toutes les 10 minutes, la carte lit les flux de presse ci-dessus et interroge les agrégateurs. Seuls les articles dont le <strong>titre</strong> "
        "contient un terme de mobilisation sont retenus : manifestation, manifestants, grève, blocage, blocus, rassemblement, mobilisation, "
        "piquet, émeutes, heurts, occupation, barrage, syndicat… Les titres sportifs ou culturels (concert, festival, exposition, football…) sont écartés.</p>"
        "<p>Quand un visiteur cherche une ville ou zoome, la carte interroge cette commune en cascade : Google Actualités, puis Bing Actualités si Google est indisponible "
        "ou ne répond rien, puis, en dernier recours, les articles déjà collectés dans les flux de presse. Une source bloquée est mise en pause automatiquement "
        "et les autres prennent le relais.</p>"
        "<p>La commune citée dans le titre est repérée automatiquement avec la base officielle des communes. Les doublons de titres sont supprimés. "
        "Un article reste affiché 24 heures après sa publication. Chaque article est classé dans un ou plusieurs thèmes par des listes de mots-clés (sans IA).</p>"
        "<p>La carte ne conserve que le titre, la source, la date et le lien : elle ne copie pas le contenu des articles, qui restent la propriété de leurs éditeurs.</p>"
        "<h2>Ce que la carte ne fait pas</h2>"
        "<p>Elle n'utilise ni compte privé, ni messagerie, ni contenu payant contourné. Elle ne suit aucune personne et ne localise pas les forces de l'ordre. "
        "Elle ne dit pas si une mobilisation est importante ou légitime : elle indique seulement combien d'articles en parlent, et où.</p>"
        "<h2>Limites</h2>"
        "<p>La presse ne relaie pas tout, et une mention dans un titre n'est pas une confirmation. Des erreurs de localisation sont possibles, "
        "et certaines sources peuvent être momentanément indisponibles ou limiter les requêtes. Pour une information officielle, "
        "consultez la préfecture ou le ministère de l'Intérieur.</p>") % (
        total, len(sc), time.strftime("%d/%m/%Y à %H:%M UTC", time.gmtime()), rows or "<li>Aucune donnée pour l'instant.</li>", chan)
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": "Sources et méthode de la carte des manifestations", "url": url,
          "inLanguage": "fr-FR", "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"},
          "publisher": {"@type": "Person", "name": "Quentin Closset", "url": "https://oraclemarin.fr/apropos/"}}
    return 200, shell("Sources et méthode : d'où viennent les données de la carte des manifestations | ça brûle",
                      "Médias agrégés en direct, flux de presse, agrégateurs, méthode de sélection des articles et limites de la carte des manifestations en France.",
                      url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


def seo_route(h):
    path = urllib.parse.urlparse(h.path).path
    code, ctype, body = 200, "text/html; charset=utf-8", None
    if path in ("/", "/index.html"):
        body = render_index()
    elif path == "/sitemap.xml":
        body, ctype = sitemap_xml(), "application/xml; charset=utf-8"
    elif path.startswith("/ville/"):
        code, body = city_page(path[len("/ville/"):].strip("/"))
    elif path == "/sources":
        code, body = sources_page()
    elif path in ("/feminicides", "/deces-police"):
        code, body = dataset_page(path.strip("/"))
    else:
        return False
    data = body.encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "public, max-age=300")
    h.end_headers()
    h.wfile.write(data)
    return True


class H(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(HERE, "static"), **k)

    def do_GET(self):
        if seo_route(self):
            return
        if self.path.startswith("/api/area"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                s_, w_, n_, e_ = (float(q[k][0]) for k in ("south", "west", "north", "east"))
                assert n_ - s_ <= 5 and e_ - w_ <= 8
                z_ = max(0, min(18, int(float(q.get("zoom", ["9"])[0]))))
                out = area(s_, w_, n_, e_, z_)
            except Exception:
                out = {"error": "bad request"}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/search") or self.path.startswith("/api/city"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                if self.path.startswith("/api/search"):
                    out = search(q.get("q", [""])[0][:60])
                else:
                    out = city(q.get("code", [""])[0][:8])
            except Exception:
                out = {"error": "bad request"}
            body = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/dataset"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = json.dumps(dataset_payload(q.get("name", [""])[0]), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/go/"):
            gid = urllib.parse.urlparse(self.path).path[4:][:24]
            target = LINKMAP.get(gid)
            if target and target.startswith(("http://", "https://")):
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(404)
        elif self.path.startswith("/api/events"):
            slim = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("v", ["1"])[0] == "2"
            with LOCK:
                st = {**STATE, "throttled_until": BLOCK["until"] if blocked() else None}
                body = json.dumps(slim_events(st) if slim else st, ensure_ascii=False, separators=(",", ":") if slim else None).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def end_headers(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/lib/") or p.endswith((".png", ".ico", ".webmanifest")):
            self.send_header("Cache-Control", "public, max-age=604800")
        super().end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    COMMUNES.update(load_communes())
    ALL.extend({(c["nom"], c["dep"]): c for c in COMMUNES.values()}.values())
    print(len(COMMUNES), "communes indexées")
    load_store()
    threading.Thread(target=datasets_loop, daemon=True).start()
    if STORE:
        print(len(STORE), "articles rechargés depuis le disque")
        rebuild()
    threading.Thread(target=loop, daemon=True).start()
    print(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
