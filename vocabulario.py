"""
vocabulario.py — Descubre el vocabulario que le falta al matcher.

QUE HACE, EN UNA LINEA
──────────────────────
Lee las busquedas y los productos que YA estan en tu base de datos, y te dice
que palabras escriben los vendedores que el matcher no esta reconociendo en los
titulos de los proveedores — y que palabra usa cada proveedor en su lugar.

PARA QUE SIRVE
──────────────
Cada proveedor titula sus productos con su propio vocabulario. Medido en este
proyecto, para la misma cosa:

    Eurocomp  ->  "HD SOLIDO EXTERNO 1TB"
    CDC       ->  "DD SOLIDO KINGSTON 2TB"
    Amazon    ->  "Samsung SSD portatil T7 1TB"

Ese caso lo descubrimos de casualidad, mirando una pantalla. Mañana alguien
busca un toner y CDC lo titula "CARTUCHO", o busca un UPS y lo llaman "NO
BREAK", y nadie se entera: el producto correcto queda en el ultimo grupo y el
vendedor cree que el proveedor no lo tiene.

Este script invierte eso: en lugar de que vos encuentres el problema, el
sistema te lo señala. Y como trabaja sobre los nombres reales de TUS
proveedores, cuanto mas se use el cotizador, mas vocabulario descubre.

NO escribe nada. Solo lee. Se puede correr cuantas veces quieras.

CUANDO CORRERLO
───────────────
Al mes o dos meses de estar en produccion, y despues cada tanto. Antes de eso
hay pocos datos y las sugerencias son ruido.

COMO SE USA
───────────
  Lo de los ultimos 30 dias (lo normal):
    python vocabulario.py

  Un periodo mas largo, para tener mas datos:
    python vocabulario.py --dias 90

  Un solo proveedor:
    python vocabulario.py --proveedor cdc

  Bajar el minimo de casos, cuando hay pocos datos todavia:
    python vocabulario.py --min-casos 2

COMO LEER LA SALIDA
───────────────────
Por cada termino que el matcher no reconoce, te muestra en cuantos productos
fallo y que palabras aparecen en esos productos. Una palabra que aparece en
casi todos es casi seguro el sinonimo que falta.

Lo que el script NO hace es decidir por vos: sugiere, y vos miras si tiene
sentido antes de agregarlo a SINONIMOS en matcher.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict

from db import get_conn
from matcher import (
    _INDICE_SINONIMOS,
    EQUIVALENCIAS_MARCA,
    STOPWORDS,
    Matcher,
    _normalizar,
    _variantes_de,
)
from modelo import ProductoSolicitado

# Palabras que aparecen en casi cualquier titulo de producto de tecnologia y no
# sirven como sinonimo de nada. Sin esto, "usb" y "pulg" saldrian sugeridos en
# cada reporte.
RUIDO = {
    "usb",
    "sata",
    "pulg",
    "pulgadas",
    "gen",
    "tipo",
    "serie",
    "pro",
    "plus",
    "max",
    "mini",
    "new",
    "nuevo",
    "original",
    "negro",
    "blanco",
    "gris",
    "color",
    "para",
    "con",
    "sin",
    "und",
    "uds",
    "pcs",
    "kit",
    "oem",
    "ghz",
    "mhz",
    "rpm",
    "mbps",
    "gbps",
    "watts",
    "volt",
    "led",
    "lcd",
    "hd",
    "fhd",
    "uhd",
    "ips",
    "bluetooth",
    "wifi",
    "inalambrico",
    "alambrico",
}

# Marcas conocidas: tampoco son sinonimos de categorias.
MARCAS = {m for m in EQUIVALENCIAS_MARCA} | {
    a for lista in EQUIVALENCIAS_MARCA.values() for a in lista
}

_RE_SOLO_NUMEROS = re.compile(r"^\d+([.,]\d+)?(gb|tb|mb|kb|w|v|k|x)?$")


def _es_palabra_util(palabra: str) -> bool:
    """
    Filtra lo que nunca va a ser un sinonimo util.

    El minimo son DOS letras, no tres: las abreviaturas que usan los
    proveedores son justamente cortas — "DD" (CDC), "HD" (Eurocomp), "PC".
    Con un minimo de tres, el script no habria encontrado el caso que lo
    motivo.
    """
    if len(palabra) < 2:
        return False
    if palabra in STOPWORDS or palabra in RUIDO or palabra in MARCAS:
        return False
    if _RE_SOLO_NUMEROS.match(palabra):
        return False
    # Codigos de modelo tipo "16mr70b", "ai198r": mezcla de letras y numeros.
    if any(c.isdigit() for c in palabra) and any(c.isalpha() for c in palabra):
        return False
    return True


def _es_termino_analizable(termino: str, marca_pedida: str | None) -> bool:
    """
    Decide si vale la pena buscarle sinonimos a un termino del pedido.

    Solo interesan las palabras que describen QUE es el producto: "disco",
    "toner", "monitor". Las marcas y las specs se excluyen — sin esto, buscar
    "toner hp 85a" generaba la sugerencia absurda de que "hp" es sinonimo de
    "cartucho", solo porque los productos de CDC decian CARTUCHO y ninguno
    decia HP en el titulo.
    """
    if len(termino) < 3:
        return False
    # Specs y codigos: 1tb, 16gb, 85a, 5520, i7
    if any(c.isdigit() for c in termino):
        return False
    if termino in MARCAS or termino in RUIDO:
        return False
    if marca_pedida and termino == _normalizar(marca_pedida):
        return False
    return True


# ---------------------------------------------------------------------
def leer_datos(dias: int, proveedor: str | None) -> list[dict]:
    """Trae las busquedas y los productos que llegaron a la pantalla."""
    filtro_proveedor = "AND pe.proveedor_codigo = %s" if proveedor else ""
    params: list = [dias]
    if proveedor:
        params.append(proveedor)

    with get_conn() as conn:
        filas = conn.execute(
            f"""
            SELECT si.id              AS item_id,
                   si.query_scraper,
                   si.categoria,
                   si.marca           AS marca_pedida,
                   si.linea,
                   si.modelo,
                   pe.proveedor_codigo,
                   pe.nombre,
                   pe.marca           AS marca_producto,
                   pe.descripcion_corta,
                   pe.match_tier
            FROM productos_encontrados pe
            JOIN solicitud_items si ON si.id = pe.solicitud_item_id
            WHERE pe.creado_en >= now() - make_interval(days => %s)
              {filtro_proveedor}
            ORDER BY si.id
            """,
            params,
        ).fetchall()
    return filas


def analizar(filas: list[dict], min_casos: int) -> dict:
    """
    Por cada termino del pedido que el matcher NO reconoce en un producto que
    igual llego a la pantalla, acumula que palabras usa ese producto.

    La logica: si el producto se guardo, el proveedor lo considero relevante y
    el matcher lo dejo pasar. Que un termino del pedido no aparezca ahi suele
    significar que el proveedor lo llama de otra forma.
    """
    # termino -> proveedor -> {"fallos": n, "total": n, "palabras": Counter}
    hallazgos: dict = defaultdict(
        lambda: defaultdict(lambda: {"fallos": 0, "total": 0, "palabras": Counter()})
    )
    busquedas_por_termino: dict[str, set] = defaultdict(set)
    # Terminos que son la CATEGORIA de lo que se pidio ("silla", "disco").
    # Solo a esos se les sugiere sinonimo: son los que definen QUE es el
    # producto. Sin esta distincion, buscar "silla ergonomica oficina" sugeria
    # que "butaca" es sinonimo de "ergonomica", de "oficina" y de "escritorio",
    # cuando el unico que importa es "silla".
    terminos_categoria: set[str] = set()

    # Agrupar por item para calcular los terminos una sola vez por busqueda.
    por_item: dict = defaultdict(list)
    for f in filas:
        por_item[f["item_id"]].append(f)

    for item_id, productos in por_item.items():
        primero = productos[0]
        solicitado = ProductoSolicitado(
            categoria=primero.get("categoria"),
            marca=primero.get("marca_pedida"),
            linea=primero.get("linea"),
            modelo=primero.get("modelo"),
            specs={},
            query_sugerida=primero.get("query_scraper") or "",
        )
        marca_pedida = primero.get("marca_pedida")
        terminos = [
            t
            for t in Matcher._terminos_pedido(solicitado)
            if _es_termino_analizable(t, marca_pedida)
        ]
        if not terminos:
            continue

        de_categoria = set(_normalizar(primero.get("categoria") or "").split())
        terminos_categoria.update(t for t in terminos if t in de_categoria)

        for p in productos:
            texto = _normalizar(
                " ".join(
                    x
                    for x in (
                        p.get("nombre"),
                        p.get("marca_producto"),
                        p.get("descripcion_corta"),
                    )
                    if x
                )
            )
            palabras_producto = [w for w in texto.split() if _es_palabra_util(w)]

            for termino in terminos:
                prov = p["proveedor_codigo"]
                registro = hallazgos[termino][prov]
                registro["total"] += 1
                busquedas_por_termino[termino].add(item_id)

                if Matcher._termino_presente(termino, texto):
                    continue

                registro["fallos"] += 1
                # Las palabras de ESTE producto, sin repetir: nos interesa en
                # cuantos productos aparece cada una, no cuantas veces en total.
                for w in set(palabras_producto):
                    registro["palabras"][w] += 1

    # Quedarse solo con lo que pasa seguido: un fallo aislado es ruido.
    resultado = {}
    for termino, por_prov in hallazgos.items():
        relevantes = {
            prov: datos
            for prov, datos in por_prov.items()
            if datos["fallos"] >= min_casos
        }
        if relevantes:
            resultado[termino] = {
                "proveedores": relevantes,
                "busquedas": len(busquedas_por_termino[termino]),
                "es_categoria": termino in terminos_categoria,
            }
    return resultado


def imprimir(resultado: dict, min_casos: int) -> None:
    if not resultado:
        print(
            "\nNo hay terminos con problemas de vocabulario en este periodo.\n"
            "Puede ser buena señal, o que todavia haya pocos datos. Probá con\n"
            "--dias 90 o con --min-casos 2."
        )
        return

    # Ordenar por impacto: los que fallan en mas productos primero.
    def impacto(item):
        return -sum(d["fallos"] for d in item[1]["proveedores"].values())

    print("\n" + "=" * 78)
    print("TERMINOS QUE EL MATCHER NO ESTA RECONOCIENDO")
    print("=" * 78)
    print(
        "Cada bloque es una palabra que los vendedores escriben y que no aparece\n"
        "en los productos que igual llegaron a la pantalla. Debajo, las palabras\n"
        "que usa ese proveedor en su lugar.\n"
    )

    sugerencias: list[tuple[str, str, str, int]] = []

    for termino, datos in sorted(resultado.items(), key=impacto):
        total_fallos = sum(d["fallos"] for d in datos["proveedores"].values())
        print("-" * 78)
        etiqueta = " [categoría]" if datos["es_categoria"] else ""
        print(
            f"TERMINO: '{termino}'{etiqueta}   "
            f"(pedido en {datos['busquedas']} búsqueda(s), "
            f"no reconocido en {total_fallos} producto(s))"
        )

        conocidos = _variantes_de(termino)
        if len(conocidos) > 1:
            print(f"  sinónimos que ya conoce: {', '.join(sorted(conocidos))}")

        for prov, d in sorted(
            datos["proveedores"].items(), key=lambda x: -x[1]["fallos"]
        ):
            pct = 100 * d["fallos"] / d["total"] if d["total"] else 0
            print(
                f"\n  [{prov}] no reconocido en {d['fallos']} de "
                f"{d['total']} productos ({pct:.0f}%)"
            )

            # Se excluye el vocabulario que YA esta en SINONIMOS, aunque sea de
            # otra categoria. Sin esto, buscando "disco duro externo" el script
            # sugeria que "dd" es sinonimo de "externo": como "disco" y "duro"
            # ya se reconocen gracias a "dd", el unico termino que falla es
            # "externo" y se le atribuian todas las palabras del producto.
            candidatas = [
                (w, n)
                for w, n in d["palabras"].most_common(10)
                if w not in conocidos and w != termino and w not in _INDICE_SINONIMOS
            ]
            if not candidatas:
                print("      (sin palabras candidatas claras)")
                continue

            for palabra, veces in candidatas[:5]:
                frecuencia = 100 * veces / d["fallos"]
                marca = ""
                if frecuencia >= 70:
                    marca = "  ← candidato fuerte"
                    if datos["es_categoria"]:
                        sugerencias.append((termino, palabra, prov, int(frecuencia)))
                elif frecuencia >= 40:
                    marca = "  ← posible"
                print(
                    f"      {palabra:<18} en {veces:3d} de {d['fallos']:3d} "
                    f"({frecuencia:3.0f}%){marca}"
                )
        print()

    if sugerencias:
        print("=" * 78)
        print("SUGERENCIAS PARA SINONIMOS")
        print("=" * 78)
        print(
            "Revisalas a ojo antes de agregarlas. El script no entiende de\n"
            "productos: solo cuenta palabras. Una sugerencia puede ser correcta\n"
            "('disco' = 'dd') o puede ser una coincidencia sin sentido.\n"
        )
        agrupadas: dict[str, set] = defaultdict(set)
        for termino, palabra, prov, pct in sugerencias:
            agrupadas[termino].add(palabra)

        for termino, palabras in sorted(agrupadas.items()):
            actuales = sorted(_variantes_de(termino))
            propuesto = sorted(set(actuales) | palabras)
            print(f"  '{termino}' → agregar: {', '.join(sorted(palabras))}")
            print(f"      quedaría: {{{', '.join(repr(p) for p in propuesto)}}}")
            print()

        print(
            "Para aplicarlas: en matcher.py, buscá el diccionario SINONIMOS y\n"
            "sumá las palabras al grupo que corresponda. Después reiniciá el worker."
        )


def resumen_salud(filas: list[dict]) -> None:
    """Foto general: cuantos productos aporta cada proveedor y en que tier."""
    if not filas:
        return
    por_prov: dict = defaultdict(Counter)
    for f in filas:
        por_prov[f["proveedor_codigo"]][f["match_tier"]] += 1

    print("=" * 78)
    print("APORTE POR PROVEEDOR EN EL PERIODO")
    print("=" * 78)
    print(
        f"  {'proveedor':<16}{'total':>8}{'exacto':>9}{'bueno':>8}{'similar':>9}{'dudoso':>8}"
    )
    for prov, tiers in sorted(por_prov.items(), key=lambda x: -sum(x[1].values())):
        total = sum(tiers.values())
        print(
            f"  {prov:<16}{total:>8}{tiers.get('exacto', 0):>9}"
            f"{tiers.get('bueno', 0):>8}{tiers.get('similar', 0):>9}"
            f"{tiers.get('dudoso', 0):>8}"
        )
    print()


# ---------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Descubre el vocabulario que le falta al matcher, "
        "a partir de los datos que ya estan en la base"
    )
    parser.add_argument(
        "--dias",
        type=int,
        default=30,
        help="cuántos días hacia atrás analizar (por defecto 30)",
    )
    parser.add_argument(
        "--proveedor",
        help="analizar un solo proveedor (por ejemplo: cdc)",
    )
    parser.add_argument(
        "--min-casos",
        type=int,
        default=3,
        help="mínimo de productos donde debe fallar un término para reportarlo "
        "(por defecto 3; bajalo si hay pocos datos)",
    )
    args = parser.parse_args()

    print(f"Analizando los últimos {args.dias} días...")
    if args.proveedor:
        print(f"Solo el proveedor: {args.proveedor}")

    try:
        filas = leer_datos(args.dias, args.proveedor)
    except Exception as e:
        print(f"✗ No pude leer la base de datos: {e}")
        return 1

    if not filas:
        print(
            "\nNo hay productos guardados en ese período.\n"
            "Probá con --dias 90, o esperá a tener más búsquedas hechas."
        )
        return 0

    items = len({f["item_id"] for f in filas})
    print(f"  {len(filas)} productos de {items} búsqueda(s)\n")

    resumen_salud(filas)
    imprimir(analizar(filas, args.min_casos), args.min_casos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
