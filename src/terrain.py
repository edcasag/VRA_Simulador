"""Perfil de declive analítico Z(x,y) e modulação de velocidade do trator.

Z(x,y) = a·x + b·y + Σ_k h_k · exp( -((x-x0_k)² + (y-y0_k)²) / (2·σ_k²) )

A velocidade do trator é modulada pela componente do gradiente projetada sobre
o vetor de movimento (heading). Em subida desacelera, em descida acelera (com
saturações em v_min, v_max).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class GaussianBump:
    h: float  # altitude máxima (m)
    x0: float
    y0: float
    sigma: float  # raio característico (m)


@dataclass
class TerrainParams:
    a: float = 0.04          # declive em x (m/m), default 4% de O→L
    b: float = 0.0           # declive em y (m/m)
    bumps: list[GaussianBump] = field(default_factory=list)
    v_nom: float = 5.0       # m/s, velocidade nominal em terreno plano
    v_min: float = 1.5       # m/s, saturação mínima
    v_max: float = 7.0       # m/s, saturação máxima
    alpha: float = 50.0      # m/s por (m/m), sensibilidade ao gradiente


def default_params(bbox: tuple[float, float, float, float]) -> TerrainParams:
    """Parâmetros default: declive uniforme + 1 bossa central."""
    xmin, ymin, xmax, ymax = bbox
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    sigma = 0.20 * max(xmax - xmin, ymax - ymin)
    return TerrainParams(
        a=0.04,
        b=0.0,
        bumps=[GaussianBump(h=2.0, x0=cx, y0=cy, sigma=sigma)],
    )


def altitude(x: float, y: float, p: TerrainParams) -> float:
    z = p.a * x + p.b * y
    for bump in p.bumps:
        dx = x - bump.x0
        dy = y - bump.y0
        z += bump.h * math.exp(-(dx * dx + dy * dy) / (2.0 * bump.sigma**2))
    return z


def gradient(x: float, y: float, p: TerrainParams) -> tuple[float, float]:
    gx = p.a
    gy = p.b
    for bump in p.bumps:
        dx = x - bump.x0
        dy = y - bump.y0
        s2 = bump.sigma**2
        e = math.exp(-(dx * dx + dy * dy) / (2.0 * s2))
        gx += bump.h * e * (-dx / s2)
        gy += bump.h * e * (-dy / s2)
    return gx, gy


def speed_at(
    x: float,
    y: float,
    heading: tuple[float, float],
    p: TerrainParams,
) -> float:
    """Velocidade do trator em (x,y) ao mover-se na direção `heading` (vetor unitário)."""
    gx, gy = gradient(x, y, p)
    hx, hy = heading
    norm = math.sqrt(hx * hx + hy * hy)
    if norm < 1e-12:
        return p.v_nom
    hx /= norm
    hy /= norm
    declive_along = gx * hx + gy * hy
    v = p.v_nom - p.alpha * declive_along
    return max(p.v_min, min(p.v_max, v))


def contour_lines(
    bbox: tuple[float, float, float, float],
    p: TerrainParams,
    spacing: float = 0.5,
    grid: int = 80,
) -> list[list[tuple[float, float]]]:
    """Marching-squares simples: gera segmentos de isolinhas a cada `spacing` m de altitude.

    Devolve lista de polylinhas (cada polylinha é uma lista de (x,y)). Para uso
    leve em pygame — sem dependência de matplotlib em runtime.
    """
    xmin, ymin, xmax, ymax = bbox
    nx = grid
    ny = grid
    xs = [xmin + (xmax - xmin) * i / (nx - 1) for i in range(nx)]
    ys = [ymin + (ymax - ymin) * j / (ny - 1) for j in range(ny)]
    z = [[altitude(x, y, p) for x in xs] for y in ys]

    z_min = min(min(row) for row in z)
    z_max = max(max(row) for row in z)
    if z_max - z_min < 1e-9:
        return []
    n_levels = max(1, int((z_max - z_min) / spacing))
    levels = [z_min + spacing * (k + 1) for k in range(n_levels) if z_min + spacing * (k + 1) < z_max]

    segments: list[list[tuple[float, float]]] = []

    def interp(p1, v1, p2, v2, level):
        if abs(v2 - v1) < 1e-12:
            return p1
        t = (level - v1) / (v2 - v1)
        return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))

    for level in levels:
        for j in range(ny - 1):
            for i in range(nx - 1):
                p00 = (xs[i], ys[j])
                p10 = (xs[i + 1], ys[j])
                p11 = (xs[i + 1], ys[j + 1])
                p01 = (xs[i], ys[j + 1])
                v00 = z[j][i]
                v10 = z[j][i + 1]
                v11 = z[j + 1][i + 1]
                v01 = z[j + 1][i]
                idx = (
                    (1 if v00 >= level else 0)
                    | (2 if v10 >= level else 0)
                    | (4 if v11 >= level else 0)
                    | (8 if v01 >= level else 0)
                )
                if idx in (0, 15):
                    continue
                # Para isolinhas suficientes, basta cobrir os 14 casos não-triviais
                # com segmentos simples (saimboo, ambíguos tratados como caso "split")
                edges = []
                # bottom edge p00-p10 (mask bit 0 vs 1)
                if (idx & 1) != ((idx >> 1) & 1):
                    edges.append(interp(p00, v00, p10, v10, level))
                # right edge p10-p11 (bit 1 vs bit 2)
                if ((idx >> 1) & 1) != ((idx >> 2) & 1):
                    edges.append(interp(p10, v10, p11, v11, level))
                # top edge p11-p01 (bit 2 vs bit 3)
                if ((idx >> 2) & 1) != ((idx >> 3) & 1):
                    edges.append(interp(p11, v11, p01, v01, level))
                # left edge p01-p00 (bit 3 vs bit 0)
                if ((idx >> 3) & 1) != (idx & 1):
                    edges.append(interp(p01, v01, p00, v00, level))
                if len(edges) == 2:
                    segments.append(edges)
                elif len(edges) == 4:
                    # caso ambíguo: dois segmentos
                    segments.append([edges[0], edges[1]])
                    segments.append([edges[2], edges[3]])

    return segments
