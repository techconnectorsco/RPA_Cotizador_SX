"""
Modulo de matching v3.

QUE CAMBIA RESPECTO A v2, Y POR QUE
───────────────────────────────────
v2 puntuaba con similitud difusa (RapidFuzz: token_set + token_sort + partial).
El problema practico: token_sort_ratio pregunta "¿la query y el nombre del
producto son la misma frase?", y en un catalogo real NUNCA lo son — el vendedor
escribe 3 o 4 palabras y el producto se llama con 12. Esa metrica arrastraba el
40% del score y hundia justamente a los productos buenos, que son los que traen
las specs en el titulo.

Caso real que motivo esta reescritura: se busco "Monitor LG 16 pulgadas" y el
monitor LG de 16 pulgadas aparecio en el grupo "dudoso", con su nombre diciendo
exactamente lo que se habia buscado.

v3 reemplaza el fuzzy por COBERTURA DE PALABRAS: de las palabras que escribio el
vendedor, cuantas aparecen en el producto. "Monitor LG 16 pulgadas" contra
"Monitor LG 16MR70-B 16 pulgadas IPS" son 4 de 4 = 100%. Es la misma cuenta que
haria un humano mirando la pantalla, y por eso se puede defender frente a un
cliente.

Las otras tres decisiones de v3:

  1. Busqueda generica (una sola palabra, sin marca ni modelo): el matcher NO
     clasifica. El buscador del proveedor ya decidio que es relevante; pisarlo
     con un score inventado solo degrada resultados validos.
  2. Sinonimos español-ingles para categorias comunes. Era lo que hundia a
     Amazon, que titula "Wireless Earbuds" cuando se busco "audifonos".
  3. El orden del proveedor se respeta dentro de cada grupo, restando una
     fraccion minima al score segun la posicion en que vino el producto. Asi no
     hace falta tocar el esquema de la base ni el frontend.

Se mantiene de v2, porque funcionaba bien:
  - Normalizacion de unidades (16" / 16'' / 16 pulg / 16-inch -> "16 pulgadas").
  - Blacklist por categoria (no mostrar cartuchos cuando se pide una impresora).
  - Penalizacion de variantes (COMBO, USADA, RECERTIFICADA...).
  - Descarte de productos sin precio visible: sin precio no se puede cotizar.
  - Los cuatro valores del enum tier_match, para no requerir migracion.

La interfaz publica no cambia: Matcher(umbral_minimo), calcular_todos(),
ResultadoMatch.to_dict() con la misma estructura. main.py, worker.py y db.py
siguen funcionando sin tocarse.
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
# PALABRAS QUE NO APORTAN A LA BUSQUEDA
# El vendedor escribe en lenguaje natural ("me gustaria encontrar una laptop
# hp"). Estas palabras no identifican nada y, si entran al conteo de cobertura,
# bajan el porcentaje de productos que SI son correctos.
# ---------------------------------------------------------------------
STOPWORDS = {
    # articulos, preposiciones, conectores
    "de",
    "del",
    "la",
    "el",
    "lo",
    "un",
    "una",
    "unos",
    "unas",
    "los",
    "las",
    "y",
    "o",
    "con",
    "sin",
    "para",
    "por",
    "en",
    "a",
    "al",
    "que",
    "su",
    "sus",
    # pronombres y formas verbales de cortesia
    "me",
    "mi",
    "mis",
    "te",
    "tu",
    "tus",
    "se",
    "nos",
    "le",
    "les",
    "yo",
    "usted",
    "ustedes",
    "ser",
    "sea",
    "hay",
    "tiene",
    "tienen",
    "tengo",
    "tengan",
    "ocupo",
    "ocupamos",
    "podrian",
    "podria",
    "puedes",
    "pueden",
    # verbos y formulas de pedido
    "necesito",
    "necesitamos",
    "quiero",
    "queremos",
    "busco",
    "buscamos",
    "buscar",
    "encontrar",
    "conseguir",
    "cotizar",
    "cotizacion",
    "cotizame",
    "gustaria",
    "gustaría",
    "requiero",
    "requerimos",
    "solicito",
    "mandame",
    "enviame",
    "pasame",
    "precio",
    "precios",
    "favor",
    "porfavor",
    "porfa",
    "hola",
    "buenas",
    "dias",
    "tardes",
    "gracias",
    "urgente",
    # unidades de pedido
    "unidad",
    "unidades",
    "pieza",
    "piezas",
    "cantidad",
    "cliente",
}


# ---------------------------------------------------------------------
# SINONIMOS DE CATEGORIA (español <-> ingles y variantes regionales)
#
# Para que sirve: varios proveedores (Amazon sobre todo) titulan en ingles.
# Sin esto, buscar "audifonos" contra "Wireless Earbuds Bluetooth" da cobertura
# cero y el producto correcto termina en el ultimo grupo. Un termino cuenta como
# presente si el, o cualquiera de sus sinonimos, aparece en el producto.
# ---------------------------------------------------------------------
SINONIMOS: dict[str, set[str]] = {
    "laptop": {"laptop", "notebook", "portatil", "computadora portatil", "ultrabook"},
    "computadora": {"computadora", "computador", "pc", "desktop", "cpu", "torre"},
    "monitor": {"monitor", "pantalla", "display", "screen"},
    "audifonos": {
        "audifonos",
        "auriculares",
        "headphones",
        "headset",
        "earbuds",
        "earphones",
        "cascos",
        "audifono",
    },
    "parlante": {
        "parlante",
        "parlantes",
        "bocina",
        "bocinas",
        "speaker",
        "speakers",
        "altavoz",
        "altavoces",
    },
    "teclado": {"teclado", "keyboard"},
    "mouse": {"mouse", "raton", "ratón"},
    "impresora": {"impresora", "printer", "multifuncional", "multifuncion"},
    "escaner": {"escaner", "scanner"},
    # Tres proveedores, tres vocabularios para la misma cosa, todos medidos:
    #   Eurocomp -> "HD SOLIDO EXTERNO 1TB"
    #   CDC      -> "DD SOLIDO KINGSTON 2TB"
    #   Amazon   -> "Samsung SSD portatil T7 1TB"
    # Un SSD ES un disco, asi que va en el mismo grupo. El costo es que pedir
    # "ssd" tambien acepta un HDD mecanico; se asume a cambio de no perder los
    # productos correctos, que es un error mucho mas visible para el vendedor.
    # "duro" va aca a proposito: suelto no significa nada, pero si el producto
    # dice "HD" o "DD" hay que darlo por cubierto.
    "disco": {
        "disco",
        "duro",
        "dd",
        "hd",
        "hdd",
        "ssd",
        "solido",
        "nvme",
        "m.2",
        "unidad",
        "almacenamiento",
        "hard drive",
        "drive",
    },
    # "externo" tambien tiene varios nombres. "portatil" entra aca aunque este
    # tambien en "laptop": en español la palabra es ambigua de verdad. El indice
    # de sinonimos es por termino y no transitivo, asi que "laptop" NO queda
    # contaminado por esto.
    "externo": {"externo", "externa", "portable", "portatil", "extraible"},
    "memoria": {"memoria", "ram", "memory"},
    "tableta": {"tableta", "tablet"},
    "celular": {"celular", "telefono", "smartphone", "movil", "phone"},
    "camara": {"camara", "camera", "webcam"},
    "proyector": {"proyector", "projector", "videobeam"},
    "bateria": {"bateria", "battery", "pila"},
    "cargador": {"cargador", "charger", "adaptador de corriente"},
    "calculadora": {"calculadora", "calculator"},
    "silla": {"silla", "chair", "sillon"},
    "switch": {"switch", "conmutador"},
    "router": {"router", "enrutador"},
    "ups": {"ups", "respaldo", "no break", "nobreak"},
    "toner": {"toner", "cartucho", "tinta", "ink"},
}

# Indice inverso: cada termino apunta al conjunto completo de sus sinonimos.
_INDICE_SINONIMOS: dict[str, set[str]] = {}
for _grupo in SINONIMOS.values():
    for _t in _grupo:
        _INDICE_SINONIMOS.setdefault(_normalizar(_t), set()).update(
            _normalizar(x) for x in _grupo
        )


def _variantes_de(termino: str) -> set[str]:
    """El termino mas sus sinonimos conocidos (siempre incluye el original)."""
    t = _normalizar(termino)
    return _INDICE_SINONIMOS.get(t, set()) | {t}


def limpiar_query(texto: str, max_palabras: int = 6) -> str:
    """
    Convierte lo que escribio el vendedor en la query que va al buscador del
    proveedor: sin palabras vacias, sin repetir, y corta.

    Por que existe: la IA venia escribiendo la query y le agregaba palabras que
    el catalogo del proveedor no usa. "laptop" se volvia "laptop portatil" y
    Eurocomp pasaba de devolver laptops a devolver parlantes; "calculadora" se
    volvia "calculadora electronica" y Amazon devolvia cero. Los buscadores de
    e-commerce funcionan con AND: cada palabra de mas achica el resultado.

    La IA sigue siendo util — extrae marca, modelo y specs para el matcher —
    pero la query la arma esta funcion, a partir del texto del vendedor y nada
    mas.

        "me gustaria un monitor LG de 16 pulgadas"  ->  "monitor lg 16 pulgadas"
        "necesito 5 laptops HP para oficina"        ->  "laptops hp oficina"
    """
    palabras: list[str] = []
    for tok in _normalizar(texto).split():
        if tok in STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        if tok in palabras:
            continue
        palabras.append(tok)
        if len(palabras) >= max_palabras:
            break
    return " ".join(palabras) or texto.strip()[:120]


def _clave_blacklist(categoria) -> str | None:
    """
    Resuelve qué entrada de BLACKLIST_CATEGORIA aplica a la categoría que
    devolvió la IA.

    Hace falta porque la IA no devuelve categorías de un catálogo cerrado: da
    texto libre como "disco duro externo", "memoria ram" o "computadora de
    escritorio". Con una búsqueda exacta por clave, la blacklist solo se
    activaba para "laptop" y "monitor" — en todo lo demás no se filtraba ni un
    accesorio, y por eso un "ADAPTADOR CONVERSOR USB-C A SATA PARA DISCO"
    llegaba a la pantalla de alguien que pidió un disco duro.

    Se busca la clave conocida más específica que aparezca dentro de la
    categoría: "disco duro externo" resuelve a "disco".
    """
    if not categoria:
        return None

    cat = _normalizar(categoria)
    if cat in BLACKLIST_CATEGORIA:
        return cat

    candidatas = [k for k in BLACKLIST_CATEGORIA if _contiene_palabra(k, cat)]
    if not candidatas:
        return None
    # La más larga es la más específica ("disco duro" gana sobre "disco").
    return max(candidatas, key=len)


def variantes_query(texto: str, solicitado=None, max_variantes: int = 5) -> list[str]:
    """
    Genera la query completa y sus versiones mas cortas, de la mas especifica a
    la mas general. Es la base de la BUSQUEDA EN CASCADA: si una query no trae
    resultados en un proveedor, se reintenta con la siguiente.

    Por que hace falta: los buscadores de e-commerce funcionan con AND, asi que
    cada palabra achica el resultado. Casos medidos en este proyecto:

        Eurocomp  "laptop"                 -> 20 productos
        Eurocomp  "laptop dell 16gb ram"   ->  3 productos
        Eurocomp  "disco duro externo 1tb" ->  0 productos

    Con el vendedor esperando frente a un spinner esto no se podia hacer. Ahora
    que la busqueda corre en segundo plano, se puede intentar tres o cuatro
    veces por proveedor sin que nadie lo note.

    `solicitado` es opcional: si se pasa un ProductoSolicitado, se aprovecha lo
    que extrajo la IA para armar variantes inteligentes — marca mas modelo (lo
    que mejor identifica un producto), categoria mas marca, categoria sola — en
    lugar de solo recortar palabras del final.

        variantes_query("monitor LG de 16 pulgadas", solicitado)
        -> ['monitor lg 16 pulgadas', 'monitor lg 16', 'monitor lg', 'monitor']
    """
    completa = limpiar_query(texto)
    if not completa:
        return []

    candidatas: list[str] = [completa]

    if solicitado is not None:
        marca = getattr(solicitado, "marca", None)
        modelo = getattr(solicitado, "modelo", None)
        categoria = getattr(solicitado, "categoria", None)

        # El modelo es lo que identifica un producto de forma unica.
        if modelo:
            candidatas.append(limpiar_query(f"{marca or ''} {modelo}"))
        if marca and categoria:
            candidatas.append(limpiar_query(f"{categoria} {marca}"))
        if categoria:
            candidatas.append(limpiar_query(str(categoria)))

    # Recortes desde el final, como red de seguridad cuando no hay extraccion.
    tokens = completa.split()
    while len(tokens) > 1:
        tokens = tokens[:-1]
        candidatas.append(" ".join(tokens))

    # Sin repetidos, y de mas palabras a menos: ese es el orden de degradacion.
    vistas: set[str] = set()
    unicas: list[str] = []
    for c in candidatas:
        c = c.strip()
        if c and c not in vistas:
            vistas.add(c)
            unicas.append(c)

    unicas.sort(key=lambda q: -len(q.split()))
    return unicas[:max_variantes]


def _posicion_de(termino: str, texto: str) -> int | None:
    """
    Donde aparece `termino` (o un sinonimo) como palabra completa en `texto`.
    None si no aparece. Se usa para decidir si una palabra de la blacklist
    describe al producto o solo a un accesorio que lo acompaña.
    """
    mejor: int | None = None
    for variante in _variantes_de(termino):
        if not variante:
            continue
        m = re.search(rf"(?<![a-z0-9]){re.escape(variante)}(?![a-z0-9])", texto)
        if m and (mejor is None or m.start() < mejor):
            mejor = m.start()
    return mejor


def _categoria_del_nombre(texto_normalizado: str, max_palabras: int = 4) -> str | None:
    """
    Que ES la cosa, segun la primera categoria conocida que aparece en el texto.

    Se apoya en como nombran los catalogos: el titulo empieza por el tipo de
    producto y despues vienen marca, modelo y especificaciones. Sirve para los
    dos lados — el nombre del candidato y lo que pidio el vendedor — porque el
    criterio es el mismo.

        "memoria ram laptop 16gb patriot"   -> memoria
        "laptop gigabyte aero 14 ... 16gb"  -> laptop
        "disco duro externo 1tb"            -> disco

    Devuelve None si el texto no menciona ninguna categoria conocida, y en ese
    caso no se filtra nada: preferimos dejar pasar un producto de mas antes que
    descartar uno bueno por no reconocer su vocabulario.
    """
    # Solo el comienzo, por lo mismo que la blacklist: lo que viene despues son
    # specs. "INTEL WHITEBOOK ING NO OS CI7-9750H 2.60-4.50/NO RAM/NO DD" es una
    # laptop barebone, y leyendo el nombre entero el "NO RAM" la convertia en
    # memoria.
    inicio = " ".join(texto_normalizado.split()[:max_palabras])

    mejor_categoria: str | None = None
    mejor_posicion: int | None = None
    for clave in SINONIMOS:
        posicion = _posicion_de(clave, inicio)
        if posicion is None:
            continue
        if mejor_posicion is None or posicion < mejor_posicion:
            mejor_posicion = posicion
            mejor_categoria = clave
    return mejor_categoria


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
        "maletin",
        "mochila",
        "bolso",
        "estuche",
        "candado",
        # Accesorios que salian primeros buscando "laptop" en Eurocomp:
        # "SOPORTE PARA LAPTOP STAND NOVA" puntuaba 94 porque dice "laptop".
        "soporte",
        "stand",
        "base",
        "cooler",
        "enfriador",
        "replicador",
        # Componentes que se venden "para laptop" pero no son una laptop.
        "memoria",
        "ram",
        "bateria",
        "pila",
        "disco",
        "ssd",
        "pantalla",
        # Repuestos. Cococo vende "Repuesto Carcasa Dell Superior Laptop
        # Wvftx", que pasaba como 'bueno 75' por decir "laptop" en el nombre.
        "repuesto",
        "carcasa",
        "bisagra",
        "touchpad",
    ],
    "impresora": ["cartucho", "tinta", "toner", "papel", "rodillo"],
    "mouse": ["mousepad", "pad para mouse", "alfombrilla", "receptor"],
    "monitor": ["soporte", "brazo", "cable", "adaptador", "protector", "filtro"],
    "teclado": ["teclas", "keycap", "cover", "funda", "protector"],
    "computadora": ["cable", "adaptador", "funda", "limpiador"],
    "disco": ["adaptador", "conversor", "case", "carcasa", "cable", "enclosure"],
    "memoria": ["adaptador", "lector", "cable"],
    "celular": ["funda", "protector", "vidrio", "cable", "cargador", "soporte"],
    "tableta": ["funda", "protector", "vidrio", "cable", "soporte", "lapiz"],
}


# ---------------------------------------------------------------------
# VARIANTES: palabras que indican que el producto NO es el original
# Si el cliente NO las pidio, penalizamos.
# ---------------------------------------------------------------------
VARIANTES_NEGATIVAS: dict[str, float] = {
    "combo": -15.0,
    "usado": -20.0,
    "usada": -20.0,
    "recertificado": -20.0,
    "recertificada": -20.0,
    "reacondicionado": -20.0,
    "reacondicionada": -20.0,
    "abierto": -10.0,
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
    "lenovo": ["lenovo", "thinkpad", "ideapad"],
    "dell": ["dell"],
    "apple": ["apple", "mac"],
    "samsung": ["samsung"],
    "logitech": ["logitech", "logi"],
}


# ---------------------------------------------------------------------
# TIERS
#
# Umbrales mas permisivos que en v2 (85/70/55). Con cobertura de palabras un
# producto correcto llega naturalmente a 90+, y uno que comparte la categoria
# pero no la marca ronda 45. Los nombres del enum se mantienen para no migrar;
# en el UI conviene rotular 'dudoso' como "Otros resultados del proveedor", que
# describe lo que es sin emitir un juicio que el producto no merece.
# ---------------------------------------------------------------------
TIER_EXACTO = "exacto"  # score >= 85
TIER_BUENO = "bueno"  # 65 - 84
TIER_SIMILAR = "similar"  # 45 - 64
TIER_DUDOSO = "dudoso"  # < 45


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
    if score >= 65:
        return TIER_BUENO
    if score >= 45:
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
    # Palabras del pedido que SI y que NO aparecieron en el producto. Es lo que
    # permite explicarle a un vendedor (o a un cliente) por que un producto
    # quedo donde quedo, sin hablar de porcentajes abstractos.
    palabras_cubiertas: list[str] = None
    palabras_faltantes: list[str] = None

    def __post_init__(self):
        if self.etiquetas_variante is None:
            self.etiquetas_variante = []
        if self.palabras_cubiertas is None:
            self.palabras_cubiertas = []
        if self.palabras_faltantes is None:
            self.palabras_faltantes = []

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
            "palabras_cubiertas": self.palabras_cubiertas,
            "palabras_faltantes": self.palabras_faltantes,
        }
        return d


class Matcher:
    """Calcula matches entre un ProductoSolicitado y una lista de candidatos."""

    def __init__(
        self,
        umbral_minimo: float = 45.0,
        pesos: dict[str, float] | None = None,
    ):
        self.umbral = umbral_minimo
        # El texto (cobertura de palabras) pesa mas que en v2 porque ahora mide
        # algo confiable. Marca y specs ajustan, no deciden.
        self.pesos = pesos or {
            "texto": 0.55,
            "marca": 0.25,
            "specs": 0.20,
        }
        assert abs(sum(self.pesos.values()) - 1.0) < 0.01, "Pesos deben sumar 1.0"

        # Cuando el vendedor escribio la marca a proposito, la marca manda: una
        # HP de 8GB va arriba de una Dell de 16GB. Con los pesos normales las dos
        # quedaban a 2 puntos y en el mismo grupo, porque perder la marca costaba
        # casi lo mismo que perder la spec. No se descarta a las otras marcas,
        # solo bajan de grupo: siguen visibles por si la HP no sirve.
        self.pesos_con_marca = {
            "texto": 0.45,
            "marca": 0.40,
            "specs": 0.15,
        }
        assert abs(sum(self.pesos_con_marca.values()) - 1.0) < 0.01

    # -----------------------------------------------------------------
    def calcular_todos(
        self,
        solicitado: ProductoSolicitado,
        candidatos: list[ProductoEncontrado],
    ) -> list[ResultadoMatch]:
        """
        Puntua todos los candidatos. `candidatos` llega en el orden en que lo
        devolvio el proveedor, y ese orden se usa como desempate.
        """
        terminos = self._terminos_pedido(solicitado)
        generica = self._es_busqueda_generica(solicitado, terminos)

        resultados = [
            self._calcular_uno(solicitado, c, terminos, generica, posicion)
            for posicion, c in enumerate(candidatos)
        ]

        # Descartados al final; entre el resto, score desc. El score ya lleva
        # descontada la posicion del proveedor, asi que ordenar por score
        # preserva el orden original dentro de cada grupo.
        resultados.sort(key=lambda r: (r.descartado, -r.score_total))
        return resultados

    def filtrar_por_umbral(
        self,
        resultados: list[ResultadoMatch],
    ) -> list[ResultadoMatch]:
        return [
            r for r in resultados if not r.descartado and r.score_total >= self.umbral
        ]

    # -----------------------------------------------------------------
    # TERMINOS DEL PEDIDO
    # -----------------------------------------------------------------
    @staticmethod
    def _terminos_pedido(p: ProductoSolicitado) -> list[str]:
        """
        Las palabras que de verdad identifican lo que se pidio, en orden y sin
        repetir. Sale del texto que escribio el vendedor mas marca, linea y
        modelo si la IA los extrajo. Se quitan las palabras vacias: si "me
        gustaria encontrar una laptop hp" entrara completo al conteo, un
        producto correcto tendria 2 de 7 palabras y quedaria descartado.

        Las specs NO entran aca: se evaluan aparte en _score_specs, donde se
        sabe que "16" significa 16 GB y no 16 pulgadas.
        """
        partes: list[str] = []
        if p.query_sugerida:
            partes.append(p.query_sugerida)
        for campo in (p.marca, p.linea, p.modelo, p.categoria):
            if campo:
                partes.append(str(campo))

        vistos: set[str] = set()
        terminos: list[str] = []
        for tok in _normalizar(" ".join(partes)).split():
            if tok in STOPWORDS or tok in vistos:
                continue
            # Tokens de una sola letra no identifican nada, salvo que sean un
            # numero (poco probable, pero inofensivo dejarlos pasar).
            if len(tok) < 2 and not tok.isdigit():
                continue
            vistos.add(tok)
            terminos.append(tok)
        return terminos

    @staticmethod
    def _es_busqueda_generica(p: ProductoSolicitado, terminos: list[str]) -> bool:
        """
        True cuando el vendedor pidio una categoria y nada mas ("calculadora",
        "audifonos", "laptop"). En ese caso no hay nada que discriminar: todos
        los resultados del proveedor son igual de validos, y el buscador del
        proveedor ya los ordeno por relevancia. Pisarlo con un score inventado
        solo degrada resultados correctos.
        """
        if p.marca or p.modelo or p.linea:
            return False
        if p.specs:
            return False
        return len(terminos) <= 1

    # -----------------------------------------------------------------
    def _calcular_uno(
        self,
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
        terminos: list[str],
        generica: bool,
        posicion: int,
    ) -> ResultadoMatch:
        nombre_cand = _normalizar(candidato.nombre)
        texto_cand = _texto_candidato(candidato)

        # ---- Filtro duro: sin precio no se puede cotizar ----
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

        # ---- Filtro duro: blacklist por categoria ----
        # Solo sobre el NOMBRE, y por POSICION: la palabra sospechosa descarta
        # unicamente si aparece ANTES que la categoria pedida. Los titulos de
        # producto empiezan por lo que la cosa es:
        #   "Cable HDMI para monitor"        -> cable antes de monitor: es un cable
        #   "Monitor LG 16MR70 + cable HDMI" -> monitor antes de cable: es un monitor
        # Sin esta regla, el segundo se descartaba entero por mencionar un
        # accesorio incluido, y era justo el producto que el vendedor buscaba.
        cat_key = _clave_blacklist(solicitado.categoria)
        if cat_key:
            palabras_negras = BLACKLIST_CATEGORIA.get(cat_key, [])
            if palabras_negras:
                # La blacklist mira solo el COMIENZO del nombre. Los catalogos
                # ponen primero que ES la cosa y despues las specs, asi que una
                # palabra sospechosa que aparece tarde describe una
                # especificacion, no al producto. Caso medido en CDC:
                #   "INTEL WHITEBOOK ING NO OS CI7-9750H 2.60-4.50/NO RAM/NO DD"
                # es una laptop barebone — el proveedor la categoriza en "LAPTOP
                # INTEL" — y se descartaba por "ser una memoria", porque dice
                # "NO RAM" en la palabra 8. Los accesorios reales que medimos
                # traen la palabra en la primera o segunda: "MEMORIA RAM LAPTOP",
                # "SOPORTE PARA LAPTOP", "MOCHILA PARA LAPTOP DELL".
                inicio_nombre = " ".join(nombre_cand.split()[:4])
                pos_categoria = _posicion_de(cat_key, nombre_cand)
                for palabra in palabras_negras:
                    # Si el vendedor pidio justamente el accesorio, no filtramos.
                    if palabra in terminos:
                        continue
                    pos_negra = _posicion_de(palabra, inicio_nombre)
                    if pos_negra is None:
                        continue
                    if pos_categoria is not None and pos_categoria < pos_negra:
                        continue  # el producto es la categoria; el accesorio viene incluido
                    return ResultadoMatch(
                        producto=candidato,
                        score_total=0.0,
                        score_texto=0.0,
                        score_marca=0.0,
                        score_specs=0.0,
                        descartado=True,
                        razon_descarte=f"es un accesorio: '{palabra}' (blacklist {cat_key})",
                    )

        # ---- Filtro duro: el producto tiene que SER de lo que se pidio ----
        # La cobertura de palabras no distingue estos dos:
        #   "MEMORIA RAM LAPTOP 16GB PATRIOT"  -> una memoria para laptop
        #   "LAPTOP GIGABYTE AERO 14 ... 16GB" -> una laptop con 16GB de RAM
        # Las dos fichas dicen memoria/ram, laptop y 16gb, asi que la laptop
        # daba 4/4 de cobertura y salia 'exacto 100' buscando memorias: un
        # producto de $1,476 marcado como perfecto cuando se pedia un repuesto
        # de $130. Lo que las separa es el orden en que el catalogo las nombra.
        #
        # La categoria pedida sale de la IA cuando esta disponible, y del texto
        # del vendedor cuando no: asi el filtro tambien funciona con USAR_IA=false.
        cat_pedida = _categoria_del_nombre(
            _normalizar(solicitado.categoria or solicitado.query_sugerida or "")
        )
        cat_producto = _categoria_del_nombre(nombre_cand)
        if cat_pedida and cat_producto and cat_pedida != cat_producto:
            return ResultadoMatch(
                producto=candidato,
                score_total=0.0,
                score_texto=0.0,
                score_marca=0.0,
                score_specs=0.0,
                descartado=True,
                razon_descarte=f"el producto es {cat_producto}, no {cat_pedida}",
            )

        # ---- Variantes (COMBO, USADA...): se aplican en los dos modos ----
        ajuste_var, etiquetas = self._ajuste_variantes(solicitado, nombre_cand)

        # ─── MODO GENERICO: se respeta el proveedor, pero no a ciegas ───
        if generica:
            cubiertas = [t for t in terminos if self._termino_presente(t, texto_cand)]

            # Respetar al proveedor no es aceptar relleno. Buscando "laptop",
            # Eurocomp devolvia PARLANTES y DISCOS DUROS: productos que no
            # comparten ni una palabra ni un sinonimo con lo que se pidio. Eso
            # no es vocabulario distinto, es el buscador llenando huecos.
            if terminos and not cubiertas:
                return ResultadoMatch(
                    producto=candidato,
                    score_total=0.0,
                    score_texto=0.0,
                    score_marca=0.0,
                    score_specs=0.0,
                    descartado=True,
                    razon_descarte="relleno del buscador: no coincide en ninguna palabra",
                    palabras_faltantes=list(terminos),
                )

            # El resto entra como 'bueno', en el orden en que vino. El descuento
            # por posicion preserva ese orden al ordenar por score.
            base = 75.0 + ajuste_var
            score = max(0.0, min(100.0, base)) - (posicion * 0.01)
            return ResultadoMatch(
                producto=candidato,
                score_total=score,
                score_texto=100.0 if cubiertas else 0.0,
                score_marca=0.0,
                score_specs=0.0,
                ajuste_variantes=ajuste_var,
                etiquetas_variante=etiquetas,
                tier=clasificar_tier(score),
                palabras_cubiertas=cubiertas,
                palabras_faltantes=[t for t in terminos if t not in cubiertas],
            )

        # ─── MODO ESPECIFICO: el matcher si discrimina ───
        cubiertas: list[str] = []
        faltantes: list[str] = []
        for t in terminos:
            if self._termino_presente(t, texto_cand):
                cubiertas.append(t)
            else:
                faltantes.append(t)

        # Cobertura CERO: ni una palabra del pedido, ni un sinonimo, aparece en
        # el producto. Eso no es "otro vocabulario" — es relleno del buscador.
        # Caso real: Eurocomp, buscando "laptop portatil", devolvia PARLANTES,
        # DISCOS DUROS y AUDIFONOS, y los 16 se guardaban como 'dudoso'.
        # Los sinonimos protegen el caso legitimo: "Wireless Earbuds" SI cubre
        # "audifonos", asi que Amazon en ingles no se ve afectado.
        if terminos and not cubiertas:
            return ResultadoMatch(
                producto=candidato,
                score_total=0.0,
                score_texto=0.0,
                score_marca=0.0,
                score_specs=0.0,
                descartado=True,
                razon_descarte="relleno del buscador: no coincide en ninguna palabra",
                palabras_faltantes=faltantes,
            )

        score_texto = (len(cubiertas) / len(terminos) * 100) if terminos else 70.0
        score_marca = self._score_marca(solicitado, candidato, texto_cand)
        score_specs = self._score_specs(solicitado, candidato, texto_cand)

        pesos = self.pesos_con_marca if solicitado.marca else self.pesos
        score_base = (
            pesos["texto"] * score_texto
            + pesos["marca"] * score_marca
            + pesos["specs"] * score_specs
        )

        bonus_identidad = self._bonus_identidad(solicitado, texto_cand)
        # Todas las palabras presentes: el producto es, al menos de nombre,
        # exactamente lo que se pidio.
        bonus_contenida = 8.0 if terminos and not faltantes else 0.0

        score_total = max(
            0.0,
            min(100.0, score_base + ajuste_var + bonus_identidad + bonus_contenida),
        ) - (posicion * 0.01)
        score_total = max(0.0, score_total)

        return ResultadoMatch(
            producto=candidato,
            score_total=score_total,
            score_texto=score_texto,
            score_marca=score_marca,
            score_specs=score_specs,
            ajuste_variantes=ajuste_var,
            bonus_identidad=bonus_identidad,
            bonus_contenida=bonus_contenida,
            etiquetas_variante=etiquetas,
            tier=clasificar_tier(score_total),
            palabras_cubiertas=cubiertas,
            palabras_faltantes=faltantes,
        )

    # -----------------------------------------------------------------
    # ¿ESTA ESTA PALABRA EN EL PRODUCTO?
    # -----------------------------------------------------------------
    @staticmethod
    def _termino_presente(termino: str, texto_cand: str) -> bool:
        """
        Un termino cuenta como presente si se cumple cualquiera de estas:

          1. Aparece como palabra completa.
          2. Aparece alguno de sus sinonimos ("audifonos" ~ "earbuds").
          3. Su forma compactada esta contenida — para modelos escritos de
             formas distintas: "16MR70" encuentra a "16MR70-B" y a "16 MR70".
          4. Hay una palabra en el producto que se le parece mucho (>= 88), lo
             que cubre plurales y erratas: "audifono" / "audifonos",
             "inalambrico" / "inalambricos".

        Es deliberadamente generoso: el proveedor ya decidio que el producto es
        relevante cuando lo devolvio en la busqueda. El trabajo del matcher es
        ordenar, no volver a filtrar lo que el buscador ya acepto.
        """
        # 1 y 2
        for variante in _variantes_de(termino):
            if variante and _contiene_palabra(variante, texto_cand):
                return True

        # 3 — solo para tokens largos, o "16" encontraria cualquier cosa
        compacto = _compactar(termino)
        if len(compacto) >= 4 and compacto in _compactar(texto_cand):
            return True

        # 4 — difuso por palabra, nunca sobre la frase completa
        if len(termino) >= 5:
            for palabra in texto_cand.split():
                if len(palabra) >= 4 and fuzz.ratio(termino, palabra) >= 88:
                    return True

        return False

    # -----------------------------------------------------------------
    @staticmethod
    def _score_marca(
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
        texto_cand: str,
    ) -> float:
        if not solicitado.marca:
            return 70.0  # sin marca pedida = neutral favorable

        marca_ped = _normalizar(solicitado.marca)
        aliases = EQUIVALENCIAS_MARCA.get(marca_ped, [marca_ped])

        # 1. Contra la marca que extrajo el scraper
        if candidato.marca:
            marca_cand = _normalizar(candidato.marca)
            if any(_contiene_palabra(a, marca_cand) for a in aliases):
                return 100.0

        # 2. Contra nombre + descripcion + specs, por palabra completa
        #    ("lg" NO debe coincidir dentro de "pulgadas")
        if any(_contiene_palabra(a, texto_cand) for a in aliases):
            return 100.0

        # Marca pedida y ausente: es otra marca. No es cero absoluto porque el
        # proveedor igual lo considero relevante, pero si una señal fuerte.
        return 10.0

    # -----------------------------------------------------------------
    def _score_specs(
        self,
        solicitado: ProductoSolicitado,
        candidato: ProductoEncontrado,
        texto_cand: str,
    ) -> float:
        if not solicitado.specs:
            return 70.0  # sin specs pedidas = neutral favorable

        specs_cand = candidato.specs or {}
        aciertos = 0
        total = 0

        for clave, valor in solicitado.specs.items():
            if valor is None or valor is False:
                continue
            total += 1
            if clave in specs_cand and _normalizar(
                specs_cand.get(clave)
            ) == _normalizar(valor):
                aciertos += 1
                continue
            if _spec_en_nombre(clave, valor, texto_cand):
                aciertos += 1

        if total == 0:
            return 70.0
        return (aciertos / total) * 100

    # -----------------------------------------------------------------
    @staticmethod
    def _ajuste_variantes(
        solicitado: ProductoSolicitado,
        nombre_cand: str,
    ) -> tuple[float, list[str]]:
        """
        Penaliza cuando el candidato dice COMBO/USADA/RECERTIFICADA y el cliente
        no lo pidio. Solo mira el NOMBRE y por palabra completa.
        """
        texto_pedido = _normalizar(
            (solicitado.query_sugerida or "")
            + " "
            + (solicitado.modelo or "")
            + " "
            + (solicitado.linea or "")
        )

        ajuste = 0.0
        etiquetas: list[str] = []
        vistas: set[str] = set()

        for palabra, penalizacion in VARIANTES_NEGATIVAS.items():
            if _contiene_palabra(palabra, nombre_cand):
                etiqueta = ETIQUETAS_VARIANTE[palabra]
                if etiqueta not in vistas:
                    etiquetas.append(etiqueta)
                    vistas.add(etiqueta)
                if not _contiene_palabra(palabra, texto_pedido):
                    ajuste += penalizacion

        return ajuste, etiquetas

    # -----------------------------------------------------------------
    @staticmethod
    def _bonus_identidad(
        solicitado: ProductoSolicitado,
        texto_cand: str,
    ) -> float:
        """
        Marca + (linea o modelo) presentes los dos: identidad clara, +10.
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
            if _contiene_palabra(c, texto_cand) or (
                len(_compactar(c)) >= 4 and _compactar(c) in compacto
            ):
                return 10.0

        return 0.0


# ---------------------------------------------------------------------
# Helpers para detectar specs en texto libre
# ---------------------------------------------------------------------
def _spec_en_nombre(clave: str, valor: Any, texto: str) -> bool:
    """Busca una spec pedida dentro del texto (ya normalizado) del candidato."""
    texto = _normalizar(texto)
    if clave == "ram_gb":
        return _contiene_gb(texto, valor)
    if clave == "almacenamiento_gb":
        return _contiene_gb(texto, valor) or _contiene_ssd_hdd(texto, valor)
    if clave == "pantalla_pulgadas":
        num = _normalizar_numero(str(valor))
        return re.search(rf"(?<![\d.]){re.escape(num)} pulgadas\b", texto) is not None
    if clave in ("cpu", "almacenamiento_tipo"):
        return _contiene_palabra(_normalizar(valor), texto)
    if valor is True:
        return _contiene_palabra(_normalizar(clave.replace("_", " ")), texto)
    return _contiene_palabra(_normalizar(valor), texto)


def _contiene_gb(texto: str, valor: int | float) -> bool:
    num = _normalizar_numero(str(valor))
    patterns = [rf"(?<![\d.]){re.escape(num)}\s*-?\s*gb\b"]
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
