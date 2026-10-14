"""Scraper del WebStore de Intcomex Costa Rica."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class IntcomexScraper(ScraperBase):
    """Busca productos en el catálogo de Intcomex Costa Rica.

    El catálogo público muestra nombre, marca, SKU, MPN e imagen. Intcomex
    oculta el precio y la disponibilidad detrás del inicio de sesión.
    """

    codigo = "intcomex"
    nombre = "Intcomex Costa Rica"
    url_base = "https://myservices.intcomex.com/es/XCR"
    requiere_login = True

    SEL_CARD = ".ecommproductcard"
    SEL_PRODUCT_LINK = ".ecommproductcard__linkincard"
    SEL_TITLE = ".ecommproductcard__title"
    SEL_BRAND = ".ecommproductcard__brand"
    SEL_META = ".ecommmetaitem"
    SEL_META_LABEL = ".ecommmetaitem__label"
    SEL_META_VALUE = ".ecommtchipcommerce__text"
    SEL_IMAGE = ".ecommproductcard__imagecontainer img"
    SEL_TAGS = ".ecommproductcard__containertags"
    SEL_LISTING = ".productlisting__contentcontainer"

    def __init__(self, browser) -> None:
        super().__init__(browser)
        self._login_error: Exception | None = None
        self._autenticado = False
        self._aviso_login_catalogo = False

    def _login(self) -> None:
        """Inicia sesión solo si se configuraron credenciales en el entorno.

        Sin credenciales, el scraper sigue usando el catálogo público. Si el
        portal pide un paso adicional como MFA, el error se informa al ejecutar
        el scraper para que no interrumpa la creación del contexto compartido.
        """
        usuario = os.getenv("INTCOMEX_USER", "").strip()
        password = os.getenv("INTCOMEX_PASSWORD", "")

        if not usuario and not password:
            print(
                "  [intcomex] Sin credenciales: se usará el catálogo público; "
                "el precio y la disponibilidad pueden no estar visibles."
            )
            return
        if not usuario or not password:
            self._login_error = RuntimeError(
                "Configura ambas variables INTCOMEX_USER e INTCOMEX_PASSWORD"
            )
            return

        page = self._page
        assert page is not None

        try:
            page.goto(
                f"{self.url_base}/store/",
                wait_until="domcontentloaded",
                timeout=config.scraper_timeout_ms,
            )
            page.get_by_role(
                "button", name=re.compile(r"^INICIAR SESIÓN$", re.IGNORECASE)
            ).first.click(timeout=config.scraper_timeout_ms)
            page.wait_for_selector("#email", timeout=config.scraper_timeout_ms)
            page.fill("#email", usuario)
            page.fill("#password", password)
            page.click("#next", timeout=config.scraper_timeout_ms)

            # Azure AD B2C devuelve al portal cuando finaliza la autenticación.
            page.wait_for_function(
                "() => location.hostname === 'myservices.intcomex.com'",
                timeout=config.scraper_timeout_ms,
            )
            page.wait_for_load_state(
                "domcontentloaded", timeout=config.scraper_timeout_ms
            )

            # El portal puede regresar al dominio del WebStore aunque el
            # formulario no haya autenticado la cuenta. La sesión se confirma
            # más adelante con los controles que aparecen en las tarjetas.
            print("  [intcomex] Formulario enviado; verificaré acceso a precios")
        except Exception as error:
            mensaje = str(error)
            if "b2clogin.com" in page.url:
                texto_pagina = page.locator("body").inner_text().lower()
                if re.search(r"código|verificaci[oó]n|sms|teléfono", texto_pagina):
                    mensaje = (
                        "Intcomex solicita una verificación adicional (MFA/OTP) "
                        "que este scraper no puede completar automáticamente"
                    )
                else:
                    mensaje = "Intcomex no completó el inicio de sesión"
            self._login_error = RuntimeError(mensaje)

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        if self._login_error:
            raise RuntimeError(
                f"No fue posible iniciar sesión en Intcomex: {self._login_error}"
            ) from self._login_error

        texto = query.strip()
        if not texto:
            return []

        url = (
            f"{self.url_base}/store/search/?by=term&value={quote_plus(texto)}"
        )
        print(f"  [intcomex] Buscando: {texto}")
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=config.scraper_timeout_ms,
        )

        # La página monta primero el contenedor y luego carga las tarjetas por
        # JavaScript. Esperar solo por el contenedor puede producir un falso 0.
        try:
            page.locator(self.SEL_LISTING).wait_for(
                state="visible", timeout=config.scraper_timeout_ms
            )
            page.wait_for_function(
                """({cardSelector}) => {
                    const hayTarjetas = document.querySelector(cardSelector);
                    const texto = document.body?.innerText || '';
                    const sinResultados = /0\\s+resultados/i.test(texto);
                    return Boolean(hayTarjetas) || sinResultados;
                }""",
                arg={"cardSelector": self.SEL_CARD},
                timeout=config.scraper_timeout_ms,
            )
        except Exception as error:
            cuerpo = page.locator("body").inner_text()
            if re.search(r"0\s+resultados", cuerpo, re.IGNORECASE):
                return []
            if not page.locator(self.SEL_CARD).count():
                raise RuntimeError(
                    "Intcomex mostró el listado, pero no cargó tarjetas de "
                    f"producto. URL actual: {page.url}. "
                    f"Texto visible: {cuerpo[:500]!r}"
                ) from error
            raise

        max_resultados = max(0, config.scraper_max_resultados)
        productos: list[ProductoEncontrado] = []
        paginas_vistas = 0

        while len(productos) < max_resultados:
            cards = page.locator(self.SEL_CARD).all()
            paginas_vistas += 1
            print(
                f"  [intcomex] Página {paginas_vistas}: "
                f"{len(cards)} tarjetas de producto"
            )

            if not self._autenticado and not self._aviso_login_catalogo:
                hay_login = any(
                    card.get_by_role(
                        "button",
                        name=re.compile(r"^INICIAR SESIÓN$", re.IGNORECASE),
                    ).count()
                    for card in cards
                )
                if hay_login:
                    print(
                        "  [intcomex] El catálogo aún solicita iniciar sesión; "
                        "no hay precio visible para esta cuenta/sesión."
                    )
                    self._aviso_login_catalogo = True

            for indice, card in enumerate(cards):
                if len(productos) >= max_resultados:
                    break
                try:
                    producto = self._parsear_card(card)
                    if producto:
                        productos.append(producto)
                except Exception as error:
                    print(f"  [intcomex] Error en tarjeta {indice}: {error}")

            if len(productos) >= max_resultados:
                break

            siguiente = page.get_by_role(
                "button", name=re.compile(r"^Siguiente$", re.IGNORECASE)
            )
            if not siguiente.count() or not siguiente.is_enabled():
                break

            primer_href = page.locator(self.SEL_PRODUCT_LINK).first.get_attribute(
                "href"
            )
            if not primer_href:
                break

            siguiente.click(timeout=config.scraper_timeout_ms)
            try:
                page.wait_for_function(
                    """href => {
                        const link = document.querySelector(
                            '.ecommproductcard__linkincard'
                        );
                        return link && link.getAttribute('href') !== href;
                    }""",
                    arg=primer_href,
                    timeout=config.scraper_timeout_ms,
                )
            except Exception:
                break

        return productos

    def _parsear_card(self, card) -> ProductoEncontrado | None:
        titulo_el = card.locator(self.SEL_TITLE).first
        enlace_el = card.locator(self.SEL_PRODUCT_LINK).first
        if not titulo_el.count() or not enlace_el.count():
            return None

        nombre = titulo_el.inner_text().strip()
        href = enlace_el.get_attribute("href") or ""
        if not nombre or not href:
            return None

        marca_el = card.locator(self.SEL_BRAND).first
        marca = marca_el.inner_text().strip() if marca_el.count() else None

        sku: str | None = None
        mpn: str | None = None
        for meta in card.locator(self.SEL_META).all():
            etiqueta_el = meta.locator(self.SEL_META_LABEL).first
            valor_el = meta.locator(self.SEL_META_VALUE).first
            if not etiqueta_el.count() or not valor_el.count():
                continue
            etiqueta = etiqueta_el.inner_text().strip().casefold()
            valor = valor_el.inner_text().strip() or None
            if etiqueta == "sku":
                sku = valor
            elif etiqueta == "mpn":
                mpn = valor

        imagen_el = card.locator(self.SEL_IMAGE).first
        imagen = imagen_el.get_attribute("src") if imagen_el.count() else None
        url_imagen = self.url_absoluta(imagen, self.url_base) if imagen else None

        tags_el = card.locator(self.SEL_TAGS).first
        etiquetas = (
            [line.strip() for line in tags_el.inner_text().splitlines() if line.strip()]
            if tags_el.count()
            else []
        )

        boton_login = card.get_by_role(
            "button", name=re.compile(r"^INICIAR SESIÓN$", re.IGNORECASE)
        )
        requiere_login = boton_login.count() > 0
        if not requiere_login:
            self._autenticado = True

        precio_texto = self._extraer_precio(card)
        precio, moneda = self.limpiar_precio(precio_texto or "")
        stock_texto = self._extraer_stock(card)
        disponible = self._interpretar_disponibilidad(stock_texto)
        categoria = self._detectar_categoria(nombre)
        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=self.url_absoluta(href, self.url_base),
            url_imagen=url_imagen,
            precio=precio,
            moneda=moneda,
            precio_texto_original=precio_texto,
            stock_texto=stock_texto,
            disponible=disponible,
            sku=sku,
            marca=marca,
            categoria=categoria,
            extra={
                "mpn": mpn,
                "etiquetas": etiquetas,
                "precio_requiere_login": True,
                "sesion_autenticada": not requiere_login,
            },
        )

    @staticmethod
    def _extraer_stock(card) -> str | None:
        """Lee la disponibilidad del texto de la tarjeta, sin abrir el producto."""
        texto = card.inner_text()
        for linea in texto.splitlines():
            linea = " ".join(linea.split())
            if not linea:
                continue
            if re.search(
                r"\b(?:disponible|disponibles|stock|agotado|agotados)\b",
                linea,
                re.IGNORECASE,
            ):
                return linea
        return None

    @staticmethod
    def _interpretar_disponibilidad(stock_texto: str | None) -> bool | None:
        if not stock_texto:
            return None
        if re.search(
            r"\b(?:agotado|agotados|sin\s+stock|sin\s+existencias?|no\s+disponible)\b",
            stock_texto,
            re.IGNORECASE,
        ):
            return False

        # Si incluye cantidad numérica (ej: "0 uds. disponibles", "11 uds. disponibles")
        match_numero = re.search(r"(-?\d+)", stock_texto)
        if match_numero:
            return int(match_numero.group(1)) > 0

        if re.search(
            r"\b(?:disponible|disponibles|en\s+stock|existencias?|en\s+existencia)\b",
            stock_texto,
            re.IGNORECASE,
        ):
            return True
        return None

    @staticmethod
    def _extraer_precio(card) -> str | None:
        """Busca un importe visible sin confundirlo con SKU o MPN."""
        patron = re.compile(
            r"(?:US\$|USD|CRC|₡|\$)\s*\d[\d.,]*"
            r"|\d[\d.,]*\s*(?:USD|CRC)",
            re.IGNORECASE,
        )

        candidatos: list[str] = []
        for elemento in card.locator(
            '[class*="price"], [class*="Price"], '
            '[class*="currency"], [class*="Currency"]'
        ).all():
            texto = elemento.inner_text().strip()
            if texto:
                candidatos.append(texto)

        # Algunas versiones del WebStore no incluyen "price" en la clase CSS.
        candidatos.append(card.inner_text())
        for texto in candidatos:
            coincidencia = patron.search(texto)
            if coincidencia:
                return coincidencia.group(0).strip()
        return None

    @staticmethod
    def _detectar_categoria(nombre: str) -> str | None:
        """Detecta categoría en base a términos clave en el nombre."""
        reglas = [
            (r"\b(?:aud[ií]fonos|auriculares|headset|earphones|headphone)\b", "Audífonos"),
            (r"\b(?:teclado|keyboard)\b", "Teclados"),
            (r"\b(?:mouse|rat[oó]n)\b", "Mouse"),
            (r"\b(?:laptop|port[aá]til|notebook)\b", "Laptops"),
            (r"\b(?:monitor|pantalla)\b", "Monitores"),
            (r"\b(?:impresora|multifuncional|printer)\b", "Impresoras"),
            (r"\b(?:disco|ssd|hdd|memoria|ram)\b", "Almacenamiento"),
            (r"\b(?:cable|adaptador|hub|dock)\b", "Cables y Conectividad"),
        ]
        for patron, categoria in reglas:
            if re.search(patron, nombre, re.IGNORECASE):
                return categoria
        return None

