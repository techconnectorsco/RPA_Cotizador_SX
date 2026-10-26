"""Scraper del catálogo de Dataformas Costa Rica"""

from __future__ import annotations

import os
import random
import re
from urllib.parse import quote_plus

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class DataformasScraper(ScraperBase):
    """Busca productos en el catálogo de Dataformas interactuando con el FrontEnd."""

    codigo = "dataformas"
    nombre = "Dataformas"
    url_base = "https://dataformas.com"
    requiere_login = True
    usa_browser_compartido = True

    SEL_LOGIN_USER = "input[type='email'], input[name='email']"
    SEL_LOGIN_PASSWORD = "input[type='password'], input[name='password']"
    SEL_LOGIN_SUBMIT = "button.Login_submit-btn"
    SEL_CARD = ".PLP_item"
    SEL_TITLE = "a.PLP_product-title, h2 a, h2"
    SEL_IMAGE = ".PLP_thumbnail img, .PLP_product-img-wrapper img, img"

    def __init__(self, browser) -> None:
        super().__init__(browser)
        self._login_error: Exception | None = None
        self._autenticado = False

    def _esperar_antes_de_enviar_login(self) -> None:
        # Aquí se maneja el delay después de llenar las credenciales y antes de enviarlas.
        demora_ms = random.randint(800, 1600)
        assert self._page is not None
        self._page.wait_for_timeout(demora_ms)

    def _esperar_catalogo_cargado(self) -> None:
        # Aquí se maneja el delay después de cargar el catálogo y antes de extraer tarjetas.
        demora_ms = random.randint(1500, 3000)
        assert self._page is not None
        self._page.wait_for_timeout(demora_ms)

    def _login(self) -> None:
        """Inicia sesión usando credenciales del entorno (.env)."""
        usuario = (os.getenv("DATAFORMAS_USER") or "").strip()
        password = (os.getenv("DATAFORMAS_PASSWORD") or "").strip()

        if not usuario and not password:
            print("  [dataformas] Sin credenciales configuradas en .env")
            return
        if not usuario or not password:
            self._login_error = RuntimeError(
                "Configura ambas variables DATAFORMAS_USER y DATAFORMAS_PASSWORD en el .env"
            )
            return

        page = self._page
        assert page is not None
        try:
            page.set_default_timeout(config.scraper_timeout_ms)
            page.goto(
                f"{self.url_base}/profile/login",
                wait_until="networkidle",
                timeout=config.scraper_timeout_ms,
            )
            page.wait_for_selector(self.SEL_LOGIN_USER, timeout=config.scraper_timeout_ms)
            page.locator(self.SEL_LOGIN_USER).first.fill(usuario)
            page.locator(self.SEL_LOGIN_PASSWORD).first.fill(password)
            self._esperar_antes_de_enviar_login()
            page.locator(self.SEL_LOGIN_SUBMIT).first.click(
                timeout=config.scraper_timeout_ms
            )

            # Esperar a que la navegación confirme la salida de /profile/login
            page.wait_for_function(
                "() => !location.pathname.includes('/profile/login')",
                timeout=config.scraper_timeout_ms,
            )
            self._autenticado = True
            print("  [dataformas] Sesión iniciada correctamente")
        except Exception as error:
            self._login_error = RuntimeError(
                f"No fue posible iniciar sesión en Dataformas: {error}"
            )

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        if self._login_error:
            raise RuntimeError(str(self._login_error)) from self._login_error

        texto = " ".join(query.split())
        if not texto:
            return []

        max_resultados = max(0, config.scraper_max_resultados)
        if max_resultados == 0:
            return []

        url = f"{self.url_base}/search?q={quote_plus(texto)}"
        print(f"  [dataformas] Buscando: {texto}")
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=config.scraper_timeout_ms,
        )
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass

        # Esperar a que las tarjetas aparezcan en el DOM o confirme cero resultados
        try:
            page.wait_for_function(
                """({cardSelector}) => {
                    const cards = Array.from(document.querySelectorAll(cardSelector));
                    if (cards.length > 0) return true;
                    const body = document.body?.innerText || '';
                    return /0\\s+productos|no\\s+se\\s+encontraron|sin\\s+resultados/i.test(body);
                }""",
                arg={"cardSelector": self.SEL_CARD},
                timeout=config.scraper_timeout_ms,
            )
        except Exception as error:
            cuerpo = page.locator("body").inner_text()
            if re.search(r"0\s+productos|sin\s+resultados", cuerpo, re.IGNORECASE):
                return []
            if not page.locator(self.SEL_CARD).count():
                raise RuntimeError(
                    f"Dataformas no cargó tarjetas para '{texto}'. URL: {page.url}"
                ) from error

        # Esperar a que las tarjetas carguen completamente sus datos (precios y disponibilidad)
        try:
            page.wait_for_function(
                """({cardSelector}) => {
                    const cards = Array.from(document.querySelectorAll(cardSelector));
                    if (cards.length === 0) return false;
                    const listos = cards.filter(card => {
                        const txt = card.innerText || '';
                        return /(?:₡|¢|CRC|\\$|USD)\\s*\\d+/i.test(txt) || /existencia|stock|agotado/i.test(txt);
                    });
                    // Esperar a que al menos el 80% de las tarjetas (o todas si son pocas) tengan datos
                    const meta = Math.max(1, Math.min(cards.length, Math.floor(cards.length * 0.8)));
                    return listos.length >= meta;
                }""",
                arg={"cardSelector": self.SEL_CARD},
                timeout=min(config.scraper_timeout_ms, 12000),
            )
        except Exception:
            pass

        cards = page.locator(self.SEL_CARD).all()
        print(f"  [dataformas] Tarjetas encontradas en FrontEnd: {len(cards)}")

        # Desplazamiento progresivo para hidratar las tarjetas y sus imágenes/precios
        for card in cards[:max_resultados]:
            try:
                card.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
        # Re-obtener las tarjetas tras el scroll para asegurar el estado hidratado
        cards = page.locator(self.SEL_CARD).all()
        self._esperar_catalogo_cargado()

        productos: list[ProductoEncontrado] = []
        vistos: set[str] = set()

        for indice, card in enumerate(cards):
            if len(productos) >= max_resultados:
                break
            try:
                producto = self._parsear_card(card)
                if producto and producto.url_detalle not in vistos:
                    productos.append(producto)
                    vistos.add(producto.url_detalle)
            except Exception as error:
                print(f"  [dataformas] Error en tarjeta {indice}: {error}")

        precios_encontrados = sum(
            producto.precio is not None for producto in productos
        )

        print(f"  [dataformas] Total productos procesados: {len(productos)}")
        print(
            f"  [dataformas] Precios detectados: "
            f"{precios_encontrados}/{len(productos)}"
        )
        return productos

    def _parsear_card(self, card) -> ProductoEncontrado | None:
        texto_card = card.inner_text().strip()

        # 1. Nombre y Enlace del producto (evitar el texto temporal "Cargando")
        nombre = ""
        href = ""

        # Selector principal de título
        titulo_el = card.locator(self.SEL_TITLE).first
        if titulo_el.count():
            txt = titulo_el.inner_text().strip()
            if txt and not self._es_placeholder(txt):
                nombre = txt
                href = titulo_el.get_attribute("href") or ""

        # Si aún no tenemos enlace o nombre válido, buscar entre enlaces de la tarjeta
        if not nombre or not href:
            for a in card.locator("a[href]").all():
                t = a.inner_text().strip()
                h = a.get_attribute("href") or ""
                if h and not h.startswith("#"):
                    if not nombre and t and not self._es_placeholder(t):
                        nombre = t
                    if not href and h:
                        href = h
                if nombre and href:
                    break

        # Fallback al atributo alt o title de la imagen si el nombre sigue vacío
        if not nombre or self._es_placeholder(nombre):
            img = card.locator("img").first
            if img.count():
                alt = (img.get_attribute("alt") or img.get_attribute("title") or "").strip()
                if alt and not self._es_placeholder(alt):
                    nombre = alt

        # Fallback para href
        if not href:
            a_first = card.locator("a[href]").first
            if a_first.count():
                href = a_first.get_attribute("href") or ""

        if not nombre or not href or self._es_placeholder(nombre):
            return None

        url_detalle = self.url_absoluta(href, self.url_base)

        # 2. SKU
        sku = self._extraer_sku(texto_card)

        # 3. Imagen (con soporte para lazy loading, data-src, srcset y currentSrc)
        url_imagen = self._extraer_imagen(card)

        # 4. Precio y moneda (siguiendo la lógica de candidatos y regex de intcomex.py)
        precio_texto = self._extraer_precio(card)
        if not precio_texto:
            self._esperar_elemento(card, "text=/(?:₡|¢|CRC|USD|\\$)/i", timeout_ms=1200)
            precio_texto = self._extraer_precio(card)

        precio, moneda = self.limpiar_precio(
            self._normalizar_texto(precio_texto or "").replace("¢", "₡")
        )
        if precio is not None and not moneda:
            moneda = "CRC"
        if precio is not None and not precio_texto:
            precio_texto = f"{moneda} {precio:,.2f}"

        # 5. Stock y disponibilidad
        stock_texto = self._extraer_stock(card)
        if not stock_texto:
            self._esperar_elemento(card, "text=/existencia|stock|agotado/i", timeout_ms=800)
            stock_texto = self._extraer_stock(card)

        disponible = self._interpretar_disponibilidad(stock_texto)
        if disponible is None:
            # Si hay botón de compra o caja de cantidad visible, el producto tiene stock
            if card.locator("input[type='number'], button.Button_action, button:has-text('Agregar'), button:has-text('Comprar')").count() > 0:
                disponible = True
                stock_texto = stock_texto or "Disponible"

        # 6. Marca y Categoría detectadas
        marca = self._extraer_marca(nombre)
        categoria = self._detectar_categoria(nombre)

        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=url_detalle,
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
                "precio_requiere_login": True,
                "sesion_autenticada": self._autenticado,
            },
        )

    @staticmethod
    def _esperar_elemento(card, selector_texto: str, timeout_ms: int = 1000) -> None:
        """Espera brevemente a que un elemento aparezca en la tarjeta antes de extraer."""
        try:
            card.locator(selector_texto).first.wait_for(
                state="visible", timeout=timeout_ms
            )
        except Exception:
            pass

    def _extraer_imagen(self, card) -> str | None:
        """Extrae la URL de la imagen considerando lazy-load y srcset."""
        img_el = card.locator(self.SEL_IMAGE).first
        if not img_el.count():
            return None

        imagen: str | None = None
        for attr in ("src", "data-src", "srcset", "data-original"):
            val = img_el.get_attribute(attr)
            if val and not val.startswith("data:image"):
                if attr == "srcset":
                    val = val.split()[0].split(",")[0]
                imagen = val
                break

        if not imagen:
            try:
                csrc = img_el.evaluate("el => el.currentSrc || el.src")
                if csrc and not csrc.startswith("data:image"):
                    imagen = csrc
            except Exception:
                pass

        return self.url_absoluta(imagen, self.url_base) if imagen else None

    @staticmethod
    def _extraer_sku(texto: str) -> str | None:
        """Extrae el SKU a partir del texto visible de la tarjeta."""
        match = re.search(
            r"(?:N\.[ºo]\s*de\s*art[íi]culo|Art[íi]culo|SKU|C[óo]digo)\s*[:：]?\s*([A-Za-z0-9_-]+)",
            texto,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else None

    @classmethod
    def _extraer_precio(cls, card) -> str | None:
        """Extrae el precio de la tarjeta, priorizando los elementos de precio."""
        texto_tarjeta_original = card.inner_text() or ""
        texto_tarjeta = cls._normalizar_texto(texto_tarjeta_original)

        candidatos: list[str] = []
        elementos_precio = card.locator(cls._selector_precio()).all()

        for elemento in elementos_precio:
            texto = cls._normalizar_texto(elemento.inner_text() or "")
            if texto:
                candidatos.append(texto)
            for atributo in ("content", "data-price", "data-value", "value"):
                valor = elemento.get_attribute(atributo)
                if valor:
                    candidatos.append(valor.strip())
            try:
                padre = elemento.evaluate(
                    "el => el.parentElement ? el.parentElement.innerText : ''"
                )
                if padre:
                    candidatos.append(cls._normalizar_texto(padre))
            except Exception:
                pass

        candidatos.append(texto_tarjeta)

        # 1. Buscar coincidencia con símbolo o código de moneda
        for texto in candidatos:
            precio = cls._buscar_precio_en_texto(texto)
            if precio:
                return precio

        # 2. Buscar por líneas individuales en el texto completo de la tarjeta
        for linea in texto_tarjeta.splitlines():
            linea = " ".join(linea.split())
            precio = cls._buscar_precio_en_texto(linea)
            if precio:
                return precio

        return None

    @classmethod
    def _buscar_precio_en_texto(cls, texto: str) -> str | None:
        """Reconoce importes con miles separados por espacios, comas o puntos."""
        texto = cls._normalizar_texto(texto)
        numero = (
            r"(?:\d{1,3}(?:[\s.,]\d{3})+(?:[.,]\d{2})?"
            r"|\d+(?:[.,]\d{2})?)"
        )
        patron = re.compile(
            rf"(?:US\$|USD|CRC|₡|¢|\$)\s*{numero}"
            rf"(?:\s*(?:USD|CRC|colones))?"
            rf"|{numero}\s*(?:USD|CRC|colones)",
            re.IGNORECASE,
        )
        coincidencia = patron.search(texto)
        return " ".join(coincidencia.group(0).split()) if coincidencia else None

    @staticmethod
    def _normalizar_texto(texto: str) -> str:
        """Normaliza superíndices (⁰¹²...) y espacios no divisibles (\xa0)."""
        return texto.translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")).replace("\xa0", " ")

    @staticmethod
    def _selector_precio() -> str:
        """Selecciona contenedores de precio habituales y metadatos del catálogo."""
        return (
            '[class*="price"], [class*="Price"], '
            '[class*="precio"], [class*="Precio"], '
            '[class*="currency"], [class*="Currency"], '
            '[class*="amount"], [class*="Amount"], '
            '[itemprop="price"], meta[itemprop="price"], '
            '[data-price], [data-value]'
        )

    @classmethod
    def _extraer_stock(cls, card) -> str | None:
        """Lee la disponibilidad del texto o elementos de inventario de la tarjeta."""
        texto = cls._normalizar_texto(card.inner_text() or "")

        # 1. Buscar coincidencia directa inline: ej "5 En existencia", "11 En existencia", "1 En existencia"
        match_stock = re.search(
            r"(\d+)\s*(?:en\s+existencia|disponibles?|unidades?|en\s+stock)",
            texto,
            re.IGNORECASE,
        )
        if match_stock:
            return f"{match_stock.group(1)} En existencia"

        # 2. Buscar si está explícitamente agotado
        if re.search(
            r"\b(?:agotado|agotados|sin\s+existencias?|sin\s+stock|no\s+disponible)\b",
            texto,
            re.IGNORECASE,
        ):
            return "Agotado"

        # 3. Buscar en elementos con clases específicas de inventario/stock
        for el in card.locator(
            '[class*="stock"], [class*="Stock"], '
            '[class*="inventory"], [class*="Inventory"], '
            '[class*="availab"], [class*="Availab"]'
        ).all():
            t = cls._normalizar_texto(el.inner_text() or "").strip()
            if t:
                return t

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

        # Si tiene número de stock: ej "5 En existencia", "0 En existencia", "-1 disponibles"
        m = re.search(r"(-?\d+)", stock_texto)
        if m:
            return int(m.group(1)) > 0

        if re.search(
            r"\b(?:disponible|disponibles|en\s+stock|existencias?|en\s+existencia)\b",
            stock_texto,
            re.IGNORECASE,
        ):
            return True
        return None

    @staticmethod
    def _extraer_marca(nombre: str) -> str | None:
        """Detecta marcas comunes dentro del nombre del producto."""
        marcas = (
            "LOGITECH", "MICROSOFT", "HYPERX", "KINGSTON", "SAMSUNG",
            "LENOVO", "ASUS", "ACER", "APPLE", "XIAOMI", "HUAWEI",
            "TOSHIBA", "PHILIPS", "VIEWSONIC", "AOC", "DELL", "HP",
            "AMD", "INTEL", "NVIDIA", "RAZER", "CANON", "EPSON",
            "BROTHER", "JBL", "SONY", "LG", "MSI", "TP-LINK", "UGREEN",
            "TRUST", "TARGUS", "GENIUS", "KLIPX", "FORZA", "NEXXT",
        )
        titulo = nombre.upper()
        for marca in marcas:
            if re.search(rf"(?<![A-Z0-9]){re.escape(marca)}(?![A-Z0-9])", titulo):
                return marca.title()
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

    @staticmethod
    def _es_placeholder(texto: str | None) -> bool:
        if not texto:
            return True
        return bool(
            re.fullmatch(
                r"(?:cargando|loading|please wait|por favor espere)[. …]*",
                texto.strip(),
                re.IGNORECASE,
            )
        )
