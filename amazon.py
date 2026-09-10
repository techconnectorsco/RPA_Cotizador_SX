"""
Scraper de Amazon (amazon.com).

Estructura confirmada del sitio:
  - Contenedor de producto:  [data-component-type="s-search-result"]
  - ASIN:                    atributo data-asin del contenedor
  - Nombre:                  h2 span
  - Link:                    <a> padre del h2 (URL relativa, prefijar dominio)
  - Imagen:                  img.s-image
  - Precio actual:           .a-price .a-offscreen (formato "COP 44,131.23")
  - Precio tachado:          .a-text-price .a-offscreen
  - Rating:                  i.a-icon-star-small span (aria-label)
  - Reviews:                 .s-underline-text (ej "(44.8K)")
  - Prime:                   .a-icon-prime
  - Sponsored:               [aria-label*="Sponsored"] o "Patrocinado"
  - Badge (Choice/Bestsell): .a-badge-text o .a-badge-label-inner

CAPTCHA:
  - Detectamos si aparece y fallamos controladamente (no reintentamos)
  - Reintentar dispara IP-ban de Amazon
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class AmazonScraper(ScraperBase):
    codigo = "amazon"
    nombre = "Amazon"
    url_base = "https://www.amazon.com"
    requiere_login = False

    SEL_CARD = '[data-component-type="s-search-result"]'

    # Indicadores de que estamos ante CAPTCHA / bloqueo
    CAPTCHA_MARKERS = [
        "Enter the characters you see below",
        "Type the characters you see in this image",
        "validateCaptcha",
        "Sorry, we just need to make sure",
        "api-services-support@amazon.com",
    ]

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        url = f"{self.url_base}/s?k={quote_plus(query)}"
        print(f"  [amazon] Cargando {url}")

        page.goto(url, wait_until="domcontentloaded", timeout=config.scraper_timeout_ms)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            print("  [amazon] networkidle no llego en 15s, continuamos")

        # ---- Verificar si nos bloquearon con CAPTCHA ----
        html_actual = page.content()
        for marker in self.CAPTCHA_MARKERS:
            if marker.lower() in html_actual.lower():
                print(f"  [amazon] ⚠️  CAPTCHA detectado ('{marker[:40]}...')")
                print("  [amazon] Abortando sin reintentos para evitar IP-ban")
                self._guardar_debug(page, query, sufijo="CAPTCHA")
                # Lanza excepcion controlada — el scraper_base la captura
                raise RuntimeError("Amazon devolvio CAPTCHA — fallando controladamente")

        # ---- Esperar productos ----
        try:
            page.wait_for_selector(self.SEL_CARD, timeout=8000)
        except Exception:
            print("  [amazon] No aparecieron productos")
            self._guardar_debug(page, query, sufijo="SIN_RESULTADOS")
            return []

        # Amazon no tiene scroll infinito, todos los productos ya están cargados
        # pero hacemos scroll ligero para disparar lazy-load de imágenes
        self._scroll_ligero(page)

        # ---- Extraer productos ----
        cards = page.locator(self.SEL_CARD).all()
        print(f"  [amazon] {len(cards)} productos detectados")

        productos: list[ProductoEncontrado] = []
        for i, card in enumerate(cards):
            try:
                producto = self._parsear_card(card)
                if producto:
                    productos.append(producto)
            except Exception as e:
                print(f"  [amazon] Error en card {i}: {e}")

        return productos

    # -----------------------------------------------------------------
    def _parsear_card(self, card) -> ProductoEncontrado | None:
        # ---- ASIN ----
        asin = card.get_attribute("data-asin")
        if not asin:
            return None

        # ---- Nombre ----
        nombre_el = card.locator("h2 span").first
        if nombre_el.count() == 0:
            return None
        nombre = nombre_el.inner_text().strip()

        # ---- URL detalle ----
        # Amazon: el <a> padre del h2 tiene el href
        link_el = card.locator("h2").locator("xpath=ancestor::a").first
        url_detalle = ""
        if link_el.count() > 0:
            href = link_el.get_attribute("href") or ""
            url_detalle = self.url_absoluta(href, self.url_base)

        # Fallback: URL construida desde el ASIN
        if not url_detalle:
            url_detalle = f"{self.url_base}/dp/{asin}"

        # ---- Imagen ----
        url_imagen: str | None = None
        img_el = card.locator("img.s-image").first
        if img_el.count() > 0:
            url_imagen = img_el.get_attribute("src")

        # ---- Precio actual ----
        precio_val: float | None = None
        precio_regular: float | None = None
        moneda: str | None = None
        precio_texto: str | None = None
        tiene_descuento = False
        pct_desc: float | None = None

        precio_el = card.locator(".a-price .a-offscreen").first
        if precio_el.count() > 0:
            precio_texto = precio_el.inner_text().strip()
            precio_val, moneda = self.limpiar_precio(precio_texto)

        # ---- Precio tachado (si hay descuento) ----
        tachado_el = card.locator(".a-text-price .a-offscreen").first
        if tachado_el.count() > 0:
            tachado_texto = tachado_el.inner_text().strip()
            precio_regular, _ = self.limpiar_precio(tachado_texto)
            if precio_regular and precio_val and precio_regular > precio_val:
                tiene_descuento = True
                pct_desc = round((1 - precio_val / precio_regular) * 100, 2)

        # ---- Marca (extraer del nombre) ----
        marca = self._inferir_marca(nombre)

        # ---- Rating ----
        rating: float | None = None
        star_el = card.locator("i[class*='a-icon-star'] span.a-icon-alt").first
        if star_el.count() > 0:
            rating_txt = star_el.inner_text()  # ej "4.6 out of 5 stars"
            m = re.search(r"(\d+(?:\.\d+)?)", rating_txt)
            if m:
                try:
                    rating = float(m.group(1))
                except ValueError:
                    pass

        # ---- Reviews count ----
        reviews_count: int | None = None
        for sel in ["a[href*='#customerReviews'] span", ".s-underline-text"]:
            el = card.locator(sel).first
            if el.count() > 0:
                txt = el.inner_text().strip()
                reviews_count = self._parsear_reviews_count(txt)
                if reviews_count is not None:
                    break

        # ---- Prime ----
        prime = card.locator("i.a-icon-prime").count() > 0

        # ---- Sponsored ----
        sponsored = (
            card.locator(
                "[aria-label*='Sponsored'], [aria-label*='Patrocinado']"
            ).count()
            > 0
        )

        # ---- Badge (Amazon's Choice, Best Seller) ----
        badge_texto: str | None = None
        badge_el = card.locator(".a-badge-text, .a-badge-label-inner").first
        if badge_el.count() > 0:
            badge_texto = badge_el.inner_text().strip()

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=url_detalle,
            url_imagen=url_imagen,
            precio=precio_val,
            precio_regular=precio_regular,
            tiene_descuento=tiene_descuento,
            descuento_porcentaje=pct_desc,
            moneda=moneda,
            precio_texto_original=precio_texto,
            marca=marca,
            sku=asin,  # el ASIN es el identificador unico en Amazon
            extra={
                "asin": asin,
                "rating": rating,
                "reviews_count": reviews_count,
                "prime": prime,
                "sponsored": sponsored,
                "badge": badge_texto,
            },
        )

    # -----------------------------------------------------------------
    @staticmethod
    def _inferir_marca(nombre: str) -> str | None:
        """
        Amazon no expone marca como campo estructurado en el listado.
        Extraemos del inicio del nombre: casi siempre el primer token es la marca.
        Ej: 'Logitech Ratón inalámbrico...' -> 'Logitech'
        """
        if not nombre:
            return None
        # Primera palabra que no sea un articulo/preposicion
        for palabra in nombre.split():
            palabra_limpia = palabra.strip(",.():;")
            if len(palabra_limpia) >= 2 and palabra_limpia.lower() not in {
                "el",
                "la",
                "los",
                "las",
                "un",
                "una",
                "de",
                "para",
                "con",
                "the",
                "a",
                "an",
                "of",
                "for",
                "with",
            }:
                return palabra_limpia
        return None

    @staticmethod
    def _parsear_reviews_count(texto: str) -> int | None:
        """
        '(44.8K)' -> 44800
        '(605)' -> 605
        '(1,234)' -> 1234
        """
        if not texto:
            return None
        t = texto.strip("()").strip()
        # Formato "X.YK" o "X.YM"
        m = re.match(r"^([\d.,]+)\s*([KMkm])?$", t)
        if not m:
            return None
        try:
            num = float(m.group(1).replace(",", ""))
            unidad = m.group(2)
            if unidad in ("K", "k"):
                num *= 1000
            elif unidad in ("M", "m"):
                num *= 1_000_000
            return int(num)
        except (ValueError, AttributeError):
            return None

    # -----------------------------------------------------------------
    def _scroll_ligero(self, page) -> None:
        """Scroll suave para disparar lazy-load de imagenes."""
        try:
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
        except Exception:
            pass

    # -----------------------------------------------------------------
    def _guardar_debug(self, page, query: str, sufijo: str = "DEBUG") -> None:
        from pathlib import Path

        Path("debug").mkdir(exist_ok=True)
        safe = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
        p = Path(f"debug/amazon_{sufijo}_{safe}.html")
        p.write_text(page.content(), encoding="utf-8")
        print(f"  [amazon] HTML guardado en {p}")
