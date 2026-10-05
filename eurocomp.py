"""Scraper autenticado del catálogo de Eurocomp Costa Rica."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class EurocompScraper(ScraperBase):
    """Busca artículos en el catálogo AJAX de Eurocomp."""

    codigo = "eurocomp"
    nombre = "Eurocomp Costa Rica"
    url_base = "https://eurocompcr.com"
    requiere_login = True

    SEL_LOGIN_USER = "#login_userid"
    SEL_LOGIN_PASSWORD = "#login_passwd"
    SEL_LOGIN_SUBMIT = "#frm_login button[type='submit']"
    SEL_SEARCH_INPUT = "#ed"
    SEL_SEARCH_SUBMIT = "#catalogo_submit"
    SEL_CARD = "article.itemex-plus-card"

    def __init__(self, browser) -> None:
        super().__init__(browser)
        self._login_error: Exception | None = None

    def _login(self) -> None:
        """Difiere un fallo de login hasta ejecutar(), que lo registra por proveedor."""
        try:
            self._iniciar_sesion()
        except Exception as error:
            self._login_error = error

    def _iniciar_sesion(self) -> None:
        page = self._page
        assert page is not None

        usuario = os.getenv("EUROCOMP_USER", "").strip()
        password = os.getenv("EUROCOMP_PASSWORD", "")
        if not usuario or not password:
            raise RuntimeError(
                "Faltan EUROCOMP_USER o EUROCOMP_PASSWORD en el archivo .env"
            )

        page.goto(
            self.url_base,
            wait_until="domcontentloaded",
            timeout=config.scraper_timeout_ms,
        )
        page.locator("#nav_login_info").click()
        page.locator("[onclick*=\"login.php\"]").click()
        page.wait_for_selector(self.SEL_LOGIN_USER, timeout=config.scraper_timeout_ms)

        page.fill(self.SEL_LOGIN_USER, usuario)
        page.fill(self.SEL_LOGIN_PASSWORD, password)
        page.locator(self.SEL_LOGIN_SUBMIT).click()

        # El login se completa por AJAX: esperamos que el menú deje de identificar
        # la sesión como visitante, sin registrar ni mostrar datos de la cuenta.
        page.wait_for_function(
            """() => {
                const nav = document.querySelector('#nav_login_info');
                return nav && !/visitante/i.test(nav.textContent || '');
            }""",
            timeout=config.scraper_timeout_ms,
        )

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        if self._login_error:
            raise RuntimeError("No fue posible iniciar sesión en Eurocomp") from self._login_error

        texto = query.strip()
        if not texto:
            return []

        print(f"  [eurocomp] Buscando: {texto}")
        page.fill(self.SEL_SEARCH_INPUT, texto)
        page.locator(self.SEL_SEARCH_SUBMIT).click()
        # El catálogo reemplaza #main_div por AJAX. También esperamos el contador
        # para que una búsqueda sin coincidencias sea un resultado exitoso vacío.
        page.wait_for_function(
            """() => /Encontrados\\s+\\d+\\s+artículos\\./i.test(
                document.querySelector('#main_div')?.innerText || ''
            )""",
            timeout=config.scraper_timeout_ms,
        )

        cards = page.locator(self.SEL_CARD).all()
        print(f"  [eurocomp] Procesando {min(len(cards), config.scraper_max_resultados)} productos")

        productos: list[ProductoEncontrado] = []
        for indice, card in enumerate(cards[: config.scraper_max_resultados]):
            try:
                producto = self._parsear_card(card)
                if producto:
                    productos.append(producto)
            except Exception as error:
                print(f"  [eurocomp] Error en card {indice}: {error}")

        return productos

    def _parsear_card(self, card) -> ProductoEncontrado | None:
        nombre_el = card.locator(".itemex-plus-title").first
        if nombre_el.count() == 0:
            return None
        nombre = nombre_el.inner_text().strip()
        if not nombre:
            return None

        sku = self._texto(card, ".itemex-plus-code")
        if sku:
            sku = re.sub(r"^C[oó]d:\s*", "", sku, flags=re.IGNORECASE).strip() or None

        marca = self._texto(card, ".itemex-plus-badge-brand")
        precio_texto = self._texto(card, ".itemex-plus-price-main")
        precio, moneda = self.limpiar_precio(precio_texto or "")
        imagen = card.locator("img").first.get_attribute("src") or ""
        url_imagen = self.url_absoluta(imagen, self.url_base) if imagen else None

        onclick = (
            card.locator(".itemex-plus-media, .itemex-plus-btn-cart")
            .first.get_attribute("onclick")
            or ""
        )
        item_id = self._extraer_item_id(onclick)
        url_detalle = (
            f"{self.url_base}/item_shop.php?item_id={item_id}"
            if item_id
            else f"{self.url_base}/item_explorar.php?ed={quote_plus(nombre)}"
        )

        impuesto = self._texto(card, ".itemex-plus-price-tax")
        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=url_detalle,
            url_imagen=url_imagen,
            precio=precio,
            moneda=moneda,
            precio_texto_original=precio_texto,
            sku=sku,
            marca=marca,
            extra={
                "item_id": item_id,
                "impuesto_texto": impuesto,
            },
        )

    @staticmethod
    def _texto(card, selector: str) -> str | None:
        elemento = card.locator(selector).first
        return elemento.inner_text().strip() if elemento.count() else None

    @staticmethod
    def _extraer_item_id(onclick: str) -> str | None:
        coincidencia = re.search(r"item_id=(\d+)", onclick)
        return coincidencia.group(1) if coincidencia else None
