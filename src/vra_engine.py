"""Motor VRA: calcula a dose-alvo (kg/ha) numa coordenada GNSS arbitrária.

Hierarquia de decisão padrão (`dose_at`, artigo SBIAGRO 2025):
1. Exclusão circular (`Label=0:Radius`) → 0
2. Inclusão circular (`Label=Rate:Radius`, Rate>0) → Rate
3. Polígono de exclusão (`Label=0`) → 0
4. Polígono de inclusão (`Label=Rate`) → Rate
5. Pontos de amostra esparsos (IDW p=2 dentro de raio 100 m, cap 5 §5.4 Eq.3)
6. Zona-base (`Field=Rate`) → Rate
7. Fora do talhão → 0

Modo de comparação `dose_at_idw_pure`: usa apenas IDW sobre amostras (centroides
dos polígonos de inclusão) — sem zonas, sem exclusões, sem campo-base. Permite
contrastar quantitativamente o método de zonas de manejo contra a interpolação
clássica (Shepard 1968, IDW p=N) sugerida pelo orientador para a tese.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .kml_parser import CircularPoint, KmlData, Polygon, SamplePoint

DEFAULT_IDW_POWER = 2.0
DEFAULT_IDW_RADIUS_M = 100.0
DEFAULT_IDW_DMIN_M = 0.5  # piso para evitar bull's-eye


@dataclass
class IdwParams:
    power: float = DEFAULT_IDW_POWER
    radius_m: float = DEFAULT_IDW_RADIUS_M
    d_min_m: float = DEFAULT_IDW_DMIN_M


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting clássico: conta interseções de um raio horizontal."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def _idw(x: float, y: float, samples: list[SamplePoint], params: IdwParams) -> float | None:
    """IDW p=2 com k-NN dentro de raio. Retorna None se nenhuma amostra cair no raio."""
    weighted_sum = 0.0
    weight_total = 0.0
    radius_sq = params.radius_m**2
    for s in samples:
        dx = x - s.x
        dy = y - s.y
        d_sq = dx * dx + dy * dy
        if d_sq > radius_sq:
            continue
        d = max(math.sqrt(d_sq), params.d_min_m)
        w = 1.0 / (d**params.power)
        weighted_sum += w * s.rate
        weight_total += w
    if weight_total == 0.0:
        return None
    return weighted_sum / weight_total


def dose_at(x: float, y: float, kml: KmlData, idw_params: IdwParams | None = None) -> float:
    """Retorna a dose-alvo (kg/ha) na posição (x, y) — coordenadas projetadas em metros."""
    params = idw_params or IdwParams()

    # 1-2: pontos circulares (raio explícito) — exclusão prioritária
    for c in kml.circles:
        if (x - c.x) ** 2 + (y - c.y) ** 2 <= c.radius_m**2:
            return c.rate

    # 3: exclusões (Rate=0) têm prioridade sobre inclusões em qualquer ordem do KML
    for z in kml.zones:
        if z.rate == 0 and point_in_polygon(x, y, z.coords_xy):
            return 0.0

    # 4: polígonos de inclusão. Quando há sobreposição (zona específica dentro
    # de uma zona-fundo), vence a de menor área — a prescrição mais específica.
    smallest_area: float | None = None
    smallest_rate: float | None = None
    for z in kml.zones:
        if z.rate > 0 and point_in_polygon(x, y, z.coords_xy):
            if smallest_area is None or z.area_m2 < smallest_area:
                smallest_area = z.area_m2
                smallest_rate = z.rate
    if smallest_rate is not None:
        return smallest_rate

    # 5: IDW por amostras esparsas
    if kml.samples:
        idw_value = _idw(x, y, kml.samples, params)
        if idw_value is not None:
            return idw_value

    # 6-7: zona-base se dentro; senão zero
    if kml.field_polygon and point_in_polygon(x, y, kml.field_polygon.coords_xy):
        return kml.field_polygon.rate
    return 0.0


def all_target_zones(kml: KmlData) -> list[Polygon]:
    """Lista zonas com Rate > 0 (úteis para relatório de erro por zona)."""
    return [z for z in kml.zones if z.rate > 0]


def polygon_centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    """Centroide geométrico (shoelace com sinal de área). Fallback para média
    simples em polígonos degenerados (área ~ 0)."""
    n = len(coords)
    if n < 3:
        cx = sum(p[0] for p in coords) / max(n, 1)
        cy = sum(p[1] for p in coords) / max(n, 1)
        return cx, cy
    a = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    a *= 0.5
    if abs(a) < 1e-9:
        cx = sum(p[0] for p in coords) / n
        cy = sum(p[1] for p in coords) / n
        return cx, cy
    return cx / (6.0 * a), cy / (6.0 * a)


def centroids_from_zones(kml: KmlData) -> list[SamplePoint]:
    """Extrai amostras IDW dos polígonos de inclusão do KML.

    Cada polígono com `rate > 0` vira um SamplePoint no centroide geométrico,
    com a taxa do polígono. Polígonos de exclusão (rate=0) NÃO entram — eles
    representam restrições espaciais, não dose. Permite comparar Zonas de
    Manejo × IDW puro sem editar o KML.
    """
    out: list[SamplePoint] = []
    for z in kml.zones:
        if z.rate <= 0:
            continue
        cx, cy = polygon_centroid(z.coords_xy)
        out.append(SamplePoint(label=z.label, rate=z.rate, x=cx, y=cy))
    return out


def dose_at_idw_pure(
    x: float,
    y: float,
    samples: list[SamplePoint],
    params: IdwParams | None = None,
) -> float:
    """Dose IDW pura: interpola apenas das amostras, sem zonas e sem
    exclusões. Retorna 0 se nenhuma amostra cair no raio."""
    p = params or IdwParams()
    value = _idw(x, y, samples, p)
    return 0.0 if value is None else value
