import re
import streamlit as st
import yfinance as yf
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from difflib import SequenceMatcher

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
    Extract most recent Actual/Previous from a TE calendar table.
    Table structure:
      [i]   "Actual"
      [i+1] "Previous"
      [i+2] "Consensus"
      [i+3] "TEForecast"
      [i+4] date
      [i+5] time
      [i+6] indicator name
      [i+7] period (e.g. "Feb")
      [i+8] actual value
      [i+9] previous value
    """
    for i, l in enumerate(lines):
        if l == "Actual" and i + 9 < len(lines) and lines[i+1] == "Previous":
            try:
                return {
                    "actual":   lines[i+8],
                    "previous": lines[i+9],
                    "ref":      f"{lines[i+7]} {lines[i+4][:4]}",  # e.g. "Feb 2026"
                }
            except: pass
    return {}

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

@st.cache_data(ttl=3600)
def fetch_pib_bloque():
    """
    PIB y derivados del Sector Real — Banco Central de Chile.
    Datos via TradingEconomics (cita BCCh como fuente).
    """
    out = {}
    configs = [
        ("PIB QoQ",          "/chile/gdp-growth",           "GDP Growth Rate QoQ"),
        ("PIB Anual",        "/chile/gdp-growth-annual",    "GDP Growth Rate YoY"),
        ("Exportaciones",    "/chile/exports",              "Exports"),
        ("Importaciones",    "/chile/imports",              "Imports"),
        ("Balanza Comercial","/chile/balance-of-trade",     "Balance of Trade"),
        ("Producción Ind.",  "/chile/industrial-production","Industrial Production YoY"),
        ("Ventas Retail",    "/chile/retail-sales-annual",  "Retail Sales YoY"),
        ("Inversión (FBCF)", "/chile/gross-fixed-capital-formation","Gross Fixed Capital"),
    ]
    for label, path, te_name in configs:
        try:
            lines = _te(path)
            full  = " ".join(lines)
            is_pct = any(x in te_name for x in ["Growth","Production","Sales"])
            # Try calendar first
            cal = _parse_te_calendar(lines)
            if cal and cal.get("actual"):
                raw_a = cal["actual"].replace("$","").replace(",","")
                raw_p = cal.get("previous","").replace("$","").replace(",","")
                mult_a = 1000 if raw_a.upper().endswith("B") else 1
                mult_p = 1000 if raw_p.upper().endswith("B") else 1
                raw_a = raw_a.rstrip("MBK%")
                raw_p = raw_p.rstrip("MBK%")
                try:
                    val_f  = float(raw_a) * mult_a
                    prev_f = float(raw_p) * mult_p
                    out[label] = {
                        "val":  val_f,
                        "prev": prev_f,
                        "ref":  cal.get("ref","—"),
                        "unit": "%" if is_pct else "M USD",
                    }
                    continue
                except: pass
            # Fallback: summary sentence
            nums = re.findall(r"(-?\d+\.?\d*)\s*percent", full, re.I)
            if nums:
                out[label] = {"val": float(nums[0]), "prev": float(nums[1]) if len(nums)>1 else None,
                              "ref": "reciente", "unit": "%"}
        except Exception:
            pass
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
    f"Actualizado: {datetime.now().strftime('%d/%m/%Y %H:%M')}  ·  "
    "Mercados al cierre del viernes 10/04/2026  ·  "
    "Publicaciones mensuales según última disponibilidad oficial"
)
st.divider()

with st.spinner("Cargando datos…"):
    minind = fetch_mindicador()
    hist   = fetch_historial()
    ipc    = fetch_ipc()
    desem  = fetch_desempleo()
    pib    = fetch_pib_bloque()
    mkt    = fetch_mercados()

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
    import pandas as pd
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
# SECCIÓN 3 – PIB Y SECTOR REAL (BCCh)
# ──────────────────────────────────────────────────────────────
st.subheader("🏛️ PIB y Sector Real — Banco Central de Chile")
st.caption("Datos publicados por el BCCh, consolidados vía TradingEconomics")

pib_order = [
    ("PIB QoQ",          "%"),
    ("PIB Anual",        "%"),
    ("Exportaciones",    "M USD"),
    ("Importaciones",    "M USD"),
    ("Balanza Comercial","M USD"),
    ("Producción Ind.",  "%"),
    ("Ventas Retail",    "%"),
    ("Inversión (FBCF)", "%"),
]

rows = [k for k, _ in pib_order if k in pib]
if rows:
    cols_pib = st.columns(min(4, len(rows)))
    for i, key in enumerate(rows):
        d    = pib[key]
        col  = cols_pib[i % 4]
        val  = d.get("val")
        prev = d.get("prev")
        unit = d.get("unit", "%")
        ref  = d.get("ref","—")

        if unit == "%":
            val_str  = _pct(val)
            try:
                diff     = val - float(prev)
                prev_str = f"{diff:+.1f}pp vs período ant."
            except: prev_str = None
        else:
            val_str  = f"USD {val:,.0f}M" if val is not None else "—"
            try:
                diff     = val - float(prev)
                prev_str = f"USD {diff:+,.0f}M vs período ant."
            except: prev_str = None

        col.metric(key, val_str, delta=prev_str)
        col.caption(f"📅 {ref} · **BCCh**")

        if (i + 1) % 4 == 0 and i + 1 < len(rows):
            cols_pib = st.columns(min(4, len(rows) - i - 1))
else:
    st.info("No se pudieron cargar los datos del sector real.")

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
        f"**PIB YoY (Q4 2025):** {_pct(pib.get('PIB Anual',{}).get('val')) if pib else '—'}  \n"
        f"**Exportaciones (feb):** USD {pib.get('Exportaciones',{}).get('val',0):,.0f}M  \n"
        f"**Balanza Comercial (feb):** USD {pib.get('Balanza Comercial',{}).get('val',0):,.0f}M"
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
