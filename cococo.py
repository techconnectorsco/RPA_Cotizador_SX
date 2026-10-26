"""Scraper del catálogo público de Cococo"""

from __future__ import annotations

import os
import random
import re
from urllib.parse import urlencode

from config import config
from modelo import ProductoEncontrado
from scraper_base import ScraperBase


class CococoScraper(ScraperBase):
    """Busca productos en el catálogo de Cococo, con login opcional."""

    codigo = "cococo"
    nombre = "Cococo"
    url_base = "https://cococo.co.cr"
    # El catálogo público sigue disponible cuando no se configura una cuenta.
    requiere_login = True

    SEL_CARD = ".card-product"
    SEL_PRODUCT_LINK = "a.product-link"
    SEL_TITLE = "h3.product-name"
    SEL_PRICE = "p.product-price"
    SEL_IMAGE = ".image-wrapper img"
    SEL_TAGS = ".card-product-badges"
    SEL_NEXT_PAGE = "a.sp-page-link[aria-label='Go to page: {page}']"
    SEL_OVERLAY = ".overlay.zindex-10.overlay-lg-transparent.d-block"

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
        """Inicia sesión en la tienda usando credenciales del entorno."""
        usuario = os.getenv("COCOCO_USER", "").strip()
        password = os.getenv("COCOCO_PASSWORD", "").strip()
        if not usuario or not password:
            self._login_error = RuntimeError(
                "Configura COCOCO_USER y COCOCO_PASSWORD en el archivo .env"
            )
            return

        page = self._page
        assert page is not None
        try:
            page.set_default_timeout(config.scraper_timeout_ms)
            page.goto(
                f"{self.url_base}/login",
                wait_until="domcontentloaded",
                timeout=config.scraper_timeout_ms,
            )
            page.locator("input[type='email']").fill(usuario)
            page.locator("input[type='password']").fill(password)
            self._esperar_antes_de_enviar_login()
            page.get_by_role("button", name="Iniciar sesión").click(
                timeout=config.scraper_timeout_ms
            )
            page.wait_for_function(
                "() => !location.pathname.replace(/\\/$/, '').endsWith('/login')",
                timeout=config.scraper_timeout_ms,
            )
            self._autenticado = True
            print("  [cococo] Sesión iniciada correctamente")
        except Exception as error:
            self._login_error = RuntimeError(
                f"No fue posible iniciar sesión en Cococo: {error}"
            )

    def buscar(self, query: str) -> list[ProductoEncontrado]:
        page = self._page
        assert page is not None

        usuario = os.getenv("COCOCO_USER", "").strip()
        password = os.getenv("COCOCO_PASSWORD", "")
        if self._login_error:
            raise RuntimeError(str(self._login_error)) from self._login_error
        if self._autenticado:
            pass
        elif usuario and password:
            self._login()
        elif usuario or password:
            raise RuntimeError(
                "Configura ambas variables COCOCO_USER y COCOCO_PASSWORD en el .env"
            )
        elif self.requiere_login:
            raise RuntimeError(
                "El login de Cococo está habilitado. Agrega COCOCO_USER y "
                "COCOCO_PASSWORD al archivo .env"
            )
        else:
            print(
                "  [cococo] Sin credenciales: se usará el catálogo público; "
                "SKU o marca podrían no estar disponibles."
            )

        if self._login_error:
            raise RuntimeError(str(self._login_error)) from self._login_error

        texto = " ".join(query.split())
        if not texto:
            return []

        limite = max(0, config.scraper_max_resultados)
        if limite == 0:
            return []

        page.set_default_timeout(config.scraper_timeout_ms)
        page.set_default_navigation_timeout(config.scraper_timeout_ms)
        print(f"  [cococo] Buscando: {texto}")
        page.goto(
            f"{self.url_base}/shop/home",
            wait_until="domcontentloaded",
            timeout=config.scraper_timeout_ms,
        )

        campo = page.locator('input[name="search"]').first
        campo.wait_for(state="visible", timeout=config.scraper_timeout_ms)
        campo.fill(texto)
        page.evaluate(
            "() => { window.__cococoZeroSince = null; }"
        )
        page.locator(".search-box_button button").click(
            timeout=config.scraper_timeout_ms
        )

        try:
            print(
                "  [cococo] Esperando a que la búsqueda termine y "
                "las tarjetas estén visibles"
            )
            page.wait_for_function(
                """({cardSelector, query}) => {
                    const body = document.body?.innerText || '';
                    const match = body.match(/(\\d+)\\s+resultados?\\s+para\\s+([^\\n]+)/i);
                    const normalizar = value => value.normalize('NFD')
                        .replace(/[\\u0300-\\u036f]/g, '').toLocaleLowerCase().trim();
                    const termino = match
                        ? normalizar(match[2]).replace(/^[\\s"“'‘]+|[\\s"”'’]+$/g, '')
                        : '';
                    const conteoCoincide = match && termino.includes(normalizar(query));
                    const cards = Array.from(document.querySelectorAll(cardSelector))
                        .filter(card => card.getClientRects().length > 0);
                    if (cards.length && (conteoCoincide || location.pathname.replace(/\\/$/, '').endsWith('/shop'))) {
                        return true;
                    }
                    if (conteoCoincide && Number(match[1]) === 0 && !cards.length) {
                        if (!window.__cococoZeroSince) window.__cococoZeroSince = Date.now();
                        return Date.now() - window.__cococoZeroSince >= 3000;
                    }
                    window.__cococoZeroSince = null;
                    return false;
                }""",
                arg={"cardSelector": self.SEL_CARD, "query": texto},
                timeout=config.scraper_timeout_ms,
            )
        except Exception as error:
            body_text = page.locator("body").inner_text()
            if not page.locator(self.SEL_CARD).count():
                raise RuntimeError(
                    "Cococo no confirmó resultados ni mostró tarjetas "
                    "para la búsqueda. "
                    f"URL actual: {page.url}. Texto visible: {body_text[:500]!r}"
                ) from error
            raise

        self._esperar_catalogo_cargado()
        cards_actuales = page.evaluate(
            """() => Array.from(document.querySelectorAll('.card-product'))
                .filter(card => card.getClientRects().length > 0)
                .map(card => {
                    const texto = selector => card.querySelector(selector)?.innerText?.trim() || '';
                    const imagen = card.querySelector('.image-wrapper img');
                    const badges = card.querySelector('.card-product-badges');
                    return {
                        nombre: texto('h3.product-name, .label-name-product'),
                        href: card.querySelector('a.product-link')?.getAttribute('href') || '',
                        precio: texto('p.product-price'),
                        imagen: imagen?.currentSrc || imagen?.getAttribute('src') || imagen?.getAttribute('data-src') || '',
                        etiquetas: badges?.innerText?.split('\\n').map(x => x.trim()).filter(Boolean) || [],
                        texto: card.innerText || ''
                    };
                }).filter(item => item.nombre && item.href)"""
        )
        if not cards_actuales:
            body_text = page.locator("body").inner_text()
            resultado = re.search(
                r"(\d+)\s+resultados?\s+para\b", body_text, re.IGNORECASE
            )
            if resultado and int(resultado.group(1)) == 0:
                print(f"  [cococo] La búsqueda '{texto}' confirmó 0 resultados")
                return []
            raise RuntimeError(
                "Cococo mostró resultados, pero no se pudo capturar ninguna "
                f"tarjeta. Texto visible: {body_text[:500]!r}"
            )

        productos: list[ProductoEncontrado] = []
        vistos: set[str] = set()
        numero_pagina = 1

        while len(productos) < limite:
            print(
                f"  [cococo] Página {numero_pagina}: "
                f"{len(cards_actuales)} tarjetas de producto capturadas"
            )
            for indice, card_data in enumerate(cards_actuales):
                if len(productos) >= limite:
                    break
                try:
                    producto = self._parsear_card(card_data)
                    if producto and producto.url_detalle not in vistos:
                        productos.append(producto)
                        vistos.add(producto.url_detalle)
                except Exception as error:
                    print(f"  [cococo] Error en tarjeta {indice}: {error}")

            if len(productos) >= limite:
                break

            siguiente_pagina = numero_pagina + 1
            siguiente = page.locator(
                self.SEL_NEXT_PAGE.format(page=siguiente_pagina)
            ).first
            if not siguiente.count():
                break

            href_actual = self._primer_href(page)
            try:
                page.locator(self.SEL_OVERLAY).wait_for(
                    state="hidden",
                    timeout=min(config.scraper_timeout_ms, 4000),
                )
                siguiente.click(timeout=config.scraper_timeout_ms)
            except Exception:
                # El overlay transparente del sitio intercepta clics físicos.
                # Disparar el evento en el enlace mantiene activa la paginación
                # de Cococo sin navegar a las fichas de producto.
                try:
                    siguiente.dispatch_event(
                        "click", timeout=config.scraper_timeout_ms
                    )
                except Exception as error:
                    print(
                        f"  [cococo] No se pudo activar la página "
                        f"{siguiente_pagina}: {error}. Conservaré los "
                        "productos ya leídos."
                    )
                    break

            try:
                page.wait_for_function(
                    """({previousHref, pageNumber, cardSelector}) => {
                        const active = document.querySelector('.sp-page-link-active');
                        const firstHref = document.querySelector('a.product-link')?.getAttribute('href') || null;
                        const cards = Array.from(document.querySelectorAll(cardSelector))
                            .filter(card => card.getClientRects().length > 0);
                        if (active?.getAttribute('aria-current') !== String(pageNumber)
                            || firstHref === previousHref || !cards.length) return false;
                        return true;
                    }""",
                    arg={
                        "previousHref": href_actual,
                        "pageNumber": siguiente_pagina,
                        "cardSelector": self.SEL_CARD,
                    },
                    timeout=config.scraper_timeout_ms,
                )
            except Exception as error:
                print(
                    f"  [cococo] No se pudo avanzar a la página "
                    f"{siguiente_pagina}: {error}. Conservaré los "
                    "productos ya leídos."
                )
                break
            self._esperar_catalogo_cargado()
            cards_actuales = page.evaluate(
                """() => Array.from(document.querySelectorAll('.card-product'))
                    .filter(card => card.getClientRects().length > 0)
                    .map(card => {
                        const texto = selector => card.querySelector(selector)?.innerText?.trim() || '';
                        const imagen = card.querySelector('.image-wrapper img');
                        const badges = card.querySelector('.card-product-badges');
                        return {
                            nombre: texto('h3.product-name, .label-name-product'),
                            href: card.querySelector('a.product-link')?.getAttribute('href') || '',
                            precio: texto('p.product-price'),
                            imagen: imagen?.currentSrc || imagen?.getAttribute('src') || imagen?.getAttribute('data-src') || '',
                            etiquetas: badges?.innerText?.split('\\n').map(x => x.trim()).filter(Boolean) || [],
                            texto: card.innerText || ''
                        };
                    }).filter(item => item.nombre && item.href)"""
            )
            numero_pagina = siguiente_pagina

        self._enriquecer_desde_api(page, texto, productos, limite)

        return productos

    def _enriquecer_desde_api(
        self,
        page,
        query: str,
        productos: list[ProductoEncontrado],
        limite: int,
    ) -> None:
        """Completa SKU, marca, categoría y stock desde el API del catálogo."""
        if not productos:
            return

        api_productos: list[dict] = []
        numero_pagina = 1
        try:
            while len(api_productos) < limite:
                parametros = urlencode(
                    {
                        "page": numero_pagina,
                        "productName": query,
                        "offert": "",
                        "match": "exact",
                        "sort": "name",
                        "order": "asc",
                        "currency": 1,
                    }
                )
                url_api = f"{self.url_base}/api/shop/products?{parametros}"
                respuesta = page.evaluate(
                    """async url => {
                        const response = await fetch(url, {credentials: 'include'});
                        if (!response.ok) throw new Error(`HTTP ${response.status}`);
                        return await response.json();
                    }""",
                    url_api,
                )
                filas = respuesta.get("data", []) if isinstance(respuesta, dict) else []
                if not filas:
                    break
                api_productos.extend(filas)
                ultima_pagina = int(respuesta.get("last_page") or numero_pagina)
                if numero_pagina >= ultima_pagina:
                    break
                numero_pagina += 1
        except Exception as error:
            print(f"  [cococo] No se pudo consultar el API de productos: {error}")
            return

        api_productos_originales = list(api_productos)
        ids = [str(fila["id"]) for fila in api_productos[:limite] if fila.get("id")]
        if ids:
            try:
                registrados = page.evaluate(
                    """async ids => await Promise.all(ids.map(async id => {
                        try {
                            const xsrfCookie = document.cookie.split('; ')
                                .find(value => value.startsWith('XSRF-TOKEN='))?.slice('XSRF-TOKEN='.length);
                            const csrfMeta = document.querySelector('meta[name="csrf-token"]')?.content;
                            const headers = {
                                'Accept': 'application/json',
                                'Content-Type': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest'
                            };
                            if (csrfMeta) headers['X-CSRF-TOKEN'] = csrfMeta;
                            if (xsrfCookie) headers['X-XSRF-TOKEN'] = decodeURIComponent(xsrfCookie);
                            const response = await fetch(
                                '/api/shop/productVisited/register',
                                {
                                    method: 'POST',
                                    credentials: 'include',
                                    headers,
                                    body: JSON.stringify({id: String(id)})
                                }
                            );
                            const responseText = await response.text();
                            let body;
                            try { body = JSON.parse(responseText); }
                            catch (_) { return {__http_status: response.status}; }
                            const candidate = body?.data?.product
                                || body?.data?.data
                                || body?.product
                                || body?.data
                                || body;
                            const product = Array.isArray(candidate) ? candidate[0] : candidate;
                            return product && typeof product === 'object'
                                ? {...product, __http_status: response.status}
                                : {__http_status: response.status};
                        } catch (_) { return {__http_status: 0}; }
                    }))""",
                    ids,
                )
                reemplazos = {
                    str(registro.get("id")): registro
                    for registro in registrados
                    if isinstance(registro, dict) and registro.get("id") is not None
                }
                categorias_register = sum(
                    bool(registro.get("category"))
                    for registro in reemplazos.values()
                )
                categorias_lista = sum(
                    bool(fila.get("category")) for fila in api_productos_originales
                )
                estados_http: dict[int, int] = {}
                for registro in registrados:
                    if isinstance(registro, dict):
                        estado = int(registro.get("__http_status", 0))
                        estados_http[estado] = estados_http.get(estado, 0) + 1
                api_productos = []
                for fila in api_productos_originales:
                    registro = reemplazos.get(str(fila.get("id")), {})
                    api_productos.append(
                        {
                            **fila,
                            **{
                                k: v
                                for k, v in registro.items()
                                if v is not None and not k.startswith("__")
                            },
                        }
                    )
                print(
                    "  [cococo] productVisited/register: "
                    f"{len(reemplazos)}/{len(ids)} respuestas con producto"
                )
                print(
                    "  [cococo] Diagnóstico categoría API: "
                    f"lista={categorias_lista}, register={categorias_register}"
                )
                if not reemplazos:
                    print(f"  [cococo] Estados HTTP de register: {estados_http}")
            except Exception as error:
                print(f"  [cococo] No se pudo consultar productVisited/register: {error}")

        por_nombre = {
            self._normalizar_nombre(
                producto.get("base_name") or producto.get("name") or ""
            ): producto
            for producto in api_productos
        }
        por_id = {
            str(fila.get("id")): fila
            for fila in api_productos
            if fila.get("id") is not None
        }
        enriquecidos = 0
        con_categoria = 0
        for producto in productos:
            fila = por_nombre.get(self._normalizar_nombre(producto.nombre))
            if not fila:
                product_id = re.search(r"/(\d+)/?(?:\?.*)?$", producto.url_detalle)
                if product_id:
                    fila = por_id.get(product_id.group(1))
            if not fila:
                continue

            marca_data = fila.get("brand") or {}
            categoria_data = fila.get("category") or {}
            sku = fila.get("code")
            if sku:
                producto.sku = str(sku).strip()
            codigo_adicional = fila.get("additional_code")
            if codigo_adicional:
                producto.extra["codigo_adicional"] = str(codigo_adicional).strip()
            producto.marca = (
                marca_data.get("des_depa")
                or marca_data.get("name")
                or producto.marca
            )
            if isinstance(categoria_data, dict):
                categoria = categoria_data.get("name") or categoria_data.get("description")
            elif isinstance(categoria_data, str):
                categoria = categoria_data.strip()
            else:
                categoria = None
            categoria = (
                categoria
                or fila.get("category_name")
                or fila.get("categoryName")
                or producto.categoria
            )
            producto.categoria = categoria
            if categoria:
                con_categoria += 1

            stock = fila.get("available_qty")
            if stock is not None:
                try:
                    cantidad = float(stock)
                    producto.stock_texto = f"{stock} disponibles"
                    producto.disponible = cantidad > 0
                except (TypeError, ValueError):
                    producto.stock_texto = str(stock)

            producto.extra["sku_marca_requiere_login"] = False
            enriquecidos += 1

        print(
            f"  [cococo] API: {enriquecidos}/{len(productos)} productos "
            f"enriquecidos; categoría disponible en {con_categoria}"
        )

    @staticmethod
    def _normalizar_nombre(nombre: str) -> str:
        nombre = re.sub(r"<[^>]*>", " ", str(nombre))
        return " ".join(re.sub(r"[^a-z0-9]+", " ", nombre.casefold()).split())

    def _parsear_card(self, card: dict) -> ProductoEncontrado | None:
        nombre = " ".join(str(card.get("nombre", "")).split())
        href = str(card.get("href", ""))
        if not nombre or not href:
            return None

        precio_original = " ".join(str(card.get("precio", "")).split()) or None
        # Cococo muestra el colón como ¢; el helper compartido reconoce ₡.
        precio, moneda = self.limpiar_precio(
            (precio_original or "").replace("¢", "₡")
        )

        imagen = str(card.get("imagen", "")) or None
        url_imagen = self.url_absoluta(imagen, self.url_base) if imagen else None

        etiquetas = card.get("etiquetas", [])
        texto_card = str(card.get("texto", ""))
        stock_texto = self._extraer_stock(texto_card)
        return ProductoEncontrado(
            proveedor=self.codigo,
            nombre=nombre,
            url_detalle=self.url_absoluta(href, self.url_base),
            url_imagen=url_imagen,
            precio=precio,
            moneda=moneda,
            precio_texto_original=precio_original,
            stock_texto=stock_texto,
            disponible=self._interpretar_disponibilidad(stock_texto),
            sku=self._extraer_campo(texto_card, "sku"),
            marca=(self._extraer_campo(texto_card, "marca") or self._extraer_marca(nombre)),
            categoria=self._extraer_campo(texto_card, "categoria"),
            extra={
                "mpn": None,
                "etiquetas": etiquetas,
                "precio_requiere_login": False,
                "sesion_autenticada": self._autenticado,
                "sku_marca_requiere_login": not self._autenticado,
            },
        )

    @staticmethod
    def _extraer_campo(texto: str, campo: str) -> str | None:
        etiquetas = {
            "sku": r"SKU",
            "marca": r"(?:Marca|Brand|Fabricante)",
            "categoria": r"(?:Categoría|Categoria|Category)",
        }
        etiqueta = etiquetas.get(campo)
        if not etiqueta:
            return None
        coincidencia = re.search(
            rf"^\s*{etiqueta}\s*[:：]\s*(.+?)\s*$",
            texto,
            re.IGNORECASE | re.MULTILINE,
        )
        return coincidencia.group(1).strip() if coincidencia else None

    @staticmethod
    def _extraer_marca(nombre: str) -> str | None:
        """Detecta marcas conocidas cuando forman parte del nombre de tarjeta."""
        marcas = (
            "LOGITECH", "MICROSOFT", "HYPERX", "KINGSTON", "SAMSUNG",
            "LENOVO", "ASUS", "ACER", "APPLE", "XIAOMI", "HUAWEI",
            "TOSHIBA", "PHILIPS", "VIEWSONIC", "AOC", "DELL", "HP",
            "AMD", "INTEL", "NVIDIA", "RAZER", "CANON", "EPSON",
            "BROTHER", "JBL", "SONY", "LG", "MSI", "TP-LINK", "UGREEN",
        )
        titulo = re.sub(r"\s+", " ", nombre).upper()
        for marca in marcas:
            if re.search(rf"(?<![A-Z0-9]){re.escape(marca)}(?![A-Z0-9])", titulo):
                return marca
        return None

    @staticmethod
    def _primer_href(page) -> str | None:
        enlace = page.locator("a.product-link").first
        return enlace.get_attribute("href") if enlace.count() else None

    @staticmethod
    def _extraer_stock(texto_card: str) -> str | None:
        """Lee stock si está escrito en la tarjeta, sin abrir el producto."""
        for linea in texto_card.splitlines():
            linea = " ".join(linea.split())
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

        # Si incluye cantidad numérica (ej: "0 disponibles", "27 disponibles")
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
