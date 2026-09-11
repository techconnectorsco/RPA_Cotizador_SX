"""
Scraper de Amazon (amazon.com).

Usa Patchright (fork de Playwright con parches anti-deteccion) para evadir
el bloqueo 503 que Amazon devuelve a Playwright estandar.

IMPORTANTE: este scraper se auto-provisiona su propio browser Patchright.
El parametro `browser` que recibe de main.py se ignora deliberadamente.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus

# CAMBIO: patchright en vez de playwright
from patchright.sync_api import sync_playwright as sync_patchright

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class AmazonScraper(ScraperBase):
    codigo = "amazon"
    nombre = "Amazon"
    url_base = "https://www.amazon.com"
    requiere_login = False
    usa_browser_compartido = False

    SEL_CARD = '[data-component-type="s-search-result"]'

    CAPTCHA_MARKERS = [
        "Enter the characters you see below",
        "Type the characters you see in this image",
        "validateCaptcha",
        "Sorry, we just need to make sure",
        "api-services-support@amazon.com",
    ]

    # CAMBIO: marcadores del 503 "Sorry! Something went wrong!"
    BLOQUEO_MARKERS = [
        "Sorry! Something went wrong",
        "cs_503_",
        "500_503.png",
    ]

    # -----------------------------------------------------------------
    # CAMBIO: __enter__ y __exit__ propios — ignoran self.browser y
    # arrancan un Patchright independiente con persistent_context.
    # -----------------------------------------------------------------
    def __enter__(self) -> "AmazonScraper":
        self._pw = sync_patchright().start()

        # Persistent context: obligatorio para que Patchright funcione bien.
        # El userdata queda en disco para reutilizar cookies/fingerprint entre corridas.
        user_data_dir = Path("./patchright_userdata/amazon")
        user_data_dir.mkdir(parents=True, exist_ok=True)

        # NO seteamos user_agent — Patchright usa el real del Chromium parchado.
        # NO seteamos viewport custom por la misma razon (usa no_viewport=True).
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=config.scraper_headless,
            no_viewport=True,
            locale="es-CR",
        )
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        return self

    def __exit__(self, *args) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if hasattr(self, "_pw") and self._pw:
                self._pw.stop()

    # -----------------------------------------------------------------
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

        html_actual = page.content()

        # CAMBIO: detectar 503 antes que CAPTCHA (es el bloqueo que estabamos viendo)
        for marker in self.BLOQUEO_MARKERS:
            if marker.lower() in html_actual.lower():
                print(f"  [amazon] ⚠️  Bloqueo 503 detectado ('{marker[:40]}...')")
                self._guardar_debug(page, query, sufijo="BLOQUEO_503")
                raise RuntimeError(
                    "Amazon devolvio pagina de error 503 — anti-bot activo"
                )

        for marker in self.CAPTCHA_MARKERS:
            if marker.lower() in html_actual.lower():
                print(f"  [amazon] ⚠️  CAPTCHA detectado ('{marker[:40]}...')")
                self._guardar_debug(page, query, sufijo="CAPTCHA")
                raise RuntimeError("Amazon devolvio CAPTCHA — fallando controladamente")

        try:
            page.wait_for_selector(self.SEL_CARD, timeout=8000)
        except Exception:
            print("  [amazon] No aparecieron productos")
            self._guardar_debug(page, query, sufijo="SIN_RESULTADOS")
            return []

        self._scroll_ligero(page)

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
    # A partir de aqui NADA cambia — pego integro para que copies-pegues completo
    # -----------------------------------------------------------------
    def _parsear_card(self, card) -> ProductoEncontrado | None:
        asin = card.get_attribute("data-asin")
        if not asin:
            return None

        nombre_el = card.locator("h2 span").first
        if nombre_el.count() == 0:
            return None
        nombre = nombre_el.inner_text().strip()

        link_el = card.locator("h2").locator("xpath=ancestor::a").first
        url_detalle = ""
        if link_el.count() > 0:
            href = link_el.get_attribute("href") or ""
            url_detalle = self.url_absoluta(href, self.url_base)
        if not url_detalle:
            url_detalle = f"{self.url_base}/dp/{asin}"

        url_imagen: str | None = None
        img_el = card.locator("img.s-image").first
        if img_el.count() > 0:
            url_imagen = img_el.get_attribute("src")

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

        tachado_el = card.locator(".a-text-price .a-offscreen").first
        if tachado_el.count() > 0:
            tachado_texto = tachado_el.inner_text().strip()
            precio_regular, _ = self.limpiar_precio(tachado_texto)
            if precio_regular and precio_val and precio_regular > precio_val:
                tiene_descuento = True
                pct_desc = round((1 - precio_val / precio_regular) * 100, 2)

        marca = self._inferir_marca(nombre)

        rating: float | None = None
        star_el = card.locator("i[class*='a-icon-star'] span.a-icon-alt").first
        if star_el.count() > 0:
            rating_txt = star_el.inner_text()
            m = re.search(r"(\d+(?:\.\d+)?)", rating_txt)
            if m:
                try:
                    rating = float(m.group(1))
                except ValueError:
                    pass

        reviews_count: int | None = None
        for sel in ["a[href*='#customerReviews'] span", ".s-underline-text"]:
            el = card.locator(sel).first
            if el.count() > 0:
                txt = el.inner_text().strip()
                reviews_count = self._parsear_reviews_count(txt)
                if reviews_count is not None:
                    break

        prime = card.locator("i.a-icon-prime").count() > 0
        sponsored = (
            card.locator(
                "[aria-label*='Sponsored'], [aria-label*='Patrocinado']"
            ).count()
            > 0
        )

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
            sku=asin,
            extra={
                "asin": asin,
                "rating": rating,
                "reviews_count": reviews_count,
                "prime": prime,
                "sponsored": sponsored,
                "badge": badge_texto,
            },
        )

    @staticmethod
    def _inferir_marca(nombre: str) -> str | None:
        if not nombre:
            return None
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
        if not texto:
            return None
        t = texto.strip("()").strip()
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

    def _scroll_ligero(self, page) -> None:
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

    def _guardar_debug(self, page, query: str, sufijo: str = "DEBUG") -> None:
        Path("debug").mkdir(exist_ok=True)
        safe = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
        p = Path(f"debug/amazon_{sufijo}_{safe}.html")
        p.write_text(page.content(), encoding="utf-8")
        print(f"  [amazon] HTML guardado en {p}")
