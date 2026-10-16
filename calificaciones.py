# -*- coding: utf-8 -*-
"""Revisa una vez por dia las calificaciones de las ONs que sigue la app, directo de las calificadoras.
Solo usa Python estandar (sin instalar nada). Lo ejecuta GitHub solo (ver .github/workflows/calificaciones.yml).

  * FIX SCR (afiliada de Fitch): tabla publica de calificaciones (ordenada por fecha) + la pagina de cada emisor.
  * Moody's Local Argentina: el Excel "Listado de Calificaciones" que publica en su pagina principal.

Compara con la ultima revision (o con el catalogo de la app la primera vez) y escribe calificaciones.json con:
  ons      -> calificacion vigente de cada ON por calificadora
  cambios  -> historial de subas / bajas detectadas
  nuevas   -> ONs nuevas calificadas en los ultimos 60 dias de emisores que sigue la app o con nota A o mejor
  estado   -> si cada fuente respondio y cuantas ONs se pudieron cruzar
Si una calificadora no responde, se conservan los datos anteriores de esa calificadora."""
import io, json, re, sys, time, unicodedata, zipfile
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

AQUI = Path(__file__).parent
SALIDA = AQUI / "calificaciones.json"
LISTADO = AQUI / "listado_calif.json"   # todas las ONs calificadas (lo usa ons_nuevas.py para calificar ONs nuevas)
NUEVOS = AQUI / "nuevos.json"           # ONs y letras nuevas cargadas por ons_nuevas.py
AVISO = AQUI / "aviso.md"               # resumen para el aviso por mail (issue de GitHub)
HOY = datetime.now(timezone(timedelta(hours=-3))).date()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
      "Accept-Language": "es-AR,es;q=0.9"}
ESCALA = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"]

# Como aparece cada emisor del catalogo en los listados (texto sin acentos, en minusculas)
EMISORES = {
    "YPF": r"\bypf\b(?! energia| luz)",
    "YPF Luz": r"ypf energia electrica|ypf luz",
    "Pampa Energía": r"\bpampa energia",
    "Telecom": r"\btelecom argentina",
    "Pan American Energy": r"pan american energy",
    "Tecpetrol": r"\btecpetrol",
    "Pluspetrol": r"\bpluspetrol",
    "Edenor": r"\bedenor\b|empresa distribuidora y comercializadora norte",
    "Vista Energy": r"\bvista (energy|oil)",
    "Genneia": r"\bgenneia",
    "IRSA": r"\birsa\b",
    "Loma Negra": r"\bloma negra",
    "Arcor": r"\barcor\b",
    "Aeropuertos Argentina 2000": r"aeropuertos argentina",
    "Capex": r"\bcapex\b",
    "Raghsa": r"\braghsa",
    "Cresud": r"\bcresud",
    "Banco Macro": r"\bbanco macro",
    "Luz de Tres Picos": r"luz de tres picos",
    "Petroquímica Comodoro Rivadavia": r"petroquimica comodoro",
    "Mastellone Hermanos": r"\bmastellone",
    "Banco Galicia": r"banco de galicia|banco galicia",
}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+\-\. ]", " ", s)).strip()


def bajar(url, binario=False, intentos=3):
    ult = None
    for _ in range(intentos):
        try:
            with urlopen(Request(url, headers=UA), timeout=40) as r:
                b = r.read()
                return b if binario else b.decode("utf-8", "replace")
        except Exception as e:  # noqa
            ult = e
            time.sleep(4)
    print("  fallo", url, ult)
    return None


ROM = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def romano(s):
    s = s.lower()
    if not s or not re.fullmatch(r"[ivxlc]+", s):
        return None
    t = 0
    for i, ch in enumerate(s):
        v = ROM[ch]
        t += -v if i + 1 < len(s) and ROM[s[i + 1]] > v else v
    return t


def formas(clase):
    """'17' -> {'17','xvii'} ; 'xix' -> {'xix','19'} ; 'a' -> {'a'}"""
    c = str(clase).strip().lower()
    f = {c}
    if c.isdigit():
        n = int(c)
        f.add(str(n))
        r = ""
        for v, sym in ((100, "c"), (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
            while n >= v:
                r += sym
                n -= v
        f.add(r)
    else:
        n = romano(c)
        if n:
            f.add(str(n))
    return f


RE_CLASE = re.compile(r"\b(?:clase|serie|cl)\.?\s*(?:n[o°]?\.?\s*)?([0-9]{1,3}|[ivxlc]{1,7}|[a-z])\b")
RE_FIX = re.compile(r"\b(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D|E)\s*\(arg\)", re.I)
RE_MOO = re.compile(r"\b(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)\.ar\b", re.I)


def clases_en(texto):
    out = set()
    for m in RE_CLASE.finditer(norm(texto)):
        out |= formas(m.group(1))
    return out


def es_on(texto):
    t = norm(texto)
    return bool(re.search(r"obligaci|\bons?\b|\bon\b", t))


def nota(c):
    m = re.match(r"(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B|CCC|CC|C|D)", str(c or "").upper())
    return ESCALA.index(m.group(1)) if m else None


STOP = {"sa", "s.a.", "s.a", "sau", "s.a.u.", "srl", "s.r.l.", "cia", "compania", "sociedad", "anonima", "de", "la", "el", "los", "las",
        "y", "e", "del", "saic", "saicf", "sacifia", "saci", "sai", "sc", "ciasa", "c.i.a.s.a.", "ltda", "inc", "ltd", "s.a.i.c.", "sca", "argentina", "arg"}


def nucleo(nombre):
    return [w for w in norm(nombre).replace(".", " ").split() if w not in STOP and len(w) > 1]


def patron_generico(nombre):
    n = nucleo(nombre)[:3]
    return r"\b" + r"\b.*\b".join(re.escape(w) for w in n) + r"\b" if n else None


def emisor_de(nombre):
    n = norm(nombre)
    for e, pat in EMISORES.items():
        if re.search(pat, n):
            return e
    return None


def fecha_iso(s):
    s = str(s or "").strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


# ---------------------------------------------------------------- catalogo de la app
def leer_catalogo():
    for nombre in ("index.html", "y_zz3_ons.js"):
        p = AQUI / nombre
        if not p.exists():
            continue
        t = p.read_text(encoding="utf-8")
        i = t.find("var ONS_CAT=[")
        if i < 0:
            continue
        blk = t[i:t.index("\n];", i)]
        filas = []
        for linea in blk.split("\n")[1:]:
            linea = linea.strip().rstrip(",")
            if linea.startswith("["):
                try:
                    filas.append(json.loads(linea))
                except Exception:
                    pass
        if filas:
            return filas
    print("No encontre el catalogo de ONs (ONS_CAT) en index.html")
    sys.exit(1)


def leer_nuevos_auto():
    """ONs que ons_nuevas.py cargo solas (para seguir su calificacion)."""
    try:
        o = json.loads(NUEVOS.read_text(encoding="utf-8"))
        return [x["fila"] for x in o.get("ons", []) if x.get("estado") == "auto" and x.get("fila")]
    except Exception:
        return []


# ---------------------------------------------------------------- lector de tablas HTML
class Tablas(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.filas, self.fila, self.celda, self.en = [], None, None, False

    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "tr":
            self.fila = []
        elif tag in ("td", "th") and self.fila is not None:
            self.celda = {"t": "", "title": "", "href": ""}
        elif self.celda is not None:
            if a.get("title") and len(a["title"]) > len(self.celda["title"]):
                self.celda["title"] = a["title"]
            if tag == "a" and a.get("href") and not self.celda["href"]:
                self.celda["href"] = a["href"]
        if self.celda is not None and tag in ("td", "th") and a.get("title"):
            self.celda["title"] = a["title"]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.celda is not None and self.fila is not None:
            c = self.celda
            c["t"] = re.sub(r"\s+", " ", c["t"]).strip()
            if c["title"] and (len(c["title"]) > len(c["t"]) or re.search(r"(\.\.\.|\u2026)$", c["t"])):
                c["full"] = c["title"]
            else:
                c["full"] = c["t"]
            self.fila.append(c)
            self.celda = None
        elif tag == "tr" and self.fila is not None:
            if self.fila:
                self.filas.append(self.fila)
            self.fila = None

    def handle_data(self, d):
        if self.celda is not None:
            self.celda["t"] += d


def tablas(html):
    p = Tablas()
    try:
        p.feed(html)
    except Exception as e:
        print("  html raro:", e)
    return p.filas


# ---------------------------------------------------------------- FIX
FIX = "https://www.fixscr.com"


def fix_grilla(paginas_max, hasta):
    """Tabla general ordenada por fecha (la mas nueva primero)."""
    filas, ids = [], {}
    for pag in range(1, paginas_max + 1):
        html = bajar(f"{FIX}/calificaciones?sort=-national_rating_date&page={pag}&per-page=70")
        if not html:
            break
        nuevas = 0
        vieja = False
        for f in tablas(html):
            if len(f) < 9:
                continue
            fecha = fecha_iso(f[1]["t"])
            if not fecha:
                continue
            reg = {"ent": f[0]["full"], "fecha": fecha, "area": f[3]["t"], "tipo": f[5]["full"],
                   "cp": f[6]["t"], "lp": f[7]["t"], "persp": f[8]["t"], "accion": f[9]["t"] if len(f) > 9 else ""}
            m = re.search(r"id=(\d+)", f[0]["href"] or f[5]["href"] or "")
            e = emisor_de(reg["ent"])
            if m and e:
                ids[e] = m.group(1)
            reg["emisor"] = e
            filas.append(reg)
            nuevas += 1
            if fecha < hasta:
                vieja = True
        print(f"  FIX pagina {pag}: {nuevas} filas")
        if nuevas == 0 or vieja:
            break
        time.sleep(1.5)
    return filas, ids


def fix_emisor(eid):
    html = bajar(f"{FIX}/emisor/view?type=emisor&id={eid}")
    if not html:
        return None
    out = []
    for f in tablas(html):
        txt = " | ".join(c["full"] for c in f)
        m = RE_FIX.search(txt)
        if not m or not es_on(txt):
            continue
        out.append({"txt": txt, "cal": m.group(0).replace(" ", ""), "fecha": fecha_iso(txt)})
    return out


# ---------------------------------------------------------------- Moody's Local
MOO = "https://moodyslocal.com.ar"


def xlsx_filas(b):
    z = zipfile.ZipFile(io.BytesIO(b))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    comp = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            comp.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    filas = []
    for nombre in sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n)):
        for row in ET.fromstring(z.read(nombre)).iter("{%s}row" % ns["m"]):
            fila = {}
            for c in row.findall("m:c", ns):
                ref = re.match(r"([A-Z]+)", c.get("r", "A")).group(1)
                col = 0
                for ch in ref:
                    col = col * 26 + ord(ch) - 64
                v = c.find("m:v", ns)
                tipo = c.get("t")
                if tipo == "s" and v is not None:
                    val = comp[int(v.text)]
                elif tipo == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter("{%s}t" % ns["m"]))
                else:
                    val = v.text if v is not None else ""
                    if val and re.fullmatch(r"4\d{4}(\.0+)?", val):  # fecha de Excel
                        val = (date(1899, 12, 30) + timedelta(days=int(float(val)))).isoformat()
                fila[col - 1] = (val or "").strip()
            if fila:
                n = max(fila) + 1
                filas.append([fila.get(i, "") for i in range(n)])
    return filas


def moodys():
    home = bajar(MOO + "/")
    if not home:
        return None, None
    urls = re.findall(r"https?://moodyslocal\.com\.ar/wp-content/uploads/[^\"'\s]+?MOODYS_LOCAL_ARGENTINA_\d{8}_\d+\.xlsx", home)
    if not urls:
        print("  Moody's: no encontre el link al Excel en la pagina principal")
        return None, None
    url = max(set(urls), key=lambda u: re.search(r"_(\d{8})_", u).group(1))
    print("  Moody's:", url)
    b = bajar(url, binario=True)
    if not b:
        return None, None
    try:
        filas = xlsx_filas(b)
    except Exception as e:
        print("  Moody's: no pude leer el Excel:", e)
        return None, None
    # encabezado: la fila que menciona emisor/entidad y calificacion
    cab, idx = None, {}
    for i, f in enumerate(filas[:40]):
        t = [norm(x) for x in f]
        if any("calific" in x for x in t) and any(re.search(r"emisor|entidad|razon|compan", x) for x in t):
            cab = i
            for j, x in enumerate(t):
                if re.search(r"emisor|entidad|razon|compan", x) and "ent" not in idx:
                    idx["ent"] = j
                elif re.search(r"instrument|clase|serie|titulo|tipo", x) and "ins" not in idx:
                    idx["ins"] = j
                elif "perspect" in x and "persp" not in idx:
                    idx["persp"] = j
                elif "fecha" in x and "fecha" not in idx:
                    idx["fecha"] = j
            break
    out = []
    for f in filas[(cab + 1 if cab is not None else 0):]:
        txt = " | ".join(f)
        m = RE_MOO.search(txt)
        if not m:
            continue
        ent = f[idx["ent"]] if "ent" in idx and idx["ent"] < len(f) else txt
        ins = f[idx["ins"]] if "ins" in idx and idx["ins"] < len(f) else txt
        out.append({"ent": ent, "ins": ins, "txt": txt, "cal": m.group(0),
                    "persp": f[idx["persp"]] if "persp" in idx and idx["persp"] < len(f) else "",
                    "fecha": (fecha_iso(f[idx["fecha"]]) if "fecha" in idx and idx["fecha"] < len(f) else None) or fecha_iso(txt)})
    fecha_lista = re.search(r"_(\d{4})(\d{2})(\d{2})_", url)
    return out, "-".join(fecha_lista.groups()) if fecha_lista else None


# ---------------------------------------------------------------- cruce con el catalogo
def cruzar(cat, filas, campo_ent, campo_ins):
    """Devuelve {ticker: fila} cuando coinciden emisor y numero de clase."""
    res = {}
    for f in filas:
        ent = f.get(campo_ent) or ""
        e = emisor_de(ent) or (emisor_de(f.get("txt", "")) if campo_ent == "txt" else None)
        if not e:
            continue
        ins = f.get(campo_ins) or f.get("txt") or ""
        if not es_on(ins + " " + f.get("txt", "")):
            continue
        cl = clases_en(ins) or clases_en(f.get("txt", ""))
        if not cl:
            continue
        for c in cat:
            if c[1] == e and formas(c[2]) & cl:
                prev = res.get(c[0])
                if not prev or (f.get("fecha") or "") >= (prev.get("fecha") or ""):
                    res[c[0]] = f
    return res


def main():
    cat = leer_catalogo()
    ya = {c[0] for c in cat}
    cat += [f for f in leer_nuevos_auto() if f[0] not in ya]
    for c in cat:  # emisores nuevos: patron armado con su nombre
        if c[1] not in EMISORES and patron_generico(c[1]):
            EMISORES[c[1]] = patron_generico(c[1])
    previo = {}
    if SALIDA.exists():
        try:
            previo = json.loads(SALIDA.read_text(encoding="utf-8"))
        except Exception:
            previo = {}
    ons_prev = previo.get("ons") or {}
    ons = {c[0]: dict(ons_prev.get(c[0]) or {"fix": c[8], "moo": c[9], "base": True}) for c in cat}
    estado = {}
    primera = not previo.get("fix_ids")

    # ---- FIX
    hasta = (HOY - timedelta(days=60)).isoformat()
    grilla, ids = fix_grilla(40 if primera else 4, "0000-00-00" if primera else hasta)
    ids = {**(previo.get("fix_ids") or {}), **ids}
    fix_filas = [dict(g, txt=g["tipo"]) for g in grilla]
    for e, eid in sorted(ids.items()):
        if not any(c[1] == e for c in cat):
            continue
        fe = fix_emisor(eid)
        if fe:
            for r in fe:
                fix_filas.append({"ent": e, "tipo": r["txt"], "txt": r["txt"], "lp": r["cal"], "fecha": r["fecha"], "accion": "", "persp": ""})
        time.sleep(1)
    m_fix = cruzar(cat, [dict(f, ent=f["ent"]) for f in fix_filas], "ent", "tipo")
    for t, f in m_fix.items():
        cal = (RE_FIX.search(f.get("lp") or "") or RE_FIX.search(f.get("txt") or ""))
        if cal:
            ons[t].update(fix=cal.group(0).replace(" ", ""), fixFecha=f.get("fecha"), fixPersp=f.get("persp") or ons[t].get("fixPersp"),
                          fixAccion=f.get("accion") or ons[t].get("fixAccion"))
            ons[t].pop("base", None)
    con_fix = [c[0] for c in cat if c[8]]
    estado["fix"] = {"ok": bool(grilla), "filas": len(fix_filas), "cruzadas": len([t for t in m_fix if t in con_fix]), "con_nota": len(con_fix)}
    print("FIX:", estado["fix"])

    # ---- Moody's Local
    mfilas, mfecha = moodys()
    if mfilas:
        m_moo = cruzar(cat, mfilas, "ent", "ins")
        for t, f in m_moo.items():
            ons[t].update(moo=f["cal"], mooFecha=f.get("fecha") or mfecha, mooPersp=f.get("persp"))
            ons[t].pop("base", None)
        con_moo = [c[0] for c in cat if c[9]]
        estado["moodys"] = {"ok": True, "filas": len(mfilas), "cruzadas": len([t for t in m_moo if t in con_moo]), "con_nota": len(con_moo), "listado": mfecha}
    else:
        estado["moodys"] = {"ok": False}
    print("Moody's:", estado["moodys"])

    # ---- cambios contra la revision anterior (o contra el catalogo)
    cambios = list(previo.get("cambios") or [])
    n0 = len(cambios)
    for c in cat:
        t = c[0]
        antes = ons_prev.get(t) or {"fix": c[8], "moo": c[9]}
        for k, ag in (("fix", "FIX"), ("moo", "Moody's Local")):
            a, n = (antes.get(k) or "").strip(), (ons[t].get(k) or "").strip()
            if a and n and a.upper() != n.upper():
                na, nn = nota(a), nota(n)
                cambios.insert(0, {"det": HOY.isoformat(), "t": t, "emi": c[1], "cl": c[2], "ag": ag, "antes": a, "ahora": n,
                                   "dir": "sube" if (na is not None and nn is not None and nn < na) else "baja" if (na is not None and nn is not None and nn > na) else "cambia",
                                   "fecha": ons[t].get("fixFecha" if k == "fix" else "mooFecha")})
    hoy_c = cambios[:len(cambios) - n0]
    cambios = cambios[:80]

    # ---- ONs nuevas calificadas (posibles licitaciones)
    conocidas = {(c[1], f) for c in cat for f in formas(c[2])}
    nuevas = []
    vistos = set()
    for g in grilla:
        if g["fecha"] < hasta or not es_on(g["tipo"]):
            continue
        if not re.search(r"asign|nueva|inicia", norm(g["accion"])):
            continue
        n = nota(re.sub(r"\(arg\)", "", g["lp"]))
        if not (g["emisor"] or (n is not None and n <= ESCALA.index("A"))):
            continue
        cl = clases_en(g["tipo"])
        if g["emisor"] and cl and any((g["emisor"], x) in conocidas for x in cl):
            continue
        k = (g["ent"], g["tipo"])
        if k in vistos:
            continue
        vistos.add(k)
        nuevas.append({"ent": g["ent"], "ins": g["tipo"], "cal": g["lp"] or g["cp"], "fecha": g["fecha"], "ag": "FIX", "sigue": bool(g["emisor"])})
    nuevas.sort(key=lambda x: x["fecha"], reverse=True)

    # emisores con acciones recientes cuya clase no se pudo leer (para revisar a mano)
    revisar = []
    for g in grilla:
        if g["emisor"] and g["fecha"] >= hasta and es_on(g["tipo"]) and not clases_en(g["tipo"]):
            revisar.append({"ent": g["ent"], "ins": g["tipo"], "cal": g["lp"] or g["cp"], "fecha": g["fecha"], "accion": g["accion"]})

    # ---- listado de todas las ONs calificadas (acumulado: FIX solo trae lo ultimo cada dia)
    try:
        lst = json.loads(LISTADO.read_text(encoding="utf-8"))
    except Exception:
        lst = {"filas": []}
    filas_l = {(r["ag"], r["ent"], r["ins"]): r for r in lst.get("filas", []) if not (r["ag"] == "Moody's Local" and mfilas)}
    for f in fix_filas:
        if not es_on(f.get("tipo") or "") or not RE_FIX.search(f.get("lp") or ""):
            continue
        k = ("FIX", f["ent"], f["tipo"])
        if k not in filas_l or (f.get("fecha") or "") >= (filas_l[k].get("fecha") or ""):
            filas_l[k] = {"ag": "FIX", "ent": f["ent"], "ins": f["tipo"], "cal": RE_FIX.search(f["lp"]).group(0).replace(" ", ""), "fecha": f.get("fecha")}
    for f in (mfilas or []):
        if es_on(f["ins"] + " " + f["txt"]):
            filas_l[("Moody's Local", f["ent"], f["ins"])] = {"ag": "Moody's Local", "ent": f["ent"], "ins": f["ins"], "cal": f["cal"], "fecha": f.get("fecha")}
    LISTADO.write_text(json.dumps({"actualizado": HOY.isoformat(), "filas": list(filas_l.values())}, ensure_ascii=False), encoding="utf-8")

    if hoy_c:
        with AVISO.open("a", encoding="utf-8") as fa:
            fa.write("## Cambios de calificación\n\n" + "\n".join(
                f"- **{c['t']}D** ({c['emi']} clase {c['cl']}): {c['ag']} {'▲ sube' if c['dir'] == 'sube' else '▼ baja' if c['dir'] == 'baja' else 'cambia'} de {c['antes']} a **{c['ahora']}**" for c in hoy_c) + "\n\n")

    sin = [c[0] for c in cat if (ons[c[0]].get("base"))]
    out = {"actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fecha": HOY.isoformat(),
           "estado": estado, "ons": ons, "cambios": cambios, "nuevas": nuevas[:40], "revisar": revisar[:30],
           "sin_cruce": sin, "fix_ids": ids}
    if not estado["fix"]["ok"] and not estado["moodys"].get("ok"):
        if previo:
            print("Ninguna calificadora respondio: no se toca calificaciones.json")
            return
        out["aviso"] = "Ninguna calificadora respondio en la primera revision; se usan las del catalogo."
    SALIDA.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Listo: {len(cambios)} cambios en historial, {len(nuevas)} nuevas, {len(sin)} ONs sin cruzar.")


if __name__ == "__main__":
    main()
