import re
import streamlit as st
import yfinance as yf
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone

st.set_page_config(page_title="MIS Macroeconómico Chile", layout="wide")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

# ─── Helpers ───────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 20) -> str:
    return requests.get(url, headers=_HEADERS, timeout=timeout).text

def _soup_text(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [l.strip() for l in soup.get_text().split("\n") if l.strip()]

def _te(path: str) -> list[str]:
    return _soup_text(_get(f"https://tradingeconomics.com{path}"))

def fmt_iso(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d/%m/%Y")

# ─── Fetchers ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600)
def fetch_mindicador() -> dict:
    """UF, Dólar, TPM, Cobre — Mindicador.cl (datos BCCh/INE)."""
    out = {}
    try:
        data = requests.get("https://mindicador.cl/api", headers=_HEADERS, timeout=10).json()
        for api_key, label in [
            ("uf",          "UF"),
            ("dolar",       "Dólar / CLP"),
            ("tpm",         "TPM"),
            ("libra_cobre", "Cobre (USD/lb)"),
            ("euro",        "Euro / CLP"),
        ]:
            e = data[api_key]
            out[label] = {"valor": e["valor"], "fecha": fmt_iso(e["fecha"])}
    except Exception:
        pass
    return out


@st.cache_data(ttl=1800)
def fetch_ipc() -> dict | None:
    """
    IPC Anual y Mensual de marzo 2026.
    Fuente primaria: INE / BCCh publicado en TradingEconomics.
    """
    try:
        lines = _te("/chile/inflation-cpi")
        full  = " ".join(lines)

        # IPC Anual
        m_yoy = re.search(
            r"Inflation Rate in Chile (?:increased|decreased|rose|fell|held|remained)"
            r".*?(\d+\.?\d*)\s*percent\s*in\s*([\w]+)\s*from\s*(\d+\.?\d*)\s*percent"
            r"\s*in\s*([\w]+)\s*of\s*(\d{4})",
            full, re.IGNORECASE,
        )
        # IPC Mensual — buscar en líneas del calendario
        # "Chile's annual inflation … On a monthly basis, prices rose 1.0% following…"
        m_mom = re.search(
            r"monthly basis.*?(?:rose|fell|increased|decreased)\s+(\d+\.?\d*)\s*%"
            r".*?following\s+(?:a\s+)?(\d+\.?\d*)\s*%",
            full, re.IGNORECASE | re.DOTALL,
        )

        result: dict = {"periodo": "Mar 2026", "fuente": "INE / BCCh"}

        if m_yoy:
            result["yoy"]      = float(m_yoy.group(1))
            result["yoy_ant"]  = float(m_yoy.group(3))
            result["mes"]      = m_yoy.group(2)
            result["mes_ant"]  = m_yoy.group(4)
            result["año"]      = m_yoy.group(5)
        if m_mom:
            result["mom"]      = float(m_mom.group(1))
            result["mom_ant"]  = float(m_mom.group(2))

        # Fallback: buscar en tabla Related (líneas numéricas)
        if "yoy" not in result:
            for i, l in enumerate(lines):
                if l == "Inflation Rate YoY" and i + 2 < len(lines):
                    try:
                        result["yoy"] = float(lines[i + 1].replace("%", ""))
                        result["yoy_ant"] = float(lines[i + 2].replace("%", ""))
                    except Exception:
                        pass
                    break
        if "mom" not in result:
            for i, l in enumerate(lines):
                if l == "Inflation Rate MoM" and i + 2 < len(lines):
                    try:
                        result["mom"] = float(lines[i + 1].replace("%", ""))
                        result["mom_ant"] = float(lines[i + 2].replace("%", ""))
                    except Exception:
                        pass
                    break

        return result if ("yoy" in result or "mom" in result) else None
    except Exception:
        return None


@st.cache_data(ttl=1800)
def fetch_pib() -> dict | None:
    """
    PIB Anual (YoY) y Crecimiento 2025 completo.
    Fuente: Banco Central de Chile publicado en TradingEconomics.
    """
    try:
        lines = _te("/chile/gdp-growth-annual")
        full  = " ".join(lines)

        result: dict = {"fuente": "BCCh"}

        # PIB YoY último trimestre
        m = re.search(
            r"GDP.*?expanded\s+(-?\d+\.?\d*)\s*percent\s*in\s*the\s*([\w]+)\s+quarter\s+of\s+(\d{4})",
            full, re.IGNORECASE,
        )
        if m:
            result["yoy"]        = float(m.group(1))
            quarter_map          = {"first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4"}
            result["trimestre"]  = f"{quarter_map.get(m.group(2).lower(), m.group(2))} {m.group(3)}"

        # Crecimiento año completo
        for i, l in enumerate(lines):
            if "Full Year GDP Growth" in l and i + 2 < len(lines):
                try:
                    result["full_year"] = float(lines[i + 1])
                    result["full_year_prev"] = float(lines[i + 2])
                    result["full_year_ref"] = "2025"
                except Exception:
                    pass
                break

        return result if ("yoy" in result or "full_year" in result) else None
    except Exception:
        return None


@st.cache_data(ttl=1800)
def fetch_desempleo() -> dict | None:
    """Desempleo desde INE / BCCh vía TradingEconomics."""
    try:
        lines = _te("/chile/unemployment-rate")
        full  = " ".join(lines)
        m = re.search(
            r"Unemployment Rate in Chile\s+(?:remained unchanged at|increased to|fell to|decreased to|rose to)"
            r"\s+(\d+\.?\d*)\s*percent\s*in\s*([\w]+)",
            full, re.IGNORECASE,
        )
        if m:
            return {"valor": float(m.group(1)), "periodo": m.group(2), "fuente": "INE / BCCh"}
    except Exception:
        pass
    return None


@st.cache_data(ttl=600)
def fetch_mercados() -> dict:
    """
    IPSA (Bolsa de Santiago), Cobre/WTI/Brent (NYSE/COMEX Nueva York).
    Retorna valores en USD y CLP.
    """
    SYMS = {
        "IPSA":  {"sym": "^IPSA",  "bolsa": "Bolsa de Santiago",       "moneda": "CLP", "unidad": "pts"},
        "Cobre": {"sym": "HG=F",   "bolsa": "COMEX — Bolsa de Nueva York", "moneda": "USD", "unidad": "USD/lb"},
        "WTI":   {"sym": "CL=F",   "bolsa": "NYMEX — Bolsa de Nueva York", "moneda": "USD", "unidad": "USD/bbl"},
        "Brent": {"sym": "BZ=F",   "bolsa": "NYMEX — Bolsa de Nueva York", "moneda": "USD", "unidad": "USD/bbl"},
    }
    # Tipo de cambio USD/CLP desde Yahoo Finance
    usd_clp = None
    try:
        fx = yf.Ticker("CLP=X").history(period="5d")
        if not fx.empty:
            usd_clp = float(fx["Close"].iloc[-1])
    except Exception:
        pass

    out = {}
    for name, cfg in SYMS.items():
        try:
            hist = yf.Ticker(cfg["sym"]).history(period="5d")
            if not hist.empty:
                last  = float(hist["Close"].iloc[-1])
                delta = float(last - hist["Close"].iloc[-2]) if len(hist) >= 2 else None
                fecha = hist.index[-1].strftime("%d/%m/%Y")
                entry = {
                    "val":    last,
                    "delta":  delta,
                    "fecha":  fecha,
                    "bolsa":  cfg["bolsa"],
                    "moneda": cfg["moneda"],
                    "unidad": cfg["unidad"],
                    "usd_clp": usd_clp,
                }
                # Convertir a CLP si el precio está en USD
                if cfg["moneda"] == "USD" and usd_clp:
                    entry["val_clp"]   = last * usd_clp
                    entry["delta_clp"] = delta * usd_clp if delta is not None else None
                out[name] = entry
        except Exception:
            pass
    return out


def fetch_noticias() -> list:
    """Noticias RSS — Diario Financiero."""
    try:
        feed = feedparser.parse(
            "https://www.df.cl/noticias/site/tax/port/all/rss_politica_y_economia.xml"
        )
    except Exception:
        return []
    out = []
    for entry in feed.entries[:5]:
        t = entry.title.lower()
        if any(w in t for w in ["cae", "baja", "crisis", "freno", "contrae", "recesión", "déficit", "caída"]):
            badge = "🔴 Negativo"
        elif any(w in t for w in ["sube", "crece", "alza", "mejora", "recupera", "repunta", "superávit", "récord"]):
            badge = "🟢 Positivo"
        else:
            badge = "🟡 Neutral"
        resumen = getattr(entry, "summary", "")
        out.append({
            "titulo":  entry.title,
            "link":    entry.link,
            "badge":   badge,
            "resumen": resumen[:220] + "…" if len(resumen) > 220 else resumen,
        })
    return out

# ─── Formato ───────────────────────────────────────────────────────────────

def _pesos(v) -> str:
    if v is None: return "—"
    return f"${v:,.2f}" if isinstance(v, float) and v != int(v) else f"${int(v):,}"

def _pct(v, decimals: int = 1) -> str:
    if v is None: return "—"
    return f"{v:.{decimals}f}%"

# ══════════════════════════════════════════════════════════════════════════
# INTERFAZ
# ══════════════════════════════════════════════════════════════════════════

st.title("📊 Dashboard Macroeconómico Chile")
st.caption(
    f"Actualizado: {datetime.now().strftime('%d/%m/%Y %H:%M')} · "
    "Datos al cierre del viernes 10/04/2026 para mercados · "
    "Publicaciones mensuales según última disponibilidad oficial"
)
st.divider()

# ── Carga de datos ─────────────────────────────────────────────────────────
with st.spinner("Cargando datos en tiempo real…"):
    minind  = fetch_mindicador()
    ipc     = fetch_ipc()
    pib     = fetch_pib()
    desem   = fetch_desempleo()
    mkt     = fetch_mercados()

# ── Sección 1: Indicadores Monetarios y de Precios ────────────────────────
st.subheader("📌 Política Monetaria y Precios — Banco Central / INE")

c1, c2, c3, c4 = st.columns(4)

# UF
with c1:
    d = minind.get("UF", {})
    st.metric("UF", _pesos(d.get("valor")))
    st.caption(f"📅 Al {d.get('fecha','—')} · BCCh vía Mindicador.cl")

# Dólar
with c2:
    d = minind.get("Dólar / CLP", {})
    st.metric("Dólar / CLP", _pesos(d.get("valor")))
    st.caption(f"📅 Al {d.get('fecha','—')} · BCCh vía Mindicador.cl")

# TPM
with c3:
    d = minind.get("TPM", {})
    st.metric("TPM (Tasa Política Monetaria)", _pct(d.get("valor")))
    st.caption(f"📅 Vigente {d.get('fecha','—')} · **Banco Central de Chile**")

# Euro
with c4:
    d = minind.get("Euro / CLP", {})
    st.metric("Euro / CLP", _pesos(d.get("valor")))
    st.caption(f"📅 Al {d.get('fecha','—')} · BCCh vía Mindicador.cl")

st.markdown("---")

# IPC y PIB fila
c5, c6, c7, c8 = st.columns(4)

# IPC Anual
with c5:
    if ipc and "yoy" in ipc:
        delta_yoy = ipc["yoy"] - ipc.get("yoy_ant", ipc["yoy"])
        st.metric(
            "IPC Anual",
            _pct(ipc["yoy"]),
            delta=f"{delta_yoy:+.1f}pp vs {ipc.get('mes_ant','mes ant.')}",
        )
        st.caption(f"📅 {ipc.get('periodo','Mar 2026')} · {ipc.get('fuente','INE / BCCh')}")
    else:
        st.metric("IPC Anual", "—")

# IPC Mensual
with c6:
    if ipc and "mom" in ipc:
        delta_mom = ipc.get("mom", 0) - ipc.get("mom_ant", 0)
        st.metric(
            "IPC Mensual",
            _pct(ipc["mom"]),
            delta=f"{delta_mom:+.1f}pp vs mes anterior",
        )
        st.caption(f"📅 {ipc.get('periodo','Mar 2026')} · {ipc.get('fuente','INE / BCCh')}")
    else:
        st.metric("IPC Mensual", "—")

# PIB Anual
with c7:
    if pib and "yoy" in pib:
        st.metric("PIB Anual (YoY)", _pct(pib["yoy"]))
        st.caption(f"📅 {pib.get('trimestre','Q4 2025')} · **{pib.get('fuente','BCCh')}**")
    else:
        st.metric("PIB Anual", "—")

# Crecimiento Año Completo
with c8:
    if pib and "full_year" in pib:
        st.metric(
            f"Crecimiento {pib.get('full_year_ref','2025')}",
            _pct(pib["full_year"]),
            delta=f"ant. {pib.get('full_year_prev','—')}%",
        )
        st.caption(f"📅 Año completo · **{pib.get('fuente','BCCh')}**")
    else:
        st.metric("Crecimiento Anual", "—")

st.divider()

# ── Sección 2: Empleo ──────────────────────────────────────────────────────
st.subheader("👷 Mercado Laboral")

c9, c9b, _, _ = st.columns(4)
with c9:
    if desem:
        st.metric("Tasa de Desempleo", _pct(desem["valor"]))
        st.caption(f"📅 {desem.get('periodo','feb 2026')} · {desem.get('fuente','INE / BCCh')}")
    else:
        st.metric("Desempleo", "—")

st.divider()

# ── Sección 3: Bolsa de Santiago ───────────────────────────────────────────
st.subheader("📈 Bolsa de Santiago — Mercado Accionario")

ipsa = mkt.get("IPSA", {})
if ipsa:
    col_ipsa, _ = st.columns([1, 3])
    with col_ipsa:
        st.metric(
            "IPSA",
            f"{ipsa['val']:,.2f} pts",
            delta=f"{ipsa['delta']:+.2f} pts" if ipsa.get("delta") is not None else None,
        )
        st.caption(f"📅 Cierre {ipsa['fecha']} · **{ipsa['bolsa']}**")
else:
    st.warning("Sin datos IPSA disponibles.")

st.divider()

# ── Sección 4: Commodities — Bolsas de Nueva York ──────────────────────────
st.subheader("🛢️ Commodities — Bolsas de Nueva York (COMEX / NYMEX)")

usd_clp = None
for v in mkt.values():
    if v.get("usd_clp"):
        usd_clp = v["usd_clp"]
        break

if usd_clp:
    st.caption(f"Tipo de cambio utilizado para conversión: **USD/CLP = ${usd_clp:,.2f}** · Bolsa de Nueva York")

cobre = mkt.get("Cobre", {})
wti   = mkt.get("WTI",   {})
brent = mkt.get("Brent", {})

cc1, cc2, cc3 = st.columns(3)

def _commodity_card(col, nombre: str, data: dict):
    with col:
        if not data:
            st.metric(nombre, "—")
            return
        usd    = data["val"]
        clp    = data.get("val_clp")
        d_usd  = data.get("delta")
        unidad = data["unidad"]
        fecha  = data["fecha"]
        bolsa  = data["bolsa"]

        # Precio principal en USD
        st.metric(
            f"{nombre}",
            f"USD {usd:,.3f}" if "lb" in unidad else f"USD {usd:,.2f}",
            delta=f"USD {d_usd:+.3f}" if d_usd is not None and "lb" in unidad
                  else (f"USD {d_usd:+.2f}" if d_usd is not None else None),
        )
        # Equivalente en CLP
        if clp:
            d_clp = data.get("delta_clp")
            st.markdown(
                f"<span style='color:#888;font-size:0.85em'>≈ **${clp:,.0f} CLP** "
                f"{'/ '+unidad.split('/')[1] if '/' in unidad else ''}"
                f"{f' ({d_clp:+,.0f})' if d_clp else ''}</span>",
                unsafe_allow_html=True,
            )
        st.caption(f"📅 Al {fecha} · **{bolsa}**")

_commodity_card(cc1, "Cobre (COMEX)",   cobre)
_commodity_card(cc2, "Petróleo WTI",    wti)
_commodity_card(cc3, "Petróleo Brent",  brent)

st.divider()

# ── Sección 5: Noticias + Panel BCCh ──────────────────────────────────────
c_news, c_bc = st.columns([3, 2])

with c_news:
    st.subheader("📰 Noticias Económicas")
    st.caption("Fuente: RSS Diario Financiero — Política y Economía")
    noticias = fetch_noticias()
    if noticias:
        for n in noticias:
            with st.expander(f"{n['badge']}  {n['titulo']}"):
                st.write(n["resumen"])
                st.link_button("Leer nota completa →", n["link"])
    else:
        st.info("No se pudieron cargar noticias. Intenta más tarde.")

with c_bc:
    st.subheader("🏦 Banco Central de Chile")
    tpm_val   = minind.get("TPM", {}).get("valor", "—")
    tpm_fecha = minind.get("TPM", {}).get("fecha", "—")

    st.info(
        f"**TPM:** {tpm_val}%  \n"
        f"Vigente desde: {tpm_fecha}  \n\n"
        f"**IPC Anual (marzo 2026):** {_pct(ipc['yoy']) if ipc and 'yoy' in ipc else '—'}  \n"
        f"**PIB Q4 2025 (YoY):** {_pct(pib['yoy']) if pib and 'yoy' in pib else '—'}  \n"
        f"**Crecimiento 2025:** {_pct(pib.get('full_year')) if pib else '—'}"
    )

    st.markdown("**🔗 Fuentes Oficiales:**")
    st.link_button("📊 Estadísticas BCCh", "https://si3.bcentral.cl/SietePublico/SP/ENEconomicIndicators")
    st.link_button("📈 TPM y Estadísticas Diarias", "https://www.bcentral.cl/inicio/-/asset_publisher/W4MRFDiNPxMq/content/tasa-de-politica-monetaria-y-estadisticas-diarias")
    st.link_button("🏛️ IPC — INE Chile", "https://www.ine.gob.cl/estadisticas/economia/indices-de-precio-e-inflacion/ipc")

st.divider()

col_btn, _ = st.columns([1, 5])
with col_btn:
    if st.button("🔄 Forzar actualización"):
        st.cache_data.clear()
        st.rerun()

st.caption(
    "**Fuentes:** "
    "[Banco Central de Chile](https://www.bcentral.cl) · "
    "[INE Chile](https://www.ine.gob.cl) · "
    "[Mindicador.cl](https://mindicador.cl) · "
    "[TradingEconomics](https://tradingeconomics.com/chile) · "
    "[Yahoo Finance](https://finance.yahoo.com) · "
    "[Diario Financiero](https://df.cl)"
)
