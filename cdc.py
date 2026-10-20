"""Scraper para el catálogo de CDC Internacional (Costa Rica)."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus
from bs4 import BeautifulSoup

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class CDCScraper(ScraperBase):
    """Busca artículos e inspecciona el detalle técnico en CDC Internacional."""

    codigo = "cdc"
    nombre = "CDCScraper"
    url_base = "https://cdcinternacional.com"
    requiere_login = True

    SEL_LOGIN_INFO = "#nav_login_info"
    SEL_LOGIN_LINK = "[onclick*='login.php']"
    SEL_LOGIN_USER = "#login_userid"
    SEL_LOGIN_PASSWORD = "#login_passwd"
    SEL_LOGIN_SUBMIT = "#frm_login button[type='submit']"
    SEL_SEARCH_INPUT = "#ed"
    SEL_SEARCH_SUBMIT = "#catalogo_submit"
    SEL_CARD = "article.itemex-plus-card"

    def __init__(self, browser) -> None:
        super().__init__(browser)
        self._login_error: Exception | None = None
        self._autenticado: bool = False

    def _login(self) -> None:
        """Autentica contra CDC si las credenciales están configuradas en .env."""
        usuario = os.getenv("CDC_USER", "").strip()
        password = os.getenv("CDC_PASSWORD", "")
        if not usuario or not password:
            print("  [cdc] CDC_USER / CDC_PASSWORD no configurados en .env. Continuando en modo consulta pública.")
            self._autenticado = False
            return

        try:
            self._iniciar_sesion(usuario, password)
            self._autenticado = True
            print("  [cdc] Sesión iniciada con éxito.")
        except Exception as error:
            self._login_error = error
            self._autenticado = False
            print(f"  [cdc] Advertencia al iniciar sesión: {error}")

    def _iniciar_sesion(self, usuario: str, password: str) -> None:
        page = self._page
        assert page is not None

        page.goto(self.url_base, wait_until="domcontentloaded", timeout=config.scraper_timeout_ms)
        page.locator(self.SEL_LOGIN_INFO).click()
        page.locator(self.SEL_LOGIN_LINK).click()
        page.wait_for_selector(self.SEL_LOGIN_USER, timeout=config.scraper_timeout_ms)

        page.fill(self.SEL_LOGIN_USER, usuario)
        page.fill(self.SEL_LOGIN_PASSWORD, password)
        page.locator(self.SEL_LOGIN_SUBMIT).click()

        # Esperar que la barra de navegación confirme el usuario autenticado
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

        texto = query.strip()
        if not texto:
            return []

        print(f"  [cdc] Buscando: '{texto}'")
        if not page.url.startswith(self.url_base):
            page.goto(self.url_base, wait_until="domcontentloaded", timeout=config.scraper_timeout_ms)

        page.fill(self.SEL_SEARCH_INPUT, texto)
        page.locator(self.SEL_SEARCH_SUBMIT).click()

        # Esperar respuesta AJAX en #main_div (resultados o sin coincidencias)
        page.wait_for_function(
            """() => {
                const div = document.querySelector('#main_div');
                const text = div ? (div.innerText || '') : '';
                return /Encontrados\\s+\\d+\\s+artículos/i.test(text) ||
                       /Sin coincidencias|Ningún artículo/i.test(text);
            }""",
            timeout=config.scraper_timeout_ms,
        )

        cards = page.locator(self.SEL_CARD).all()
        total_a_procesar = min(len(cards), config.scraper_max_resultados)
        print(f"  [cdc] Listado obtenido ({len(cards)} artículos). Extrayendo detalle de {total_a_procesar} productos...")

        productos: list[ProductoEncontrado] = []
        for indice, card in enumerate(cards[:total_a_procesar]):
            try:
                prod = self._parsear_card_con_detalle(card)
                if prod:
                    productos.append(prod)
            except Exception as error:
                print(f"  [cdc] Error procesando card {indice}: {error}")

        return productos

    def _parsear_card_con_detalle(self, card) -> ProductoEncontrado | None:
        nombre_el = card.locator(".itemex-plus-title").first
        if nombre_el.count() == 0:
            return None
        nombre = nombre_el.inner_text().strip()
        if not nombre:
            return None

        # Datos básicos presentes en la card
        sku_card = self._texto(card, ".itemex-plus-code")
        if sku_card:
            sku_card = re.sub(r"^C[oó]d:\s*", "", sku_card, flags=re.IGNORECASE).strip() or None

        marca_card = self._texto(card, ".itemex-plus-badge-brand")

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
            else f"{self.url_base}/index.php?ed={quote_plus(nombre)}"
        )

        precio_card_texto = self._texto(card, ".itemex-plus-price-main")
        impuesto_card_texto = self._texto(card, ".itemex-plus-price-tax")

        # Inspección del detalle del producto
        detalle_data = self._obtener_detalle(item_id, url_detalle) if item_id else {}

        # Consolidación de campos
        sku = sku_card or detalle_data.get("sku")
        marca = marca_card or detalle_data.get("marca")
        categoria = detalle_data.get("categoria")
        specs = detalle_data.get("specs", {})
        stock_texto = detalle_data.get("stock_texto")
        disponible = detalle_data.get("disponible")
        impuesto_texto = detalle_data.get("impuesto_texto") or impuesto_card_texto

        # Precios
        precio_raw = detalle_data.get("precio_texto") or precio_card_texto
        if precio_raw and impuesto_texto and impuesto_texto.lower() not in precio_raw.lower():
            precio_raw = f"{precio_raw} {impuesto_texto}".strip()

        precio, moneda = self.limpiar_precio(precio_raw or "")
        precio_regular = detalle_data.get("precio_regular")
        tiene_descuento = detalle_data.get("tiene_descuento", False)
        descuento_porcentaje = detalle_data.get("descuento_porcentaje")

        # Regla de negocio: si el stock numérico es <= 0, disponible debe ser False
        if detalle_data.get("stock_numerico") is not None and detalle_data.get("stock_numerico") <= 0:
            disponible = False

        extra = {
            "item_id": item_id,
            "mpn": detalle_data.get("mpn"),
            "modelo": specs.get("Modelo"),
            "garantia": specs.get("Garantía") or specs.get("Garantia"),
            "cabys": specs.get("CABYS"),
            "barcode": specs.get("BarCode"),
            "medida": specs.get("Medida"),
            "dimensiones": specs.get("Dimensiones"),
            "peso": specs.get("Peso"),
            "sucursales": detalle_data.get("sucursales", []),
            "impuesto_texto": impuesto_texto,
            "precio_requiere_login": True,
            "sesion_autenticada": self._autenticado,
        }

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=url_detalle,
            url_imagen=url_imagen,
            precio=precio,
            precio_regular=precio_regular,
            tiene_descuento=tiene_descuento,
            descuento_porcentaje=descuento_porcentaje,
            moneda=moneda,
            precio_texto_original=precio_raw,
            stock_texto=stock_texto,
            disponible=disponible,
            sku=sku,
            marca=marca,
            categoria=categoria,
            descripcion_corta=detalle_data.get("descripcion_corta"),
            specs=specs,
            extra=extra,
        )

    def _obtener_detalle(self, item_id: str, url_detalle: str) -> dict:
        """Obtiene la ficha técnica del producto usando la sesión activa del navegador."""
        assert self._context is not None
        try:
            resp = self._context.request.get(url_detalle)
            if resp.status != 200:
                return {}
            html = resp.text()
            soup = BeautifulSoup(html, "html.parser")
            return self._parsear_html_detalle(soup)
        except Exception as error:
            print(f"  [cdc] Error obteniendo detalle item_id={item_id}: {error}")
            return {}

    def _parsear_html_detalle(self, soup: BeautifulSoup) -> dict:
        data: dict = {
            "specs": {},
            "sucursales": [],
            "stock_numerico": None,
            "disponible": None,
            "stock_texto": None,
        }

        # 1. Categoría jerárquica desde breadcrumbs
        cat_links = [
            re.sub(r"\s+", " ", a.get_text(strip=True))
            for a in soup.find_all("a")
            if "item_explorar" in a.get("onclick", "")
        ]
        cat_links = [
            c for c in cat_links
            if c.lower() not in ("inicio", "seguir explorando") and not c.lower().startswith("seguir")
        ]
        if cat_links:
            data["categoria"] = " / ".join(cat_links)

        # 2. Especificaciones técnicas desde etiquetas <dl>
        specs: dict[str, str] = {}
        for dl in soup.find_all("dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                clave = dt.get_text(strip=True).rstrip(":")
                valor = dd.get_text(strip=True)
                if clave and valor and not clave.startswith("..."):
                    specs[clave] = valor
        data["specs"] = specs

        if "Marca" in specs:
            data["marca"] = specs["Marca"]
        if "Código" in specs:
            data["sku"] = specs["Código"]
        elif "Codigo" in specs:
            data["sku"] = specs["Codigo"]

        mpn = specs.get("# Parte") or specs.get("Parte") or specs.get("Modelo")
        if mpn:
            data["mpn"] = mpn

        if "I.V.A" in specs:
            data["impuesto_texto"] = specs["I.V.A"]

        # 3. Stock y disponibilidad
        panel_el = soup.find(class_="itemshop-plus-price-panel")
        panel_texto = panel_el.get_text(" ", strip=True) if panel_el else ""

        footer_el = soup.find(class_="itemshop-plus-stock-footer")
        sucursales = []
        if footer_el:
            sucursales = [
                btn.get_text(strip=True)
                for btn in footer_el.find_all("button")
                if btn.get_text(strip=True)
            ]
            data["sucursales"] = sucursales

        partes_stock = []
        if panel_texto and "es necesario iniciar" not in panel_texto.lower():
            partes_stock.append(panel_texto)
        if sucursales:
            partes_stock.append(f"Sucursales: {', '.join(sucursales)}")

        stock_completo = " | ".join(partes_stock) if partes_stock else None
        data["stock_texto"] = stock_completo

        # Interpretación de stock numérico y booleano
        if stock_completo:
            m = re.search(r"(\d+)\s*(?:en existencias?|uds?\.?|unidades?|disponibles?)", stock_completo, re.IGNORECASE)
            if m:
                stock_num = int(m.group(1))
                data["stock_numerico"] = stock_num
                data["disponible"] = stock_num > 0

            if re.search(r"\b(agotado|sin existencias?|no disponible|fuera de stock)\b", stock_completo, re.IGNORECASE):
                data["disponible"] = False
            elif data["disponible"] is None and re.search(r"\b(disponible|en existencias?|en stock|bodega)\b", stock_completo, re.IGNORECASE):
                data["disponible"] = True

        # 4. Precios (cuando la sesión esté autenticada)
        h4_el = soup.select_one(".itemshop-plus-price-panel h4")
        if h4_el:
            strong_el = h4_el.find("strong")
            small_el = h4_el.find("small")
            data["precio_texto"] = strong_el.get_text(strip=True) if strong_el else h4_el.get_text(strip=True)
            if small_el:
                data["impuesto_texto"] = small_el.get_text(strip=True)
        else:
            precio_el = soup.select_one(".price-main, .itemshop-plus-price-panel .price")
            if precio_el:
                data["precio_texto"] = precio_el.get_text(strip=True)

        return data

    @classmethod
    def limpiar_precio(cls, texto: str) -> tuple[float | None, str | None]:
        """Limpia el precio y elimina sufijos fiscales (+ I.V.A) para que las siglas no interfieran con los decimales."""
        if not texto:
            return None, None
        texto_limpio = re.sub(r"(?:\+|\b)i\.?v\.?a\.?", "", texto, flags=re.IGNORECASE).strip()
        return super().limpiar_precio(texto_limpio)

    @staticmethod
    def _texto(card, selector: str) -> str | None:
        el = card.locator(selector).first
        return el.inner_text().strip() if el.count() else None

    @staticmethod
    def _extraer_item_id(onclick: str) -> str | None:
        coincidencia = re.search(r"item_id=(\d+)", onclick)
        return coincidencia.group(1) if coincidencia else None
