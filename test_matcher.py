"""
Pruebas de regresion del Matcher (v2.1).

Uso:
    python test_matcher.py
    (o con pytest: pytest test_matcher.py)
"""

from matcher import Matcher, _normalizar
from modelo import ProductoEncontrado, ProductoSolicitado

m = Matcher(umbral_minimo=55.0)

MONITOR_LG_16 = ProductoSolicitado(
    categoria="monitor",
    marca="LG",
    specs={"pantalla_pulgadas": 16},
    query_sugerida="monitor 16 pulgadas LG",
)

LAPTOP_LENOVO = ProductoSolicitado(
    categoria="laptop",
    marca="Lenovo",
    linea="ThinkPad",
    specs={"ram_gb": 16, "almacenamiento_gb": 512, "cpu": "i7"},
    query_sugerida="Lenovo ThinkPad i7 16GB",
)


_SIN = object()  # centinela: "usar precio por defecto"


def _cand(nombre: str, descripcion: str = "", marca: str | None = None, specs=None, precio=_SIN):
    return ProductoEncontrado(
        proveedor="test",
        nombre=nombre,
        url_detalle="http://test",
        descripcion_corta=descripcion or None,
        marca=marca,
        specs=specs or {},
        precio=125000.0 if precio is _SIN else precio,
        moneda="CRC",
    )


def _match(sol, nombre, **kw):
    return m._calcular_uno(sol, _cand(nombre, **kw))


# ---------------------------------------------------------------------
# Normalizacion
# ---------------------------------------------------------------------
def test_normaliza_pulgadas():
    for txt in ['16"', "16''", "16 pulg", "16 pulg.", "16-inch", "16 inch", '16.0"', "16”", "16 pulgadas"]:
        assert "16 pulgadas" in _normalizar(txt), txt


def test_normaliza_capacidad():
    assert _normalizar("16 GB") == "16gb"
    assert _normalizar("16-gb") == "16gb"
    assert _normalizar("1 TB") == "1tb"


# ---------------------------------------------------------------------
# Monitor LG 16 pulgadas: variantes de escritura -> EXACTO
# ---------------------------------------------------------------------
def test_monitor_variantes_exacto():
    nombres = [
        "MONITOR PORTATIL LG 16 PULGADAS WQXGA",
        'MONITOR PORTATIL LG 16MR70 16" IPS',
        "MONITOR PORTATIL LG 16MR70 16 pulg",
        'MONITOR PORTATIL LG 16.0" WQXGA',
        "LG 16 PULGADAS MONITOR PORTATIL",
        "Monitor Portátil LG 16-inch IPS",
    ]
    for n in nombres:
        r = _match(MONITOR_LG_16, n)
        assert r.tier == "exacto", f"{n} -> {r.tier} ({r.score_total:.1f})"


def test_monitor_datos_en_descripcion_exacto():
    r = _match(MONITOR_LG_16, "MONITOR PORTATIL 16MR70", descripcion="LG 16 pulgadas IPS")
    assert r.tier == "exacto", r.score_total


def test_monitor_datos_en_specs_del_proveedor():
    r = _match(MONITOR_LG_16, "MONITOR PORTATIL 16MR70", specs={"Marca": "LG", "Pantalla": '16"'})
    assert r.score_marca == 100.0 and r.score_specs == 100.0
    assert r.tier == "exacto", r.score_total


# ---------------------------------------------------------------------
# Negativos: NO deben quedar como exacto
# ---------------------------------------------------------------------
def test_lg_no_coincide_con_pulgadas():
    # Antes: "lg" in "pulgadas" == True -> marca 100 por error
    r = _match(MONITOR_LG_16, "MONITOR SAMSUNG 16 PULGADAS")
    assert r.score_marca == 0.0
    assert r.tier != "exacto", r.score_total


def test_tamano_distinto_no_es_exacto():
    r = _match(MONITOR_LG_16, "MONITOR LG 27 PULGADAS IPS")
    assert r.score_specs == 0.0
    assert r.tier != "exacto", r.score_total


def test_15_6_no_cuenta_como_16():
    r = _match(MONITOR_LG_16, 'MONITOR LG 15.6" IPS')
    assert r.score_specs == 0.0


def test_accesorio_descartado():
    r = _match(MONITOR_LG_16, "SOPORTE PARA MONITOR LG 16 PULGADAS")
    assert r.descartado


def test_descripcion_con_cable_no_descarta():
    r = _match(MONITOR_LG_16, "MONITOR LG 16 PULGADAS", descripcion="Incluye cable USB-C")
    assert not r.descartado


def test_combo_penalizado():
    r = _match(MONITOR_LG_16, "MONITOR LG 16 PULGADAS COMBO")
    assert "COMBO" in r.etiquetas_variante and r.ajuste_variantes < 0


def test_demo_no_detectado_dentro_de_otra_palabra():
    r = _match(MONITOR_LG_16, "MONITOR LG 16 PULGADAS DEMOLEDOR")
    assert r.etiquetas_variante == []


# ---------------------------------------------------------------------
# Laptops: RAM / almacenamiento / CPU
# ---------------------------------------------------------------------
def test_laptop_specs_exacto():
    r = _match(LAPTOP_LENOVO, "LAPTOP LENOVO THINKPAD E14 CORE I7-1255U 16 GB 512GB SSD")
    assert r.score_specs == 100.0
    assert r.bonus_identidad == 10.0
    assert r.tier == "exacto", r.score_total


def test_laptop_ram_distinta():
    r = _match(LAPTOP_LENOVO, "LAPTOP LENOVO THINKPAD E14 CORE I7 8GB 512GB SSD")
    assert r.score_specs < 100.0


def test_almacenamiento_en_tb():
    sol = ProductoSolicitado(categoria="laptop", specs={"almacenamiento_gb": 1024}, query_sugerida="laptop 1TB")
    r = _match(sol, "LAPTOP DELL 16GB 1TB SSD")
    assert r.score_specs == 100.0


# ---------------------------------------------------------------------
# Sin precio visible -> descartado
# ---------------------------------------------------------------------
def test_sin_precio_none_es_dudoso():
    r = _match(MONITOR_LG_16, "MONITOR LG 16 PULGADAS IPS", precio=None)
    assert r.tier == "dudoso", r.tier
    assert r.score_total <= 50.0
    assert r.descartado
    assert r.razon_descarte == "sin_precio_visible"


def test_precio_cero_es_dudoso():
    r = _match(MONITOR_LG_16, "MONITOR LG 16 PULGADAS IPS", precio=0.0)
    assert r.tier == "dudoso" and r.descartado


def test_con_precio_sigue_exacto():
    r = _match(MONITOR_LG_16, "MONITOR LG 16 PULGADAS IPS", precio=125000.0)
    assert r.tier == "exacto", r.score_total


def test_sin_precio_no_agrega_etiqueta():
    # La UI ya muestra "sin precio visible"; el matcher no agrega etiqueta
    r = _match(MONITOR_LG_16, "MONITOR LG 16 PULGADAS IPS", precio=None)
    assert r.etiquetas_variante == []


def test_sin_precio_queda_al_final():
    cands = [
        _cand("MONITOR LG 16 PULGADAS IPS", precio=None),  # match perfecto sin precio
        _cand("MONITOR SAMSUNG 24 PULGADAS", precio=90000.0),  # match malo con precio
        _cand("MONITOR LG 16 PULGADAS WQXGA", precio=150000.0),
    ]
    res = m.calcular_todos(MONITOR_LG_16, cands)
    assert res[-1].producto.precio is None
    assert res[0].producto.precio == 150000.0
    # Sin precio se descarta; los productos cotizables se conservan
    assert res[-1].descartado
    assert all(not r.descartado for r in res[:-1])


import unittest
from unittest.mock import patch
import db
from matcher import Matcher, _normalizar
from modelo import ProductoSolicitado, ProductoEncontrado

class MatcherTests(unittest.TestCase):
    def test_regresiones(self):
        m = Matcher()
        pedido = ProductoSolicitado(categoria='monitor', marca='LG', specs={'pantalla_pulgadas': 16}, query_sugerida='monitor 16 pulgadas LG')
        for nombre, descripcion in [('MONITOR LG 16MR70 16 pulg', None), ('MONITOR LG 16.0" WQXGA', None), ('MONITOR LG 16MR70 16" IPS', None), ('MONITOR PORTATIL 16MR70', 'LG 16 pulgadas')]:
            with self.subTest(nombre=nombre):
                c = ProductoEncontrado('test', nombre, '', precio=100, descripcion_corta=descripcion)
                self.assertEqual(m._calcular_uno(pedido, c).tier, 'exacto')
        c = ProductoEncontrado('test', 'MONITOR SAMSUNG 16 pulgadas', '', precio=100)
        self.assertEqual(m._calcular_uno(pedido, c).score_marca, 0)
        for precio in [None, 0, -1]:
            c = ProductoEncontrado('test', 'MONITOR LG 16 pulgadas', '', precio=precio)
            r = m._calcular_uno(pedido, c)
            self.assertEqual(r.tier, 'dudoso')
            self.assertEqual(r.score_total, 0)
            self.assertTrue(r.descartado)
            self.assertEqual(r.razon_descarte, "sin_precio_visible")
            self.assertEqual(m.filtrar_por_umbral([r]), [])
        for unidad in ['16"', "16''", '16 pulg', '16-inch', '16.0”']:
            self.assertEqual(_normalizar(unidad), '16 pulgadas')
        self.assertEqual(_normalizar('1 TB'), '1tb')
        self.assertEqual(_normalizar('16-gb'), '16gb')

    def test_persistencia_sin_precio(self):
        for precio in [None, 0, -1]:
            with self.subTest(precio=precio), patch.object(db, 'get_conn') as conexion:
                conn = conexion.return_value.__enter__.return_value
                producto = {'precio': precio, 'match': {'tier': 'exacto', 'descartado': False}}
                self.assertEqual(db.guardar_productos_encontrados('item', 'ejecucion', [producto]), 0)
                conn.execute.assert_not_called()

    def test_persistencia_precio_positivo(self):
        producto = ProductoEncontrado('test', 'Monitor', '', precio=100).to_dict()
        producto['match'] = {'tier': 'exacto', 'descartado': False}
        with patch.object(db, 'get_conn') as conexion:
            self.assertEqual(db.guardar_productos_encontrados('item', 'ejecucion', [producto]), 1)
            conexion.return_value.__enter__.return_value.execute.assert_called_once()

    def test_blacklist_tiene_prioridad(self):
        r = Matcher()._calcular_uno(ProductoSolicitado(categoria='monitor'), ProductoEncontrado('test', 'cable monitor', '', precio=None))
        self.assertTrue(r.descartado)
        self.assertIn('blacklist', r.razon_descarte)


if __name__ == "__main__":
    pruebas = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    fallos = 0
    for nombre, fn in pruebas:
        try:
            fn()
            print(f"  OK    {nombre}")
        except AssertionError as e:
            fallos += 1
            print(f"  FALLO {nombre}: {e}")
    print(f"\n{len(pruebas) - fallos}/{len(pruebas)} pruebas OK")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MatcherTests)
    resultado = unittest.TextTestRunner().run(suite)
    raise SystemExit(1 if fallos or not resultado.wasSuccessful() else 0)
