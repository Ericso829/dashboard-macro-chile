"""
Dashboard Macroeconómico Chile
================================
Fuentes: BCCh · INE · Mindicador.cl · TradingEconomics · Yahoo Finance · World Bank
Autor: —
Versión: 2.0
"""

import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import feedparser
import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

TZ_CL = ZoneInfo("America/Santiago")

HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

BASE_TE = "https://tradingeconomics.com"
BASE_WB = "https://api.worldbank.org/v2/country/CL/indicator"

# Rutas de TradingEconomics para cada indicador
TE_PATHS: dict[str, str] = {
    "ipc":              "/chile/inflation-cpi",
    "desempleo":        "/chile/unemployment-rate",
    "gdp_growth":       "/chile/gdp-growth",
    "produccion_ind":   "/chile/industrial-production",
    "retail_yoy":       "/chile/retail-sales-annual",
    "exportaciones":    "/chile/exports",
    "importaciones":    "/chile/imports",
    "balanza":          "/chile/balance-of-trade",
    "consumo_privado":  "/chile/consumer-spending",
    "gasto_gobierno":   "/chile/government-spending",
    "cuenta_corriente": "/chile/current-account",
    "cta_cte_pib":      "/chile/current-account-to-gdp",
    "deuda_externa":    "/chile/external-debt",
    "reservas":         "/chile/foreign-exchange-reserves",
    "deuda_gob_pib":    "/chile/government-debt-to-gdp",
    "balance_fiscal":   "/chile/government-budget",
    "ied":              "/chile/foreign-direct-investment",
}

# Códigos World Bank para series históricas anuales
WB_CODES: dict[str, str] = {
    "Crecimiento PIB (%)":      "NY.GDP.MKTP.KD.ZG",
    "PIB Nominal (B USD)":      "NY.GDP.MKTP.CD",
    "PIB per cápita (USD)":     "NY.GDP.PCAP.CD",
    "PIB per cápita PPP (USD)": "NY.GDP.PCAP.PP.CD",
    "Consumo Hogares (% PIB)":  "NE.CON.PETC.ZS",
    "FBCF (% PIB)":             "NE.GDI.TOTL.ZS",
    "Exportaciones (% PIB)":    "NE.EXP.GNFS.ZS",
    "Importaciones (% PIB)":    "NE.IMP.GNFS.ZS",
}

# Símbolos de mercado (Yahoo Finance)
MARKET_SYMBOLS: dict[str, dict] = {
    "IPSA":  {"sym": "^IPSA",  "bolsa": "Bolsa de Santiago",          "moneda": "CLP", "unidad": "pts"},
    "Cobre": {"sym": "HG=F",   "bolsa": "COMEX – Bolsa de Nueva York", "moneda": "USD", "unidad": "USD/lb"},
    "WTI":   {"sym": "CL=F",   "bolsa": "NYMEX – Bolsa de Nueva York", "moneda": "USD", "unidad": "USD/bbl"},
    "Brent": {"sym": "BZ=F",   "bolsa": "NYMEX – Bolsa de Nueva York", "moneda": "USD", "unidad": "USD/bbl"},
}

# Indicadores afectados por cada tema económico (para clasificar noticias)
INDICADOR_MAP: dict[str, list[str]] = {
    "IPC / Inflación":     ["ipc", "inflación", "inflacion", "precios", "cpi", "canasta", "transporte", "educación"],
    "Dólar / Tipo cambio": ["dólar", "dolar", "tipo de cambio", "usd", "peso", "paridad", "clp"],
    "TPM / Tasa":          ["tpm", "tasa", "banco central", "política monetaria", "interés", "bcentral"],
    "PIB / Crecimiento":   ["pib", "gdp", "crecimiento", "actividad", "imacec", "recesión", "recesion"],
    "Empleo":              ["empleo", "desempleo", "trabajo", "laboral", "ocupación", "cesantía"],
    "Cobre / Minería":     ["cobre", "minería", "mineria", "codelco", "libra", "producción minera"],
    "Petróleo":            ["petróleo", "petroleo", "combustible", "gasolina", "bencina", "wti", "brent"],
    "IPSA / Bolsa":        ["ipsa", "bolsa", "acciones", "mercado accionario", "bvs"],
    "Comercio Exterior":   ["exporta", "importa", "balanza", "comercio exterior", "aranceles", "arancel"],
    "Fiscal / Deuda":      ["hacienda", "presupuesto", "fiscal", "deuda", "déficit", "superávit", "tributario"],
}

# Lista plana de palabras clave económicas (usada para filtrar noticias)
ECO_KEYWORDS: list[str] = [palabra for lista in INDICADOR_MAP.values() for palabra in lista]

st.set_page_config(page_title="MIS Macroeconómico Chile", layout="wide")


# ═══════════════════════════════════════════════════════════════
# UTILIDADES DE RED
# ═══════════════════════════════════════════════════════════════

def _fetch_html(url: str, timeout: int = 20) -> str:
    """Descarga el HTML de una URL con los headers globales."""
    respuesta = requests.get(url, headers=HEADERS, timeout=timeout)
    respuesta.raise_for_status()
    return respuesta.text


def _html_a_lineas(html: str) -> list[str]:
    """Extrae líneas de texto no vacías a partir de HTML."""
    texto = BeautifulSoup(html, "html.parser").get_text()
    return [linea.strip() for linea in texto.split("\n") if linea.strip()]


def _lineas_te(path: str) -> list[str]:
    """Descarga y parsea una página de TradingEconomics como lista de líneas."""
    return _html_a_lineas(_fetch_html(f"{BASE_TE}{path}"))


# ═══════════════════════════════════════════════════════════════
# UTILIDADES DE FORMATO
# ═══════════════════════════════════════════════════════════════

def _fmt_iso(iso: str) -> str:
    """Convierte un string ISO 8601 al formato dd/mm/yyyy."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d/%m/%Y")


def _fmt_pct(valor: float | None, decimales: int = 1) -> str:
    """Formatea un número como porcentaje. Devuelve '—' si el valor es None."""
    if valor is None:
        return "—"
    return f"{valor:.{decimales}f}%"


def _fmt_usd(valor: float | None, decimales: int = 2) -> str:
    """Formatea un número como dólares USD. Devuelve '—' si el valor es None."""
    if valor is None:
        return "—"
    return f"USD {valor:,.{decimales}f}"


def _fmt_clp(valor: float | None) -> str:
    """Formatea un número como pesos chilenos. Devuelve '—' si el valor es None."""
    if valor is None:
        return "—"
    return f"${valor:,.0f}"


def _similitud(texto_a: str, texto_b: str) -> float:
    """Ratio de similitud entre dos strings (para deduplicación de noticias)."""
    return SequenceMatcher(None, texto_a.lower(), texto_b.lower()).ratio()


# ═══════════════════════════════════════════════════════════════
# PARSEO DE TRADINGECONOMICS
# ═══════════════════════════════════════════════════════════════

def _parsear_num(raw: str | None) -> float | None:
    """
    Convierte strings como '$2790M', '-1.6%', '335.52', '$-4.60B' a float.
    Retorna None si el valor no es parseable.
    """
    if not raw:
        return None
    texto = str(raw).strip()
    multiplicador = 1
    upper = texto.upper()
    if upper.endswith("B"):
        multiplicador = 1_000
    elif upper.endswith("T"):
        multiplicador = 1_000_000
    limpio = texto.replace("$", "").replace(",", "").rstrip("MBKTmbtk%")
    try:
        return float(limpio) * multiplicador
    except ValueError:
        return None


def _parsear_calendario_te(lineas: list[str]) -> dict:
    """
    Extrae el valor actual más reciente publicado desde una tabla de calendario
    de TradingEconomics. Cada fila tiene formato: YYYY-MM-DD / HH:MM / indicador /
    período / actual / anterior / ...

    Retorna un dict con keys: actual, previous, ref. Dict vacío si no hay datos.
    """
    patron_fecha = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    hoy = datetime.now(TZ_CL).date()

    # Buscar el encabezado de la tabla del calendario
    idx_encabezado = None
    for indice, linea in enumerate(lineas):
        if linea == "Actual" and indice + 1 < len(lineas) and lineas[indice + 1] == "Previous":
            idx_encabezado = indice
            break

    if idx_encabezado is None:
        return {}

    # Determinar el fin de la región del calendario
    idx_fin = len(lineas)
    for indice in range(idx_encabezado + 4, len(lineas)):
        if lineas[indice] == "Related":
            idx_fin = indice
            break

    # Recorrer filas del calendario (cada fila comienza con una fecha YYYY-MM-DD)
    mejor = {}
    indice = idx_encabezado + 4
    while indice < idx_fin - 4:
        if patron_fecha.match(lineas[indice]):
            fecha_fila   = lineas[indice]
            periodo      = lineas[indice + 3] if indice + 3 < idx_fin else "—"
            actual       = lineas[indice + 4] if indice + 4 < idx_fin else None
            anterior     = lineas[indice + 5] if indice + 5 < idx_fin else None

            # Aceptar solo filas con valor numérico publicado (no pronósticos futuros)
            if actual and re.search(r"\d", actual):
                try:
                    fecha_pub = datetime.strptime(fecha_fila, "%Y-%m-%d").date()
                    if fecha_pub <= hoy:
                        mejor = {
                            "actual":   actual,
                            "previous": anterior,
                            "ref":      f"{periodo} {fecha_fila[:4]}",
                        }
                except ValueError:
                    pass
            indice += 5
        else:
            indice += 1

    return mejor


def _parsear_tabla_related_html(html: str) -> dict[str, dict]:
    """
    Parsea la tabla HTML 'Related' de una página de TradingEconomics.
    Retorna: {nombre_indicador: {last, prev, unit, ref}}
    """
    resultado: dict[str, dict] = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tabla in soup.find_all("table"):
            encabezados = [th.get_text(strip=True) for th in tabla.find_all("th")]
            if "Last" not in encabezados or "Previous" not in encabezados:
                continue
            for fila in tabla.find_all("tr"):
                celdas = [celda.get_text(strip=True) for celda in fila.find_all("td")]
                if len(celdas) < 4 or not celdas[0] or celdas[0] in ("Related", "Last", "Previous"):
                    continue
                ultimo   = _parsear_num(celdas[1])
                anterior = _parsear_num(celdas[2])
                if ultimo is not None:
                    resultado[celdas[0]] = {
                        "last": ultimo,
                        "prev": anterior,
                        "unit": celdas[3] if len(celdas) > 3 else "",
                        "ref":  celdas[4] if len(celdas) > 4 else "",
                    }
    except Exception as exc:
        logger.warning("Error parseando tabla Related: %s", exc)
    return resultado


def _obtener_indicador_calendario(path: str, unidad: str) -> dict | None:
    """
    Obtiene el valor más reciente del calendario para un path de TradingEconomics.
    Retorna un dict {val, prev, ref, unit} o None si no hay datos.
    """
    try:
        lineas = _lineas_te(path)
        calendario = _parsear_calendario_te(lineas)
        if not calendario or not calendario.get("actual"):
            return None
        valor   = _parsear_num(calendario["actual"])
        anterior = _parsear_num(calendario.get("previous"))
        if valor is None:
            return None
        return {
            "val":  valor,
            "prev": anterior,
            "ref":  calendario.get("ref", "—"),
            "unit": unidad,
        }
    except Exception as exc:
        logger.warning("Error obteniendo calendario TE para %s: %s", path, exc)
        return None


def _obtener_indicador_related(path: str, unidad: str) -> dict | None:
    """
    Fallback: obtiene el primer valor disponible desde la tabla Related de TE.
    Retorna un dict {val, prev, ref, unit} o None si no hay datos.
    """
    try:
        html = _fetch_html(f"{BASE_TE}{path}")
        tabla = _parsear_tabla_related_html(html)
        for _, fila in tabla.items():
            if fila["last"] is not None:
                return {
                    "val":  fila["last"],
                    "prev": fila["prev"],
                    "ref":  fila.get("ref", "—"),
                    "unit": unidad,
                }
    except Exception as exc:
        logger.warning("Error obteniendo Related TE para %s: %s", path, exc)
    return None


# ═══════════════════════════════════════════════════════════════
# FETCHERS – INDICADORES BCCh / INE
# ═══════════════════════════════════════════════════════════════

@st.cache_data(ttl=3600)
def fetch_mindicador() -> dict[str, dict]:
    """
    Obtiene los indicadores diarios del BCCh desde Mindicador.cl.
    Retorna: {label: {valor, fecha}}. TTL: 1 hora.
    """
    resultado: dict[str, dict] = {}
    try:
        datos = requests.get("https://mindicador.cl/api", headers=HEADERS, timeout=10).json()
        claves = [
            ("uf",           "UF"),
            ("dolar",        "Dólar/CLP"),
            ("tpm",          "TPM"),
            ("euro",         "Euro/CLP"),
            ("libra_cobre",  "Cobre"),
            ("utm",          "UTM"),
        ]
        for clave, etiqueta in claves:
            entrada = datos[clave]
            resultado[etiqueta] = {
                "valor": entrada["valor"],
                "fecha": _fmt_iso(entrada["fecha"]),
            }
    except Exception as exc:
        logger.warning("Error obteniendo Mindicador: %s", exc)
    return resultado


@st.cache_data(ttl=3600)
def fetch_historial() -> dict[str, list[dict]]:
    """
    Retorna los últimos 5 valores mensuales de IPC, TPM, USD/CLP y UF.
    Deduplica por año-mes para evitar repeticiones. TTL: 1 hora.
    """
    historial: dict[str, list[dict]] = {}
    series = [
        ("ipc",   "IPC"),
        ("tpm",   "TPM"),
        ("dolar", "USD/CLP"),
        ("uf",    "UF"),
    ]
    for clave, etiqueta in series:
        try:
            datos = requests.get(
                f"https://mindicador.cl/api/{clave}",
                headers=HEADERS,
                timeout=10,
            ).json().get("serie", [])

            vistos: set[str] = set()
            puntos: list[dict] = []
            for registro in datos:
                anio_mes = registro["fecha"][:7]
                if anio_mes not in vistos:
                    vistos.add(anio_mes)
                    puntos.append({"fecha": anio_mes, "valor": registro["valor"]})
                if len(puntos) == 5:
                    break
            historial[etiqueta] = puntos
        except Exception as exc:
            logger.warning("Error obteniendo historial para %s: %s", etiqueta, exc)
            historial[etiqueta] = []
    return historial


@st.cache_data(ttl=1800)
def fetch_ipc() -> dict | None:
    """
    Extrae la variación anual y mensual del IPC de Chile desde TradingEconomics.
    Incluye fallback a la tabla de la página si el regex principal falla.
    Retorna dict con keys: yoy, yoy_ant, mes, mes_ant, año, mom, mom_ant. TTL: 30 min.
    """
    try:
        lineas = _lineas_te(TE_PATHS["ipc"])
        texto_completo = " ".join(lineas)

        resultado: dict = {}

        patron_yoy = re.search(
            r"Inflation Rate in Chile (?:increased|decreased|rose|fell|held).*?"
            r"(\d+\.?\d*)\s*percent\s*in\s*(\w+)\s*from\s*(\d+\.?\d*)\s*percent\s*in\s*(\w+)\s*of\s*(\d{4})",
            texto_completo,
            re.I,
        )
        patron_mom = re.search(
            r"monthly basis.*?(?:rose|fell|increased|decreased)\s+(\d+\.?\d*)\s*%"
            r".*?following\s+(?:a\s+)?(\d+\.?\d*)\s*%",
            texto_completo,
            re.I | re.S,
        )

        if patron_yoy:
            resultado.update({
                "yoy":     float(patron_yoy.group(1)),
                "yoy_ant": float(patron_yoy.group(3)),
                "mes":     patron_yoy.group(2),
                "mes_ant": patron_yoy.group(4),
                "año":     patron_yoy.group(5),
            })
        if patron_mom:
            resultado.update({
                "mom":     float(patron_mom.group(1)),
                "mom_ant": float(patron_mom.group(2)),
            })

        # Fallback desde la tabla de la página
        for indice, linea in enumerate(lineas):
            if linea == "Inflation Rate YoY" and "yoy" not in resultado and indice + 1 < len(lineas):
                try:
                    resultado["yoy"] = float(lineas[indice + 1].replace("%", ""))
                except ValueError:
                    pass
            if linea == "Inflation Rate MoM" and "mom" not in resultado and indice + 1 < len(lineas):
                try:
                    resultado["mom"] = float(lineas[indice + 1].replace("%", ""))
                except ValueError:
                    pass

        return resultado or None

    except Exception as exc:
        logger.warning("Error obteniendo IPC: %s", exc)
        return None


@st.cache_data(ttl=1800)
def fetch_desempleo() -> dict | None:
    """
    Extrae la tasa de desempleo de Chile desde TradingEconomics.
    Retorna dict {valor, periodo} o None si no hay datos. TTL: 30 min.
    """
    try:
        texto = " ".join(_lineas_te(TE_PATHS["desempleo"]))
        patron = re.search(
            r"Unemployment Rate in Chile\s+"
            r"(?:remained unchanged at|increased to|fell to|decreased to|rose to)"
            r"\s+(\d+\.?\d*)\s*percent\s*in\s*(\w+)",
            texto,
            re.I,
        )
        if patron:
            return {"valor": float(patron.group(1)), "periodo": patron.group(2)}
    except Exception as exc:
        logger.warning("Error obteniendo desempleo: %s", exc)
    return None


# ═══════════════════════════════════════════════════════════════
# FETCHERS – PIB (BCCh vía TradingEconomics)
# ═══════════════════════════════════════════════════════════════

def _fetch_tasas_gdp() -> dict[str, dict]:
    """
    Obtiene las tasas de crecimiento del PIB (QoQ, anual, producción industrial,
    retail) desde TradingEconomics.
    """
    tasas: dict[str, dict] = {}

    try:
        html_gdp  = _fetch_html(f"{BASE_TE}{TE_PATHS['gdp_growth']}")
        lineas    = _html_a_lineas(html_gdp)
        related   = _parsear_tabla_related_html(html_gdp)

        # PIB trimestral (calendario)
        calendario = _parsear_calendario_te(lineas)
        if calendario and calendario.get("actual"):
            valor = _parsear_num(calendario["actual"])
            if valor is not None:
                tasas["PIB QoQ"] = {
                    "val":  valor,
                    "prev": _parsear_num(calendario.get("previous")),
                    "ref":  calendario.get("ref", "—"),
                    "unit": "%",
                }

        # Tasas adicionales desde la tabla Related
        mapa_related: dict[str, tuple[str, str]] = {
            "Full Year GDP Growth": ("PIB Año Completo 2025", "%"),
            "GDP Growth Rate YoY":  ("PIB Anual",             "%"),
        }
        for clave_te, (etiqueta, unidad) in mapa_related.items():
            fila = related.get(clave_te)
            if fila:
                tasas[etiqueta] = {
                    "val":  fila["last"],
                    "prev": fila["prev"],
                    "ref":  fila.get("ref", "—"),
                    "unit": unidad,
                }
    except Exception as exc:
        logger.warning("Error obteniendo tasas GDP: %s", exc)

    # Producción industrial y retail vía calendario
    indicadores_extra = [
        ("Producción Ind. YoY", TE_PATHS["produccion_ind"], "%"),
        ("Ventas Retail YoY",   TE_PATHS["retail_yoy"],    "%"),
    ]
    for etiqueta, path, unidad in indicadores_extra:
        dato = _obtener_indicador_calendario(path, unidad)
        if dato:
            tasas[etiqueta] = dato

    return tasas


def _fetch_valores_gdp(related: dict[str, dict]) -> dict[str, dict]:
    """
    Obtiene los valores absolutos del PIB (nominal, per cápita, etc.)
    a partir de la tabla Related ya parseada.
    """
    valores: dict[str, dict] = {}
    mapa: dict[str, tuple[str, str]] = {
        "GDP":                      ("PIB Nominal",             "B USD"),
        "GDP Constant Prices":      ("PIB Constante (CLP B)",   "B CLP"),
        "GDP per Capita":           ("PIB per cápita",          "USD"),
        "GDP per Capita PPP":       ("PIB per cápita PPP",      "USD"),
        "Gross National Product":   ("Producto Nacional Bruto", "B CLP"),
        "Gross Fixed Capital Formation": ("Inversión (FBCF)",   "B CLP"),
    }
    for clave_te, (etiqueta, unidad) in mapa.items():
        fila = related.get(clave_te)
        if fila:
            valores[etiqueta] = {
                "val":  fila["last"],
                "prev": fila["prev"],
                "ref":  fila.get("ref", "—"),
                "unit": unidad,
            }
    return valores


def _fetch_sectores_gdp(related: dict[str, dict]) -> dict[str, dict]:
    """
    Obtiene el PIB por sector productivo a partir de la tabla Related ya parseada.
    """
    sectores: dict[str, dict] = {}
    mapa: dict[str, str] = {
        "GDP from Agriculture":         "Agropecuario",
        "GDP from Construction":        "Construcción",
        "GDP from Manufacturing":       "Industria Manufactura",
        "GDP from Mining":              "Minería",
        "GDP from Public Administration": "Administración Pública",
        "GDP from Services":            "Servicios",
        "GDP from Transport":           "Transporte",
        "GDP from Utilities":           "Utilities / Energía",
    }
    for clave_te, etiqueta in mapa.items():
        fila = related.get(clave_te)
        if fila:
            sectores[etiqueta] = {
                "val":  fila["last"],
                "prev": fila["prev"],
                "ref":  fila.get("ref", "—"),
                "unit": "B CLP",
            }
    return sectores


def _fetch_componentes() -> dict[str, dict]:
    """
    Obtiene los componentes de la demanda agregada: exportaciones, importaciones,
    balanza comercial, consumo privado y gasto de gobierno.
    """
    componentes: dict[str, dict] = {}

    # Exportaciones, importaciones y balanza vía calendario
    comercio = [
        ("Exportaciones",     TE_PATHS["exportaciones"], "M USD"),
        ("Importaciones",     TE_PATHS["importaciones"], "M USD"),
        ("Balanza Comercial", TE_PATHS["balanza"],       "M USD"),
    ]
    for etiqueta, path, unidad in comercio:
        dato = _obtener_indicador_calendario(path, unidad)
        if dato:
            componentes[etiqueta] = dato

    # Consumo privado y gasto de gobierno vía tabla Related
    gasto = [
        ("Consumo Privado", TE_PATHS["consumo_privado"],  "B CLP", "Spending"),
        ("Gasto Gobierno",  TE_PATHS["gasto_gobierno"],   "B CLP", "Spending"),
    ]
    for etiqueta, path, unidad, palabra_clave in gasto:
        try:
            html = _fetch_html(f"{BASE_TE}{path}")
            tabla = _parsear_tabla_related_html(html)
            for nombre, fila in tabla.items():
                if palabra_clave.lower() in nombre.lower():
                    componentes[etiqueta] = {
                        "val":  fila["last"],
                        "prev": fila["prev"],
                        "ref":  fila.get("ref", "—"),
                        "unit": unidad,
                    }
                    break
        except Exception as exc:
            logger.warning("Error obteniendo %s: %s", etiqueta, exc)

    return componentes


def _fetch_externo() -> dict[str, dict]:
    """
    Obtiene los indicadores del sector externo y fiscal (cuenta corriente,
    reservas, deuda, balance fiscal, IED).
    """
    externo: dict[str, dict] = {}
    indicadores = [
        ("Cuenta Corriente",     TE_PATHS["cuenta_corriente"], "B USD"),
        ("Cta. Cte. % PIB",      TE_PATHS["cta_cte_pib"],      "%"),
        ("Deuda Externa",        TE_PATHS["deuda_externa"],     "M USD"),
        ("Reservas Int.",        TE_PATHS["reservas"],          "M USD"),
        ("Deuda Gob. % PIB",     TE_PATHS["deuda_gob_pib"],     "%"),
        ("Balance Fiscal % PIB", TE_PATHS["balance_fiscal"],    "%"),
        ("IED",                  TE_PATHS["ied"],               "M USD"),
    ]
    for etiqueta, path, unidad in indicadores:
        dato = _obtener_indicador_calendario(path, unidad)
        if dato:
            externo[etiqueta] = dato
        else:
            # Fallback vía tabla Related
            dato_fallback = _obtener_indicador_related(path, unidad)
            if dato_fallback:
                externo[etiqueta] = dato_fallback

    return externo


@st.cache_data(ttl=3600)
def fetch_pib_completo() -> dict[str, dict]:
    """
    PIB completo del Banco Central de Chile, consolidado vía TradingEconomics.
    Organizado en 5 grupos: tasas, valores, componentes, sectores, externo.
    TTL: 1 hora.
    """
    # Parsear la página de GDP growth una sola vez (la usan tasas, valores y sectores)
    related_gdp: dict[str, dict] = {}
    try:
        html_gdp  = _fetch_html(f"{BASE_TE}{TE_PATHS['gdp_growth']}")
        related_gdp = _parsear_tabla_related_html(html_gdp)
    except Exception as exc:
        logger.warning("Error descargando página GDP growth: %s", exc)

    return {
        "tasas":       _fetch_tasas_gdp(),
        "valores":     _fetch_valores_gdp(related_gdp),
        "componentes": _fetch_componentes(),
        "sectores":    _fetch_sectores_gdp(related_gdp),
        "externo":     _fetch_externo(),
    }


# ═══════════════════════════════════════════════════════════════
# FETCHER – WORLD BANK (series anuales históricas)
# ═══════════════════════════════════════════════════════════════

def _wb_serie(codigo: str, n: int = 12) -> dict[str, float]:
    """
    Descarga una serie anual del World Bank para Chile.
    Retorna dict ordenado {año: valor}, del más antiguo al más reciente.
    """
    try:
        respuesta = requests.get(
            f"{BASE_WB}/{codigo}?format=json&per_page={n}&mrv={n}",
            headers=HEADERS,
            timeout=30,
        )
        if respuesta.status_code != 200:
            return {}
        cuerpo = respuesta.json()
        datos = cuerpo[1] if len(cuerpo) > 1 else []
        puntos = {
            registro["date"]: round(registro["value"], 3)
            for registro in (datos or [])
            if registro["value"] is not None
        }
        return dict(sorted(puntos.items()))
    except Exception as exc:
        logger.warning("Error obteniendo World Bank %s: %s", codigo, exc)
        return {}


@st.cache_data(ttl=86400)  # 24 h — datos anuales cambian con poca frecuencia
def fetch_wb_historial() -> dict[str, dict[str, float]]:
    """
    Series históricas anuales de Chile desde el World Bank.
    Retorna: {etiqueta: {año: valor}}. TTL: 24 horas.
    """
    resultado: dict[str, dict[str, float]] = {}
    for etiqueta, codigo in WB_CODES.items():
        serie = _wb_serie(codigo, n=15)
        if serie:
            # Convertir PIB nominal a billones (USD)
            if "B USD" in etiqueta:
                serie = {anio: round(valor / 1e9, 2) for anio, valor in serie.items()}
            resultado[etiqueta] = serie
    return resultado


# ═══════════════════════════════════════════════════════════════
# FETCHER – MERCADOS (Bolsa Santiago + NYSE / COMEX)
# ═══════════════════════════════════════════════════════════════

@st.cache_data(ttl=600)  # 10 min — datos de mercado cambian frecuentemente
def fetch_mercados() -> dict[str, dict]:
    """
    Obtiene precios de cierre del IPSA, Cobre, WTI y Brent via Yahoo Finance.
    Incluye conversión a CLP si el tipo de cambio está disponible.
    TTL: 10 minutos.
    """
    # Obtener tipo de cambio USD/CLP
    usd_clp: float | None = None
    try:
        hist_fx = yf.Ticker("CLP=X").history(period="5d")
        if not hist_fx.empty:
            usd_clp = float(hist_fx["Close"].iloc[-1])
    except Exception as exc:
        logger.warning("Error obteniendo tipo de cambio USD/CLP: %s", exc)

    mercados: dict[str, dict] = {}
    for nombre, config in MARKET_SYMBOLS.items():
        try:
            historial = yf.Ticker(config["sym"]).history(period="5d")
            if historial.empty:
                continue
            ultimo    = float(historial["Close"].iloc[-1])
            variacion = (
                float(ultimo - historial["Close"].iloc[-2])
                if len(historial) >= 2
                else None
            )
            entrada = {
                **config,
                "val":     ultimo,
                "delta":   variacion,
                "fecha":   historial.index[-1].strftime("%d/%m/%Y"),
                "usd_clp": usd_clp,
            }
            if config["moneda"] == "USD" and usd_clp:
                entrada["val_clp"]   = ultimo * usd_clp
                entrada["delta_clp"] = variacion * usd_clp if variacion is not None else None
            mercados[nombre] = entrada
        except Exception as exc:
            logger.warning("Error obteniendo mercado %s: %s", nombre, exc)

    return mercados


# ═══════════════════════════════════════════════════════════════
# FETCHER – NOTICIAS ECONÓMICAS (multi-fuente)
# ═══════════════════════════════════════════════════════════════

def _clasificar_indicadores(texto: str) -> list[str]:
    """Identifica los indicadores económicos mencionados en un texto de noticia."""
    texto_lower = texto.lower()
    etiquetas = [
        categoria
        for categoria, palabras in INDICADOR_MAP.items()
        if any(palabra in texto_lower for palabra in palabras)
    ]
    return etiquetas or ["General"]


def _clasificar_impacto(texto: str) -> str:
    """Clasifica el impacto económico de una noticia como positivo, negativo o neutral."""
    texto_lower = texto.lower()
    palabras_negativas = [
        "cae", "baja", "crisis", "freno", "contrae", "recesión", "recesion",
        "déficit", "caída", "caida", "alza combustible", "alza del dólar",
        "inflación sube", "desempleo sube", "aranceles",
    ]
    palabras_positivas = [
        "sube", "crece", "alza ipsa", "alza bolsa", "mejora", "recupera",
        "repunta", "superávit", "inflación baja", "dólar baja",
        "empleo sube", "crecimiento",
    ]
    if any(palabra in texto_lower for palabra in palabras_negativas):
        return "🔴 Negativo"
    if any(palabra in texto_lower for palabra in palabras_positivas):
        return "🟢 Positivo"
    return "🟡 Neutral"


def _deduplicar_noticias(items: list[dict], umbral: float = 0.55) -> list[dict]:
    """
    Elimina noticias duplicadas comparando similitud de títulos.
    Cuando hay duplicados, conserva el resumen más largo y fusiona las fuentes.
    """
    resultado: list[dict] = []
    for item in items:
        es_duplicado = False
        for existente in resultado:
            if _similitud(item["titulo"], existente["titulo"]) > umbral:
                es_duplicado = True
                # Conservar el resumen más informativo
                if len(item.get("resumen", "")) > len(existente.get("resumen", "")):
                    existente["resumen"] = item["resumen"]
                existente["fuentes"] = list(set(existente.get("fuentes", []) + item.get("fuentes", [])))
                break
        if not es_duplicado:
            resultado.append(item)
    return resultado


def _enriquecer_noticia(item: dict) -> dict:
    """Añade las claves 'impacto' e 'indicadores' a una noticia."""
    texto_combinado = item["titulo"] + " " + item.get("resumen", "")
    item["impacto"]     = _clasificar_impacto(texto_combinado)
    item["indicadores"] = _clasificar_indicadores(texto_combinado)
    return item


def _fetch_noticias_df() -> list[dict]:
    """Scrapeado de artículos desde Diario Financiero (df.cl/mercados)."""
    noticias: list[dict] = []
    try:
        soup = BeautifulSoup(_fetch_html("https://www.df.cl/mercados", timeout=15), "html.parser")
        for articulo in soup.find_all("article")[:10]:
            titulo_tag = articulo.find(["h2", "h3"])
            enlace_tag = articulo.find("a", href=True)
            parrafo    = articulo.find("p")
            if not (titulo_tag and enlace_tag):
                continue
            href = enlace_tag["href"]
            if not href.startswith("http"):
                href = "https://www.df.cl" + href
            noticias.append({
                "titulo":  titulo_tag.get_text(strip=True)[:160],
                "link":    href,
                "resumen": parrafo.get_text(strip=True)[:220] if parrafo else "",
                "fuentes": ["Diario Financiero"],
            })
    except Exception as exc:
        logger.warning("Error scrapeando DF: %s", exc)
    return noticias


def _fetch_noticias_uchile_scraping() -> list[dict]:
    """Scrapeado de artículos económicos desde Radio Universidad de Chile."""
    noticias: list[dict] = []
    try:
        soup = BeautifulSoup(_fetch_html("https://radio.uchile.cl/economia/", timeout=15), "html.parser")
        for enlace in soup.find_all("a", href=True):
            titulo = enlace.get_text(strip=True)
            href   = enlace["href"]
            if not href.startswith("http"):
                href = "https://radio.uchile.cl" + href
            if (len(titulo) > 35
                    and "radio.uchile.cl" in href
                    and any(palabra in titulo.lower() for palabra in ECO_KEYWORDS)):
                noticias.append({
                    "titulo":  titulo[:160],
                    "link":    href,
                    "resumen": "",
                    "fuentes": ["Radio Universidad de Chile"],
                })
    except Exception as exc:
        logger.warning("Error scrapeando UChile: %s", exc)
    return noticias


def _fetch_noticias_uchile_rss() -> list[dict]:
    """Artículos económicos desde el feed RSS de Radio Universidad de Chile."""
    noticias: list[dict] = []
    try:
        feed = feedparser.parse("https://radio.uchile.cl/feed/")
        for entrada in feed.entries:
            titulo  = entrada.title
            resumen = getattr(entrada, "summary", "")
            if any(palabra in (titulo + resumen).lower() for palabra in ECO_KEYWORDS):
                noticias.append({
                    "titulo":  titulo[:160],
                    "link":    entrada.link,
                    "resumen": resumen[:220],
                    "fuentes": ["Radio Universidad de Chile"],
                })
    except Exception as exc:
        logger.warning("Error obteniendo RSS UChile: %s", exc)
    return noticias


def _fetch_noticias_emol() -> list[dict]:
    """Scrapeado de artículos económicos desde Emol."""
    noticias: list[dict] = []
    try:
        soup = BeautifulSoup(_fetch_html("https://www.emol.com/economia/", timeout=15), "html.parser")
        for enlace in soup.find_all("a", href=True):
            titulo = enlace.get_text(strip=True)
            href   = enlace["href"]
            if not href.startswith("http"):
                href = "https://www.emol.com" + href
            if (len(titulo) > 35
                    and ("emol.com" in href or href.startswith("/"))
                    and any(palabra in titulo.lower() for palabra in ECO_KEYWORDS)):
                noticias.append({
                    "titulo":  titulo[:160],
                    "link":    href,
                    "resumen": "",
                    "fuentes": ["Emol"],
                })
    except Exception as exc:
        logger.warning("Error scrapeando Emol: %s", exc)
    return noticias


def fetch_noticias() -> list[dict]:
    """
    Agrega noticias económicas de múltiples fuentes, las enriquece con
    clasificación de impacto e indicadores afectados, deduplica y ordena.
    Retorna hasta 20 noticias únicas ordenadas por impacto (negativo primero).
    """
    todas = (
        _fetch_noticias_df()
        + _fetch_noticias_uchile_scraping()
        + _fetch_noticias_uchile_rss()
        + _fetch_noticias_emol()
    )
    enriquecidas = [_enriquecer_noticia(noticia) for noticia in todas]
    deduplicadas = _deduplicar_noticias(enriquecidas, umbral=0.55)

    orden_impacto = {"🔴 Negativo": 0, "🟢 Positivo": 1, "🟡 Neutral": 2}
    deduplicadas.sort(key=lambda noticia: orden_impacto.get(noticia["impacto"], 3))
    return deduplicadas[:20]


# ═══════════════════════════════════════════════════════════════
# HELPERS DE RENDER
# ═══════════════════════════════════════════════════════════════

def _tarjeta_metrica(
    col: st.delta_generator.DeltaGenerator,
    etiqueta: str,
    valor: str,
    delta: str | None = None,
    caption: str | None = None,
) -> None:
    """Renderiza una métrica de Streamlit con caption opcional."""
    col.metric(etiqueta, valor, delta=delta)
    if caption:
        col.caption(caption)


def _bloque_commodity(col: st.delta_generator.DeltaGenerator, nombre: str, datos: dict) -> None:
    """Renderiza una tarjeta de commodity con precio en USD y equivalente en CLP."""
    with col:
        if not datos:
            st.metric(nombre, "—")
            return

        es_libra = "lb" in datos.get("unidad", "")
        precio_str = (
            f"USD {datos['val']:,.3f}" if es_libra
            else f"USD {datos['val']:,.2f}"
        )
        variacion = datos.get("delta")
        variacion_str = (
            f"USD {variacion:+.3f}" if (variacion is not None and es_libra)
            else f"USD {variacion:+.2f}" if variacion is not None
            else None
        )

        st.metric(nombre, precio_str, delta=variacion_str)

        if datos.get("val_clp"):
            var_clp = datos.get("delta_clp")
            sufijo_unidad = (
                "/" + datos["unidad"].split("/")[1]
                if "/" in datos.get("unidad", "")
                else ""
            )
            variacion_clp_str = f"  ({var_clp:+,.0f})" if var_clp else ""
            st.markdown(
                f"<small style='color:#777'>≈ <b>{_fmt_clp(datos['val_clp'])} CLP</b>"
                f"{sufijo_unidad}{variacion_clp_str}</small>",
                unsafe_allow_html=True,
            )

        st.caption(f"📅 {datos['fecha']} · **{datos['bolsa']}**")


def _fmt_val_pib(dato: dict) -> str:
    """Formatea el valor de un indicador PIB según su unidad."""
    valor = dato.get("val")
    unidad = dato.get("unit", "")
    if valor is None:
        return "—"
    if unidad == "%":      return f"{valor:.1f}%"
    if unidad == "B USD":  return f"USD {valor:,.2f}B"
    if unidad == "B CLP":  return f"CLP {valor:,.1f}B"
    if unidad == "M USD":  return f"USD {valor:,.0f}M"
    if unidad == "USD":    return f"USD {valor:,.0f}"
    return f"{valor:,.2f}"


def _fmt_delta_pib(dato: dict) -> str | None:
    """Calcula y formatea la variación respecto al período anterior de un indicador PIB."""
    valor   = dato.get("val")
    anterior = dato.get("prev")
    unidad  = dato.get("unit", "")
    if valor is None or anterior is None:
        return None
    try:
        diferencia = valor - float(anterior)
        if unidad == "%":               return f"{diferencia:+.1f}pp vs período ant."
        if unidad in ("B USD", "B CLP"): return f"{diferencia:+.2f} vs período ant."
        if unidad == "M USD":           return f"USD {diferencia:+,.0f}M vs período ant."
        if unidad == "USD":             return f"USD {diferencia:+,.0f} vs período ant."
        return f"{diferencia:+.2f} vs período ant."
    except (TypeError, ValueError):
        return None


def _render_tarjetas_pib(datos_grupo: dict[str, dict], n_cols: int = 4) -> None:
    """Renderiza tarjetas de métricas para un grupo de indicadores PIB."""
    items = list(datos_grupo.items())
    if not items:
        st.info("Sin datos disponibles en este momento.")
        return

    for bloque_inicio in range(0, len(items), n_cols):
        bloque = items[bloque_inicio : bloque_inicio + n_cols]
        columnas = st.columns(len(bloque))
        for columna, (etiqueta, dato) in zip(columnas, bloque):
            columna.metric(etiqueta, _fmt_val_pib(dato), delta=_fmt_delta_pib(dato))
            columna.caption(f"📅 {dato.get('ref', '—')} · **BCCh**")


def _render_tabla_pib(datos_grupo: dict[str, dict]) -> None:
    """Renderiza una tabla comparativa para un grupo de indicadores PIB."""
    filas = [
        {
            "Indicador":        etiqueta,
            "Valor Actual":     _fmt_val_pib(dato),
            "Período Anterior": _fmt_val_pib({**dato, "val": dato.get("prev")}),
            "Variación":        _fmt_delta_pib(dato) or "—",
            "Período":          dato.get("ref", "—"),
            "Unidad":           dato.get("unit", "—"),
        }
        for etiqueta, dato in datos_grupo.items()
    ]
    if filas:
        df = pd.DataFrame(filas).set_index("Indicador")
        st.dataframe(df, use_container_width=True)


def _grafico_wb(wb_hist: dict, etiqueta_serie: str, titulo: str, eje_y: str = "") -> None:
    """Renderiza un gráfico de línea con datos históricos del World Bank."""
    serie = wb_hist.get(etiqueta_serie, {})
    if not serie:
        return
    df_grafico = pd.DataFrame({
        "Año": list(serie.keys()),
        eje_y or titulo: list(serie.values()),
    }).set_index("Año")
    st.line_chart(df_grafico, height=220)
    st.caption(f"Fuente: World Bank Open Data · Chile · {etiqueta_serie}")


# ═══════════════════════════════════════════════════════════════
# APP – LAYOUT PRINCIPAL
# ═══════════════════════════════════════════════════════════════

st.title("📊 Dashboard Macroeconómico Chile")
st.caption(
    f"Actualizado: {datetime.now(TZ_CL).strftime('%d/%m/%Y %H:%M')} (hora Chile)  ·  "
    "Publicaciones según última disponibilidad oficial"
)
st.divider()

with st.spinner("Cargando datos…"):
    datos_minind  = fetch_mindicador()
    datos_hist    = fetch_historial()
    datos_ipc     = fetch_ipc()
    datos_desem   = fetch_desempleo()
    datos_pib     = fetch_pib_completo()
    datos_mkt     = fetch_mercados()
    datos_wb_hist = fetch_wb_historial()

# Tipo de cambio USD/CLP (para conversiones en la UI)
usd_clp_rate: float | None = next(
    (v["usd_clp"] for v in datos_mkt.values() if v.get("usd_clp")),
    None,
)

# ──────────────────────────────────────────────────────────────
# SECCIÓN 1 – POLÍTICA MONETARIA Y PRECIOS (BCCh / INE)
# ──────────────────────────────────────────────────────────────
st.subheader("📌 Política Monetaria y Precios — Banco Central / INE")

col1, col2, col3, col4 = st.columns(4)
_tarjeta_metrica(
    col1, "UF",
    f"${datos_minind.get('UF', {}).get('valor', 0):,.2f}",
    caption=f"📅 {datos_minind.get('UF', {}).get('fecha', '—')} · BCCh vía Mindicador.cl",
)
_tarjeta_metrica(
    col2, "Dólar / CLP",
    f"${datos_minind.get('Dólar/CLP', {}).get('valor', 0):,.2f}",
    caption=f"📅 {datos_minind.get('Dólar/CLP', {}).get('fecha', '—')} · BCCh vía Mindicador.cl",
)
_tarjeta_metrica(
    col3, "TPM",
    f"{datos_minind.get('TPM', {}).get('valor', '—')}%",
    caption=f"📅 {datos_minind.get('TPM', {}).get('fecha', '—')} · **Banco Central de Chile**",
)
_tarjeta_metrica(
    col4, "Euro / CLP",
    f"${datos_minind.get('Euro/CLP', {}).get('valor', 0):,.2f}",
    caption=f"📅 {datos_minind.get('Euro/CLP', {}).get('fecha', '—')} · BCCh vía Mindicador.cl",
)

col5, col6, col7, col8 = st.columns(4)

if datos_ipc:
    mes_referencia = datos_ipc.get("mes", "último período")
    mes_anterior   = datos_ipc.get("mes_ant", "mes ant.")
    _tarjeta_metrica(
        col5, "IPC Anual",
        _fmt_pct(datos_ipc.get("yoy")),
        delta=f"{datos_ipc['yoy'] - datos_ipc.get('yoy_ant', datos_ipc.get('yoy', 0)):+.1f}pp vs {mes_anterior}",
        caption=f"📅 {mes_referencia} {datos_ipc.get('año', '')} · INE / BCCh",
    )
    _tarjeta_metrica(
        col6, "IPC Mensual",
        _fmt_pct(datos_ipc.get("mom")),
        delta=f"{datos_ipc.get('mom', 0) - datos_ipc.get('mom_ant', 0):+.1f}pp vs mes anterior",
        caption=f"📅 {mes_referencia} {datos_ipc.get('año', '')} · INE / BCCh",
    )
else:
    col5.metric("IPC Anual", "—")
    col6.metric("IPC Mensual", "—")

_tarjeta_metrica(
    col7, "UTM",
    f"${datos_minind.get('UTM', {}).get('valor', 0):,}",
    caption=f"📅 {datos_minind.get('UTM', {}).get('fecha', '—')} · BCCh",
)

if datos_desem:
    _tarjeta_metrica(
        col8, "Tasa Desempleo",
        _fmt_pct(datos_desem["valor"]),
        caption=f"📅 {datos_desem.get('periodo', '—')} · INE / BCCh",
    )
else:
    col8.metric("Tasa Desempleo", "—")

# ──────────────────────────────────────────────────────────────
# SECCIÓN 2 – VALORES HISTÓRICOS (mini tabla)
# ──────────────────────────────────────────────────────────────
with st.expander("🕐 Valores Históricos — últimos 5 períodos", expanded=False):
    tabs_hist = st.tabs(["IPC", "USD/CLP", "TPM", "UF"])
    etiquetas_hist = ["IPC", "USD/CLP", "TPM", "UF"]
    for tab, etiqueta in zip(tabs_hist, etiquetas_hist):
        with tab:
            datos_serie = datos_hist.get(etiqueta, [])
            if datos_serie:
                df = pd.DataFrame(datos_serie).rename(columns={"fecha": "Período", "valor": etiqueta})
                df[etiqueta] = df[etiqueta].apply(
                    lambda v: f"{v:.2f}%" if etiqueta in ("IPC", "TPM") else f"${v:,.2f}"
                )
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

tab_tasas, tab_vals, tab_comp, tab_sect, tab_ext = st.tabs([
    "📈 Tasas de Crecimiento",
    "💰 Valores Absolutos",
    "🔗 Componentes (Demanda)",
    "🏭 PIB por Sector",
    "🌐 Externo y Fiscal",
])

with tab_tasas:
    st.markdown("##### Tasas de crecimiento real — BCCh / INE")
    _render_tarjetas_pib(datos_pib.get("tasas", {}))

    st.markdown("---")
    st.markdown("**📊 Historial anual — Crecimiento del PIB (%, 2010–2024)**")
    _grafico_wb(datos_wb_hist, "Crecimiento PIB (%)", "Crecimiento PIB", "% anual")

    with st.expander("📋 Tabla comparativa — tasas actuales", expanded=False):
        _render_tabla_pib(datos_pib.get("tasas", {}))

with tab_vals:
    st.markdown("##### Tamaño del PIB y PIB per cápita — BCCh / World Bank")
    _render_tarjetas_pib(datos_pib.get("valores", {}))

    pib_nominal = datos_pib.get("valores", {}).get("PIB Nominal", {}).get("val")
    pib_percap  = datos_pib.get("valores", {}).get("PIB per cápita", {}).get("val")
    if usd_clp_rate and pib_nominal and pib_percap:
        st.info(
            f"**Equivalencia en pesos chilenos** (USD/CLP = ${usd_clp_rate:,.2f}):  \n"
            f"- PIB Nominal: **CLP {pib_nominal * usd_clp_rate / 1_000:,.1f}B**  \n"
            f"- PIB per cápita: **CLP {pib_percap * usd_clp_rate:,.0f}** por persona"
        )

    st.markdown("---")
    col_h1, col_h2 = st.columns(2)
    with col_h1:
        st.markdown("**📊 PIB Nominal (B USD) — 2010–2024**")
        _grafico_wb(datos_wb_hist, "PIB Nominal (B USD)", "PIB Nominal", "B USD")
    with col_h2:
        st.markdown("**📊 PIB per cápita (USD) — 2010–2024**")
        _grafico_wb(datos_wb_hist, "PIB per cápita (USD)", "PIB per cápita", "USD")

    col_h3, col_h4 = st.columns(2)
    with col_h3:
        st.markdown("**📊 PIB per cápita PPP (USD) — 2010–2024**")
        _grafico_wb(datos_wb_hist, "PIB per cápita PPP (USD)", "PPP per cápita", "USD")

    with st.expander("📋 Tabla histórica anual — valores absolutos", expanded=False):
        series_hist = ["PIB Nominal (B USD)", "PIB per cápita (USD)", "PIB per cápita PPP (USD)"]
        filas_hist: dict[str, dict] = {}
        for lbl in series_hist:
            for anio, valor in datos_wb_hist.get(lbl, {}).items():
                filas_hist.setdefault(anio, {})[lbl] = valor
        if filas_hist:
            df_hist = pd.DataFrame(filas_hist).T.sort_index(ascending=False)
            st.dataframe(df_hist, use_container_width=True)

    with st.expander("📋 Tabla comparativa — valores actuales", expanded=False):
        _render_tabla_pib(datos_pib.get("valores", {}))

with tab_comp:
    st.markdown("##### Componentes de la demanda agregada — BCCh")
    _render_tarjetas_pib(datos_pib.get("componentes", {}))

    exp_val = datos_pib.get("componentes", {}).get("Exportaciones", {}).get("val")
    imp_val = datos_pib.get("componentes", {}).get("Importaciones", {}).get("val")
    if exp_val and imp_val:
        balanza = exp_val - imp_val
        col_b1, col_b2, col_b3, _ = st.columns([1, 1, 1, 1])
        col_b1.metric("Exportaciones",  f"USD {exp_val:,.0f}M")
        col_b2.metric("Importaciones",  f"USD {imp_val:,.0f}M")
        col_b3.metric(
            "Balanza neta",
            f"USD {balanza:,.0f}M",
            delta="🟢 superávit" if balanza >= 0 else "🔴 déficit",
        )

    st.markdown("---")
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.markdown("**📊 Consumo de hogares (% PIB)**")
        _grafico_wb(datos_wb_hist, "Consumo Hogares (% PIB)", "Consumo Hogares", "% PIB")
    with col_c2:
        st.markdown("**📊 Formación bruta de capital fijo (% PIB)**")
        _grafico_wb(datos_wb_hist, "FBCF (% PIB)", "FBCF", "% PIB")

    col_c3, col_c4 = st.columns(2)
    with col_c3:
        st.markdown("**📊 Exportaciones (% PIB)**")
        _grafico_wb(datos_wb_hist, "Exportaciones (% PIB)", "Exportaciones", "% PIB")
    with col_c4:
        st.markdown("**📊 Importaciones (% PIB)**")
        _grafico_wb(datos_wb_hist, "Importaciones (% PIB)", "Importaciones", "% PIB")

    with st.expander("📋 Tabla comparativa — componentes actuales", expanded=False):
        _render_tabla_pib(datos_pib.get("componentes", {}))

with tab_sect:
    st.markdown("##### PIB por sector productivo — Banco Central de Chile")
    st.caption("Valores en CLP Billones · Fuente: BCCh vía TradingEconomics")
    sectores = datos_pib.get("sectores", {})

    if sectores:
        _render_tarjetas_pib(sectores, n_cols=4)

        st.markdown("---")
        st.markdown("**📊 Aporte de cada sector al PIB (CLP Billones)**")

        vals_actuales = {k: d.get("val", 0) for k, d in sectores.items() if d.get("val")}
        vals_anteriores = {k: d.get("prev", 0) for k, d in sectores.items() if d.get("prev")}

        if vals_actuales:
            df_sect = pd.DataFrame({
                "Actual (B CLP)":   pd.Series(vals_actuales),
                "Anterior (B CLP)": pd.Series(vals_anteriores),
            }).sort_values("Actual (B CLP)", ascending=False)
            st.bar_chart(df_sect, height=350)

        with st.expander("📋 Tabla comparativa — sectores actuales vs anterior", expanded=True):
            filas_sect = []
            for etiqueta, dato in sectores.items():
                val  = dato.get("val")
                prev = dato.get("prev")
                variacion_pct = (
                    f"{(val - prev) / prev * 100:+.1f}%"
                    if val and prev and prev != 0
                    else "—"
                )
                filas_sect.append({
                    "Sector":           etiqueta,
                    "Actual (B CLP)":   f"{val:,.2f}" if val else "—",
                    "Anterior (B CLP)": f"{prev:,.2f}" if prev else "—",
                    "Variación %":      variacion_pct,
                    "Período":          dato.get("ref", "—"),
                })
            df_s = pd.DataFrame(filas_sect).set_index("Sector")
            st.dataframe(df_s, use_container_width=True)
    else:
        st.info("Sin datos de sector disponibles en este momento.")

with tab_ext:
    st.markdown("##### Sector externo, reservas y posición fiscal — BCCh")
    _render_tarjetas_pib(datos_pib.get("externo", {}))
    st.link_button(
        "📊 Estadísticas BCCh — Sector Externo",
        "https://www.bcentral.cl/web/banco-central/areas/estadisticas/sector-externo",
    )
    with st.expander("📋 Tabla comparativa — sector externo y fiscal", expanded=False):
        _render_tabla_pib(datos_pib.get("externo", {}))

st.divider()

# ──────────────────────────────────────────────────────────────
# SECCIÓN 4 – BOLSA DE SANTIAGO
# ──────────────────────────────────────────────────────────────
st.subheader("📈 Bolsa de Santiago — Mercado Accionario")

datos_ipsa = datos_mkt.get("IPSA", {})
col_i1, col_i2, _ = st.columns([1, 1, 2])
if datos_ipsa:
    variacion_ipsa = datos_ipsa.get("delta")
    col_i1.metric(
        "IPSA",
        f"{datos_ipsa['val']:,.2f} pts",
        delta=f"{variacion_ipsa:+.2f} pts" if variacion_ipsa is not None else None,
    )
    col_i1.caption(f"📅 Cierre {datos_ipsa['fecha']} · **{datos_ipsa['bolsa']}**")
else:
    col_i1.metric("IPSA", "—")

st.divider()

# ──────────────────────────────────────────────────────────────
# SECCIÓN 5 – COMMODITIES (NYSE / COMEX)
# ──────────────────────────────────────────────────────────────
st.subheader("🛢️ Commodities — Bolsas de Nueva York (COMEX / NYMEX)")
if usd_clp_rate:
    st.caption(f"Tipo de cambio para conversión: **USD/CLP = ${usd_clp_rate:,.2f}** · Yahoo Finance")

col_c1, col_c2, col_c3 = st.columns(3)
_bloque_commodity(col_c1, "Cobre (COMEX)",  datos_mkt.get("Cobre", {}))
_bloque_commodity(col_c2, "Petróleo WTI",   datos_mkt.get("WTI",   {}))
_bloque_commodity(col_c3, "Petróleo Brent", datos_mkt.get("Brent", {}))

st.divider()

# ──────────────────────────────────────────────────────────────
# SECCIÓN 6 – NOTICIAS ECONÓMICAS + PANEL BCCh
# ──────────────────────────────────────────────────────────────
col_noticias, col_bcentral = st.columns([3, 2])

with col_noticias:
    st.subheader("📰 Noticias Económicas")
    st.caption(
        "Fuentes: Diario Financiero · Radio Universidad de Chile · Emol  "
        "· (deduplicadas y clasificadas)"
    )

    noticias = fetch_noticias()

    col_f1, col_f2 = st.columns([2, 2])
    with col_f1:
        filtro_impacto = st.selectbox(
            "Filtrar por impacto",
            ["Todos", "🔴 Negativo", "🟡 Neutral", "🟢 Positivo"],
            key="filtro_impacto",
        )
    todos_indicadores = sorted({ind for noticia in noticias for ind in noticia.get("indicadores", [])})
    with col_f2:
        filtro_indicador = st.selectbox(
            "Filtrar por indicador afectado",
            ["Todos"] + todos_indicadores,
            key="filtro_ind",
        )

    noticias_filtradas = noticias
    if filtro_impacto != "Todos":
        noticias_filtradas = [n for n in noticias_filtradas if n["impacto"] == filtro_impacto]
    if filtro_indicador != "Todos":
        noticias_filtradas = [n for n in noticias_filtradas if filtro_indicador in n.get("indicadores", [])]

    if noticias_filtradas:
        for noticia in noticias_filtradas:
            indicadores_str = " · ".join(f"`{i}`" for i in noticia["indicadores"])
            fuentes_str     = " · ".join(noticia.get("fuentes", []))
            with st.expander(f"{noticia['impacto']}  {noticia['titulo']}"):
                if noticia.get("resumen"):
                    st.write(noticia["resumen"])
                st.markdown(f"**Indicadores afectados:** {indicadores_str}")
                st.caption(f"Fuente: {fuentes_str}")
                st.link_button("Leer nota completa →", noticia["link"])
    else:
        st.info("No hay noticias para los filtros seleccionados.")

with col_bcentral:
    st.subheader("🏦 Banco Central de Chile")

    tpm_datos = datos_minind.get("TPM", {})
    pib_anual = _fmt_val_pib(datos_pib.get("tasas", {}).get("PIB Anual", {}))
    pib_percap_str = _fmt_val_pib(datos_pib.get("valores", {}).get("PIB per cápita", {}))
    export_str = _fmt_val_pib(datos_pib.get("componentes", {}).get("Exportaciones", {}))
    balanza_str = _fmt_val_pib(datos_pib.get("componentes", {}).get("Balanza Comercial", {}))
    ipc_anual = _fmt_pct(datos_ipc.get("yoy") if datos_ipc else None)
    ipc_mensual = _fmt_pct(datos_ipc.get("mom") if datos_ipc else None)

    st.info(
        f"**TPM: {tpm_datos.get('valor', '—')}%**  \n"
        f"Vigente desde: {tpm_datos.get('fecha', '—')}  \n\n"
        f"**IPC Anual:** {ipc_anual}  \n"
        f"**IPC Mensual:** {ipc_mensual}  \n"
        f"**PIB Anual:** {pib_anual}  \n"
        f"**PIB per cápita:** {pib_percap_str}  \n"
        f"**Exportaciones:** {export_str}  \n"
        f"**Balanza Comercial:** {balanza_str}"
    )

    st.markdown("**🔗 Fuentes oficiales:**")
    st.link_button(
        "📊 Estadísticas BCCh",
        "https://si3.bcentral.cl/SietePublico/SP/ENEconomicIndicators",
    )
    st.link_button(
        "📈 TPM y Estadísticas Diarias",
        "https://www.bcentral.cl/inicio/-/asset_publisher/W4MRFDiNPxMq/content/tasa-de-politica-monetaria-y-estadisticas-diarias",
    )
    st.link_button(
        "🏛️ IPC — INE Chile",
        "https://www.ine.gob.cl/estadisticas/economia/indices-de-precio-e-inflacion/ipc",
    )
    st.link_button(
        "📉 PIB — BCCh",
        "https://www.bcentral.cl/web/banco-central/areas/estadisticas/sector-real",
    )

st.divider()

# ──────────────────────────────────────────────────────────────
# PIE DE PÁGINA
# ──────────────────────────────────────────────────────────────
col_btn, _ = st.columns([1, 5])
with col_btn:
    if st.button("🔄 Forzar actualización"):
        st.cache_data.clear()
        st.rerun()

st.caption(
    "**Fuentes:** "
    "[BCCh](https://www.bcentral.cl) · "
    "[INE](https://www.ine.gob.cl) · "
    "[Mindicador.cl](https://mindicador.cl) · "
    "[TradingEconomics](https://tradingeconomics.com/chile) · "
    "[Yahoo Finance](https://finance.yahoo.com) · "
    "[DF](https://df.cl) · "
    "[Radio U. de Chile](https://radio.uchile.cl) · "
    "[Emol](https://www.emol.com)"
)
