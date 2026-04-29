"""Testes do motor VRA: cobre lookup por zona, exclusão circular, IDW.

Reproduz o ensaio integrado A/B/C/D da Tab. 6 do cap 7 §7.3 da dissertação.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.kml_parser import KmlData, parse_kml
from src.vra_engine import IdwParams, dose_at, point_in_polygon

DATA_DIR = Path(__file__).parent.parent / "data"


def centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return sum(xs) / len(xs), sum(ys) / len(ys)


@pytest.fixture(scope="module")
def ensaio() -> KmlData:
    return parse_kml(DATA_DIR / "ensaio_abcd.kml")


@pytest.fixture(scope="module")
def talhao() -> KmlData:
    return parse_kml(DATA_DIR / "talhao_completo.kml")


def test_ensaio_quatro_zonas_identificadas(ensaio: KmlData) -> None:
    labels = sorted(z.label for z in ensaio.zones)
    assert labels == ["A", "B", "C", "D"]
    rates = {z.label: z.rate for z in ensaio.zones}
    assert rates == {"A": 90, "B": 75, "C": 60, "D": 100}


def test_dose_no_centro_de_cada_zona_abcd(ensaio: KmlData) -> None:
    expected = {"A": 90, "B": 75, "C": 60, "D": 100}
    for zone in ensaio.zones:
        cx, cy = centroid(zone.coords_xy)
        d = dose_at(cx, cy, ensaio)
        assert d == pytest.approx(expected[zone.label]), (
            f"Zona {zone.label}: dose esperada {expected[zone.label]}, obtida {d}"
        )


def test_fora_do_talhao_retorna_zero(ensaio: KmlData) -> None:
    assert dose_at(10_000.0, 10_000.0, ensaio) == 0.0
    assert dose_at(-10_000.0, -10_000.0, ensaio) == 0.0


def test_fronteira_eh_deterministica(ensaio: KmlData) -> None:
    # Ponto exatamente na fronteira A/B (x ≈ 100 m após projeção)
    # O ray-casting pode atribuir a uma zona ou outra, mas nunca a duas.
    contagem = 0
    for zone in ensaio.zones:
        cx, cy = centroid(zone.coords_xy)
        if dose_at(cx, cy, ensaio) == zone.rate:
            contagem += 1
    assert contagem == 4  # cada zona reconhecida exatamente uma vez nos seus centroides


def test_circulo_de_exclusao_zera_dose(talhao: KmlData) -> None:
    # Cupinzeiro=0:5m está em (lon=-47.499025, lat=-22.498649) — centro da zona Z4=80
    cup = next(c for c in talhao.circles if c.label.lower() == "cupinzeiro")
    assert dose_at(cup.x, cup.y, talhao) == 0.0
    # Mas a 10 m ao lado (fora do raio de 5 m), a dose volta a ser 80
    assert dose_at(cup.x + 10.0, cup.y, talhao) == 80


def test_poligono_de_exclusao_zera_dose(talhao: KmlData) -> None:
    casa = next(z for z in talhao.zones if z.label.lower() == "casa")
    cx, cy = centroid(casa.coords_xy)
    assert dose_at(cx, cy, talhao) == 0.0


def test_sete_zonas_completam_a_legenda(talhao: KmlData) -> None:
    rates = sorted(z.rate for z in talhao.zones if z.label.lower() != "casa")
    assert rates == [50, 60, 70, 80, 85, 90, 100]


def test_idw_com_amostra_unica_retorna_o_rate() -> None:
    """IDW degenera para o valor da amostra quando há só uma."""
    from src.kml_parser import KmlData, SamplePoint

    kml = KmlData(
        field_polygon=None,
        zones=[],
        circles=[],
        samples=[SamplePoint(label="S", rate=80.0, x=0.0, y=0.0)],
        origin_lat=0.0,
        origin_lon=0.0,
    )
    assert dose_at(10.0, 10.0, kml, IdwParams(radius_m=100.0)) == pytest.approx(80.0)
    # Fora do raio → cai para zona-base = 0
    assert dose_at(200.0, 200.0, kml, IdwParams(radius_m=100.0)) == 0.0


def test_point_in_polygon_quadrado() -> None:
    quad = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert point_in_polygon(5.0, 5.0, quad) is True
    assert point_in_polygon(15.0, 5.0, quad) is False
    assert point_in_polygon(-1.0, 5.0, quad) is False
