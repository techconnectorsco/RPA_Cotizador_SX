"""Scraper autenticado del catálogo de Eurocomp Costa Rica."""

from __future__ import annotations

import os
import random
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
    PAUSA_MIN_MS = 800
    PAUSA_MAX_MS = 1600

    # Cuánto esperamos a que el catálogo AJAX conteste algo — productos o un
    # aviso de que no hay nada. Es MUCHO menor que scraper_timeout_ms (30 s) a
    # propósito: una búsqueda que no encuentra nada tiene que costar segundos,
    # no medio minuto. Antes, cuando Eurocomp no hallaba el producto, el
    # scraper agotaba los 30 s y reventaba con TimeoutError, y en el log se
    # veía como si el proveedor estuviera caído.
    ESPERA_RESULTADOS_MS = 12000

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
        page.locator('[onclick*="login.php"]').click()
        page.wait_for_selector(self.SEL_LOGIN_USER, timeout=config.scraper_timeout_ms)

        page.fill(self.SEL_LOGIN_USER, usuario)
        page.fill(self.SEL_LOGIN_PASSWORD, password)
        self._esperar_antes_de_enviar()
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
            raise RuntimeError(
                "No fue posible iniciar sesión en Eurocomp"
            ) from self._login_error

        texto = query.strip()
        if not texto:
            return []

        print(f"  [eurocomp] Buscando: {texto}")
        page.fill(self.SEL_SEARCH_INPUT, texto)
        self._esperar_antes_de_enviar()
        page.locator(self.SEL_SEARCH_SUBMIT).click()

        if not self._esperar_respuesta(page):
            # Ninguna señal llegó en el plazo. Puede ser lentitud puntual o un
            # cambio en el sitio. No lanzamos: devolver vacío deja la búsqueda
            # de este proveedor como "sin resultados" en lugar de como un fallo,
            # y el resto de los proveedores sigue su curso.
            print(
                "  [eurocomp] El catálogo no respondió en "
                f"{self.ESPERA_RESULTADOS_MS // 1000}s; lo tomo como búsqueda sin coincidencias"
            )
            return []

        total_sitio = self._total_reportado(page)
        url_plantilla, ultima_pagina = self._info_paginacion(page)
        if total_sitio is not None:
            print(
                f"  [eurocomp] El sitio reporta {total_sitio} artículos "
                f"en {ultima_pagina} página(s)"
            )

        productos: list[ProductoEncontrado] = []
        vistos: set[str] = set()
        pagina = 1

        while True:
            if self._leer_pagina(page, pagina, productos, vistos) == 0:
                break
            if len(productos) >= config.scraper_max_resultados:
                break
            if pagina >= ultima_pagina or not url_plantilla:
                break
            pagina += 1
            if not self._ir_a_pagina(page, url_plantilla, pagina):
                print(f"  [eurocomp] Conservo los {len(productos)} ya leídos")
                break

        if not productos:
            print("  [eurocomp] Sin coincidencias para esta búsqueda")
            return []

        print(f"  [eurocomp] Productos obtenidos: {len(productos)}")
        return productos

    def _leer_pagina(
        self,
        page,
        pagina: int,
        productos: list[ProductoEncontrado],
        vistos: set[str],
    ) -> int:
        """Parsea las tarjetas visibles y acumula las que no se habían leído.

        Devuelve cuántas tarjetas había en la página (0 corta el recorrido).
        """
        cards = page.locator(self.SEL_CARD).all()
        if not cards:
            return 0

        nuevos = 0
        for indice, card in enumerate(cards):
            if len(productos) >= config.scraper_max_resultados:
                break
            try:
                producto = self._parsear_card(card)
            except Exception as error:
                print(f"  [eurocomp] Error en card {indice}: {error}")
                continue
            if producto is None:
                continue
            clave = producto.sku or producto.url_detalle or producto.nombre
            if clave in vistos:
                continue
            productos.append(producto)
            vistos.add(clave)
            nuevos += 1

        print(
            f"  [eurocomp] Página {pagina}: {len(cards)} tarjetas, "
            f"{nuevos} nuevos (acumulado {len(productos)})"
        )
        return len(cards)

    @staticmethod
    def _info_paginacion(page) -> tuple[str | None, int]:
        """Lee de la barra de paginación la URL AJAX y cuántas páginas hay.

        Eurocomp pagina llamando LINK_ajax('item_explorar.php?...&page=N',
        'main_div') desde el onclick de cada enlace. Tomamos una de esas URLs
        como plantilla y el mayor page=N (el de "Última") como tope.
        """
        try:
            datos = page.evaluate("""() => {
                    const enlaces = Array.from(document.querySelectorAll(
                        '.itemex-plus-pagination a.page-link[onclick]'
                    ));
                    let url = null;
                    let ultima = 1;
                    for (const a of enlaces) {
                        const onclick = a.getAttribute('onclick') || '';
                        const destino = onclick.match(/LINK_ajax\\('([^']+)'/);
                        if (destino && !url) url = destino[1];
                        const numero = onclick.match(/[?&]page=(\\d+)/);
                        if (numero) {
                            ultima = Math.max(ultima, parseInt(numero[1], 10));
                        }
                    }
                    return {url, ultima};
                }""")
        except Exception as error:
            print(f"  [eurocomp] No se pudo leer la paginación: {error}")
            return None, 1

        if not isinstance(datos, dict):
            return None, 1
        return datos.get("url"), max(1, int(datos.get("ultima") or 1))

    def _ir_a_pagina(self, page, url_plantilla: str, pagina: int) -> bool:
        """Carga una página del catálogo reutilizando el AJAX del propio sitio."""
        url = re.sub(r"([?&]page=)\d+", rf"\g<1>{pagina}", url_plantilla)
        if "page=" not in url:
            url = f"{url}&page={pagina}"

        try:
            page.evaluate("destino => LINK_ajax(destino, 'main_div')", url)
            page.wait_for_function(
                """({cardSelector, esperada}) => {
                    const activa = document.querySelector(
                        '.itemex-plus-pagination .page-item.active .page-link'
                    );
                    if (!activa) return false;
                    if (activa.textContent.trim() !== String(esperada)) return false;
                    return document.querySelectorAll(cardSelector).length > 0;
                }""",
                arg={"cardSelector": self.SEL_CARD, "esperada": pagina},
                timeout=self.ESPERA_RESULTADOS_MS,
            )
            return True
        except Exception as error:
            print(f"  [eurocomp] La página {pagina} no cargó: {error}")
            return False

    def _esperar_respuesta(self, page) -> bool:
        """
        Espera a que el catálogo AJAX dé CUALQUIER señal de haber contestado:
        tarjetas de producto, el contador de artículos, o un aviso de que no
        hay coincidencias. Devuelve False si no llegó ninguna.

        La versión anterior esperaba un único patrón muy estricto
        (/Encontrados\\s+\\d+\\s+artículos\\./) que exigía la palabra exacta, un
        número, la tilde y el punto final. Cuando la búsqueda no encontraba
        nada y el sitio escribía otra cosa, la condición no se cumplía jamás y
        se perdían 30 segundos por búsqueda.
        """
        try:
            page.wait_for_function(
                """() => {
                    const main = document.querySelector('#main_div');
                    if (!main) return false;

                    // 1. Hay tarjetas de producto: respondió con resultados.
                    if (main.querySelector('article.itemex-plus-card')) return true;

                    const t = (main.innerText || '').toLowerCase();
                    if (!t.trim()) return false;

                    // 2. El contador, en cualquiera de sus formas
                    //    ("Encontrados 12 artículos", "encontrados 0 articulos").
                    if (/encontrad\\w*\\s+\\d+/.test(t)) return true;

                    // 3. Avisos de búsqueda vacía, en sus variantes habituales.
                    if (/no\\s+(se\\s+)?(encontr|hay|existe)/.test(t)) return true;
                    if (/sin\\s+(resultado|coincidencia|articulo|artículo)/.test(t)) return true;
                    if (/0\\s+(resultado|coincidencia|articulo|artículo)/.test(t)) return true;

                    return false;
                }""",
                timeout=self.ESPERA_RESULTADOS_MS,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _total_reportado(page) -> int | None:
        """Cuántos artículos dice el sitio que encontró, si lo informa."""
        try:
            texto = page.locator("#main_div").inner_text(timeout=2000)
        except Exception:
            return None
        coincidencia = re.search(r"encontrad\w*\s+(\d+)", texto, re.IGNORECASE)
        return int(coincidencia.group(1)) if coincidencia else None

    def _esperar_antes_de_enviar(self) -> None:
        """Introduce una pausa breve y variable antes de una acción remota."""
        page = self._page
        assert page is not None
        page.wait_for_timeout(random.randint(self.PAUSA_MIN_MS, self.PAUSA_MAX_MS))

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
            card.locator(
                ".itemex-plus-media, .itemex-plus-btn-cart"
            ).first.get_attribute("onclick")
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
