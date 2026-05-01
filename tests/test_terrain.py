"""Testes do perfil de declive e modulação de velocidade."""

from __future__ import annotations

import math

import pytest

from src.terrain import (
    GaussianBump,
    TerrainParams,
    altitude,
    contour_lines,
    gradient,
    speed_at,
)


def numerical_gradient(x: float, y: float, p: TerrainParams, h: float = 1e-3) -> tuple[float, float]:
    gx = (altitude(x + h, y, p) - altitude(x - h, y, p)) / (2 * h)
    gy = (altitude(x, y + h, p) - altitude(x, y - h, p)) / (2 * h)
    return gx, gy


def test_altitude_terreno_plano() -> None:
    p = TerrainParams(a=0.0, b=0.0, bumps=[])
    assert altitude(50.0, 50.0, p) == 0.0
    assert altitude(-100.0, 200.0, p) == 0.0


def test_altitude_rampa_uniforme() -> None:
    p = TerrainParams(a=0.05, b=0.0, bumps=[])
    # Rampa de 5% subindo em x: 100 m → 5 m de altitude
    assert altitude(100.0, 0.0, p) == pytest.approx(5.0)
    assert altitude(0.0, 100.0, p) == 0.0


def test_altitude_bossa_gaussiana_no_centro() -> None:
    p = TerrainParams(a=0.0, b=0.0, bumps=[GaussianBump(h=3.0, x0=0.0, y0=0.0, sigma=20.0)])
    assert altitude(0.0, 0.0, p) == pytest.approx(3.0)
    # A 4·σ de distância, contribuição é desprezível (~exp(-8) < 0.0004)
    assert altitude(80.0, 0.0, p) == pytest.approx(0.0, abs=0.01)


def test_gradiente_analitico_vs_numerico() -> None:
    p = TerrainParams(
        a=0.03,
        b=-0.02,
        bumps=[
            GaussianBump(h=2.0, x0=10.0, y0=20.0, sigma=15.0),
            GaussianBump(h=-1.5, x0=-30.0, y0=5.0, sigma=10.0),
        ],
    )
    test_pts = [(0.0, 0.0), (10.0, 20.0), (-30.0, 5.0), (50.0, -10.0), (-50.0, 50.0)]
    for x, y in test_pts:
        ga = gradient(x, y, p)
        gn = numerical_gradient(x, y, p)
        assert ga[0] == pytest.approx(gn[0], abs=1e-4), f"dz/dx em ({x},{y})"
        assert ga[1] == pytest.approx(gn[1], abs=1e-4), f"dz/dy em ({x},{y})"


def test_speed_terreno_plano_eh_nominal() -> None:
    p = TerrainParams(a=0.0, b=0.0, bumps=[], v_nom=5.0)
    for heading in [(1, 0), (0, 1), (-1, 0), (0.7071, 0.7071)]:
        assert speed_at(50.0, 50.0, heading, p) == pytest.approx(5.0)


def test_speed_subida_satura_em_v_min() -> None:
    # Rampa 10% subindo em x; v = 5 - 50*0.1 = 0 → satura em v_min
    p = TerrainParams(a=0.10, b=0.0, bumps=[], v_nom=5.0, v_min=1.5, v_max=7.0, alpha=50.0)
    assert speed_at(0.0, 0.0, (1.0, 0.0), p) == pytest.approx(1.5)


def test_speed_descida_satura_em_v_max() -> None:
    # Rampa 10% subindo em x, mas trator descendo (heading -x)
    p = TerrainParams(a=0.10, b=0.0, bumps=[], v_nom=5.0, v_min=1.5, v_max=7.0, alpha=50.0)
    assert speed_at(0.0, 0.0, (-1.0, 0.0), p) == pytest.approx(7.0)


def test_speed_lateralmente_a_rampa_eh_nominal() -> None:
    p = TerrainParams(a=0.05, b=0.0, bumps=[], v_nom=5.0, alpha=50.0)
    # Heading perpendicular à rampa (sobe em x, anda em y) → declive_along = 0
    assert speed_at(0.0, 0.0, (0.0, 1.0), p) == pytest.approx(5.0)


def test_speed_subida_intermediaria() -> None:
    # 5% subindo: v = 5 - 50*0.05 = 2.5 m/s — coerente com §7.3 (variação ~2× nominal)
    p = TerrainParams(a=0.05, b=0.0, bumps=[], v_nom=5.0, alpha=50.0)
    assert speed_at(0.0, 0.0, (1.0, 0.0), p) == pytest.approx(2.5)


def test_contour_lines_terreno_plano_inclinado() -> None:
    p = TerrainParams(a=0.05, b=0.0, bumps=[])
    bbox = (0.0, 0.0, 100.0, 100.0)
    segs = contour_lines(bbox, p, spacing=1.0, grid=30)
    # Rampa de 5% em 100m gera 5 m de variação → ~4 isolinhas a 1m
    assert len(segs) > 0


def test_contour_lines_terreno_perfeitamente_plano_vazio() -> None:
    p = TerrainParams(a=0.0, b=0.0, bumps=[])
    segs = contour_lines((0.0, 0.0, 100.0, 100.0), p, spacing=0.5, grid=20)
    assert segs == []
