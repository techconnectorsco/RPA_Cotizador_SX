"""
Modulo de matching v2.

Mejoras vs v1:
  1. Score textual combinado (token_set + token_sort + partial) con
     penalizacion cuando la query es muy corta.
  2. Bonus/penalizaciones por "variantes" (COMBO, USADA, RECERTIFICADA...).
  3. Defaults neutrales ajustados de 50 -> 70 (evita empates masivos).
  4. Clasificacion en TIERS (exacto/bueno/similar/dudoso) para la UI.

Mejoras v2.1 (consistencia solicitud vs proveedor):
  5. Marca, specs y bonus se evaluan sobre nombre + descripcion_corta +
     specs del proveedor (antes solo el nombre).
  6. Normalizacion de unidades: 16" / 16'' / 16 pulg / 16-inch / 16.0"
     -> "16 pulgadas"; 16 GB / 16-gb -> "16gb"; tildes eliminadas.
  7. Busqueda por palabra completa (evita que "lg" haga match en "pulgadas").
  8. Query sin palabras duplicadas y bonus que respeta marcas cortas (LG, HP)
     y numeros (16, 24, 27).
  9. Productos SIN precio visible se descartan y no se guardan en la DB.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from modelo import ProductoEncontrado, ProductoSolicitado


# ---------------------------------------------------------------------
# NORMALIZACION de texto (se aplica igual a solicitud y a candidato)
# ---------------------------------------------------------------------
_RE_PULGADAS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*-?\s*"
    r"(?:\"|''|'|”|“|″|pulgadas|pulgada|pulg\b\.?|inches\b|inch\b|in\b)"
)
_RE_CAPACIDAD = re.compile(r"(\d+(?:[.,]\d+)?)\s*-?\s*(gb|tb)\b")


def _normalizar_numero(num: str) -> str:
    """'16.0' -> '16', '15,6' -> '15.6'."""
    n = num.replace(",", ".")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n


def _normalizar(texto: Any) -> str:
    """
    Minusculas, sin tildes y con unidades unificadas:
      '16"', "16''", '16 pulg.', '16-inch', '16.0”'  -> '16 pulgadas'
      '16 GB', '16-gb'                                -> '16gb'
    """
    if texto is None or texto == "":
        return ""
    t = str(texto).lower()
    t = _RE_PULGADAS.sub(lambda m: f" {_normalizar_numero(m.group(1))} pulgadas ", t)
    t = _RE_CAPACIDAD.sub(lambda m: f"{_normalizar_numero(m.group(1))}{m.group(2)}", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip()


def _contiene_palabra(termino: str, texto: str) -> bool:
    """True si `termino` aparece como palabra completa (no como subcadena)."""
    termino = termino.strip()
    if not termino:
        return False
    patron = rf"(?<![a-z0-9]){re.escape(termino)}(?![a-z0-9])"
    return re.search(patron, texto) is not None


def _compactar(texto: str) -> str:
    """'16MR70-B' -> '16mr70b' (para comparar modelos sin guiones/espacios)."""
    return re.sub(r"[^a-z0-9]", "", texto.lower())


def _texto_candidato(candidato: ProductoEncontrado) -> str:
    """Nombre + marca + descripcion corta + specs del proveedor, normalizado."""
    partes = [
        candidato.nombre or "",
        candidato.marca or "",
        candidato.descripcion_corta or "",
    ]
    for k, v in (candidato.specs or {}).items():
        if v is not None:
            partes.append(f"{k} {v}")
    return _normalizar(" ".join(partes))


# ---------------------------------------------------------------------
# BLACKLIST por categoria: descarta accesorios cuando piden el producto
# ---------------------------------------------------------------------
BLACKLIST_CATEGORIA: dict[str, list[str]] = {
    "laptop": [
        "cartucho",
        "tinta",
        "toner",
        "adaptador",
        "cable",
        "cargador",
        "funda",
        "maleta",
        "mochila",
        "candado",
        "soporte",
    ],
    "impresora": ["cartucho", "tinta", "toner", "papel", "rodillo"],
    "mouse": ["mousepad", "pad para mouse", "alfombrilla"],
    "monitor": ["soporte", "brazo", "cable", "adaptador"],
    "teclado": ["teclas", "keycap", "cover", "funda"],
}


# ---------------------------------------------------------------------
# VARIANTES: palabras que indican que el producto NO es el original
# Si el cliente NO las pidio, penalizamos.
# ---------------------------------------------------------------------
VARIANTES_NEGATIVAS: dict[str, float] = {
    "combo": -15.0,  # bundle con accesorios
    "usado": -20.0,
    "usada": -20.0,
    "recertificado": -20.0,
    "recertificada": -20.0,
    "reacondicionado": -20.0,
    "reacondicionada": -20.0,
    "abierto": -10.0,  # caja abierta
    "abierta": -10.0,
    "demo": -10.0,
    "outlet": -10.0,
}

ETIQUETAS_VARIANTE = {
    "combo": "COMBO",
    "usado": "USADO",
    "usada": "USADO",
    "recertificado": "RECERTIFICADO",
    "recertificada": "RECERTIFICADO",
    "reacondicionado": "REACONDICIONADO",
    "reacondicionada": "REACONDICIONADO",
    "abierto": "CAJA ABIERTA",
    "abierta": "CAJA ABIERTA",
    "demo": "DEMO",
    "outlet": "OUTLET",
}

# Equivalencias de nombres de marca comunes
EQUIVALENCIAS_MARCA: dict[str, list[str]] = {
    "hp": ["hp", "hewlett", "hewlett packard", "hewlett-packard"],
    "asus": ["asus", "asustek"],
    "acer": ["acer"],
    "lg": ["lg", "lg electronics"],
}

STOPWORDS = {
    "de",
    "la",
    "el",
    "un",
    "una",
    "los",
    "las",
    "y",
    "con",
    "para",
    "por",
    "en",
}


# ---------------------------------------------------------------------
# TIERS para clasificar visualmente el match
# ---------------------------------------------------------------------
TIER_EXACTO = "exacto"  # score >= 85
TIER_BUENO = "bueno"  # 70 - 84
TIER_SIMILAR = "similar"  # 55 - 69
TIER_DUDOSO = "dudoso"  # < 55


def sin_precio_visible(producto: ProductoEncontrado) -> bool:
    """True si el proveedor no mostro un precio utilizable (None, 0 o invalido)."""
    precio = producto.precio
    if precio is None:
        return True
    try:
        return float(precio) <= 0
    except (TypeError, ValueError):
        return True


def clasificar_tier(score: float) -> str:
    if score >= 85:
        return TIER_EXACTO
    if score >= 70:
        return TIER_BUENO
    if score >= 55:
        return TIER_SIMILAR
    return TIER_DUDOSO


@dataclass
class ResultadoMatch:
    """Un producto candidato con su score y desglose."""

    producto: ProductoEncontrado
    score_total: float
    score_texto: float
    score_marca: float
    score_specs: float
    ajuste_variantes: float = 0.0
    bonus_identidad: float = 0.0
    bonus_contenida: float = 0.0
    etiquetas_variante: list[str] = None
    tier: str = TIER_DUDOSO
    descartado: bool = False
    razon_descarte: str | None = None

    def __post_init__(self):
        if self.etiquetas_variante is None:
            self.etiquetas_variante = []

    def to_dict(self) -> dict[str, Any]:
        d = self.producto.to_dict()
        d["match"] = {
            "score_total": round(self.score_total, 2),
            "score_texto": round(self.score_texto, 2),
            "score_marca": round(self.score_marca, 2),
            "score_specs": round(self.score_specs, 2),
            "ajuste_variantes": round(self.ajuste_variantes, 2),
            "bonus_identidad": round(self.bonus_identidad, 2),
            "bonus_contenida": round(self.bonus_contenida, 2),
            "etiquetas_variante": self.etiquetas_variante,
            "tier": self.tier,
            "descartado": self.descartado,
            "razon_descarte": self.razon_descarte,
        }
        return d


class Matcher:
    """Calcula matches entre un ProductoSolicitado y una lista de candidatos."""

    def __init__(
        self,
        umbral_minimo: float = 55.0,
        pesos: dict[str, float] | None = None,
    ):
        self.umbral = umbral_minimo
        self.pesos = pesos or {
            "texto": 0.50,
            "marca": 0.25,
            "specs": 0.25,
        }
        assert abs(sum(self.pesos.values()) - 1.0) < 0.01, "Pesos deben sumar 1.0"

    def calcular_todos(
        self,
        solicitado: ProductoSolicitado,
        candidatos: list[ProductoEncontrado],
    ) -> list[ResultadoMatch]:
        resultados = [self._calcular_uno(solicitado, c) for c in candidatos]
        # Ordenar: descartados al final, luego los SIN precio visible
        # (siempre detras de los cotizables), luego por score desc,
        # y para empates: mas barato primero (util para queries genericas)
        resultados.sort(
            key=lambda r: (
                r.descartado,
                sin_precio_visible(r.producto),
                -r.score_total,
                r.producto.precio if r.producto.precio is not None else float("inf"),
            )
        )
        return resultados

    def filtrar_por_umbral(
        self,
        resultados: list[ResultadoMatch],
    ) -> list[ResultadoMatch]:
        return [
            r for r in resultados if not r.descartado and r.score_total >= self.umbral
        ]

    # -----------------------------------------------------------------
    def _calcular_uno(
        self,
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
    ) -> ResultadoMatch:
        # Nombre normalizado: base para blacklist, variantes y score textual
        nombre_cand = _normalizar(candidato.nombre)
        # Texto completo (nombre + marca + descripcion + specs): base para
        # marca, specs y bonus, asi no se pierde info que el proveedor puso
        # fuera del titulo.
        texto_cand = _texto_candidato(candidato)

        # ---- Filtro duro: blacklist por categoria ----
        # Solo sobre el NOMBRE: la descripcion suele decir "incluye cable"
        # y eso no convierte al producto en un accesorio.
        if solicitado.categoria:
            cat_key = _normalizar(solicitado.categoria)
            for palabra in BLACKLIST_CATEGORIA.get(cat_key, []):
                if _contiene_palabra(palabra, nombre_cand):
                    return ResultadoMatch(
                        producto=candidato,
                        score_total=0.0,
                        score_texto=0.0,
                        score_marca=0.0,
                        score_specs=0.0,
                        descartado=True,
                        razon_descarte=f"contiene '{palabra}' (blacklist {cat_key})",
                    )

        if sin_precio_visible(candidato):
            return ResultadoMatch(
                producto=candidato,
                score_total=0.0,
                score_texto=0.0,
                score_marca=0.0,
                score_specs=0.0,
                descartado=True,
                razon_descarte="sin_precio_visible",
            )

        # ---- Score textual (mejorado) ----
        query = self._componer_query(solicitado)
        score_texto = self._score_textual(query, nombre_cand)

        # ---- Score de marca ----
        score_marca = self._score_marca(solicitado, candidato, texto_cand)

        # ---- Score de specs ----
        score_specs = self._score_specs(solicitado, candidato, texto_cand)

        # ---- Ponderacion base ----
        score_base = (
            self.pesos["texto"] * score_texto
            + self.pesos["marca"] * score_marca
            + self.pesos["specs"] * score_specs
        )

        # ---- Ajuste por variantes (COMBO, USADA, etc.) ----
        ajuste_var, etiquetas_variante = self._ajuste_variantes(
            solicitado, candidato, nombre_cand
        )

        # ---- Bonus por match de identidad (marca + linea/modelo) ----
        bonus_identidad = self._bonus_identidad(solicitado, texto_cand)

        # ---- Bonus por query totalmente contenida en el nombre ----
        bonus_contenida = self._bonus_query_contenida(solicitado, texto_cand)

        # ---- Score total (clamp a 0-100) ----
        score_total = max(
            0.0, min(100.0, score_base + ajuste_var + bonus_identidad + bonus_contenida)
        )
        tier = clasificar_tier(score_total)

        return ResultadoMatch(
            producto=candidato,
            score_total=score_total,
            score_texto=score_texto,
            score_marca=score_marca,
            score_specs=score_specs,
            ajuste_variantes=ajuste_var,
            bonus_identidad=bonus_identidad,
            bonus_contenida=bonus_contenida,
            etiquetas_variante=etiquetas_variante,
            tier=tier,
        )

    # -----------------------------------------------------------------
    # Score textual: combina 3 estrategias + penaliza queries cortas
    # -----------------------------------------------------------------
    @staticmethod
    def _score_textual(query: str, nombre: str) -> float:
        q = _normalizar(query)
        n = _normalizar(nombre)

        r_set = fuzz.token_set_ratio(q, n)  # todas las palabras estan?
        r_sort = fuzz.token_sort_ratio(q, n)  # orden similar?
        r_partial = fuzz.partial_ratio(q, n)  # substring?

        base = r_set * 0.3 + r_sort * 0.4 + r_partial * 0.3

        # Penalizar cuando query MUY corta vs nombre largo
        # (evita que "laptop" de 100 contra "LAPTOP DELL PRO 16 ULTRA...")
        # palabras_q = len(q.split())
        # palabras_n = len(n.split())
        # if palabras_q <= 2 and palabras_n > 6:
        #     base *= 0.60
        # elif palabras_q <= 3 and palabras_n > 8:
        #     base *= 0.80

        return base

    # -----------------------------------------------------------------
    @staticmethod
    def _componer_query(p: ProductoSolicitado) -> str:
        partes: list[str] = []
        if p.categoria:
            partes.append(p.categoria)
        if p.marca:
            partes.append(p.marca)
        if p.linea:
            partes.append(p.linea)
        if p.modelo:
            partes.append(p.modelo)
        for k, v in (p.specs or {}).items():
            if v is not None and k in (
                "ram_gb",
                "almacenamiento_gb",
                "cpu",
                "pantalla_pulgadas",
            ):
                partes.append(str(v))
        if p.query_sugerida:
            partes.append(p.query_sugerida)

        # Quitar palabras repetidas (preservando el orden). Sin esto,
        # "monitor LG 16 monitor 16 pulgadas LG" baja el token_sort_ratio.
        vistas: set[str] = set()
        tokens: list[str] = []
        for tok in _normalizar(" ".join(partes)).split():
            if tok not in vistas:
                vistas.add(tok)
                tokens.append(tok)
        return " ".join(tokens)

    # -----------------------------------------------------------------
    @staticmethod
    def _score_marca(
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
        texto_cand: str | None = None,
    ) -> float:
        if not solicitado.marca:
            return 70.0  # sin marca pedida = neutral favorable (antes era 50)

        marca_ped = _normalizar(solicitado.marca)

        # Normalizar variantes de nombres de marca comunes
        aliases = EQUIVALENCIAS_MARCA.get(marca_ped, [marca_ped])

        # 1. Comparar contra marca extraida por el scraper
        if candidato.marca:
            marca_cand = _normalizar(candidato.marca)
            if any(_contiene_palabra(a, marca_cand) for a in aliases):
                return 100.0

        # 2. Fallback: buscar en nombre + descripcion + specs
        #    (por palabra completa: "lg" NO debe coincidir con "pulgadas")
        texto = texto_cand if texto_cand is not None else _texto_candidato(candidato)
        if any(_contiene_palabra(a, texto) for a in aliases):
            return 100.0

        return 0.0

    # -----------------------------------------------------------------
    def _score_specs(
        self,
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
        texto_cand: str | None = None,
    ) -> float:
        if not solicitado.specs:
            return 70.0  # sin specs pedidas = neutral favorable (antes 50)

        texto = texto_cand if texto_cand is not None else _texto_candidato(candidato)
        specs_cand = candidato.specs or {}

        aciertos = 0
        total = 0

        for clave, valor in solicitado.specs.items():
            if valor is None or valor is False:
                continue
            total += 1
            if clave in specs_cand and _normalizar(specs_cand.get(clave)) == _normalizar(
                valor
            ):
                aciertos += 1
                continue
            if _spec_en_nombre(clave, valor, texto):
                aciertos += 1

        if total == 0:
            return 70.0
        return (aciertos / total) * 100

    # -----------------------------------------------------------------
    @staticmethod
    def _ajuste_variantes(
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
        nombre_cand: str | None = None,
    ) -> tuple[float, list[str]]:
        """
        Devuelve (ajuste_score, lista_etiquetas_detectadas).
        Penaliza cuando el candidato tiene palabras como COMBO/USADA/etc.
        y el cliente NO las pidio explicitamente.
        Solo mira el NOMBRE y por palabra completa.
        """
        nombre_lower = nombre_cand if nombre_cand is not None else _normalizar(candidato.nombre)
        texto_pedido = _normalizar(
            (solicitado.query_sugerida or "")
            + " "
            + (solicitado.modelo or "")
            + " "
            + (solicitado.linea or "")
        )

        ajuste = 0.0
        etiquetas: list[str] = []
        etiquetas_vistas: set[str] = set()  # evita duplicados (usado/usada)

        for palabra, penalizacion in VARIANTES_NEGATIVAS.items():
            if _contiene_palabra(palabra, nombre_lower):
                etiqueta = ETIQUETAS_VARIANTE[palabra]
                if etiqueta not in etiquetas_vistas:
                    etiquetas.append(etiqueta)
                    etiquetas_vistas.add(etiqueta)
                # Solo penalizar si el usuario NO lo pidió
                if not _contiene_palabra(palabra, texto_pedido):
                    ajuste += penalizacion

        return ajuste, etiquetas

    # ---------------------------------------------------------------------
    # Helpers para detectar specs en texto libre
    # ---------------------------------------------------------------------
    @staticmethod
    def _bonus_identidad(
        solicitado: ProductoSolicitado,
        texto_cand: str,
    ) -> float:
        """
        Si marca + (linea o modelo) del pedido aparecen todos en el
        texto del candidato, es un match de identidad clara: +10.
        """
        if not solicitado.marca:
            return 0.0

        marca_ped = _normalizar(solicitado.marca)
        aliases = EQUIVALENCIAS_MARCA.get(marca_ped, [marca_ped])
        if not any(_contiene_palabra(a, texto_cand) for a in aliases):
            return 0.0

        compacto = _compactar(texto_cand)
        for campo in (solicitado.linea, solicitado.modelo):
            if not campo:
                continue
            c = _normalizar(campo)
            # marca + linea / marca + modelo (tolerante a guiones y espacios:
            # "16MR70" coincide con "16MR70-B" o "16 MR70")
            if _contiene_palabra(c, texto_cand) or (
                len(_compactar(c)) >= 4 and _compactar(c) in compacto
            ):
                return 10.0

        return 0.0

    # -----------------------------------------------------------------
    @staticmethod
    def _bonus_query_contenida(
        solicitado: ProductoSolicitado,
        texto_cand: str,
    ) -> float:
        """
        Si TODAS las palabras significativas de la query aparecen en el
        texto del candidato (en cualquier orden), +8.

        Palabras ignoradas: articulos. Se conservan marcas cortas (LG, HP)
        y numeros (16, 24) porque son los que definen el producto.
        """
        if not solicitado.query_sugerida:
            return 0.0

        palabras = [
            w
            for w in _normalizar(solicitado.query_sugerida).split()
            if w not in STOPWORDS and (len(w) >= 2 or w.isdigit())
        ]
        if not palabras:
            return 0.0

        # Todas las palabras deben estar (como palabra completa)
        if all(_contiene_palabra(p, texto_cand) for p in palabras):
            return 8.0
        return 0.0


def _spec_en_nombre(clave: str, valor: Any, nombre: str) -> bool:
    """
    Busca una spec pedida dentro del texto (ya normalizado) del candidato.
    Se mantiene el nombre por compatibilidad, pero recibe el texto completo.
    """
    nombre = _normalizar(nombre)
    if clave == "ram_gb":
        return _contiene_gb(nombre, valor)
    if clave == "almacenamiento_gb":
        return _contiene_gb(nombre, valor) or _contiene_ssd_hdd(nombre, valor)
    if clave == "pantalla_pulgadas":
        num = _normalizar_numero(str(valor))
        # Tras _normalizar, 16" / 16 pulg / 16-inch / 16.0" quedan "16 pulgadas"
        return re.search(rf"(?<![\d.]){re.escape(num)} pulgadas\b", nombre) is not None
    if clave in ("cpu", "almacenamiento_tipo"):
        return _contiene_palabra(_normalizar(valor), nombre)
    # Spec booleana (ej. "tactil": True): buscar el nombre de la clave
    if valor is True:
        return _contiene_palabra(_normalizar(clave.replace("_", " ")), nombre)
    # Cualquier otra spec: buscar el valor como palabra completa
    return _contiene_palabra(_normalizar(valor), nombre)


def _contiene_gb(texto: str, valor: int | float) -> bool:
    num = _normalizar_numero(str(valor))
    patterns = [
        rf"(?<![\d.]){re.escape(num)}\s*-?\s*gb\b",
    ]
    # 1024 / 1000 GB tambien se escriben como 1TB, 2048 como 2TB, etc.
    try:
        v = float(valor)
        for divisor in (1024, 1000):
            if v >= divisor and v % divisor == 0:
                patterns.append(rf"(?<![\d.]){int(v // divisor)}\s*-?\s*tb\b")
    except (TypeError, ValueError):
        pass
    return any(re.search(p, texto, re.IGNORECASE) for p in patterns)


def _contiene_ssd_hdd(texto: str, valor: int | float) -> bool:
    num = re.escape(_normalizar_numero(str(valor)))
    patterns = [
        rf"(?<![\d.]){num}\s*(?:gb|g)?[-\s]*(?:ssd|hdd|nvme)\b",
        rf"\b(?:ssd|hdd|nvme)[-\s]*{num}(?![\d.])",
    ]
    return any(re.search(p, texto, re.IGNORECASE) for p in patterns)
