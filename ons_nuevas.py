# -*- coding: utf-8 -*-
"""Detecta ONs en dolares y letras a tasa fija (LECAP/BONCAP) nuevas y arma solo su cronograma.
Solo usa Python estandar. Lo ejecuta GitHub despues de calificaciones.py (ver .github/workflows/calificaciones.yml).

Fuente: datos abiertos de BYMA (sin clave):
  * panel de ONs y de bonos publicos -> que tickers cotizan
  * ficha tecnica de cada especie -> emisor, denominacion (clase), ley, moneda, emision, vencimiento,
    y el TEXTO oficial de intereses y de amortizacion, que se interpreta aca.

Antes de cargar nada, el lector se pone a prueba con lo que la app ya tiene cargado y verificado:
  * lee las fichas de las ONs del catalogo y compara tasa, frecuencia, vencimiento y cronograma;
  * calcula el pago final de las letras ya cargadas y lo compara con el real.
Solo si acierta en la gran mayoria de los casos, las ONs nuevas (calificadas A o mejor) y las letras
entran solas a la app ("auto"); el resto queda "pendiente" con el motivo, para revisar a mano.

Escribe nuevos.json (lo lee la app) y fichas_byma.json (cache de fichas, para no pedirlas todos los dias)."""
import json, re, ssl, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from calificaciones import (AQUI, AVISO, EMISORES, ESCALA, HOY, clases_en, emisor_de, formas, leer_catalogo, nota, norm,
                            nucleo, romano)

BYMA = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free"
SALIDA = AQUI / "nuevos.json"
CACHE = AQUI / "fichas_byma.json"
LISTADO = AQUI / "listado_calif.json"
SSL_BYMA = ssl._create_unverified_context()  # BYMA usa un certificado que Python no reconoce; son datos publicos de solo lectura
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json",
       "Content-Type": "application/json"}
MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9,
         "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
NUM = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
       "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "veinte": 20}


def post(ruta, cuerpo, intentos=2):
    ult = None
    for _ in range(intentos):
        try:
            req = Request(f"{BYMA}/{ruta}", data=json.dumps(cuerpo).encode(), headers=HDR, method="POST")
            with urlopen(req, timeout=40, context=SSL_BYMA) as r:
                return json.load(r)
        except Exception as e:  # noqa
            ult = e
            time.sleep(3)
    print("  fallo BYMA", ruta, cuerpo, ult)
    return None


def lista(o):
    if isinstance(o, list):
        return o
    if isinstance(o, dict):
        return o.get("data") or []
    return []


# ---------------------------------------------------------------- fechas
def bajo(s):
    """minusculas y sin acentos, pero conserva %, comas y barras (norm() las borra)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s)


def ymd(s):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def fechas_texto(t):
    """Todas las fechas del texto, en orden de aparicion: (posicion, date)."""
    t = bajo(t)
    out = []
    for m in re.finditer(r"(\d{1,2})\s+de\s+([a-z]+)\s+(?:de|del)\s+(\d{4})", t):
        if m.group(2) in MESES:
            try:
                out.append((m.start(), date(int(m.group(3)), MESES[m.group(2)], int(m.group(1)))))
            except ValueError:
                pass
    for m in re.finditer(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b", t):
        try:
            out.append((m.start(), date(int(m.group(3)), int(m.group(2)), int(m.group(1)))))
        except ValueError:
            pass
    return sorted(out)


def mas_meses(d, m):
    y, mo = divmod(d.month - 1 + m, 12)
    y += d.year
    import calendar
    return date(y, mo + 1, min(d.day, calendar.monthrange(y, mo + 1)[1]))


def cupones(vto, fr, desde):
    """Fechas de pago armadas hacia atras desde el vencimiento (igual que la app)."""
    paso, out, k = 12 // fr, [vto], 1
    while True:
        f = mas_meses(vto, -paso * k)
        if f <= desde:
            break
        out.insert(0, f)
        k += 1
    return out


def pct(s):
    return float(s.replace(".", "").replace(",", ".")) if re.fullmatch(r"\d{1,3}(\.\d{3})*(,\d+)?", s) else float(s.replace(",", "."))


# ---------------------------------------------------------------- lector de la ficha (ON)
def leer_interes(txt):
    t = bajo(txt)
    if re.search(r"variable|badlar|sofr|tamar|\bcer\b|\buva\b|margen", t):
        return None, None, "tasa variable o ajustable"
    tasas = sorted({round(pct(m.group(1)), 4) for m in re.finditer(r"(\d{1,2}(?:[.,]\d{1,8})?)\s*%", t)})
    tasas = [x for x in tasas if 0 < x < 30]
    if not tasas:
        return None, None, "no se encontro la tasa"
    if len(tasas) > 1:
        return None, None, "tasa escalonada (" + ", ".join(f"{x}%" for x in tasas) + ")"
    fr = None
    if re.search(r"trimestral", t):
        fr = 4
    elif re.search(r"semestral", t):
        fr = 2
    elif re.search(r"mensual", t):
        fr = 12
    elif re.search(r"anualmente|en forma anual|pagaderos? anual|una vez al ano", t):
        fr = 1
    else:
        dias = {(d.day, d.month) for _, d in fechas_texto(t)}
        n = len({m for _, m in dias})
        fr = {1: 1, 2: 2, 4: 4, 12: 12}.get(n)
    if not fr:
        return tasas[0] / 100, None, "no se encontro cada cuanto paga"
    return tasas[0] / 100, fr, None


def leer_amort(txt, vto, emision, fr):
    t = bajo(txt)
    fechas = fechas_texto(txt)
    # 1) porcentajes con fecha ("33% el 18 de julio de 2029")
    pares = []
    for m in re.finditer(r"(\d{1,3}(?:[.,]\d{1,8})?)\s*%", t):
        sig = [f for p, f in fechas if m.end() <= p <= m.end() + 90]
        ant = [f for p, f in fechas if m.start() - 90 <= p < m.start()]
        if sig:
            pares.append((sig[0], pct(m.group(1))))
        elif ant:
            pares.append((ant[-1], pct(m.group(1))))
    pares = sorted(set(pares))
    if pares and abs(sum(p for _, p in pares) - 100) < 0.2:
        return pares, None
    # 2) bullet
    if not re.search(r"cuotas", t) and re.search(r"vencimiento|unico pago|un solo pago|integramente|totalidad", t):
        return "B", None
    # 3) N cuotas iguales
    m = re.search(r"(\d{1,2}|[a-z]+)\s*(?:\((\d{1,2})\))?\s+cuotas", t)
    if m:
        n = int(m.group(2)) if m.group(2) else (int(m.group(1)) if m.group(1).isdigit() else NUM.get(m.group(1)))
        if n and re.search(r"iguales|igual", t):
            paso = 12 if re.search(r"anual", t) and not re.search(r"semestral|trimestral", t) else 3 if re.search(r"trimestral", t) else 6 if re.search(r"semestral", t) else (12 // fr if fr else None)
            if paso:
                ds = [vto]
                while len(ds) < n:
                    ds.insert(0, mas_meses(vto, -paso * len(ds)))
                if not fechas or any(abs((d - ds[0]).days) < 5 for _, d in fechas):
                    return [(d, round(100 / n, 4)) for d in ds], None
                return None, f"{n} cuotas iguales pero no coincide la primera fecha"
        return None, "amortiza en cuotas y no se pudo leer el cronograma"
    return None, "no se pudo leer la amortizacion"


def a_fila(f, sym, emisor_app, clase, ley, fix="", moo=""):
    """ficha -> fila del catalogo de la app, o (None, motivo)."""
    vto, emi = ymd(f.get("fechaVencimiento")), ymd(f.get("fechaEmision"))
    if not vto:
        return None, "sin fecha de vencimiento"
    tasa, fr, mot = leer_interes(f.get("interes") or "")
    if mot:
        return None, mot
    am, mot = leer_amort(f.get("formaAmortizacion") or "", vto, emi, fr)
    if mot:
        return None, mot
    if am != "B":
        # cada cuota tiene que caer en una fecha de pago de intereses (armadas desde el vencimiento)
        cs = cupones(vto, fr, (emi or HOY) - timedelta(days=400))
        ajust = []
        for d, p in am:
            c = min(cs, key=lambda x: abs((x - d).days))
            if abs((c - d).days) > 20:
                return None, f"la cuota del {d.isoformat()} no coincide con una fecha de pago"
            ajust.append([("2000-01-01" if c <= HOY else c.isoformat()), p])
        jun = {}
        for d, p in ajust:
            jun[d] = round(jun.get(d, 0) + p, 4)
        am = [[d, p] for d, p in sorted(jun.items())]
        if len(am) == 1 and am[0][0] == vto.isoformat():
            am = "B"
    return [sym, emisor_app, clase, ley, vto.isoformat(), tasa, fr, am, fix, moo], None


def ley_app(f):
    l = norm(f.get("ley") or "") + " " + norm(f.get("paisLey") or "")
    return "NY" if re.search(r"nueva york|new york|extranjer|inglaterra|ingles", l) else "ARG"


def clase_de(denom):
    m = re.search(r"\b(?:clase|serie)\s+([0-9]{1,3}|[ivxlc]{1,7}|[a-z])\b", norm(denom))
    if not m:
        return None
    tok = m.group(1)
    if tok.isdigit():
        return str(int(tok))
    r = romano(tok)
    return str(r) if (r and len(tok) > 1) else tok.upper()


def nombre_emisor(e):
    k = emisor_de(e)
    if k:
        return k
    limpio = re.sub(r"\b(s\.?\s?a\.?\s?u?\.?|s\.?r\.?l\.?|s\.?a\.?i\.?c\.?.*)$", "", e.strip(), flags=re.I).strip(" ,.")
    return limpio.title() if limpio.isupper() else limpio


def calificar(emisor_byma, clase, listado):
    """Mejor nota de FIX y de Moody's para ese emisor y clase en el listado de las calificadoras."""
    nb = set(nucleo(emisor_byma))
    k = emisor_de(emisor_byma)
    mejor = {}
    for r in listado:
        ne = set(nucleo(r["ent"]))
        mismo = (k and emisor_de(r["ent"]) == k) or (nb and ne and (nb <= ne or ne <= nb))
        if not mismo or not (formas(clase) & clases_en(r["ins"])):
            continue
        ag = "fix" if r["ag"] == "FIX" else "moo"
        if ag not in mejor or (r.get("fecha") or "") >= (mejor[ag].get("fecha") or ""):
            mejor[ag] = r
    return (mejor.get("fix") or {}).get("cal", ""), (mejor.get("moo") or {}).get("cal", "")


def iguales(a, b):
    """Compara la fila leida de BYMA con la del catalogo."""
    if abs(a[5] - b[5]) > 0.0006 or a[6] != b[6] or a[4] != b[4]:
        return False
    if a[7] == "B" or b[7] == "B":
        return a[7] == b[7]
    norm_am = lambda am: sorted((d if d > HOY.isoformat() else "2000-01-01", round(p, 1)) for d, p in am)
    fa, fb = {}, {}
    for d, p in norm_am(a[7]):
        fa[d] = round(fa.get(d, 0) + p, 1)
    for d, p in norm_am(b[7]):
        fb[d] = round(fb.get(d, 0) + p, 1)
    return all(abs(fa.get(k, 0) - fb.get(k, 0)) <= 0.2 for k in set(fa) | set(fb))


# ---------------------------------------------------------------- letras
def meses_entre(a, b):
    m = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day < a.day:
        m -= 1
    base = mas_meses(a, m)
    return m + (b - base).days / max(1, (mas_meses(a, m + 1) - base).days)


METODOS = {
    "meses_calendario": lambda e, v, tem: 100 * (1 + tem) ** meses_entre(e, v),
    "dias_30": lambda e, v, tem: 100 * (1 + tem) ** ((v - e).days / 30),
    "dias_365_12": lambda e, v, tem: 100 * (1 + tem) ** ((v - e).days * 12 / 365),
}


def tem_de(txt):
    t = bajo(txt)
    ps = [pct(m.group(1)) for m in re.finditer(r"(\d{1,2}(?:[.,]\d{1,6})?)\s*%", t)]
    ps = [p for p in ps if 0.1 < p < 15]
    return ps[0] / 100 if ps else None


def leer_letras_ref():
    t = (AQUI / "index.html").read_text(encoding="utf-8")
    return {m.group(1): (m.group(2), float(m.group(3))) for m in
            re.finditer(r'\{"t":"([ST]\d{1,2}[A-Z]\d)","vto":"([\d-]+)","pago":([\d.]+)\}', t)}


# ---------------------------------------------------------------- bonos soberanos y BOPREAL en dolares
def leer_ref():
    t = (AQUI / "index.html").read_text(encoding="utf-8")
    i = t.find("REF_EMBEBIDA = ")
    if i < 0:
        return {}
    try:
        return json.JSONDecoder().raw_decode(t[i + len("REF_EMBEBIDA = "):])[0]
    except Exception:
        return {}


def d360(a, b):
    """dias 30/360 (europeo)"""
    return (b.year - a.year) * 360 + (b.month - a.month) * 30 + min(b.day, 30) - min(a.day, 30)


def tramos_tasa(txt):
    """[(desde, tasa)] : un tramo por cada parte del texto con un % y una fecha (step-up); o [(None, tasa)] si es fija."""
    t = bajo(txt)
    if re.search(r"variable|badlar|sofr|tamar|\bcer\b|\buva\b|margen|ajustad", t):
        return None, "tasa variable o ajustable"
    partes = re.split(r";|\n|\b[ivx]{1,4}\.\s|\b[a-h]\)\s", t)
    tr = []
    for pz in partes:
        ps = [pct(m.group(1)) for m in re.finditer(r"(\d{1,2}(?:[.,]\d{1,8})?)\s*%", pz)]
        ps = [x for x in ps if 0 < x < 30]
        fs = fechas_texto(pz)
        if len(set(ps)) == 1 and fs:
            tr.append((fs[0][1], ps[0] / 100))
    if len(tr) >= 2:
        return sorted(tr), None
    tasas = sorted({x for x in (pct(m.group(1)) for m in re.finditer(r"(\d{1,2}(?:[.,]\d{1,8})?)\s*%", t)) if 0 < x < 30})
    if len(tasas) == 1:
        return [(None, tasas[0] / 100)], None
    return None, ("no se encontro la tasa" if not tasas else "tasas que no se pudieron ordenar por fecha")


def frec(t):
    t = bajo(t)
    for pat, fr in ((r"mensual", 12), (r"trimestral", 4), (r"semestral", 2), (r"anualmente|en forma anual|una vez al ano", 1)):
        if re.search(pat, t):
            return fr
    return None


def flujos_bono(f):
    """ficha -> (flujos [[fecha,total,interes,residual,cupon]], motivo)"""
    vto = ymd(f.get("fechaVencimiento"))
    dev = ymd(f.get("fechaDevenganIntereses")) or ymd(f.get("fechaEmision"))
    if not vto or not dev:
        return None, "sin fechas de emision o vencimiento"
    tr, mot = tramos_tasa(f.get("interes") or "")
    if mot:
        return None, mot
    fr = frec(f.get("interes") or "") or frec(f.get("formaAmortizacion") or "")
    if not fr:
        return None, "no se encontro cada cuanto paga"
    paso = 12 // fr
    # primera fecha de pago: la primera fecha del texto de intereses posterior al devengamiento que no sea un cambio de tasa
    cand = [d for _, d in fechas_texto(f.get("interes") or "") if dev < d < vto and d not in {x for x, _ in tr if x}]
    fechas, stub_final = [], False
    if cand:
        ancla = min(cand)
        import calendar
        fin_mes = ancla.day == calendar.monthrange(ancla.year, ancla.month)[1]
        def en(k):
            d = mas_meses(ancla, paso * k)
            return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1]) if fin_mes else d
        k = 0
        while True:
            d = en(k)
            if (vto - d).days <= 5:
                break
            fechas.append(d)
            k += 1
        stub_final = vto != en(k)
        fechas.append(vto)
    else:
        fechas = [d for d in cupones(vto, fr, dev)]
    am, mot = leer_amort(f.get("formaAmortizacion") or "", vto, dev, fr)
    if mot:
        return None, mot
    am = [(vto, 100.0)] if am == "B" else am
    amd = {}
    for d, pz in am:
        c = min(fechas, key=lambda x: abs((x - d).days))
        if abs((c - d).days) > 20:
            return None, f"la cuota del {d.isoformat()} no coincide con una fecha de pago"
        amd[c] = amd.get(c, 0) + pz
    if abs(sum(amd.values()) - 100) > 0.2:
        return None, "las cuotas no suman 100%"
    tasa = lambda d: [r for x, r in tr if x is None or x <= d][-1] if [r for x, r in tr if x is None or x <= d] else tr[0][1]
    res, prev = 100.0, dev
    out = [[dev.isoformat(), 0.0, 0.0, 100.0, tasa(dev)]]  # fila inicial: desde aca corre el interes (la app la usa para el interes corrido)
    for d in fechas:
        r = tasa(prev)
        stub = prev == dev or (d == vto and stub_final)
        i = r * res * d360(prev, d) / 360 if stub else r * res / fr
        a = amd.get(d, 0) * 1.0
        out.append([d.isoformat(), round(i + a, 8), round(i, 8), round(res, 6), r])
        res -= a
        prev = d
    return out, None


def flujos_iguales(a, b):
    """mismos pagos futuros: fechas a menos de 4 dias (feriados y fines de semana) y montos a menos de 0,03"""
    hoy = HOY.isoformat()
    fa = [(ymd(x[0]), x[1]) for x in a if x[0] > hoy]
    fb = [(ymd(x[0]), x[1]) for x in b if x[0] > hoy]
    if len(fa) != len(fb):
        return False
    # se compara el acumulado: si un pago se corre por un feriado, el interes pasa de un cupon al siguiente
    ca = cb = 0
    for (da, ma), (db, mb) in zip(fa, fb):
        ca += ma
        cb += mb
        if abs((da - db).days) > 4 or abs(ca - cb) > 0.1:
            return False
    return True


TIPO_PUB = [(r"cer|ajustable", "CER"), (r"linked|dolar estadounidense al tipo de cambio|comunicacion a ?3500", "Dólar linked"),
            (r"tamar", "TAMAR"), (r"dual", "Dual")]


# ---------------------------------------------------------------- principal
def main():
    cat = leer_catalogo()
    en_cat = {c[0] for c in cat}
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    try:
        previo = json.loads(SALIDA.read_text(encoding="utf-8"))
    except Exception:
        previo = {}
    try:
        listado = json.loads(LISTADO.read_text(encoding="utf-8")).get("filas", [])
    except Exception:
        listado = []

    def ficha(sym, refrescar=False):
        if sym in cache and not refrescar and cache[sym].get("_f", "") >= (HOY - timedelta(days=120)).isoformat():
            return cache[sym]
        d = lista(post("bnown/fichatecnica/especies/general", {"symbol": sym}))
        time.sleep(0.4)
        if d:
            cache[sym] = dict(d[0], _f=HOY.isoformat())
            return cache[sym]
        return None

    # 1) que cotiza
    pan_on = lista(post("negociable-obligations", {"page_size": 5000}))
    pan_pb = lista(post("public-bonds", {"page_size": 3000}))
    if not pan_on and not pan_pb:
        print("BYMA no respondio: no se toca nuevos.json")
        CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        return
    print(f"BYMA: {len(pan_on)} filas de ONs, {len(pan_pb)} de bonos publicos")

    # 2) prueba del lector con el catalogo
    ok = tot = 0
    difs = []
    for c in cat:
        f = ficha(c[0] + "D")
        if not f:
            continue
        fila, mot = a_fila(f, c[0], c[1], c[2], c[3])
        tot += 1
        if fila and iguales(fila, c):
            ok += 1
        else:
            difs.append({"t": c[0], "motivo": mot or "datos distintos", "leido": fila and fila[4:8], "catalogo": c[4:8]})
    tasa_ok = ok / tot if tot else 0
    modo_auto_on = tot >= 20 and tasa_ok >= 0.8
    print(f"Prueba del lector de ONs: {ok}/{tot} coinciden con el catalogo -> {'automatico' if modo_auto_on else 'solo pendientes'}")

    # 3) ONs nuevas en dolares
    hoy_iso = HOY.isoformat()
    primera = not previo.get("ons") and not previo.get("letras")
    alta = "2000-01-01" if primera else hoy_iso  # en la primera revision todo lo que ya cotizaba no se marca como "nueva"
    sims = sorted({r.get("symbol") for r in pan_on if str(r.get("symbol", "")).endswith("D")
                   and (r.get("maturityDate") or "9999") > (HOY + timedelta(days=60)).isoformat()})
    prev_ons = {x["t"]: x for x in previo.get("ons", [])}
    ons = []
    for s in sims:
        base = s[:-1]
        if base in en_cat or not re.fullmatch(r"[A-Z0-9]{3,5}", base):
            continue
        f = ficha(s)
        if not f or "dolar" not in norm(f.get("moneda")) or "linked" in norm(f.get("moneda")):
            continue
        if not re.search(r"obligac", norm((f.get("tipoEspecie") or "") + " " + (f.get("denominacion") or ""))):
            continue
        emi_b = f.get("emisor") or ""
        clase = clase_de(f.get("denominacion") or "")
        sigue = bool(emisor_de(emi_b))
        fix, moo = calificar(emi_b, clase, listado) if clase else ("", "")
        notas = [n for n in (nota(fix.replace("(arg)", "")), nota(moo.replace(".ar", ""))) if n is not None]
        mejor = min(notas) if notas else None
        if not sigue and (mejor is None or mejor > ESCALA.index("A")):
            continue  # fuera del universo de la app (A o mejor)
        fila, mot = a_fila(f, base, nombre_emisor(emi_b), clase or "?", ley_app(f), fix, moo)
        if not mot and not clase:
            mot = "no se encontro el numero de clase"
        if not mot and mejor is None:
            mot = "todavia sin calificacion en los listados (se carga cuando aparezca)"
        if not mot and mejor > ESCALA.index("A"):
            mot = "calificacion menor a A"
        if not mot and not modo_auto_on:
            mot = "el lector todavia no paso la prueba con el catalogo"
        estado = "auto" if not mot else "pendiente"
        p = prev_ons.get(base, {})
        ons.append({"t": base, "estado": estado, "motivo": mot, "fila": fila, "desde": p.get("desde") or alta,
                    "ficha": {k: f.get(k) for k in ("denominacion", "emisor", "ley", "moneda", "fechaEmision", "fechaVencimiento",
                                                   "interes", "formaAmortizacion", "montoNominal", "codigoIsin")}})
    print(f"ONs nuevas: {sum(o['estado'] == 'auto' for o in ons)} automaticas, {sum(o['estado'] != 'auto' for o in ons)} pendientes")

    # 4) letras a tasa fija
    ref = leer_letras_ref()
    err = {k: [] for k in METODOS}
    for t, (vto, pago) in ref.items():
        f = ficha(t)
        if not f:
            continue
        e, v, tem = ymd(f.get("fechaEmision")), ymd(f.get("fechaVencimiento")), tem_de(f.get("interes") or "")
        if e and v and tem:
            for k, fn in METODOS.items():
                err[k].append(abs(fn(e, v, tem) / pago - 1))
    validos = {k: max(v) for k, v in err.items() if len(v) >= 3}
    metodo = min(validos, key=validos.get) if validos else None
    modo_auto_l = bool(metodo) and validos[metodo] < 0.0005
    print(f"Prueba de letras: {metodo} error max {validos.get(metodo)} -> {'automatico' if modo_auto_l else 'solo pendientes'}")
    letras = []
    syms_l = sorted({r.get("symbol") for r in pan_pb if re.fullmatch(r"[ST]\d{1,2}[EFMAYJLGSOND]\d", str(r.get("symbol", "")))})
    prev_l = {x["t"]: x for x in previo.get("letras", [])}
    for s in syms_l:
        if s in ref:
            continue
        f = ficha(s)
        if not f:
            continue
        e, v, tem = ymd(f.get("fechaEmision")), ymd(f.get("fechaVencimiento")), tem_de(f.get("interes") or "")
        if not v or v <= HOY:
            continue
        mot = None if (e and tem) else "no se encontro la TEM o la fecha de emision"
        pago = round(METODOS[metodo](e, v, tem), 6) if (metodo and not mot) else None
        if not mot and not modo_auto_l:
            mot = "el calculo del pago todavia no paso la prueba con las letras cargadas"
        letras.append({"t": s, "vto": v.isoformat(), "pago": pago, "tem": tem, "emision": e and e.isoformat(),
                       "estado": "auto" if not mot else "pendiente", "motivo": mot, "desde": prev_l.get(s, {}).get("desde") or alta,
                       "interes": (f.get("interes") or "")[:600]})

    # 5) bonos soberanos y BOPREAL en dolares
    REF = leer_ref()
    rb = {b["t"]: b for b in REF.get("bonos", [])}
    okb = totb = 0
    difb = []
    for t, b in rb.items():
        f = ficha(t + "D") or ficha(t)
        if not f:
            continue
        fl, mot = flujos_bono(f)
        totb += 1
        if fl and flujos_iguales(fl, b["flujos"]):
            okb += 1
        else:
            difb.append({"t": t, "motivo": mot or "flujos distintos"})
    modo_auto_b = totb >= 5 and okb / totb >= 0.8
    print(f"Prueba de bonos: {okb}/{totb} -> {'automatico' if modo_auto_b else 'solo pendientes'}")
    conocidos = set(rb) | set(ref) | {x["t"] for x in (REF.get("cer") or {}).get("bonos", [])} | {x["t"] for x in (REF.get("cer") or {}).get("duales", [])}
    prev_b = {x["t"]: x for x in previo.get("bonos", [])}
    prev_x = {x["t"]: x for x in previo.get("otros", [])}
    bonos, otros = [], []
    lim = (HOY + timedelta(days=60)).isoformat()
    base_pb = {}
    for r in pan_pb:
        sy = str(r.get("symbol", ""))
        if not re.fullmatch(r"[A-Z0-9]{3,6}", sy) or (r.get("maturityDate") or "0") <= lim:
            continue
        b = sy[:-1] if sy[-1] in "DC" and sy[:-1] in {str(x.get("symbol")) for x in pan_pb} else sy
        base_pb.setdefault(b, r.get("maturityDate"))
    for b in sorted(base_pb):
        if b in conocidos or re.fullmatch(r"[ST]\d{1,2}[EFMAYJLGSOND]\d", b):
            continue
        f = ficha(b)
        if not f:
            continue
        mon, emi_t = bajo(f.get("moneda")), bajo(f.get("emisor"))
        if not re.search(r"gobierno nacional|nacion|tesoro|banco central|bcra|republica argentina", emi_t):
            continue  # provincias, municipios y otros quedan afuera
        fic = {k: f.get(k) for k in ("denominacion", "emisor", "ley", "moneda", "fechaEmision", "fechaVencimiento", "interes", "formaAmortizacion", "codigoIsin")}
        if "dolar" in mon and "linked" not in mon:
            fl, mot = flujos_bono(f)
            if not mot and not modo_auto_b:
                mot = "el lector de bonos todavia no paso la prueba con los bonos cargados"
            ley = "Nueva York" if ley_app(f) == "NY" else "Argentina"
            tipo = "BOPREAL (emisor BCRA)" if re.search(r"banco central|bcra", emi_t) else ("Global nuevo" if ley == "Nueva York" else "Bonar nuevo")
            bonos.append({"t": b, "nombre": (f.get("denominacion") or b).strip()[:80], "ley": ley, "tipo": tipo,
                          "vto": (ymd(f.get("fechaVencimiento")) or HOY).isoformat(), "flujos": fl, "estado": "auto" if not mot else "pendiente",
                          "motivo": mot, "desde": prev_b.get(b, {}).get("desde") or alta, "ficha": fic})
        else:
            txt = bajo((f.get("moneda") or "") + " " + (f.get("interes") or "") + " " + (f.get("denominacion") or ""))
            tipo = next((n for pat, n in TIPO_PUB if re.search(pat, txt)), "En pesos")
            otros.append({"t": b, "tipo": tipo, "vto": (ymd(f.get("fechaVencimiento")) or HOY).isoformat(), "estado": "pendiente",
                          "motivo": "familia que hoy se carga a mano (necesita la serie de " + {"CER": "CER", "Dólar linked": "dólar A3500", "TAMAR": "TAMAR"}.get(tipo, "tasas") + ")",
                          "desde": prev_x.get(b, {}).get("desde") or alta, "ficha": fic})
    print(f"Bonos nuevos en dolares: {sum(x['estado'] == 'auto' for x in bonos)} automaticos, {sum(x['estado'] != 'auto' for x in bonos)} pendientes; otros: {len(otros)}")

    out = {"actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds"), "fecha": hoy_iso,
           "prueba": {"ons": {"ok": ok, "total": tot, "auto": modo_auto_on, "difs": difs[:40]},
                      "letras": {"metodo": metodo, "error_max": validos.get(metodo), "auto": modo_auto_l},
                      "bonos": {"ok": okb, "total": totb, "auto": modo_auto_b, "difs": difb[:20]}},
           "ons": ons, "letras": letras, "bonos": bonos, "otros": otros}
    SALIDA.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 5) aviso por mail: solo lo que aparece por primera vez
    nuevos_o = [o for o in ons if o["t"] not in prev_ons or prev_ons[o["t"]].get("estado") != o["estado"]]
    nuevos_l = [l for l in letras if l["t"] not in prev_l or prev_l[l["t"]].get("estado") != l["estado"]]
    nuevos_b = [x for x in bonos if x["t"] not in prev_b or prev_b[x["t"]].get("estado") != x["estado"]]
    nuevos_x = [x for x in otros if x["t"] not in prev_x]
    if primera:
        with AVISO.open("a", encoding="utf-8") as fa:
            fa.write(f"## Primera revisión de emisiones\n\nSe leyeron las fichas técnicas de BYMA. El lector acertó en {ok} de {tot} ONs del catálogo"
                     f" y {'reprodujo' if modo_auto_l else 'no reprodujo'} el pago de las letras cargadas.\n\n"
                     f"- ONs en dólares (A o mejor) que ya cotizaban y se sumaron solas: {sum(o['estado'] == 'auto' for o in ons)}\n"
                     f"- Pendientes de revisar: {sum(o['estado'] != 'auto' for o in ons)}\n- Letras sumadas: {sum(l['estado'] == 'auto' for l in letras)}\n"
                     f"- Bonos en dólares sumados: {sum(x['estado'] == 'auto' for x in bonos)} (el lector acertó en {okb} de {totb} bonos cargados)\n"
                     f"- Bonos en pesos (CER, duales, dólar linked) detectados para cargar a mano: {len(otros)}\n\n"
                     "Desde ahora vas a recibir un aviso cada vez que aparezca una nueva.\n\n")
    elif nuevos_o or nuevos_l or nuevos_b or nuevos_x:
        lin = []
        for o in nuevos_o:
            fl = o["fila"]
            desc = (f"{fl[1]} clase {fl[2]}, ley {fl[3]}, cupón {fl[5] * 100:.2f}%, vence {fl[4]}, "
                    f"{'bullet' if fl[7] == 'B' else 'amortiza en ' + str(len(fl[7])) + ' cuotas'}, {fl[8] or fl[9]}") if fl else o["ficha"].get("denominacion")
            lin.append(f"- **{o['t']}D** — {desc}: " + ("**cargada en la app**" if o["estado"] == "auto" else f"pendiente ({o['motivo']})"))
        for l in nuevos_l:
            lin.append(f"- **{l['t']}** — letra, vence {l['vto']}" + (f", paga {l['pago']:.4f}: **cargada en la app**" if l["estado"] == "auto" else f": pendiente ({l['motivo']})"))
        for x in nuevos_b:
            lin.append(f"- **{x['t']}D** — {x['nombre']}, vence {x['vto']}" + (": **cargado en la app**" if x["estado"] == "auto" else f": pendiente ({x['motivo']})"))
        for x in nuevos_x:
            lin.append(f"- **{x['t']}** — bono {x['tipo']}, vence {x['vto']}: {x['motivo']}")
        with AVISO.open("a", encoding="utf-8") as fa:
            fa.write("## Instrumentos nuevos\n\n" + "\n".join(lin) + "\n\nVerificalos en Mi cartera › Novedades.\n\n")


if __name__ == "__main__":
    main()
