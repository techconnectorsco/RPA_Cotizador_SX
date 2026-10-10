"""Scraper del catálogo público de Mayorista BPC Costa Rica."""

from __future__ import annotations

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

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        texto = query.strip()
        if not texto:
            return []

        url = f"{self.url_base}/busqueda?s={quote_plus(texto)}"
        vistos: set[str] = set()
        candidatos: list[dict[str, str | None]] = []

        while url and len(candidatos) < config.scraper_max_resultados:
            print(f"  [bpc] Cargando {url}")
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
                if not datos or datos["url_detalle"] in vistos:
                    continue
                vistos.add(datos["url_detalle"])
                candidatos.append(datos)
                if len(candidatos) >= config.scraper_max_resultados:
                    break

            url = self._siguiente_pagina(page)

        print(f"  [bpc] Fichas a procesar: {len(candidatos)}")
        productos: list[ProductoEncontrado] = []
        for datos in candidatos:
            productos.append(self._completar_desde_ficha(page, datos))

        print(f"  [bpc] Productos obtenidos: {len(productos)}")
        return productos

    def _datos_tarjeta(self, card) -> dict[str, str | None] | None:
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

        return {
            "nombre": nombre,
            "url_detalle": self.url_absoluta(url_detalle, self.url_base),
            "url_imagen": self.url_absoluta(url_imagen, self.url_base) if url_imagen else None,
            "id_producto": card.get_attribute("data-id-product"),
        }

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

    def _completar_desde_ficha(self, page, datos: dict[str, str | None]) -> ProductoEncontrado:
        """Obtiene nombre completo, marca y referencia desde la ficha pública."""
        nombre = datos["nombre"] or ""
        marca = sku = None
        url_imagen = datos["url_imagen"]

        try:
            page.goto(
                datos["url_detalle"] or "",
                wait_until="domcontentloaded",
                timeout=config.scraper_timeout_ms,
            )
            nombre = self._texto(page, "h1.h1") or nombre
            marca = self._texto(page, ".product-manufacturer img")
            if not marca:
                marca = self._texto(page, ".product-manufacturer a")
            sku = self._texto(page, ".product-reference span")
            url_imagen = self._imagen_ficha(page) or url_imagen
        except Exception as error:
            print(f"  [bpc] Ficha no disponible: {datos['url_detalle']} ({error})")

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=datos["url_detalle"] or "",
            url_imagen=url_imagen,
            precio=None,
            moneda=None,
            precio_texto_original=None,
            stock_texto=None,
            disponible=None,
            sku=sku,
            marca=marca,
            extra={"id_producto": datos["id_producto"]},
        )

    @staticmethod
    def _texto(parent, selector: str) -> str | None:
        elemento = parent.locator(selector).first
        if elemento.count() == 0:
            return None
        texto = elemento.get_attribute("alt") or elemento.inner_text()
        return texto.strip() or None

    @staticmethod
    def _imagen_ficha(page) -> str | None:
        imagen = page.locator(".product-cover img.js-qv-product-cover").first
        if imagen.count() == 0:
            return None
        return imagen.get_attribute("src") or None
