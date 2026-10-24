"""Scraper del catálogo público de Mayorista BPC Costa Rica."""

from __future__ import annotations

import random
from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class BPCScraper(ScraperBase):
    """Busca productos públicos de BPC (PrestaShop).

    BPC opera el catálogo público sin precios ni disponibilidad visible: esos
    datos se solicitan a un asesor desde cada ficha. Por ello se devuelven como
    ``None`` y no se intenta leer valores internos que el sitio no presenta al
    comprador.
    """

    codigo = "bpc"
    nombre = "Mayorista BPC"
    url_base = "https://www.mayoristabpc.com"
    requiere_login = False

    SEL_CARD = "article.product-miniature.js-product-miniature"
    SEL_CARD_LINK = "h2.product-title a, h3.product-title a"
    SEL_CARD_IMAGE = "a.product-thumbnail img"
    SEL_NO_RESULTS = "#product-search-no-matches"
    SEL_LOAD_MORE = ".ets_plp_pagination a.load_more"
    PAUSA_MIN_MS = 800
    PAUSA_MAX_MS = 1600

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        texto = query.strip()
        if not texto:
            return []

        url = f"{self.url_base}/busqueda?s={quote_plus(texto)}"
        vistos: set[str] = set()
        productos: list[ProductoEncontrado] = []

        while url and len(productos) < config.scraper_max_resultados:
            print(f"  [bpc] Cargando {url}")
            self._esperar_antes_de_buscar()
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.scraper_timeout_ms,
            )

            if page.locator(self.SEL_NO_RESULTS).count() > 0:
                break

            try:
                page.wait_for_selector(self.SEL_CARD, timeout=config.scraper_timeout_ms)
            except Exception:
                # Una página de resultados sin tarjetas y sin el mensaje de
                # cero coincidencias se interpreta como catálogo vacío.
                break

            cards = page.locator(self.SEL_CARD)
            cantidad = cards.count()
            if cantidad == 0:
                break

            for indice in range(cantidad):
                datos = self._datos_tarjeta(cards.nth(indice))
                if not datos or datos.url_detalle in vistos:
                    continue
                vistos.add(datos.url_detalle)
                productos.append(datos)
                if len(productos) >= config.scraper_max_resultados:
                    break

            url = self._siguiente_pagina(page)

        print(f"  [bpc] Productos obtenidos: {len(productos)}")
        return productos

    def _datos_tarjeta(self, card) -> ProductoEncontrado | None:
        enlace = card.locator(self.SEL_CARD_LINK).first
        if enlace.count() == 0:
            return None

        url_detalle = enlace.get_attribute("href") or ""
        nombre = enlace.inner_text().strip()
        if not url_detalle or not nombre:
            return None

        imagen = card.locator(self.SEL_CARD_IMAGE).first
        url_imagen = None
        if imagen.count() > 0:
            url_imagen = imagen.get_attribute("data-full-size-image-url") or imagen.get_attribute("src")

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=self.url_absoluta(url_detalle, self.url_base),
            url_imagen=self.url_absoluta(url_imagen, self.url_base) if url_imagen else None,
            extra={"id_producto": card.get_attribute("data-id-product")},
        )

    def _siguiente_pagina(self, page) -> str | None:
        """Obtiene la URL de "Ver más" sin depender del JavaScript del módulo."""
        enlace = page.locator(self.SEL_LOAD_MORE).first
        if enlace.count() == 0:
            return None

        href = enlace.get_attribute("href")
        if not href:
            return None

        # El módulo agrega from-xhr para su llamada parcial; al navegar con
        # Playwright se debe cargar la página completa para conservar selectores.
        href = href.replace("&from-xhr", "").replace("?from-xhr", "")
        return self.url_absoluta(href, self.url_base)

    def _esperar_antes_de_buscar(self) -> None:
        """Introduce una pausa breve y variable antes de cargar resultados."""
        page = self._page
        assert page is not None
        page.wait_for_timeout(random.randint(self.PAUSA_MIN_MS, self.PAUSA_MAX_MS))
