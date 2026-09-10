"""
Estructuras de datos comunes.
Todo scraper devuelve una lista de ProductoEncontrado.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


@dataclass
class ProductoEncontrado:
    """
    Producto tal como lo trajo un scraper de un proveedor.
    Estructura uniforme sin importar el proveedor.
    """

    # Identificacion
    proveedor: str  # "eurocomp", "intelec", etc.
    nombre: str  # nombre completo del producto
    url_detalle: str  # link a la pagina del producto

    # Multimedia
    url_imagen: str | None = None

    # Precio (puede venir null si el sitio no lo muestra sin login)
    # Precio (puede venir null si el sitio no lo muestra sin login)
    precio: float | None = None  # precio actual (con descuento si aplica)
    precio_regular: float | None = None  # precio antes del descuento (si hay)
    tiene_descuento: bool = False
    descuento_porcentaje: float | None = None  # 29.0 para -29%
    moneda: str | None = None  # "CRC", "USD"
    precio_texto_original: str | None = None  # tal como aparece: "₡125,000.00"

    # Stock
    stock_texto: str | None = None  # "Disponible", "3 en stock", "Agotado"
    disponible: bool | None = None  # interpretacion booleana

    # Identificadores adicionales
    sku: str | None = None
    marca: str | None = None
    categoria: str | None = None

    # Descripcion / specs
    descripcion_corta: str | None = None
    specs: dict[str, Any] = field(default_factory=dict)

    # Metadatos
    scrapeado_en: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    extra: dict[str, Any] = field(
        default_factory=dict
    )  # cualquier campo especifico del proveedor

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResultadoScraper:
    """Resultado completo de correr UN scraper para UNA busqueda."""

    proveedor: str
    query: str
    exitoso: bool
    productos: list[ProductoEncontrado] = field(default_factory=list)
    duracion_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proveedor": self.proveedor,
            "query": self.query,
            "exitoso": self.exitoso,
            "duracion_ms": self.duracion_ms,
            "error": self.error,
            "total_productos": len(self.productos),
            "productos": [p.to_dict() for p in self.productos],
        }


@dataclass
class ProductoSolicitado:
    """Lo que un cliente pide, interpretado por la IA."""

    categoria: str | None = None
    marca: str | None = None
    modelo: str | None = None
    linea: str | None = None
    specs: dict[str, Any] = field(default_factory=dict)
    cantidad: int = 1
    query_sugerida: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
