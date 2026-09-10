"""
Modulo de matching v2.

Mejoras vs v1:
  1. Score textual combinado (token_set + token_sort + partial) con
     penalizacion cuando la query es muy corta.
  2. Bonus/penalizaciones por "variantes" (COMBO, USADA, RECERTIFICADA...).
  3. Defaults neutrales ajustados de 50 -> 70 (evita empates masivos).
  4. Clasificacion en TIERS (exacto/bueno/similar/dudoso) para la UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from modelo import ProductoEncontrado, ProductoSolicitado

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


# ---------------------------------------------------------------------
# TIERS para clasificar visualmente el match
# ---------------------------------------------------------------------
TIER_EXACTO = "exacto"  # score >= 95
TIER_BUENO = "bueno"  # 80 - 94
TIER_SIMILAR = "similar"  # 65 - 79
TIER_DUDOSO = "dudoso"  # < 65


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
        # Ordenar: descartados al final, luego por score desc,
        # y para empates: mas barato primero (util para queries genericas)
        resultados.sort(
            key=lambda r: (
                r.descartado,
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
        nombre_cand = candidato.nombre.lower()

        # ---- Filtro duro: blacklist por categoria ----
        if solicitado.categoria:
            cat_key = solicitado.categoria.lower()
            for palabra in BLACKLIST_CATEGORIA.get(cat_key, []):
                if palabra in nombre_cand:
                    return ResultadoMatch(
                        producto=candidato,
                        score_total=0.0,
                        score_texto=0.0,
                        score_marca=0.0,
                        score_specs=0.0,
                        descartado=True,
                        razon_descarte=f"contiene '{palabra}' (blacklist {cat_key})",
                    )

        # ---- Score textual (mejorado) ----
        query = self._componer_query(solicitado)
        score_texto = self._score_textual(query, nombre_cand)

        # ---- Score de marca ----
        score_marca = self._score_marca(solicitado, candidato)

        # ---- Score de specs ----
        score_specs = self._score_specs(solicitado, candidato)

        # ---- Ponderacion base ----
        score_base = (
            self.pesos["texto"] * score_texto
            + self.pesos["marca"] * score_marca
            + self.pesos["specs"] * score_specs
        )

        # ---- Ajuste por variantes (COMBO, USADA, etc.) ----
        ajuste_var, etiquetas_variante = self._ajuste_variantes(solicitado, candidato)

        # ---- Bonus por match de identidad (marca + linea/modelo) ----
        bonus_identidad = self._bonus_identidad(solicitado, candidato)

        # ---- Bonus por query totalmente contenida en el nombre ----
        bonus_contenida = self._bonus_query_contenida(solicitado, candidato)

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
        q = query.lower()
        n = nombre.lower()

        r_set = fuzz.token_set_ratio(q, n)  # todas las palabras estan?
        r_sort = fuzz.token_sort_ratio(q, n)  # orden similar?
        r_partial = fuzz.partial_ratio(q, n)  # substring?

        base = r_set * 0.3 + r_sort * 0.4 + r_partial * 0.3

        # Penalizar cuando query MUY corta vs nombre largo
        # (evita que "laptop" de 100 contra "LAPTOP DELL PRO 16 ULTRA...")
        palabras_q = len(q.split())
        palabras_n = len(n.split())
        if palabras_q <= 2 and palabras_n > 6:
            base *= 0.60
        elif palabras_q <= 3 and palabras_n > 8:
            base *= 0.80

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
        return " ".join(partes)

    # -----------------------------------------------------------------
    @staticmethod
    def _score_marca(
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
    ) -> float:
        if not solicitado.marca:
            return 70.0  # sin marca pedida = neutral favorable (antes era 50)

        marca_ped = solicitado.marca.lower()

        # Normalizar variantes de nombres de marca comunes
        equivalencias = {
            "hp": ["hp", "hewlett", "hewlett packard", "hewlett-packard"],
            "asus": ["asus", "asustek"],
            "acer": ["acer"],
        }
        aliases = equivalencias.get(marca_ped, [marca_ped])

        # 1. Comparar contra marca extraida por el scraper
        if candidato.marca:
            marca_cand = candidato.marca.lower()
            if any(a in marca_cand for a in aliases):
                return 100.0

        # 2. Fallback: buscar en el nombre
        nombre_cand = candidato.nombre.lower()
        if any(a in nombre_cand for a in aliases):
            return 100.0

        return 0.0

    # -----------------------------------------------------------------
    def _score_specs(
        self,
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
    ) -> float:
        if not solicitado.specs:
            return 70.0  # sin specs pedidas = neutral favorable (antes 50)

        nombre_cand = candidato.nombre.lower()
        specs_cand = candidato.specs or {}

        aciertos = 0
        total = 0

        for clave, valor in solicitado.specs.items():
            if valor is None:
                continue
            total += 1
            if specs_cand.get(clave) == valor:
                aciertos += 1
                continue
            if _spec_en_nombre(clave, valor, nombre_cand):
                aciertos += 1

        if total == 0:
            return 70.0
        return (aciertos / total) * 100

    # -----------------------------------------------------------------
    @staticmethod
    def _ajuste_variantes(
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
    ) -> tuple[float, list[str]]:
        """
        Devuelve (ajuste_score, lista_etiquetas_detectadas).
        Penaliza cuando el candidato tiene palabras como COMBO/USADA/etc.
        y el cliente NO las pidio explicitamente.
        """
        nombre_lower = candidato.nombre.lower()
        texto_pedido = (
            (solicitado.query_sugerida or "").lower()
            + " "
            + (solicitado.modelo or "").lower()
            + " "
            + (solicitado.linea or "").lower()
        )

        ajuste = 0.0
        etiquetas: list[str] = []
        etiquetas_vistas: set[str] = set()  # evita duplicados (usado/usada)

        for palabra, penalizacion in VARIANTES_NEGATIVAS.items():
            if palabra in nombre_lower:
                etiqueta = ETIQUETAS_VARIANTE[palabra]
                if etiqueta not in etiquetas_vistas:
                    etiquetas.append(etiqueta)
                    etiquetas_vistas.add(etiqueta)
                # Solo penalizar si el usuario NO lo pidió
                if palabra not in texto_pedido:
                    ajuste += penalizacion

        return ajuste, etiquetas

    # ---------------------------------------------------------------------
    # Helpers para detectar specs en texto libre
    # ---------------------------------------------------------------------
    @staticmethod
    def _bonus_identidad(
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
    ) -> float:
        """
        Si marca + (linea o modelo) del pedido aparecen todos en el
        nombre del candidato, es un match de identidad clara: +10.
        """
        if not solicitado.marca:
            return 0.0

        nombre = candidato.nombre.lower()
        marca_ok = solicitado.marca.lower() in nombre

        if not marca_ok:
            return 0.0

        # marca + linea
        if solicitado.linea and solicitado.linea.lower() in nombre:
            return 10.0
        # marca + modelo
        if solicitado.modelo and solicitado.modelo.lower() in nombre:
            return 10.0

        return 0.0

    # -----------------------------------------------------------------
    @staticmethod
    def _bonus_query_contenida(
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
    ) -> float:
        """
        Si TODAS las palabras significativas de la query aparecen en el
        nombre del candidato (en cualquier orden), +8.

        Palabras ignoradas: articulos y palabras muy cortas.
        """
        if not solicitado.query_sugerida:
            return 0.0

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
        palabras = [
            w.lower()
            for w in solicitado.query_sugerida.split()
            if len(w) >= 3 and w.lower() not in STOPWORDS
        ]
        if not palabras:
            return 0.0

        nombre = candidato.nombre.lower()
        # Todas las palabras deben estar
        if all(p in nombre for p in palabras):
            return 8.0
        return 0.0


def _spec_en_nombre(clave: str, valor: Any, nombre: str) -> bool:
    if clave == "ram_gb":
        return _contiene_gb(nombre, valor)
    if clave == "almacenamiento_gb":
        return _contiene_gb(nombre, valor) or _contiene_ssd_hdd(nombre, valor)
    if clave == "pantalla_pulgadas":
        patterns = [
            rf'\b{valor}["\'\u2033]',
            rf"\b{valor}\s*(?:pulgadas|inch|in)\b",
            rf"\b{re.escape(str(valor))}\s*”",
        ]
        return any(re.search(p, nombre) for p in patterns)
    if clave == "cpu":
        return str(valor).lower() in nombre
    if clave == "almacenamiento_tipo":
        return str(valor).lower() in nombre
    return False


def _contiene_gb(texto: str, valor: int | float) -> bool:
    patterns = [
        rf"\b{valor}\s*gb\b",
        rf"\b{valor}gb\b",
        rf"\b{valor}-gb\b",
    ]
    return any(re.search(p, texto, re.IGNORECASE) for p in patterns)


def _contiene_ssd_hdd(texto: str, valor: int | float) -> bool:
    patterns = [
        rf"\b{valor}\s*(?:gb|g)?[-\s]*(?:ssd|hdd|nvme)\b",
        rf"\b(?:ssd|hdd|nvme)[-\s]*{valor}",
    ]
    return any(re.search(p, texto, re.IGNORECASE) for p in patterns)
