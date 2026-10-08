"""Scraper del catálogo público de Compunómicos Costa Rica."""

from __future__ import annotations

from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class CompunomicosScraper(ScraperBase):
    """Busca productos públicos de Compunómicos y completa datos desde su ficha."""

    codigo = "compunomicos"
    nombre = "Compunómicos"
    url_base = "https://compunomicos.com"
    requiere_login = False

    SEL_CARD = "ul.products li.product"
    SEL_LINK = "a.woocommerce-LoopProduct-link"
    SEL_TITLE = "h2.woocommerce-loop-product__title"
    SEL_PRICE = ".price"
    SEL_IMAGE = ".et_shop_image img"
    SEL_NO_RESULTS = ".not-found-title"

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        texto = query.strip()
        if not texto:
            return []

        productos: list[ProductoEncontrado] = []
        pagina = 1

        while len(productos) < config.scraper_max_resultados:
            url = self._url_busqueda(texto, pagina)
            print(f"  [compunomicos] Cargando {url}")
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.scraper_timeout_ms,
            )

            if page.locator(self.SEL_NO_RESULTS).count() > 0:
                break

            cards = page.locator(self.SEL_CARD)
            cantidad_cards = cards.count()
            if cantidad_cards == 0:
                break

            # La navegación a la ficha invalida los locators de esta página;
            # por eso primero copiamos los datos visibles de cada tarjeta.
            encontrados_en_pagina = [
                self._datos_tarjeta(cards.nth(indice))
                for indice in range(cantidad_cards)
            ]

            validos = [datos for datos in encontrados_en_pagina if datos is not None]
            if not validos:
                break

            restantes = config.scraper_max_resultados - len(productos)
            for datos in validos[:restantes]:
                productos.append(self._completar_desde_ficha(page, datos))

            # Una página parcial es la última. Evita una petición adicional
            # cuando el catálogo tiene menos de 24 resultados en esa página.
            if len(validos) < cantidad_cards or cantidad_cards < 24:
                break
            pagina += 1

        print(f"  [compunomicos] Productos obtenidos: {len(productos)}")
        return productos

    def _url_busqueda(self, query: str, pagina: int) -> str:
        parametros = f"s={quote_plus(query)}&post_type=product"
        if pagina > 1:
            parametros += f"&product-page={pagina}"
        return f"{self.url_base}/?{parametros}"

    def _datos_tarjeta(self, card) -> dict | None:
        enlace = card.locator(self.SEL_LINK).first
        titulo = card.locator(self.SEL_TITLE).first
        if enlace.count() == 0 or titulo.count() == 0:
            return None

        nombre = titulo.inner_text().strip()
        url_detalle = enlace.get_attribute("href") or ""
        if not nombre or not url_detalle:
            return None

        precio_texto = self._texto(card, self.SEL_PRICE)
        precio_actual_texto = self._texto(card, ".price ins") or self._texto(
            card, ".price .woocommerce-Price-amount"
        )
        precio, moneda = self.limpiar_precio(precio_actual_texto or "")
        precio_regular_texto = self._texto(card, ".price del")
        precio_regular, _ = self.limpiar_precio(precio_regular_texto or "")

        imagen = card.locator(self.SEL_IMAGE).first
        imagen_url = None
        if imagen.count() > 0:
            imagen_url = imagen.get_attribute("data-src") or imagen.get_attribute("src")

        clases = card.get_attribute("class") or ""
        disponible = True if "instock" in clases else False if "outofstock" in clases else None
        stock_texto = "En stock" if disponible is True else "Agotado" if disponible is False else None

        return {
            "nombre": nombre,
            "url_detalle": self.url_absoluta(url_detalle, self.url_base),
            "url_imagen": self.url_absoluta(imagen_url, self.url_base) if imagen_url else None,
            "precio": precio,
            "precio_regular": precio_regular,
            "tiene_descuento": precio_regular is not None,
            "moneda": moneda,
            "precio_texto_original": precio_texto,
            "stock_texto": stock_texto,
            "disponible": disponible,
        }

    def _completar_desde_ficha(self, page, datos: dict) -> ProductoEncontrado:
        """Completa SKU, stock, marca y categoría sin descartar la tarjeta si falla."""
        sku = marca = categoria = None
        stock_texto = datos["stock_texto"]
        disponible = datos["disponible"]

        try:
            page.goto(
                datos["url_detalle"],
                wait_until="domcontentloaded",
                timeout=config.scraper_timeout_ms,
            )
            sku = self._texto(page, ".sku")
            marca = self._valor_atributo(page, "Marca")
            categoria = self._texto(page, ".posted_in a")
            stock_ficha = self._texto(page, ".stock")
            if stock_ficha:
                stock_texto = stock_ficha
                disponible = "out-of-stock" not in (
                    page.locator(".stock").first.get_attribute("class") or ""
                )
        except Exception as error:
            print(f"  [compunomicos] Ficha no disponible: {datos['url_detalle']} ({error})")

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=datos["nombre"],
            url_detalle=datos["url_detalle"],
            url_imagen=datos["url_imagen"],
            precio=datos["precio"],
            precio_regular=datos["precio_regular"],
            tiene_descuento=datos["tiene_descuento"],
            moneda=datos["moneda"],
            precio_texto_original=datos["precio_texto_original"],
            stock_texto=stock_texto,
            disponible=disponible,
            sku=sku,
            marca=marca,
            categoria=categoria,
        )

    @staticmethod
    def _texto(parent, selector: str) -> str | None:
        elemento = parent.locator(selector).first
        return elemento.inner_text().strip() if elemento.count() else None

    @staticmethod
    def _valor_atributo(page, etiqueta: str) -> str | None:
        filas = page.locator(".woocommerce-product-attributes tr")
        for indice in range(filas.count()):
            fila = filas.nth(indice)
            encabezado = fila.locator("th").first
            if encabezado.count() and encabezado.inner_text().strip().casefold() == etiqueta.casefold():
                return CompunomicosScraper._texto(fila, "td")
        return None
