"""
Scraper de Intelec (intelec.co.cr).

Estructura confirmada del sitio (Woodmart + WooCommerce + Elementor):
  - Contenedor de producto:  div.wd-product
  - Titulo + link:           h3.wd-entities-title > a
  - Imagen:                  .wd-product-thumb img  (usa data-src por lazy-load)
  - Precio actual:           span.price ins bdi   (si hay descuento)
                             span.price bdi       (si no hay descuento)
  - Precio anterior:         span.price del bdi   (tachado, solo si hay descuento)
  - Descuento badge:         .asnp-esb-sale-badge-dyn-1
  - SKU:                     .wd-sku
  - Marca:                   .wd-product-brands-links a
  - Categoria:               .wd-product-cats a
  - Stock:                   .wd-product-stock  (o clase 'instock'/'outofstock' del contenedor)
  - Data attrs utiles:       data-id, data-product_id, data-product_sku

Paginacion: infinita (clase 'pagination-infinit' en el grid).
Sin login necesario. Precios en colones (CRC).
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class IntelecScraper(ScraperBase):
    codigo = "intelec"
    nombre = "Intelec"
    url_base = "https://www.intelec.co.cr"
    requiere_login = False

    # Selector principal del contenedor de producto
    SEL_CARD = "div.wd-product"

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        url = f"{self.url_base}/?s={quote_plus(query)}&post_type=product"
        print(f"  [intelec] Cargando {url}")

        page.goto(url, wait_until="domcontentloaded", timeout=config.scraper_timeout_ms)

        # Esperar que la red se calme (WooCommerce trae varios recursos async)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            print("  [intelec] networkidle no llego en 15s, continuamos")

        # Esperar a que aparezcan los productos
        try:
            page.wait_for_selector(self.SEL_CARD, timeout=8000)
        except Exception:
            print("  [intelec] No aparecieron productos. Guardando HTML de debug...")
            self._guardar_debug(page, query)
            return []

        # Scroll hasta el final: traemos TODOS los productos disponibles
        self._scroll_hasta_el_final(page)

        # Extraer productos
        cards = page.locator(self.SEL_CARD).all()
        print(f"  [intelec] Procesando {len(cards)} productos")

        productos: list[ProductoEncontrado] = []
        for i, card in enumerate(cards):
            try:
                producto = self._parsear_card(card)
                if producto:
                    productos.append(producto)
            except Exception as e:
                print(f"  [intelec] Error en card {i}: {e}")

        return productos

    # -----------------------------------------------------------------
    # Extraccion de un producto
    # -----------------------------------------------------------------
    def _parsear_card(self, card) -> ProductoEncontrado | None:
        # ---- Nombre + URL detalle ----
        titulo_link = card.locator("h3.wd-entities-title a").first
        if titulo_link.count() == 0:
            return None
        nombre = titulo_link.inner_text().strip()
        url_detalle = titulo_link.get_attribute("href") or ""
        url_detalle = self.url_absoluta(url_detalle, self.url_base)

        # ---- ID interno WooCommerce (del atributo data-id del contenedor) ----
        id_interno = card.get_attribute("data-id") or None

        # ---- Imagen ----
        # Perfmatters usa data-src hasta que hace scroll,
        # pero luego pasa el valor a src. Probamos ambos.
        url_imagen: str | None = None
        img_el = card.locator(".wd-product-thumb img").first
        if img_el.count() > 0:
            for attr in ("src", "data-src"):
                val = img_el.get_attribute(attr)
                if val and "data:image" not in val:  # descartar placeholders base64
                    url_imagen = val
                    break

        # ---- SKU ----
        sku: str | None = None
        sku_el = card.locator(".wd-sku").first
        if sku_el.count() > 0:
            sku = sku_el.inner_text().strip()
        if not sku:
            # Fallback: data-product_sku del boton "Añadir al carrito"
            btn = card.locator(".add-to-cart-loop").first
            if btn.count() > 0:
                sku = btn.get_attribute("data-product_sku")

        # ---- Marca ----
        marca: str | None = None
        marca_el = card.locator(".wd-product-brands-links a").first
        if marca_el.count() > 0:
            marca = marca_el.inner_text().strip()

        # ---- Categoria ----
        categoria: str | None = None
        cat_el = card.locator(".wd-product-cats a").first
        if cat_el.count() > 0:
            categoria = cat_el.inner_text().strip()

        # ---- Stock ----
        stock_texto: str | None = None
        disponible: bool | None = None
        stock_el = card.locator(".wd-product-stock").first
        if stock_el.count() > 0:
            stock_texto = stock_el.inner_text().strip()

        # Fallback: clases del contenedor
        clases = card.get_attribute("class") or ""
        if "instock" in clases:
            disponible = True
            if not stock_texto:
                stock_texto = "En stock"
        elif "outofstock" in clases:
            disponible = False
            if not stock_texto:
                stock_texto = "Agotado"

        # ---- Precios ----
        precio_actual, precio_regular, moneda, precio_texto, tiene_desc, pct_desc = (
            self._extraer_precios(card)
        )

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=url_detalle,
            url_imagen=url_imagen,
            precio=precio_actual,
            precio_regular=precio_regular,
            tiene_descuento=tiene_desc,
            descuento_porcentaje=pct_desc,
            moneda=moneda,
            precio_texto_original=precio_texto,
            stock_texto=stock_texto,
            disponible=disponible,
            sku=sku,
            marca=marca,
            categoria=categoria,
            extra={
                "id_interno": id_interno,
                "en_oferta": "sale" in clases,
            },
        )

    def _extraer_precios(self, card) -> tuple:
        """
        Devuelve: (precio_actual, precio_regular, moneda, precio_texto, tiene_desc, pct_desc)

        Estructura HTML:
          Con descuento:
            <span class="price">
              <del><span><bdi>₡43,500</bdi></span></del>   <- precio regular
              <ins><span><bdi>₡30,900</bdi></span></ins>   <- precio actual (rebajado)
            </span>
          Sin descuento:
            <span class="price">
              <span><bdi>₡5,700</bdi></span>               <- precio unico
            </span>
        """
        precio_actual: float | None = None
        precio_regular: float | None = None
        moneda: str | None = None
        precio_texto_completo: str | None = None
        tiene_descuento = False
        pct_desc: float | None = None

        precio_wrapper = card.locator("span.price").first
        if precio_wrapper.count() == 0:
            return (None, None, None, None, False, None)

        precio_texto_completo = precio_wrapper.inner_text().strip()

        # Con descuento (hay <ins>)
        ins_bdi = card.locator("span.price ins bdi").first
        del_bdi = card.locator("span.price del bdi").first

        if ins_bdi.count() > 0:
            tiene_descuento = True
            precio_actual, moneda = self.limpiar_precio(ins_bdi.inner_text())
            if del_bdi.count() > 0:
                precio_regular, _ = self.limpiar_precio(del_bdi.inner_text())

            # Descuento porcentual: leer del badge si existe
            badge = card.locator(".asnp-esb-sale-text-badge-dyn-1").first
            if badge.count() > 0:
                pct_txt = badge.inner_text()
                m = re.search(r"(\d+(?:\.\d+)?)", pct_txt)
                if m:
                    pct_desc = float(m.group(1))
            # Si no hubo badge, calcular
            if pct_desc is None and precio_regular and precio_actual:
                pct_desc = round((1 - precio_actual / precio_regular) * 100, 2)
        else:
            # Sin descuento: primer bdi dentro de .price (sin del/ins)
            bdi_simple = card.locator(
                "span.price > span.woocommerce-Price-amount bdi"
            ).first
            if bdi_simple.count() > 0:
                precio_actual, moneda = self.limpiar_precio(bdi_simple.inner_text())

        return (
            precio_actual,
            precio_regular,
            moneda,
            precio_texto_completo,
            tiene_descuento,
            pct_desc,
        )

    # -----------------------------------------------------------------
    # Scroll para paginacion infinita + lazy load
    # -----------------------------------------------------------------
    def _scroll_hasta_el_final(self, page) -> None:
        """
        Scroll hasta que la pagination-infinit ya no cargue mas productos.
        Sin limite artificial de cantidad — traemos todo lo disponible.
        """
        max_scrolls_sin_cambio = 3  # si 3 scrolls seguidos no cargan nada, terminamos
        sin_cambio = 0
        prev_count = 0
        scroll_num = 0

        while sin_cambio < max_scrolls_sin_cambio:
            scroll_num += 1
            count_actual = page.locator(self.SEL_CARD).count()

            # Scroll al final
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1800)  # esperar carga async

            count_nuevo = page.locator(self.SEL_CARD).count()

            if count_nuevo == prev_count:
                sin_cambio += 1
                print(
                    f"  [intelec] scroll #{scroll_num}: {count_nuevo} productos "
                    f"(sin cambio {sin_cambio}/{max_scrolls_sin_cambio})"
                )
            else:
                sin_cambio = 0
                print(
                    f"  [intelec] scroll #{scroll_num}: {count_nuevo} productos "
                    f"(+{count_nuevo - prev_count})"
                )

            prev_count = count_nuevo

            # Tope de seguridad absoluto (por si algo va mal)
            if scroll_num > 50:
                print(f"  [intelec] tope de 50 scrolls alcanzado")
                break

        print(f"  [intelec] Total final: {prev_count} productos disponibles")

        # Scroll suave por toda la pagina para disparar lazy-load de imagenes
        page.evaluate("""
            async () => {
                let y = 0;
                const step = 500;
                while (y < document.body.scrollHeight) {
                    window.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, 150));
                    y += step;
                }
                window.scrollTo(0, 0);
            }
        """)
        page.wait_for_timeout(500)

    # -----------------------------------------------------------------
    def _guardar_debug(self, page, query: str) -> None:
        from pathlib import Path

        Path("debug").mkdir(exist_ok=True)
        safe = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
        p = Path(f"debug/intelec_FALLO_{safe}.html")
        p.write_text(page.content(), encoding="utf-8")
        print(f"  [intelec] HTML guardado en {p}")
