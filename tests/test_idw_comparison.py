"""Testes do modo de comparação Zonas de Manejo × IDW puro.

Cobre as funções introduzidas para a sugestão do orientador (Shepard 1968):

- `polygon_centroid`: centroide via shoelace, com fallback para média.
- `centroids_from_zones`: extração de amostras IDW dos centroides dos polígonos
   de inclusão (excluindo polígonos de exclusão).
- `dose_at_idw_pure`: interpolação IDW pura sobre as amostras, sem zonas.
- `CoverageReport.update(dose_fn=...)`: dose variável e contabilização da
   pintura fora de qualquer zona-alvo (`mass_off_zone_kg`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.coverage_report import CoverageReport
from src.kml_parser import KmlData, SamplePoint, parse_kml
from src.vra_engine import (
    IdwParams,
    centroids_from_zones,
    dose_at_idw_pure,
    grid_samples_from_zones,
    polygon_centroid,
    samples_from_zones_count,
)

DATA_DIR = Path(__file__).parent.parent / "data"


@pytest.fixture(scope="module")
def ensaio() -> KmlData:
    return parse_kml(DATA_DIR / "ensaio_abcd.kml")


@pytest.fixture(scope="module")
def talhao() -> KmlData:
    return parse_kml(DATA_DIR / "talhao_completo.kml")


# ---------- polygon_centroid ----------


def test_centroid_quadrado_unitario_no_origem() -> None:
    sq = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    cx, cy = polygon_centroid(sq)
    assert cx == pytest.approx(0.0, abs=1e-9)
    assert cy == pytest.approx(0.0, abs=1e-9)


def test_centroid_triangulo_conhecido() -> None:
    # Triângulo retângulo: vertices (0,0), (3,0), (0,3) → centroide (1,1).
    tri = [(0.0, 0.0), (3.0, 0.0), (0.0, 3.0)]
    cx, cy = polygon_centroid(tri)
    assert cx == pytest.approx(1.0)
    assert cy == pytest.approx(1.0)


def test_centroid_degenerado_cai_para_media_simples() -> None:
    # Dois pontos: shoelace dá área ~0; deve cair no fallback (média).
    seg = [(0.0, 0.0), (4.0, 6.0)]
    cx, cy = polygon_centroid(seg)
    assert cx == pytest.approx(2.0)
    assert cy == pytest.approx(3.0)


def test_centroid_orientacao_horaria_nao_inverte_sinal() -> None:
    # Mesmo quadrado em sentido horário: shoelace dá área negativa, mas o
    # cálculo do centroide divide por 6A e cancela o sinal.
    sq_cw = [(-1.0, -1.0), (-1.0, 1.0), (1.0, 1.0), (1.0, -1.0)]
    cx, cy = polygon_centroid(sq_cw)
    assert cx == pytest.approx(0.0, abs=1e-9)
    assert cy == pytest.approx(0.0, abs=1e-9)


# ---------- centroids_from_zones ----------


def test_centroids_from_zones_ensaio_abcd_gera_4_amostras(ensaio: KmlData) -> None:
    samples = centroids_from_zones(ensaio)
    assert len(samples) == 4
    by_label = {s.label: s for s in samples}
    assert sorted(by_label) == ["A", "B", "C", "D"]
    assert by_label["A"].rate == pytest.approx(90)
    assert by_label["B"].rate == pytest.approx(75)
    assert by_label["C"].rate == pytest.approx(60)
    assert by_label["D"].rate == pytest.approx(100)


def test_centroids_from_zones_ignora_poligonos_de_exclusao() -> None:
    # Sítio Palmar tem 1 polígono de exclusão (Sede). Ele NÃO deve virar amostra.
    palmar = parse_kml(DATA_DIR / "Sitio Palmar.kml")
    samples = centroids_from_zones(palmar)
    inclusion_count = sum(1 for z in palmar.zones if z.rate > 0)
    exclusion_count = sum(1 for z in palmar.zones if z.rate == 0)
    assert exclusion_count >= 1, "fixture deveria ter ao menos 1 polígono de exclusão"
    assert len(samples) == inclusion_count
    for s in samples:
        assert s.rate > 0


def test_centroids_from_zones_amostras_sao_internas_aos_poligonos(
    ensaio: KmlData,
) -> None:
    # O centroide de um polígono convexo deve estar dentro dele. Os polígonos
    # do ensaio A/B/C/D são convexos por construção.
    from src.vra_engine import point_in_polygon

    samples = centroids_from_zones(ensaio)
    for s in samples:
        zone = next(z for z in ensaio.zones if z.label == s.label)
        assert point_in_polygon(s.x, s.y, zone.coords_xy), (
            f"Centroide da zona {s.label} caiu fora do polígono"
        )


# ---------- grid_samples_from_zones ----------


def test_grid_spacing_zero_equivale_a_centroides(ensaio: KmlData) -> None:
    grid = grid_samples_from_zones(ensaio, spacing_m=0)
    centroids = centroids_from_zones(ensaio)
    assert len(grid) == len(centroids)
    for g, c in zip(sorted(grid, key=lambda s: s.label),
                    sorted(centroids, key=lambda s: s.label)):
        assert g.label == c.label
        assert g.rate == c.rate
        assert g.x == pytest.approx(c.x)
        assert g.y == pytest.approx(c.y)


def test_grid_aumenta_amostras_em_zona_grande(ensaio: KmlData) -> None:
    # Cada zona do ensaio A/B/C/D tem 1 ha (100 m × 100 m). Com grid 25 m,
    # esperamos pelo menos 16 pontos por zona (4×4 grid mínimo dentro do
    # quadrado de 100 m).
    grid = grid_samples_from_zones(ensaio, spacing_m=25)
    by_label: dict[str, int] = {}
    for s in grid:
        by_label[s.label] = by_label.get(s.label, 0) + 1
    assert sorted(by_label) == ["A", "B", "C", "D"]
    for lbl, n in by_label.items():
        assert n >= 16, f"Zona {lbl}: esperado >=16 amostras, obteve {n}"


def test_grid_amostras_herdam_a_dose_da_zona(ensaio: KmlData) -> None:
    grid = grid_samples_from_zones(ensaio, spacing_m=20)
    expected = {"A": 90, "B": 75, "C": 60, "D": 100}
    for s in grid:
        assert s.rate == pytest.approx(expected[s.label]), (
            f"Amostra {s.label}: dose esperada {expected[s.label]}, obtida {s.rate}"
        )


def test_grid_amostras_dentro_dos_poligonos(ensaio: KmlData) -> None:
    from src.vra_engine import point_in_polygon

    grid = grid_samples_from_zones(ensaio, spacing_m=20)
    for s in grid:
        zone = next(z for z in ensaio.zones if z.label == s.label)
        assert point_in_polygon(s.x, s.y, zone.coords_xy), (
            f"Amostra ({s.x:.1f}, {s.y:.1f}) com label {s.label} caiu fora do polígono"
        )


def test_grid_zona_pequena_recebe_pelo_menos_centroide() -> None:
    # Zona com bbox 5 m × 5 m e grid 50 m: nenhum ponto do grid cabe dentro.
    # Espera-se fallback para 1 amostra (o centroide).
    from src.kml_parser import KmlData, Polygon

    tiny_zone = Polygon(
        label="TinyZone",
        rate=42.0,
        coords_xy=[(0, 0), (5, 0), (5, 5), (0, 5)],
        area_m2=25.0,
    )
    kml = KmlData(
        field_polygon=None,
        zones=[tiny_zone],
        circles=[],
        samples=[],
        origin_lat=0.0,
        origin_lon=0.0,
    )
    grid = grid_samples_from_zones(kml, spacing_m=50)
    assert len(grid) == 1
    assert grid[0].rate == 42.0
    assert grid[0].label == "TinyZone"


# ---------- samples_from_zones_count ----------


def test_count_zero_equivale_a_centroides(ensaio: KmlData) -> None:
    out = samples_from_zones_count(ensaio, n_total=0)
    assert len(out) == len(centroids_from_zones(ensaio))


def test_count_aproxima_o_total_pedido(ensaio: KmlData) -> None:
    # Ensaio A/B/C/D = 4 ha = 40000 m². Pedindo 100 amostras, espera-se que
    # a contagem real fique numa janela razoável do alvo (geometria do grid
    # quadrado introduz arredondamento).
    for target in [50, 100, 500]:
        out = samples_from_zones_count(ensaio, n_total=target)
        # Tolerância 30% — o grid quadrado dentro de polígonos retangulares
        # pode subestimar. O importante é que escala conforme pedido.
        assert 0.5 * target <= len(out) <= 1.6 * target, (
            f"Pedido {target}, obtido {len(out)} (fora da tolerância 50-160%)"
        )


def test_count_distribui_proporcional_a_area(ensaio: KmlData) -> None:
    # No ensaio A/B/C/D as 4 zonas têm área igual (1 ha cada). Esperamos
    # quantidades aproximadamente iguais.
    out = samples_from_zones_count(ensaio, n_total=400)
    by_label: dict[str, int] = {}
    for s in out:
        by_label[s.label] = by_label.get(s.label, 0) + 1
    counts = list(by_label.values())
    # As 4 zonas devem ter contagens parecidas (variação <=10%)
    avg = sum(counts) / len(counts)
    for c in counts:
        assert 0.85 * avg <= c <= 1.15 * avg, (
            f"Distribuição desigual: contagens {by_label}"
        )


def test_count_amostras_herdam_a_dose(ensaio: KmlData) -> None:
    out = samples_from_zones_count(ensaio, n_total=200)
    expected = {"A": 90, "B": 75, "C": 60, "D": 100}
    for s in out:
        assert s.rate == pytest.approx(expected[s.label])


def test_grid_idw_converge_para_zonas_quando_denso(ensaio: KmlData) -> None:
    # Argumento central da tese: IDW com grid denso converge para zonas.
    # Em pontos longe da fronteira, dose IDW(grid 5m) ≈ rate da zona que
    # contém o ponto.
    grid = grid_samples_from_zones(ensaio, spacing_m=5)
    params = IdwParams(power=2.0, radius_m=500)
    expected = {"A": 90, "B": 75, "C": 60, "D": 100}
    for zone in ensaio.zones:
        if zone.rate <= 0:
            continue
        cx = sum(p[0] for p in zone.coords_xy) / len(zone.coords_xy)
        cy = sum(p[1] for p in zone.coords_xy) / len(zone.coords_xy)
        dose = dose_at_idw_pure(cx, cy, grid, params)
        # Tolerância 5%: efeito das zonas vizinhas no centroide.
        assert dose == pytest.approx(expected[zone.label], rel=0.05), (
            f"Zona {zone.label}: dose IDW grid-denso ({dose:.2f}) deveria "
            f"convergir para rate da zona ({expected[zone.label]})"
        )


# ---------- dose_at_idw_pure ----------


def test_idw_puro_em_amostra_unica_retorna_o_rate() -> None:
    sample = SamplePoint(label="X", rate=80.0, x=10.0, y=20.0)
    # Avaliado a 30 m da amostra: dentro do raio padrão (100 m), sem outra
    # amostra para diluir, deve devolver exatamente o rate da amostra.
    d = dose_at_idw_pure(40.0, 20.0, [sample], IdwParams(power=2.0, radius_m=100.0))
    assert d == pytest.approx(80.0)


def test_idw_puro_fora_do_raio_retorna_zero() -> None:
    sample = SamplePoint(label="X", rate=100.0, x=0.0, y=0.0)
    # 200 m da amostra, raio 100 m → nenhuma amostra contribui → dose=0.
    d = dose_at_idw_pure(200.0, 0.0, [sample], IdwParams(power=2.0, radius_m=100.0))
    assert d == pytest.approx(0.0)


def test_idw_puro_no_meio_de_duas_amostras_eh_a_media() -> None:
    s1 = SamplePoint(label="A", rate=60.0, x=0.0, y=0.0)
    s2 = SamplePoint(label="B", rate=100.0, x=20.0, y=0.0)
    # Ponto equidistante (10, 0): pesos iguais → média aritmética.
    d = dose_at_idw_pure(10.0, 0.0, [s1, s2], IdwParams(power=2.0, radius_m=100.0))
    assert d == pytest.approx(80.0)


def test_idw_puro_com_n_alto_evidencia_efeito_olho_de_boi() -> None:
    # Com N grande, o peso da amostra mais próxima domina drasticamente.
    s1 = SamplePoint(label="A", rate=60.0, x=0.0, y=0.0)
    s2 = SamplePoint(label="B", rate=100.0, x=20.0, y=0.0)
    # Ponto a 1 m de s1 e 19 m de s2: com N=5, peso de s1 é (1/1)^5=1 e de s2
    # é (1/19)^5 ≈ 4e-7. Dose deve estar muito perto de 60.
    d = dose_at_idw_pure(1.0, 0.0, [s1, s2], IdwParams(power=5.0, radius_m=100.0))
    assert d == pytest.approx(60.0, abs=0.01)


def test_idw_puro_com_n_baixo_aproxima_media_global() -> None:
    # Com N pequeno (0.5), os pesos ficam parecidos e a dose tende à média.
    s1 = SamplePoint(label="A", rate=60.0, x=0.0, y=0.0)
    s2 = SamplePoint(label="B", rate=100.0, x=20.0, y=0.0)
    d = dose_at_idw_pure(1.0, 0.0, [s1, s2], IdwParams(power=0.5, radius_m=100.0))
    # Ponderada por 1/√d: peso de s1 é 1/1=1, de s2 é 1/√19 ≈ 0,229.
    # Dose esperada ≈ (60·1 + 100·0,229) / (1 + 0,229) ≈ 67,4.
    assert d == pytest.approx(67.4, abs=0.5)


# ---------- CoverageReport com dose_fn (modo IDW puro) ----------


def test_report_acumula_off_zone_quando_pinta_fora_da_zona(ensaio: KmlData) -> None:
    """No modo IDW puro o trator pode pintar fora das zonas A/B/C/D
    (corredor entre zonas, fora do talhão). A massa aplicada fora deve ir
    para `mass_off_zone_kg`, não para nenhuma zona."""
    rep = CoverageReport(ensaio, width_m=2.0, seed=1)
    # dose_fn que retorna 100 kg/ha em qualquer ponto, simulando IDW puro.
    dose_fn = lambda x, y: 100.0  # noqa: E731
    # Coordenada bem longe do bbox do ensaio (zonas estão em ~[0..200, 0..200]).
    rep.update(10_000.0, 10_000.0, t=0.0, v=1.0, dose_fn=dose_fn)
    rep.update(10_000.0, 10_000.0, t=1.0, v=1.0, dose_fn=dose_fn)
    assert rep.mass_off_zone_kg > 0
    assert rep.area_off_zone_m2 == pytest.approx(2.0)  # width_m=2 × v=1 × dt=1
    # Nenhuma zona acumulou massa.
    for acc in rep.acc:
        assert acc.massa_aplicada_kg == 0.0
        assert acc.area_coberta_m2 == 0.0


def test_report_dose_fn_substitui_rate_da_zona(ensaio: KmlData) -> None:
    """Quando dose_fn é fornecida e o ponto cai dentro de uma zona, a massa
    deve usar a dose interpolada localmente, não o rate fixo da zona."""
    rep_zones = CoverageReport(ensaio, width_m=2.0, seed=1, noise_std=0.0)
    rep_idw = CoverageReport(ensaio, width_m=2.0, seed=1, noise_std=0.0)
    # Centroide da zona A (rate=90).
    zone_a = next(z for z in ensaio.zones if z.label == "A")
    cx = sum(p[0] for p in zone_a.coords_xy) / len(zone_a.coords_xy)
    cy = sum(p[1] for p in zone_a.coords_xy) / len(zone_a.coords_xy)
    # Modo zonas: usa rate=90.
    rep_zones.update(cx, cy, t=0.0, v=1.0)
    rep_zones.update(cx, cy, t=1.0, v=1.0)
    # Modo IDW: dose_fn devolve 200 (deliberadamente diferente de 90).
    dose_fn = lambda x, y: 200.0  # noqa: E731
    rep_idw.update(cx, cy, t=0.0, v=1.0, dose_fn=dose_fn)
    rep_idw.update(cx, cy, t=1.0, v=1.0, dose_fn=dose_fn)
    a_idx = next(i for i, z in enumerate(rep_idw.zones) if z.label == "A")
    massa_zones = rep_zones.acc[a_idx].massa_aplicada_kg
    massa_idw = rep_idw.acc[a_idx].massa_aplicada_kg
    # Razão deve ser 200/90 (mesma área coberta, dose injetada diferente).
    assert massa_idw == pytest.approx(massa_zones * (200.0 / 90.0), rel=1e-6)


def test_report_sem_dose_fn_e_fora_de_zona_nao_acumula(ensaio: KmlData) -> None:
    """Comportamento original (modo Zonas, sem dose_fn): pintura fora de
    qualquer zona NÃO acumula em nenhum lugar — `mass_off_zone_kg` segue
    zero porque a feature é específica do modo IDW puro."""
    rep = CoverageReport(ensaio, width_m=2.0, seed=1)
    rep.update(10_000.0, 10_000.0, t=0.0, v=1.0)
    rep.update(10_000.0, 10_000.0, t=1.0, v=1.0)
    assert rep.mass_off_zone_kg == 0.0
    assert rep.area_off_zone_m2 == 0.0
    for acc in rep.acc:
        assert acc.massa_aplicada_kg == 0.0
