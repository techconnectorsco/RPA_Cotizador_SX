"""
Contrato base para todo scraper de proveedor.

Cada proveedor (intelec.py, eurocomp.py, etc.) debe:
  1. Heredar de ScraperBase
  2. Definir sus atributos de clase (codigo, nombre, url_base)
  3. Implementar el metodo buscar(query) -> list[ProductoEncontrado]

Cualquier fallo en un scraper NO debe afectar a los otros.
Toda excepcion se captura y se registra, pero no se propaga.
"""

from __future__ import annotations

import time
import traceback
from abc import ABC, abstractmethod

from playwright.sync_api import Browser, BrowserContext, Page

from config import config
from modelo import ProductoEncontrado, ResultadoScraper


class ScraperBase(ABC):
    """Clase base para todos los scrapers de proveedor."""

    # ---- Atributos que cada subclase DEBE definir ----
    codigo: str  # ej: "intelec"
    nombre: str  # ej: "Intelec"
    url_base: str  # ej: "https://www.intelec.co.cr"
    requiere_login: bool = False
    usa_browser_compartido: bool = True

    def __init__(self, browser: Browser):
        self.browser = browser
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ---- Context manager para uso con `with` ----
    def __enter__(self) -> "ScraperBase":
        self._context = self.browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="es-CR",
        )
        self._page = self._context.new_page()
        if self.requiere_login:
            self._login()
        return self

    def __exit__(self, *args) -> None:
        if self._context:
            self._context.close()

    def _login(self) -> None:
        """Sobrescribir en scrapers que requieran login."""
        raise NotImplementedError(
            f"{self.codigo}: requiere_login=True pero no implemento _login()"
        )

    # ---- Metodo publico principal ----
    def ejecutar(self, query: str) -> ResultadoScraper:
        """
        Wrapper de buscar() que captura errores y mide tiempo.
        Nunca lanza excepcion — siempre devuelve un ResultadoScraper.
        """
        inicio = time.perf_counter()
        try:
            productos = self.buscar(query)
            duracion = int((time.perf_counter() - inicio) * 1000)
            return ResultadoScraper(
                proveedor=self.codigo,
                query=query,
                exitoso=True,
                productos=productos,
                duracion_ms=duracion,
            )
        except Exception as e:
            duracion = int((time.perf_counter() - inicio) * 1000)
            error_full = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            print(f"⚠ [{self.codigo}] ERROR: {e}")
            return ResultadoScraper(
                proveedor=self.codigo,
                query=query,
                exitoso=False,
                productos=[],
                duracion_ms=duracion,
                error=error_full,
            )

    # ---- Metodo que cada subclase implementa ----
    @abstractmethod
    def buscar(self, query: str) -> list[ProductoEncontrado]:
        """
        Busca productos que coincidan con `query`.
        Devuelve lista de ProductoEncontrado (puede estar vacia).
        """
        raise NotImplementedError

    # ---- Helpers utiles para todas las subclases ----
    @staticmethod
    def limpiar_precio(texto: str) -> tuple[float | None, str | None]:
        """
        '₡125,000.00' -> (125000.00, 'CRC')
        '$250.00'     -> (250.00, 'USD')
        'COP 44,131'  -> (44131.00, 'COP')
        Devuelve (precio, moneda) o (None, None) si no se pudo parsear.
        """
        if not texto:
            return None, None
        t = texto.strip()

        # Detectar moneda
        moneda = None
        if "₡" in t or "CRC" in t.upper() or "COLONES" in t.upper():
            moneda = "CRC"
        elif "$" in t or "USD" in t.upper():
            moneda = "USD"
        elif "COP" in t.upper():
            moneda = "COP"

        # Extraer solo digitos, comas y puntos
        limpio = "".join(c for c in t if c.isdigit() or c in ".,")
        if not limpio:
            return None, moneda

        # Heuristica separador miles/decimales:
        # - Si tiene ambos, el ultimo caracter separador es decimal
        # - Si solo tiene coma y hay 3 digitos despues, es miles
        # - Si solo tiene punto, es decimal
        if "," in limpio and "." in limpio:
            if limpio.rfind(",") > limpio.rfind("."):
                limpio = limpio.replace(".", "").replace(",", ".")
            else:
                limpio = limpio.replace(",", "")
        elif "," in limpio:
            partes = limpio.split(",")
            if len(partes[-1]) == 3:
                limpio = limpio.replace(",", "")
            else:
                limpio = limpio.replace(",", ".")

        try:
            return float(limpio), moneda
        except ValueError:
            return None, moneda

    @staticmethod
    def url_absoluta(url: str, base: str) -> str:
        """Convierte '/producto/x' -> 'https://sitio.com/producto/x'."""
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return base.rstrip("/") + url
        return base.rstrip("/") + "/" + url
