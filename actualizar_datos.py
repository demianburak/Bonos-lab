# -*- coding: utf-8 -*-
"""Baja los precios de internet y escribe datos.json. Solo usa Python estandar (sin instalar nada).
Lo ejecuta GitHub solo, cada pocos minutos en horario de mercado (ver .github/workflows/actualizar.yml).
Si una fuente falla, conserva el ultimo dato bueno de esa fuente y lo avisa en 'avisos'."""
import csv, io, json, math, re, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SALIDA = Path(__file__).with_name("datos.json")
HISTORICO = Path(__file__).with_name("historico.json")
RE_FUT = re.compile(r"^DLR(0[1-9]|1[0-2])(\d{4})$")


def bajar(url, intentos=3):
    for i in range(intentos):
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as r:
                return json.load(r)
        except Exception as e:
            ultimo = e
            time.sleep(3)
    print("  fallo", url, ultimo)
    return None


def positivo(reg, *campos):
    for c in campos:
        v = reg.get(c)
        try:
            x = float(str(v).replace(",", "."))
            if math.isfinite(x) and x > 0:
                return x
        except (TypeError, ValueError):
            pass
    return None


def ultimo_habil(a, m):
    d = date(a + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


previo = {}
if SALIDA.exists():
    try:
        previo = json.loads(SALIDA.read_text(encoding="utf-8"))
    except Exception:
        previo = {}
avisos, out = [], {k: previo.get(k) for k in ("oficial", "a3500", "futuros")}

# 1) bonos, letras y especies D/C (data912)
precios = {}
for url in ("https://data912.com/live/arg_bonds", "https://data912.com/live/arg_notes",
            "https://data912.com/live/arg_letras", "https://data912.com/live/arg_bills",
            "https://data912.com/live/arg_corp"):
    data = bajar(url, 2 if "notes" in url or "letras" in url or "bills" in url else 3)
    if isinstance(data, list):
        for reg in data:
            s = reg.get("symbol")
            if not s:
                continue
            p = {"c": positivo(reg, "c", "px_last", "last"), "bid": positivo(reg, "px_bid", "bid"), "ask": positivo(reg, "px_ask", "ask"), "v": positivo(reg, "v", "volume")}
            p = {k: v for k, v in p.items() if v is not None}
            if p and s not in precios:
                precios[s] = p
precios_ok = len(precios) >= 20
if not precios_ok:
    # data912 suele rechazar a los servidores de GitHub. No cortamos: seguimos con el resto de las fuentes
    # (futuros, A3500, CER, TAMAR, Tesoro) y conservamos los ultimos precios; el navegador los pide directo a data912.
    precios = previo.get("precios") or {}
    if len(precios) < 20:
        print("No se pudo bajar el listado de precios y no hay anteriores; no se toca datos.json.")
        sys.exit(1)
    avisos.append("precios: data912 no respondio desde el servidor; se conservan los anteriores")
    print("  data912 no respondio: se conservan los precios anteriores y se sigue con las demas fuentes.")

# 2) dolar oficial
d = bajar("https://dolarapi.com/v1/dolares/oficial", 2)
if isinstance(d, dict):
    v = d.get("venta") or d.get("promedio") or d.get("compra")
    if v:
        out["oficial"] = float(v)
else:
    avisos.append("dolar oficial: se conserva el anterior")

# 3) A3500 del BCRA
hoy = date.today()
d = bajar(f"https://api.bcra.gob.ar/estadisticascambiarias/v1.0/Cotizaciones/USD?fechadesde={(hoy - timedelta(days=10)).isoformat()}&fechahasta={hoy.isoformat()}", 2)
mejor = None
if isinstance(d, dict):
    for r in d.get("results") or []:
        try:
            f = date.fromisoformat(str(r.get("fecha"))[:10]); v = float((r.get("detalle") or [{}])[0].get("tipoCotizacion"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0 and (mejor is None or f > mejor[0]):
            mejor = (f, v)
if mejor:
    out["a3500"] = mejor[1]
else:
    avisos.append("A3500: se conserva el anterior")

# 4) futuros de dolar (A3 Mercados)
reg, pag = [], 1
while pag <= 10:
    d = bajar(f"https://apicem.matbarofex.com.ar/api/v2/closing-prices?product=DLR&type=FUT&from={(hoy - timedelta(days=10)).isoformat()}&to={hoy.isoformat()}&page={pag}&pageSize=200&segment=Monedas", 2)
    if not isinstance(d, dict) or not isinstance(d.get("data"), list):
        break
    reg.extend(d["data"])
    if not d["data"] or len(reg) >= (d.get("totalEntries") or 0):
        break
    pag += 1
por_fecha = {}
for r in reg:
    m = RE_FUT.match(str(r.get("symbol") or ""))
    aj = positivo(r, "settlement", "close")
    try:
        f = date.fromisoformat(str(r.get("dateTime") or "")[:10])
    except ValueError:
        continue
    if not m or aj is None:
        continue
    venc = ultimo_habil(int(m.group(2)), int(m.group(1)))
    if venc <= f:
        continue
    por_fecha.setdefault(f, {})[r["symbol"]] = {"symbol": r["symbol"], "venc": venc.isoformat(), "ajuste": aj}
ok = [f for f, c in por_fecha.items() if len(c) >= 3]
if ok:
    out["futuros"] = sorted(por_fecha[max(ok)].values(), key=lambda c: c["venc"])
    out["fecha_futuros"] = max(ok).isoformat()
else:
    avisos.append("futuros: se conservan los anteriores")

# 5) curva del Tesoro de EE.UU. (home.treasury.gov)
COLS = [(1/12, "1 Mo"), (0.25, "3 Mo"), (0.5, "6 Mo"), (1, "1 Yr"), (2, "2 Yr"), (3, "3 Yr"), (5, "5 Yr"), (7, "7 Yr"), (10, "10 Yr"), (20, "20 Yr"), (30, "30 Yr")]
out["tesoro"] = previo.get("tesoro")
for anio in (hoy.year, hoy.year - 1):
    try:
        url = f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{anio}/all?type=daily_treasury_yield_curve&field_tdr_date_value={anio}&page&_format=csv"
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=25) as r:
            filas = list(csv.DictReader(io.StringIO(r.read().decode("utf-8-sig"))))
        def fch(f):
            try:
                return datetime.strptime(str(f.get("Date", "")).strip(), "%m/%d/%Y")
            except ValueError:
                return datetime.min
        ult = max(filas, key=fch)
        curva = [[a, float(ult[c]) / 100] for a, c in COLS if ult.get(c) not in (None, "")]
        if len(curva) >= 6:
            out["tesoro"] = {"fecha": fch(ult).date().isoformat(), "curva": curva}
            break
    except Exception as e:
        print("  Tesoro:", e)
else:
    avisos.append("curva del Tesoro: se conserva la anterior")

# 6) CER y TAMAR del BCRA (API de variables monetarias v4.0)  -- SIN PROBAR contra la fuente real
def habiles_antes(d, n):
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def serie_bcra(id_var, desde, hasta):
    d = bajar(f"https://api.bcra.gob.ar/estadisticas/v4.0/monetarias/{id_var}?desde={desde.isoformat()}&hasta={hasta.isoformat()}&limit=3000", 2)
    filas = []
    try:
        for r in (d.get("results") or []):
            det = r.get("detalle") if isinstance(r, dict) and "detalle" in r else [r]
            for x in det:
                f = date.fromisoformat(str(x.get("fecha"))[:10]); v = float(x.get("valor"))
                if math.isfinite(v):
                    filas.append((f, v))
    except Exception as e:
        print("  BCRA", id_var, e)
        return []
    return sorted(filas)


out["cer"] = previo.get("cer")
try:
    liq = hoy + timedelta(days=1)
    while liq.weekday() >= 5:
        liq += timedelta(days=1)
    f_cer = habiles_antes(liq, 10)
    cer = [x for x in serie_bcra(30, f_cer - timedelta(days=15), f_cer) if x[0] <= f_cer]
    tam = serie_bcra(44, habiles_antes(date(2026, 6, 12), 10), hoy)
    nuevo = dict(out.get("cer") or {})
    if cer:
        nuevo["cer"] = {"fecha": cer[-1][0].isoformat(), "valor": cer[-1][1]}
    if len(tam) >= 5:
        nuevo["tamar"] = {"media": round(sum(v for _, v in tam) / len(tam) / 100, 8), "n": len(tam), "ultima": tam[-1][0].isoformat()}
    if nuevo:
        out["cer"] = nuevo
    if not cer or len(tam) < 5:
        avisos.append("CER/TAMAR: parte sin actualizar (se usa el ultimo dato bueno o el Excel)")
except Exception as e:
    avisos.append("CER/TAMAR: se conserva lo anterior")
    print("  CER/TAMAR:", e)

# 7) historico de precios (una vez por dia) -> historico.json   -- SIN PROBAR contra la fuente real
try:
    hace = (time.time() - HISTORICO.stat().st_mtime) / 3600 if HISTORICO.exists() else 999
    if hace > 20:
        base = date(2020, 1, 1)
        hist = {}
        try:
            hist = json.loads(HISTORICO.read_text(encoding="utf-8")) if HISTORICO.exists() else {}
        except Exception:
            hist = {}
        for tk in ("AL29", "AL30", "AL35", "AE38", "AL41", "GD29", "GD30", "GD35", "GD38", "GD41", "GD46"):
            d = bajar(f"https://data912.com/historical/bonds/{tk}", 2)
            if isinstance(d, list) and len(d) > 30:
                ds, ps = [], []
                for r in sorted(d, key=lambda r: str(r.get("date"))):
                    try:
                        dd = date.fromisoformat(str(r.get("date"))[:10]); pp = float(r.get("c"))
                    except (TypeError, ValueError):
                        continue
                    if pp > 0:
                        ds.append((dd - base).days); ps.append(round(pp, 4))
                if ds:
                    hist[tk] = {"d": ds, "p": ps}
        if hist:
            HISTORICO.write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")
except Exception as e:
    print("  historico:", e)

out.update({"actualizado": datetime.now(timezone.utc).isoformat(timespec="seconds") if precios_ok else (previo.get("actualizado") or datetime.now(timezone.utc).isoformat(timespec="seconds")), "origen": "data912.com, BCRA, A3 Mercados, dolarapi.com",
            "precios": precios, "avisos": avisos})
SALIDA.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
print("OK:", len(precios), "precios", "| avisos:", avisos or "ninguno")
