"""Scraper del catálogo público de Compunómicos Costa Rica."""

from __future__ import annotations

import random
import re
from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class CompunomicosScraper(ScraperBase):
    """Busca productos públicos de Compunómicos desde sus tarjetas de resultados."""

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
    PAUSA_MIN_MS = 800
    PAUSA_MAX_MS = 1600
    ESPERA_RESULTADOS_MS = 10000

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        texto = query.strip()
        if not texto:
            return []

        productos: list[ProductoEncontrado] = []
        vistos: set[str] = set()
        pagina = 1
        cards_por_pagina = 0

        while len(productos) < config.scraper_max_resultados:
            url = self._url_busqueda(texto, pagina)
            print(f"  [compunomicos] Cargando {url}")
            self._esperar_antes_de_buscar()
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.scraper_timeout_ms,
            )

            cantidad_cards = self._esperar_resultados(pagina)
            if cantidad_cards == 0:
                break

            cards = page.locator(self.SEL_CARD)
            nuevos = 0
            for indice in range(cantidad_cards):
                if len(productos) >= config.scraper_max_resultados:
                    break
                try:
                    producto = self._datos_tarjeta(cards.nth(indice))
                except Exception as error:
                    print(f"  [compunomicos] Error en tarjeta {indice}: {error}")
                    continue
                if producto is None or producto.url_detalle in vistos:
                    continue
                productos.append(producto)
                vistos.add(producto.url_detalle)
                nuevos += 1

            print(
                f"  [compunomicos] Página {pagina}: {cantidad_cards} tarjetas, "
                f"{nuevos} nuevos (acumulado {len(productos)})"
            )

            # La primera página define cuántas tarjetas trae el catálogo por
            # página; una página con menos que esa es la última.
            if pagina == 1:
                cards_por_pagina = cantidad_cards
            if nuevos == 0 or cantidad_cards < cards_por_pagina:
                break
            pagina += 1

        print(f"  [compunomicos] Productos obtenidos: {len(productos)}")
        return productos

    def _esperar_resultados(self, pagina: int) -> int:
        """Espera a que el catálogo pinte tarjetas o confirme cero resultados.

        Devuelve cuántas tarjetas quedaron visibles. Antes se contaba
        inmediatamente después del goto, y una página lenta se leía como
        búsqueda sin resultados.
        """
        page = self._page
        assert page is not None

        try:
            page.wait_for_function(
                """({cardSelector, vacioSelector}) => {
                    if (document.querySelectorAll(cardSelector).length > 0) return true;
                    if (document.querySelector(vacioSelector)) return true;
                    const texto = document.body?.innerText || '';
                    return /no se (?:encontraron|han encontrado)|sin resultados|no products were found/i.test(texto);
                }""",
                arg={
                    "cardSelector": self.SEL_CARD,
                    "vacioSelector": self.SEL_NO_RESULTS,
                },
                timeout=self.ESPERA_RESULTADOS_MS,
            )
        except Exception:
            print(
                f"  [compunomicos] La página {pagina} no confirmó resultados "
                f"en {self.ESPERA_RESULTADOS_MS} ms"
            )

        cantidad = page.locator(self.SEL_CARD).count()
        if cantidad == 0:
            if page.locator(self.SEL_NO_RESULTS).count() > 0:
                print("  [compunomicos] El sitio reporta: sin resultados")
            else:
                cuerpo = " ".join((page.locator("body").inner_text() or "").split())
                print(
                    "  [compunomicos] Sin tarjetas y sin mensaje de vacío. "
                    f"URL final: {page.url}"
                )
                print(f"  [compunomicos] Texto visible: {cuerpo[:300]!r}")
        return cantidad

    def _url_busqueda(self, query: str, pagina: int) -> str:
        parametros = f"s={quote_plus(query)}&post_type=product"
        if pagina > 1:
            parametros += f"&product-page={pagina}"
        return f"{self.url_base}/?{parametros}"

    def _datos_tarjeta(self, card) -> ProductoEncontrado | None:
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
        disponible = (
            True if "instock" in clases else False if "outofstock" in clases else None
        )
        stock_texto = (
            "En stock"
            if disponible is True
            else "Agotado" if disponible is False else None
        )

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=self.url_absoluta(url_detalle, self.url_base),
            url_imagen=(
                self.url_absoluta(imagen_url, self.url_base) if imagen_url else None
            ),
            precio=precio,
            precio_regular=precio_regular,
            tiene_descuento=precio_regular is not None,
            moneda=moneda,
            precio_texto_original=precio_texto,
            stock_texto=stock_texto,
            disponible=disponible,
            marca=self._extraer_marca(nombre),
        )

    @staticmethod
    def _extraer_marca(nombre: str) -> str | None:
        """Detecta marcas conocidas dentro del nombre del producto."""
        marcas = (
            "LOGITECH",
            "MICROSOFT",
            "HYPERX",
            "KINGSTON",
            "SAMSUNG",
            "LENOVO",
            "ASUS",
            "ACER",
            "APPLE",
            "XIAOMI",
            "HUAWEI",
            "TOSHIBA",
            "PHILIPS",
            "VIEWSONIC",
            "AOC",
            "DELL",
            "HP",
            "AMD",
            "INTEL",
            "NVIDIA",
            "RAZER",
            "CANON",
            "EPSON",
            "BROTHER",
            "JBL",
            "SONY",
            "LG",
            "MSI",
            "TP-LINK",
            "UGREEN",
            "TRUST",
            "TARGUS",
            "GENIUS",
            "KLIPX",
            "FORZA",
            "NEXXT",
        )
        titulo = re.sub(r"\s+", " ", nombre).upper()
        for marca in marcas:
            if re.search(rf"(?<![A-Z0-9]){re.escape(marca)}(?![A-Z0-9])", titulo):
                return marca.title()
        return None

    def _esperar_antes_de_buscar(self) -> None:
        """Introduce una pausa breve y variable antes de cargar resultados."""
        page = self._page
        assert page is not None
        page.wait_for_timeout(random.randint(self.PAUSA_MIN_MS, self.PAUSA_MAX_MS))

    @staticmethod
    def _texto(parent, selector: str) -> str | None:
        elemento = parent.locator(selector).first
        return elemento.inner_text().strip() if elemento.count() else None
