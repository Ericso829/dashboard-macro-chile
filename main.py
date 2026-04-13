import re
import pandas as pd
import streamlit as st
import yfinance as yf
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher

_TZ_CL = ZoneInfo("America/Santiago")

st.set_page_config(page_title="MIS Macroeconómico Chile", layout="wide")

_H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _get(url, timeout=20):
    return requests.get(url, headers=_H, timeout=timeout).text

def _lines(html):
    return [l.strip() for l in BeautifulSoup(html, "html.parser").get_text().split("\n") if l.strip()]

def _te(path):
    return _lines(_get(f"https://tradingeconomics.com{path}"))

def _fmt_iso(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d/%m/%Y")

def _pct(v, dec=1):
    if v is None: return "—"
    return f"{v:.{dec}f}%"

def _usd(v, dec=2):
    if v is None: return "—"
    return f"USD {v:,.{dec}f}"

def _clp(v):
    if v is None: return "—"
    return f"${v:,.0f}"

def _sim(a, b):
    """Title similarity ratio for deduplication."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

# ═══════════════════════════════════════════════════════════════
# FETCHERS – INDICADORES BCENTRAL / INE
# ═══════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600)
def fetch_mindicador():
    out = {}
    try:
        data = requests.get("https://mindicador.cl/api", headers=_H, timeout=10).json()
        for key, label in [("uf","UF"),("dolar","Dólar/CLP"),("tpm","TPM"),
                           ("euro","Euro/CLP"),("libra_cobre","Cobre"),("utm","UTM")]:
            e = data[key]
            out[label] = {"valor": e["valor"], "fecha": _fmt_iso(e["fecha"])}
    except Exception:
        pass
    return out

@st.cache_data(ttl=3600)
def fetch_historial():
    """Últimos 5 valores mensuales de indicadores clave."""
    hist = {}
    for key, label in [("ipc","IPC"), ("tpm","TPM"), ("dolar","USD/CLP"), ("uf","UF")]:
        try:
            serie = requests.get(f"https://mindicador.cl/api/{key}", headers=_H, timeout=10).json().get("serie",[])
            # Deduplicate by year-month
            seen, pts = set(), []
            for s in serie:
                ym = s["fecha"][:7]
                if ym not in seen:
                    seen.add(ym)
                    pts.append({"fecha": ym, "valor": s["valor"]})
                if len(pts) == 5: break
            hist[label] = pts
        except Exception:
            hist[label] = []
    return hist

@st.cache_data(ttl=1800)
def fetch_ipc():
    try:
        lines = _te("/chile/inflation-cpi")
        full  = " ".join(lines)
        m = re.search(
            r"Inflation Rate in Chile (?:increased|decreased|rose|fell|held).*?"
            r"(\d+\.?\d*)\s*percent\s*in\s*(\w+)\s*from\s*(\d+\.?\d*)\s*percent\s*in\s*(\w+)\s*of\s*(\d{4})",
            full, re.I)
        mom = re.search(
            r"monthly basis.*?(?:rose|fell|increased|decreased)\s+(\d+\.?\d*)\s*%"
            r".*?following\s+(?:a\s+)?(\d+\.?\d*)\s*%", full, re.I | re.S)
        r = {}
        if m:
            r.update({"yoy": float(m.group(1)), "yoy_ant": float(m.group(3)),
                      "mes": m.group(2), "mes_ant": m.group(4), "año": m.group(5)})
        if mom:
            r.update({"mom": float(mom.group(1)), "mom_ant": float(mom.group(2))})
        # Fallback table
        for i, l in enumerate(lines):
            if l == "Inflation Rate YoY" and "yoy" not in r and i+1 < len(lines):
                try: r["yoy"] = float(lines[i+1].replace("%",""))
                except: pass
            if l == "Inflation Rate MoM" and "mom" not in r and i+1 < len(lines):
                try: r["mom"] = float(lines[i+1].replace("%",""))
                except: pass
        return r or None
    except Exception:
        return None

@st.cache_data(ttl=1800)
def fetch_desempleo():
    try:
        full = " ".join(_te("/chile/unemployment-rate"))
        m = re.search(
            r"Unemployment Rate in Chile\s+(?:remained unchanged at|increased to|fell to|decreased to|rose to)"
            r"\s+(\d+\.?\d*)\s*percent\s*in\s*(\w+)", full, re.I)
        if m: return {"valor": float(m.group(1)), "periodo": m.group(2)}
    except Exception: pass
    return None

# ═══════════════════════════════════════════════════════════════
# FETCHER – PIB Y SECTOR REAL (BCCh vía TradingEconomics)
# ═══════════════════════════════════════════════════════════════

def _parse_te_calendar(lines):
    """
    Extract the most recently PUBLISHED actual value from a TradingEconomics
    calendar table. The calendar contains multiple rows chronologically.
    Each row: YYYY-MM-DD / HH:MM / indicator / period / actual / previous / ...
    We scan forward from the 'Actual' header, collect all date-rows that have a
    non-empty actual value, and return the last one (most recent published).
    """
    DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    today   = datetime.now(_TZ_CL).date()

    header_idx = None
    for i, l in enumerate(lines):
        if l == "Actual" and i + 1 < len(lines) and lines[i+1] == "Previous":
            header_idx = i
            break
    if header_idx is None:
        return {}

    # Find end of calendar region
    end_idx = len(lines)
    for i in range(header_idx + 4, len(lines)):
        if lines[i] == "Related":
            end_idx = i
            break

    # Scan calendar rows (each row starts with a YYYY-MM-DD date)
    best = {}
    i = header_idx + 4
    while i < end_idx - 4:
        if DATE_RE.match(lines[i]):
            row_date = lines[i]
            period   = lines[i+3] if i+3 < end_idx else "—"
            actual   = lines[i+4] if i+4 < end_idx else None
            prev     = lines[i+5] if i+5 < end_idx else None
            # Accept only rows with a real numeric/currency actual (not a future forecast)
            if actual and re.search(r"[\d]", actual):
                try:
                    pub = datetime.strptime(row_date, "%Y-%m-%d").date()
                    if pub <= today:
                        best = {
                            "actual":   actual,
                            "previous": prev,
                            "ref":      f"{period} {row_date[:4]}",
                        }
                except: pass
            # Advance: skip past this date's values to the next row
            i += 5
        else:
            i += 1

    return best

def _parse_te_related(lines, indicator_name):
    """Find Last/Previous/Unit/Reference for a named indicator in the Related table."""
    for i, l in enumerate(lines):
        if indicator_name.lower() in l.lower() and i+4 < len(lines):
            try:
                return {
                    "last": float(lines[i+1].replace(",",".")),
                    "prev": float(lines[i+2].replace(",",".")),
                    "unit": lines[i+3],
                    "ref":  lines[i+4],
                }
            except: pass
    return {}

def _parse_num(raw):
    """Parse a value string like '$2790M', '-1.6%', '335.52', '$-4.60B' → float."""
    if not raw: return None
    raw = str(raw).strip()
    mult = 1
    if raw.upper().endswith("B"): mult = 1_000
    if raw.upper().endswith("T"): mult = 1_000_000
    raw = raw.replace("$", "").replace(",", "").rstrip("MBKTmbtk%")
    try:
        return float(raw) * mult
    except:
        return None

def _parse_te_related_table(html):
    """
    Parse the HTML Related table from a TradingEconomics page.
    Returns dict: {indicator_name: {last, prev, unit, ref}}
    """
    out = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        for t in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in t.find_all("th")]
            if "Last" in headers and "Previous" in headers:
                for row in t.find_all("tr"):
                    cells = [c.get_text(strip=True) for c in row.find_all("td")]
                    if len(cells) >= 4 and cells[0] and cells[0] not in ("Related","Last","Previous"):
                        last = _parse_num(cells[1])
                        prev = _parse_num(cells[2])
                        if last is not None:
                            out[cells[0]] = {
                                "last": last, "prev": prev,
                                "unit": cells[3] if len(cells) > 3 else "",
                                "ref":  cells[4] if len(cells) > 4 else "",
                            }
    except Exception:
        pass
    return out

def _fetch_calendar_indicator(path, label, unit):
    """Fetch the most recent calendar actual for one TE path."""
    try:
        lines = _te(path)
        cal = _parse_te_calendar(lines)
        if cal and cal.get("actual"):
            va = _parse_num(cal["actual"])
            vp = _parse_num(cal.get("previous"))
            if va is not None:
                return {"val": va, "prev": vp, "ref": cal.get("ref","—"), "unit": unit}
    except Exception:
        pass
    return None

@st.cache_data(ttl=3600)
def fetch_pib_completo():
    """
    PIB completo — Banco Central de Chile.
    Fuentes: TradingEconomics (cita BCCh), World Bank (series anuales).
    Organizado en 5 grupos: tasas, valores, componentes, sectores, externo.
    """
    out = {
        "tasas":      {},   # growth rates
        "valores":    {},   # absolute values
        "componentes":{},   # demand-side components
        "sectores":   {},   # supply-side GDP by sector
        "externo":    {},   # external + fiscal
    }

    # ── 1. GDP growth page — calendar + full Related table ────────
    try:
        html_gdp = _get("https://tradingeconomics.com/chile/gdp-growth")
        lines_gdp = _lines(html_gdp)
        related   = _parse_te_related_table(html_gdp)

        # Calendar: PIB QoQ (most recent quarter)
        cal = _parse_te_calendar(lines_gdp)
        if cal and cal.get("actual"):
            out["tasas"]["PIB QoQ"] = {
                "val":  _parse_num(cal["actual"]),
                "prev": _parse_num(cal.get("previous")),
                "ref":  cal.get("ref","—"), "unit": "%",
            }

        # Related table values
        RELATED_MAP = {
            # key in TE Related → (our label, group, unit)
            "Full Year GDP Growth":    ("PIB Año Completo 2025",  "tasas",      "%"),
            "GDP Growth Rate YoY":     ("PIB Anual",              "tasas",      "%"),
            "GDP":                     ("PIB Nominal",            "valores",    "B USD"),
            "GDP Constant Prices":     ("PIB Constante (CLP B)",  "valores",    "B CLP"),
            "GDP per Capita":          ("PIB per cápita",         "valores",    "USD"),
            "GDP per Capita PPP":      ("PIB per cápita PPP",     "valores",    "USD"),
            "Gross National Product":  ("Producto Nacional Bruto","valores",    "B CLP"),
            "Gross Fixed Capital Formation": ("Inversión (FBCF)", "componentes","B CLP"),
            "GDP from Agriculture":    ("Agropecuario",           "sectores",   "B CLP"),
            "GDP from Construction":   ("Construcción",           "sectores",   "B CLP"),
            "GDP from Manufacturing":  ("Industria Manufactura",  "sectores",   "B CLP"),
            "GDP from Mining":         ("Minería",                "sectores",   "B CLP"),
            "GDP from Public Administration": ("Administración Pública","sectores","B CLP"),
            "GDP from Services":       ("Servicios",              "sectores",   "B CLP"),
            "GDP from Transport":      ("Transporte",             "sectores",   "B CLP"),
            "GDP from Utilities":      ("Utilities / Energía",    "sectores",   "B CLP"),
        }
        for te_key, (our_label, group, unit) in RELATED_MAP.items():
            r = related.get(te_key)
            if r:
                out[group][our_label] = {
                    "val":  r["last"], "prev": r["prev"],
                    "ref":  r.get("ref","—"), "unit": unit,
                }
    except Exception:
        pass

    # ── 2. Additional growth rates ────────────────────────────────
    for label, path, unit in [
        ("Producción Ind. YoY", "/chile/industrial-production", "%"),
        ("Ventas Retail YoY",   "/chile/retail-sales-annual",   "%"),
    ]:
        d = _fetch_calendar_indicator(path, label, unit)
        if d: out["tasas"][label] = d

    # ── 3. Trade data (calendar-based, most recent month) ─────────
    for label, path, unit in [
        ("Exportaciones",     "/chile/exports",         "M USD"),
        ("Importaciones",     "/chile/imports",         "M USD"),
        ("Balanza Comercial", "/chile/balance-of-trade","M USD"),
    ]:
        d = _fetch_calendar_indicator(path, label, unit)
        if d: out["componentes"][label] = d

    # Consumo + Gasto Gobierno from related tables
    for label, path, unit in [
        ("Consumo Privado", "/chile/consumer-spending", "B CLP"),
        ("Gasto Gobierno",  "/chile/government-spending","B CLP"),
    ]:
        try:
            html = _get(f"https://tradingeconomics.com{path}")
            rel  = _parse_te_related_table(html)
            for te_key, row in rel.items():
                if "Consumer Spending" in te_key or "Government Spending" in te_key or "Spending" in te_key:
                    out["componentes"][label] = {
                        "val": row["last"], "prev": row["prev"],
                        "ref": row.get("ref","—"), "unit": unit,
                    }
                    break
        except Exception:
            pass

    # ── 4. Sector externo y fiscal ────────────────────────────────
    for label, path, unit in [
        ("Cuenta Corriente",     "/chile/current-account",          "B USD"),
        ("Cta. Cte. % PIB",      "/chile/current-account-to-gdp",   "%"),
        ("Deuda Externa",        "/chile/external-debt",            "M USD"),
        ("Reservas Int.",        "/chile/foreign-exchange-reserves", "M USD"),
        ("Deuda Gob. % PIB",     "/chile/government-debt-to-gdp",   "%"),
        ("Balance Fiscal % PIB", "/chile/government-budget",        "%"),
        ("IED",                  "/chile/foreign-direct-investment", "M USD"),
    ]:
        d = _fetch_calendar_indicator(path, label, unit)
        if d:
            out["externo"][label] = d
        else:
            # fallback via Related table
            try:
                html = _get(f"https://tradingeconomics.com{path}")
                rel  = _parse_te_related_table(html)
                for _, row in rel.items():
                    if row["last"] is not None:
                        out["externo"][label] = {
                            "val":  row["last"], "prev": row["prev"],
                            "ref":  row.get("ref","—"), "unit": unit,
                        }
                        break
            except Exception:
                pass

    return out

# ─────────────────────────────────────────────────────────────────
# World Bank historical series (annual, up to 12 years)
# ─────────────────────────────────────────────────────────────────
_WB = "https://api.worldbank.org/v2/country/CL/indicator"

def _wb_series(code, n=12):
    """Fetch a World Bank indicator series for Chile, returns sorted {year:value} dict."""
    try:
        r = requests.get(f"{_WB}/{code}?format=json&per_page={n}&mrv={n}",
                         headers=_H, timeout=30)
        data = r.json()[1] if r.status_code == 200 and len(r.json()) > 1 else []
        pts  = {d["date"]: round(d["value"], 3)
                for d in (data or []) if d["value"] is not None}
        return dict(sorted(pts.items()))  # oldest → newest
    except Exception:
        return {}

@st.cache_data(ttl=86400)   # 24h — annual data rarely changes
def fetch_wb_historial():
    """
    World Bank annual historical series for Chile.
    Returns dict of {indicator_label: {year: value, ...}}.
    Cached 24h.
    """
    codes = {
        "Crecimiento PIB (%)":      "NY.GDP.MKTP.KD.ZG",
        "PIB Nominal (B USD)":      "NY.GDP.MKTP.CD",
        "PIB per cápita (USD)":     "NY.GDP.PCAP.CD",
        "PIB per cápita PPP (USD)": "NY.GDP.PCAP.PP.CD",
        "Consumo Hogares (% PIB)":  "NE.CON.PETC.ZS",
        "FBCF (% PIB)":             "NE.GDI.TOTL.ZS",
        "Exportaciones (% PIB)":    "NE.EXP.GNFS.ZS",
        "Importaciones (% PIB)":    "NE.IMP.GNFS.ZS",
    }
    out = {}
    for label, code in codes.items():
        s = _wb_series(code, n=15)
        if s:
            # Convert PIB Nominal to billions
            if "B USD" in label:
                s = {k: round(v/1e9, 2) for k, v in s.items()}
            out[label] = s
    return out

# ═══════════════════════════════════════════════════════════════
# FETCHER – MERCADOS (Bolsa Santiago + NYSE/COMEX)
# ═══════════════════════════════════════════════════════════════

@st.cache_data(ttl=600)
def fetch_mercados():
    SYMS = {
        "IPSA":  {"sym":"^IPSA", "bolsa":"Bolsa de Santiago",         "moneda":"CLP","unidad":"pts"},
        "Cobre": {"sym":"HG=F",  "bolsa":"COMEX – Bolsa de Nueva York","moneda":"USD","unidad":"USD/lb"},
        "WTI":   {"sym":"CL=F",  "bolsa":"NYMEX – Bolsa de Nueva York","moneda":"USD","unidad":"USD/bbl"},
        "Brent": {"sym":"BZ=F",  "bolsa":"NYMEX – Bolsa de Nueva York","moneda":"USD","unidad":"USD/bbl"},
    }
    usd_clp = None
    try:
        fx = yf.Ticker("CLP=X").history(period="5d")
        if not fx.empty: usd_clp = float(fx["Close"].iloc[-1])
    except: pass

    out = {}
    for name, cfg in SYMS.items():
        try:
            hist = yf.Ticker(cfg["sym"]).history(period="5d")
            if not hist.empty:
                last  = float(hist["Close"].iloc[-1])
                delta = float(last - hist["Close"].iloc[-2]) if len(hist) >= 2 else None
                e = {**cfg, "val": last, "delta": delta,
                     "fecha": hist.index[-1].strftime("%d/%m/%Y"), "usd_clp": usd_clp}
                if cfg["moneda"] == "USD" and usd_clp:
                    e["val_clp"]   = last * usd_clp
                    e["delta_clp"] = delta * usd_clp if delta else None
                out[name] = e
        except: pass
    return out

# ═══════════════════════════════════════════════════════════════
# FETCHER – NOTICIAS MULTI-FUENTE
# ═══════════════════════════════════════════════════════════════

# Mapping keywords → which indicator is affected
INDICADOR_MAP = {
    "IPC / Inflación":    ["ipc","inflación","inflacion","precios","cpi","canasta","transporte","educación"],
    "Dólar / Tipo cambio":["dólar","dolar","tipo de cambio","usd","peso","paridad","clp"],
    "TPM / Tasa":         ["tpm","tasa","banco central","política monetaria","interés","bcentral"],
    "PIB / Crecimiento":  ["pib","gdp","crecimiento","actividad","imacec","recesión","recesion"],
    "Empleo":             ["empleo","desempleo","trabajo","laboral","ocupación","cesantía"],
    "Cobre / Minería":    ["cobre","minería","mineria","codelco","libra","producción minera"],
    "Petróleo":           ["petróleo","petroleo","combustible","gasolina","bencina","wti","brent"],
    "IPSA / Bolsa":       ["ipsa","bolsa","acciones","mercado accionario","bvs"],
    "Comercio Exterior":  ["exporta","importa","balanza","comercio exterior","aranceles","arancel"],
    "Fiscal / Deuda":     ["hacienda","presupuesto","fiscal","deuda","déficit","superávit","tributario"],
}

ECO_KW = [w for lst in INDICADOR_MAP.values() for w in lst]

def _tag_indicadores(text):
    low = text.lower()
    tags = [k for k, ws in INDICADOR_MAP.items() if any(w in low for w in ws)]
    return tags or ["General"]

def _impacto(text):
    low = text.lower()
    neg = ["cae","baja","crisis","freno","contrae","recesión","recesion","déficit","caída","caida",
           "alza combustible","alza del dólar","inflación sube","desempleo sube","aranceles"]
    pos = ["sube","crece","alza ipsa","alza bolsa","mejora","recupera","repunta","superávit",
           "inflación baja","dólar baja","empleo sube","crecimiento"]
    if any(w in low for w in neg): return "🔴 Negativo"
    if any(w in low for w in pos): return "🟢 Positivo"
    return "🟡 Neutral"

def _deduplicate(items, threshold=0.55):
    kept = []
    for item in items:
        dup = False
        for k in kept:
            if _sim(item["titulo"], k["titulo"]) > threshold:
                dup = True
                # Merge: keep the one with longer summary
                if len(item.get("resumen","")) > len(k.get("resumen","")):
                    k["resumen"] = item.get("resumen","")
                    k["fuentes"] = list(set(k.get("fuentes",[]) + item.get("fuentes",[])))
                break
        if not dup:
            kept.append(item)
    return kept

def _scrape_links(url, link_filter_fn, base_url=""):
    """Generic link scraper that returns list of {titulo, link}."""
    out = []
    try:
        soup = BeautifulSoup(_get(url, timeout=15), "html.parser")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            href  = a["href"]
            if not href.startswith("http"): href = base_url + href
            if link_filter_fn(title, href):
                out.append({"titulo": title[:160], "link": href,
                            "resumen": "", "fuentes": []})
    except: pass
    return out

def fetch_noticias():
    items = []

    # ── 1. DF Mercados (artículos scrapeados) ───────────────────
    try:
        soup = BeautifulSoup(_get("https://www.df.cl/mercados", timeout=15), "html.parser")
        for art in soup.find_all("article")[:10]:
            t = art.find(["h2","h3"])
            a = art.find("a", href=True)
            p = art.find("p")
            if t and a:
                href = a["href"]
                if not href.startswith("http"): href = "https://www.df.cl" + href
                items.append({
                    "titulo":  t.get_text(strip=True)[:160],
                    "link":    href,
                    "resumen": p.get_text(strip=True)[:220] if p else "",
                    "fuentes": ["Diario Financiero"],
                })
    except: pass

    # ── 2. UChile Economía (scraping página) ───────────────────
    try:
        soup = BeautifulSoup(_get("https://radio.uchile.cl/economia/", timeout=15), "html.parser")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            href  = a["href"]
            if not href.startswith("http"): href = "https://radio.uchile.cl" + href
            if (len(title) > 35 and "radio.uchile.cl" in href and
                    any(k in title.lower() for k in ECO_KW)):
                items.append({"titulo": title[:160], "link": href,
                              "resumen": "", "fuentes": ["Radio Universidad de Chile"]})
    except: pass

    # ── 3. UChile RSS (filtrado por economía) ──────────────────
    try:
        feed = feedparser.parse("https://radio.uchile.cl/feed/")
        for e in feed.entries:
            t  = e.title
            sm = getattr(e, "summary", "")
            if any(k in (t+sm).lower() for k in ECO_KW):
                items.append({
                    "titulo":  t[:160],
                    "link":    e.link,
                    "resumen": sm[:220],
                    "fuentes": ["Radio Universidad de Chile"],
                })
    except: pass

    # ── 4. Emol Economía (scraping) ───────────────────────────
    try:
        soup = BeautifulSoup(_get("https://www.emol.com/economia/", timeout=15), "html.parser")
        for a in soup.find_all("a", href=True):
            title = a.get_text(strip=True)
            href  = a["href"]
            if (len(title) > 35 and
                    ("emol.com" in href or href.startswith("/")) and
                    any(k in title.lower() for k in ECO_KW)):
                if not href.startswith("http"): href = "https://www.emol.com" + href
                items.append({"titulo": title[:160], "link": href,
                              "resumen": "", "fuentes": ["Emol"]})
    except: pass

    # ── Enriquecer + deduplicar ───────────────────────────────
    for item in items:
        item["impacto"]     = _impacto(item["titulo"] + " " + item["resumen"])
        item["indicadores"] = _tag_indicadores(item["titulo"] + " " + item["resumen"])

    deduped = _deduplicate(items, threshold=0.55)
    # Sort: Negativo first, then Positivo, then Neutral
    order = {"🔴 Negativo": 0, "🟢 Positivo": 1, "🟡 Neutral": 2}
    deduped.sort(key=lambda x: order.get(x["impacto"], 3))
    return deduped[:20]

# ═══════════════════════════════════════════════════════════════
# RENDER HELPERS
# ═══════════════════════════════════════════════════════════════

def _metric_card(col, label, value, delta=None, caption=None):
    col.metric(label, value, delta=delta)
    if caption: col.caption(caption)

def _commodity_block(col, nombre, data):
    with col:
        if not data:
            st.metric(nombre, "—")
            return
        usd_str = (f"USD {data['val']:,.3f}" if "lb" in data["unidad"]
                   else f"USD {data['val']:,.2f}")
        d = data.get("delta")
        d_str = (f"USD {d:+.3f}" if d is not None and "lb" in data["unidad"]
                 else f"USD {d:+.2f}" if d is not None else None)
        st.metric(nombre, usd_str, delta=d_str)
        if data.get("val_clp"):
            dc = data.get("delta_clp")
            st.markdown(
                f"<small style='color:#777'>≈ <b>{_clp(data['val_clp'])} CLP</b>"
                f"{'/' + data['unidad'].split('/')[1] if '/' in data['unidad'] else ''}"
                f"{f'  ({dc:+,.0f})' if dc else ''}</small>",
                unsafe_allow_html=True,
            )
        st.caption(f"📅 {data['fecha']} · **{data['bolsa']}**")

# ═══════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════

st.title("📊 Dashboard Macroeconómico Chile")
st.caption(
    f"Actualizado: {datetime.now(_TZ_CL).strftime('%d/%m/%Y %H:%M')} (hora Chile)  ·  "
    "Mercados al cierre del viernes 10/04/2026  ·  "
    "Publicaciones mensuales según última disponibilidad oficial"
)
st.divider()

with st.spinner("Cargando datos…"):
    minind  = fetch_mindicador()
    hist    = fetch_historial()
    ipc     = fetch_ipc()
    desem   = fetch_desempleo()
    pib     = fetch_pib_completo()
    mkt     = fetch_mercados()
    wb_hist = fetch_wb_historial()

usd_clp_rate = next((v["usd_clp"] for v in mkt.values() if v.get("usd_clp")), None)

# ──────────────────────────────────────────────────────────────
# SECCIÓN 1 – POLÍTICA MONETARIA Y PRECIOS (BCCh / INE)
# ──────────────────────────────────────────────────────────────
st.subheader("📌 Política Monetaria y Precios — Banco Central / INE")

c1,c2,c3,c4 = st.columns(4)
_metric_card(c1,"UF",
    f"${minind.get('UF',{}).get('valor',0):,.2f}",
    caption=f"📅 {minind.get('UF',{}).get('fecha','—')} · BCCh vía Mindicador.cl")
_metric_card(c2,"Dólar / CLP",
    f"${minind.get('Dólar/CLP',{}).get('valor',0):,.2f}",
    caption=f"📅 {minind.get('Dólar/CLP',{}).get('fecha','—')} · BCCh vía Mindicador.cl")
_metric_card(c3,"TPM",
    f"{minind.get('TPM',{}).get('valor','—')}%",
    caption=f"📅 {minind.get('TPM',{}).get('fecha','—')} · **Banco Central de Chile**")
_metric_card(c4,"Euro / CLP",
    f"${minind.get('Euro/CLP',{}).get('valor',0):,.2f}",
    caption=f"📅 {minind.get('Euro/CLP',{}).get('fecha','—')} · BCCh vía Mindicador.cl")

c5,c6,c7,c8 = st.columns(4)
if ipc:
    _metric_card(c5,"IPC Anual",_pct(ipc.get("yoy")),
        delta=f"{ipc['yoy']-ipc.get('yoy_ant',ipc.get('yoy',0)):+.1f}pp vs {ipc.get('mes_ant','mes ant.')}",
        caption="📅 Mar 2026 · INE / BCCh")
    _metric_card(c6,"IPC Mensual",_pct(ipc.get("mom")),
        delta=f"{ipc.get('mom',0)-ipc.get('mom_ant',0):+.1f}pp vs mes anterior",
        caption="📅 Mar 2026 · INE / BCCh")
else:
    c5.metric("IPC Anual","—"); c6.metric("IPC Mensual","—")

_metric_card(c7,"UTM",
    f"${minind.get('UTM',{}).get('valor',0):,}",
    caption=f"📅 {minind.get('UTM',{}).get('fecha','—')} · BCCh")

if desem:
    _metric_card(c8,"Tasa Desempleo",_pct(desem["valor"]),
        caption=f"📅 {desem.get('periodo','feb 2026')} · INE / BCCh")
else:
    c8.metric("Tasa Desempleo","—")

# ──────────────────────────────────────────────────────────────
# SECCIÓN 2 – VALORES HISTÓRICOS (mini tabla)
# ──────────────────────────────────────────────────────────────
with st.expander("🕐 Valores Históricos — últimos 5 períodos", expanded=False):
    tabs = st.tabs(["IPC","USD/CLP","TPM","UF"])
    labels = ["IPC","USD/CLP","TPM","UF"]
    for tab, label in zip(tabs, labels):
        with tab:
            data = hist.get(label, [])
            if data:
                df = pd.DataFrame(data).rename(columns={"fecha":"Período","valor":label})
                df[label] = df[label].apply(
                    lambda v: f"{v:.2f}%" if label in ["IPC","TPM"]
                    else f"${v:,.2f}")
                st.table(df.set_index("Período"))
            else:
                st.info("Sin datos históricos disponibles.")

st.divider()

# ──────────────────────────────────────────────────────────────
# SECCIÓN 3 – PIB COMPLETO (BCCh)
# ──────────────────────────────────────────────────────────────
st.subheader("🏛️ PIB y Cuentas Nacionales — Banco Central de Chile")
st.caption(
    "Datos publicados por el BCCh consolidados vía TradingEconomics · "
    "[Ver en BCCh →](https://www.bcentral.cl/web/banco-central/areas/estadisticas/sector-real)"
)

def _fmt_val(d):
    v, u = d.get("val"), d.get("unit","")
    if v is None: return "—"
    if u == "%":     return f"{v:.1f}%"
    if u == "B USD": return f"USD {v:,.2f}B"
    if u == "B CLP": return f"CLP {v:,.1f}B"
    if u == "M USD": return f"USD {v:,.0f}M"
    if u == "USD":   return f"USD {v:,.0f}"
    return f"{v:,.2f}"

def _fmt_delta(d):
    v, p, u = d.get("val"), d.get("prev"), d.get("unit","")
    if v is None or p is None: return None
    try:
        diff = v - float(p)
        if u == "%":               return f"{diff:+.1f}pp vs período ant."
        if u in ("B USD","B CLP"): return f"{diff:+.2f} vs período ant."
        if u == "M USD":           return f"USD {diff:+,.0f}M vs período ant."
        if u == "USD":             return f"USD {diff:+,.0f} vs período ant."
        return f"{diff:+.2f} vs período ant."
    except: return None

def _render_metrics(group_data, ncols=4):
    """Render metric cards for a PIB group."""
    items = list(group_data.items())
    if not items:
        st.info("Sin datos disponibles en este momento.")
        return
    cols = st.columns(min(ncols, len(items)))
    for i, (label, d) in enumerate(items):
        col = cols[i % ncols]
        col.metric(label, _fmt_val(d), delta=_fmt_delta(d))
        col.caption(f"📅 {d.get('ref','—')} · **BCCh**")
        if (i + 1) % ncols == 0 and i + 1 < len(items):
            cols = st.columns(min(ncols, len(items) - i - 1))

def _render_table(group_data):
    """Render a comparison table for a PIB group."""
    rows = []
    for label, d in group_data.items():
        rows.append({
            "Indicador":        label,
            "Valor Actual":     _fmt_val(d),
            "Período Anterior": _fmt_val({**d, "val": d.get("prev")}),
            "Variación":        _fmt_delta(d) or "—",
            "Período":          d.get("ref","—"),
            "Unidad":           d.get("unit","—"),
        })
    if rows:
        df = pd.DataFrame(rows).set_index("Indicador")
        st.dataframe(df, width="stretch")

def _wb_chart(wb_hist, series_label, title, y_label=""):
    """Render a World Bank historical line chart."""
    series = wb_hist.get(series_label, {})
    if not series: return
    df_chart = pd.DataFrame({"Año": list(series.keys()), y_label or title: list(series.values())})
    df_chart = df_chart.set_index("Año")
    st.line_chart(df_chart, height=220)
    st.caption(f"Fuente: World Bank Open Data · Chile · {series_label}")

# ─── 5-tab PIB section ────────────────────────────────────────────
tab_tasas, tab_vals, tab_comp, tab_sect, tab_ext = st.tabs([
    "📈 Tasas de Crecimiento",
    "💰 Valores Absolutos",
    "🔗 Componentes (Demanda)",
    "🏭 PIB por Sector",
    "🌐 Externo y Fiscal",
])

with tab_tasas:
    st.markdown("##### Tasas de crecimiento real — BCCh / INE")
    _render_metrics(pib.get("tasas", {}))

    st.markdown("---")
    st.markdown("**📊 Historial anual — Crecimiento del PIB (%, 2010–2024)**")
    _wb_chart(wb_hist, "Crecimiento PIB (%)", "Crecimiento PIB", "% anual")

    with st.expander("📋 Tabla comparativa — tasas actuales", expanded=False):
        _render_table(pib.get("tasas", {}))

with tab_vals:
    st.markdown("##### Tamaño del PIB y PIB per cápita — BCCh / World Bank")
    _render_metrics(pib.get("valores", {}))

    if usd_clp_rate:
        pib_nom = pib.get("valores",{}).get("PIB Nominal",{}).get("val")
        percap  = pib.get("valores",{}).get("PIB per cápita",{}).get("val")
        if pib_nom and percap:
            st.info(
                f"**Equivalencia en pesos chilenos** (USD/CLP = ${usd_clp_rate:,.2f}):  \n"
                f"- PIB Nominal: **CLP {pib_nom * usd_clp_rate / 1_000:,.1f}B**  \n"
                f"- PIB per cápita: **CLP {percap * usd_clp_rate:,.0f}** por persona"
            )

    st.markdown("---")
    col_h1, col_h2 = st.columns(2)
    with col_h1:
        st.markdown("**📊 PIB Nominal (B USD) — 2010–2024**")
        _wb_chart(wb_hist, "PIB Nominal (B USD)", "PIB Nominal", "B USD")
    with col_h2:
        st.markdown("**📊 PIB per cápita (USD) — 2010–2024**")
        _wb_chart(wb_hist, "PIB per cápita (USD)", "PIB per cápita", "USD")

    col_h3, col_h4 = st.columns(2)
    with col_h3:
        st.markdown("**📊 PIB per cápita PPP (USD) — 2010–2024**")
        _wb_chart(wb_hist, "PIB per cápita PPP (USD)", "PPP per cápita", "USD")
    with col_h4:
        pass

    with st.expander("📋 Tabla histórica anual — valores absolutos", expanded=False):
        hist_labels = ["PIB Nominal (B USD)", "PIB per cápita (USD)", "PIB per cápita PPP (USD)"]
        rows_h = {}
        for lbl in hist_labels:
            for yr, v in wb_hist.get(lbl, {}).items():
                rows_h.setdefault(yr, {})[lbl] = v
        if rows_h:
            df_h = pd.DataFrame(rows_h).T.sort_index(ascending=False)
            st.dataframe(df_h, width="stretch")

    with st.expander("📋 Tabla comparativa — valores actuales", expanded=False):
        _render_table(pib.get("valores", {}))

with tab_comp:
    st.markdown("##### Componentes de la demanda agregada — BCCh")
    _render_metrics(pib.get("componentes", {}))

    exp = pib.get("componentes",{}).get("Exportaciones",{}).get("val")
    imp = pib.get("componentes",{}).get("Importaciones",{}).get("val")
    if exp and imp:
        bal   = exp - imp
        c_b1, c_b2, c_b3, _ = st.columns([1,1,1,1])
        c_b1.metric("Exportaciones", f"USD {exp:,.0f}M")
        c_b2.metric("Importaciones", f"USD {imp:,.0f}M")
        c_b3.metric("Balanza neta", f"USD {bal:,.0f}M",
                    delta="🟢 superávit" if bal >= 0 else "🔴 déficit")

    st.markdown("---")
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.markdown("**📊 Consumo de hogares (% PIB) — histórico**")
        _wb_chart(wb_hist, "Consumo Hogares (% PIB)", "Consumo Hogares", "% PIB")
    with col_c2:
        st.markdown("**📊 Formación bruta de capital fijo (% PIB) — histórico**")
        _wb_chart(wb_hist, "FBCF (% PIB)", "FBCF", "% PIB")

    col_c3, col_c4 = st.columns(2)
    with col_c3:
        st.markdown("**📊 Exportaciones (% PIB) — histórico**")
        _wb_chart(wb_hist, "Exportaciones (% PIB)", "Exportaciones", "% PIB")
    with col_c4:
        st.markdown("**📊 Importaciones (% PIB) — histórico**")
        _wb_chart(wb_hist, "Importaciones (% PIB)", "Importaciones", "% PIB")

    with st.expander("📋 Tabla comparativa — componentes actuales", expanded=False):
        _render_table(pib.get("componentes", {}))

with tab_sect:
    st.markdown("##### PIB por sector productivo — Banco Central de Chile")
    st.caption("Valores en CLP Billones · Dec 2025 · Fuente: BCCh vía TradingEconomics")
    sectores = pib.get("sectores", {})

    if sectores:
        _render_metrics(sectores, ncols=4)

        st.markdown("---")
        st.markdown("**📊 Aporte de cada sector al PIB (CLP Billones, período actual)**")

        sect_vals = {k: d.get("val", 0) for k, d in sectores.items() if d.get("val")}
        prev_vals = {k: d.get("prev", 0) for k, d in sectores.items() if d.get("prev")}

        if sect_vals:
            df_sect = pd.DataFrame({
                "Actual (B CLP)":   pd.Series(sect_vals),
                "Anterior (B CLP)": pd.Series(prev_vals),
            }).sort_values("Actual (B CLP)", ascending=False)
            st.bar_chart(df_sect, height=350)

        with st.expander("📋 Tabla comparativa — sectores actuales vs anterior", expanded=True):
            rows_s = []
            for label, d in sectores.items():
                v, p = d.get("val"), d.get("prev")
                pct = f"{(v-p)/p*100:+.1f}%" if v and p and p != 0 else "—"
                rows_s.append({
                    "Sector":            label,
                    "Actual (B CLP)":    f"{v:,.2f}" if v else "—",
                    "Anterior (B CLP)":  f"{p:,.2f}" if p else "—",
                    "Variación %":       pct,
                    "Período":           d.get("ref","—"),
                })
            df_s = pd.DataFrame(rows_s).set_index("Sector")
            st.dataframe(df_s, width="stretch")
    else:
        st.info("Sin datos de sector disponibles en este momento.")

with tab_ext:
    st.markdown("##### Sector externo, reservas y posición fiscal — BCCh")
    _render_metrics(pib.get("externo", {}))

    st.link_button("📊 Estadísticas BCCh — Sector Externo",
        "https://www.bcentral.cl/web/banco-central/areas/estadisticas/sector-externo")

    with st.expander("📋 Tabla comparativa — sector externo y fiscal", expanded=False):
        _render_table(pib.get("externo", {}))

st.divider()

# ──────────────────────────────────────────────────────────────
# SECCIÓN 4 – BOLSA DE SANTIAGO
# ──────────────────────────────────────────────────────────────
st.subheader("📈 Bolsa de Santiago — Mercado Accionario")

ipsa = mkt.get("IPSA",{})
ci1, ci2, _ = st.columns([1,1,2])
if ipsa:
    ci1.metric("IPSA",f"{ipsa['val']:,.2f} pts",
               delta=f"{ipsa['delta']:+.2f} pts" if ipsa.get("delta") else None)
    ci1.caption(f"📅 Cierre {ipsa['fecha']} · **{ipsa['bolsa']}**")
else:
    ci1.metric("IPSA","—")

st.divider()

# ──────────────────────────────────────────────────────────────
# SECCIÓN 5 – COMMODITIES (NYSE / COMEX)
# ──────────────────────────────────────────────────────────────
st.subheader("🛢️ Commodities — Bolsas de Nueva York (COMEX / NYMEX)")
if usd_clp_rate:
    st.caption(f"Tipo de cambio para conversión: **USD/CLP = ${usd_clp_rate:,.2f}** · Yahoo Finance")

cc1, cc2, cc3 = st.columns(3)
_commodity_block(cc1, "Cobre (COMEX)", mkt.get("Cobre",{}))
_commodity_block(cc2, "Petróleo WTI",  mkt.get("WTI",{}))
_commodity_block(cc3, "Petróleo Brent",mkt.get("Brent",{}))

st.divider()

# ──────────────────────────────────────────────────────────────
# SECCIÓN 6 – NOTICIAS ECONÓMICAS + PANEL BCCh
# ──────────────────────────────────────────────────────────────
cn, cb = st.columns([3, 2])

with cn:
    st.subheader("📰 Noticias Económicas")
    st.caption("Fuentes: Diario Financiero · Radio Universidad de Chile · Emol · (deduplicadas y clasificadas)")

    noticias = fetch_noticias()

    # Filtros
    fc1, fc2 = st.columns([2, 2])
    with fc1:
        filtro_impacto = st.selectbox(
            "Filtrar por impacto",
            ["Todos","🔴 Negativo","🟡 Neutral","🟢 Positivo"],
            key="filtro_impacto"
        )
    all_indicadores = sorted({ind for n in noticias for ind in n.get("indicadores",[])})
    with fc2:
        filtro_ind = st.selectbox(
            "Filtrar por indicador afectado",
            ["Todos"] + all_indicadores,
            key="filtro_ind"
        )

    filtered = noticias
    if filtro_impacto != "Todos":
        filtered = [n for n in filtered if n["impacto"] == filtro_impacto]
    if filtro_ind != "Todos":
        filtered = [n for n in filtered if filtro_ind in n.get("indicadores",[])]

    if filtered:
        for n in filtered:
            inds  = " · ".join(f"`{i}`" for i in n["indicadores"])
            fuent = " · ".join(n.get("fuentes",[]))
            with st.expander(f"{n['impacto']}  {n['titulo']}"):
                if n.get("resumen"):
                    st.write(n["resumen"])
                st.markdown(f"**Indicadores afectados:** {inds}")
                st.caption(f"Fuente: {fuent}")
                st.link_button("Leer nota completa →", n["link"])
    else:
        st.info("No hay noticias para los filtros seleccionados.")

with cb:
    st.subheader("🏦 Banco Central de Chile")
    tpm = minind.get("TPM", {})
    st.info(
        f"**TPM: {tpm.get('valor','—')}%**  \n"
        f"Vigente desde: {tpm.get('fecha','—')}  \n\n"
        f"**IPC Anual (mar 2026):** {_pct(ipc['yoy']) if ipc and 'yoy' in ipc else '—'}  \n"
        f"**IPC Mensual (mar 2026):** {_pct(ipc.get('mom')) if ipc else '—'}  \n"
        f"**PIB Anual:** {_fmt_val(pib.get('tasas',{}).get('PIB Anual',{}))}  \n"
        f"**PIB per cápita:** {_fmt_val(pib.get('valores',{}).get('PIB per cápita',{}))}  \n"
        f"**Exportaciones:** {_fmt_val(pib.get('componentes',{}).get('Exportaciones',{}))}  \n"
        f"**Balanza Comercial:** {_fmt_val(pib.get('componentes',{}).get('Balanza Comercial',{}))}"
    )
    st.markdown("**🔗 Fuentes oficiales:**")
    st.link_button("📊 Estadísticas BCCh",
        "https://si3.bcentral.cl/SietePublico/SP/ENEconomicIndicators")
    st.link_button("📈 TPM y Estadísticas Diarias",
        "https://www.bcentral.cl/inicio/-/asset_publisher/W4MRFDiNPxMq/content/tasa-de-politica-monetaria-y-estadisticas-diarias")
    st.link_button("🏛️ IPC — INE Chile",
        "https://www.ine.gob.cl/estadisticas/economia/indices-de-precio-e-inflacion/ipc")
    st.link_button("📉 PIB — BCCh",
        "https://www.bcentral.cl/web/banco-central/areas/estadisticas/sector-real")

st.divider()
col_btn, _ = st.columns([1,5])
with col_btn:
    if st.button("🔄 Forzar actualización"):
        st.cache_data.clear()
        st.rerun()

st.caption(
    "**Fuentes:** [BCCh](https://www.bcentral.cl) · [INE](https://www.ine.gob.cl) · "
    "[Mindicador.cl](https://mindicador.cl) · [TradingEconomics](https://tradingeconomics.com/chile) · "
    "[Yahoo Finance](https://finance.yahoo.com) · [DF](https://df.cl) · "
    "[Radio U. de Chile](https://radio.uchile.cl) · [Emol](https://www.emol.com)"
)
