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


def _zone_contains_sample(
    zone: Polygon, samples: list[SamplePoint]
) -> bool:
    """True se alguma amostra externa cai dentro do polígono da zona.

    Usado para evitar duplicação visual: nos KMLs do Google Earth o autor
    costuma posicionar uma marca interna com a taxa (ex.: ponto "120" dentro
    do polígono "Poor=120") como rótulo visível da zona. Se essa marca já
    existe, gerar o centroide adiciona um segundo ponto quase sobreposto.
    """
    return any(point_in_polygon(s.x, s.y, zone.coords_xy) for s in samples)


def centroids_from_zones(kml: KmlData) -> list[SamplePoint]:
    """Extrai amostras IDW dos polígonos de inclusão do KML.

    Cada polígono com `rate > 0` vira um SamplePoint no centroide geométrico,
    com a taxa do polígono. Polígonos de exclusão (rate=0) NÃO entram — eles
    representam restrições espaciais, não dose. Permite comparar Zonas de
    Manejo × IDW puro sem editar o KML.

    Zonas que já contêm uma amostra externa (Placemark de Point com a taxa,
    usado pelo autor do KML como rótulo da zona) NÃO recebem centroide: a
    marca interna já representa a zona, e somar o centroide criaria um
    segundo ponto quase no mesmo lugar.
    """
    out: list[SamplePoint] = []
    for z in kml.zones:
        if z.rate <= 0:
            continue
        if _zone_contains_sample(z, kml.samples):
            continue
        cx, cy = polygon_centroid(z.coords_xy)
        out.append(SamplePoint(label=z.label, rate=z.rate, x=cx, y=cy))
    return out


def samples_from_zones_count(kml: KmlData, n_total: int) -> list[SamplePoint]:
    """Densifica a amostragem para aproximadamente `n_total` pontos no total,
    distribuídos proporcionalmente à área das zonas de inclusão.

    Mais didático que `grid_samples_from_zones` para argumentação na tese:
    o usuário especifica "quero comparar com 50 amostras" (cada amostra = um
    tubo de solo + análise de laboratório), em vez de "quero grid de 70 m".
    Internamente, calcula-se o espaçamento que produziria n_total pontos
    distribuídos uniformemente por toda a área somada das zonas.

    Como o grid é uniforme, zonas grandes recebem mais pontos
    automaticamente (zona com 5× a área recebe ~5× as amostras), mantendo
    a distribuição proporcional à área — equivalente a uma campanha real
    de amostragem com densidade fixa por hectare.

    Quando n_total <= 0, retorna apenas os centroides (1 por zona).
    """
    if n_total <= 0:
        return centroids_from_zones(kml)
    zones_with_rate = [z for z in kml.zones if z.rate > 0]
    total_area = sum(z.area_m2 for z in zones_with_rate)
    if total_area <= 0:
        return centroids_from_zones(kml)
    spacing = math.sqrt(total_area / n_total)
    return grid_samples_from_zones(kml, spacing)


def grid_samples_from_zones(kml: KmlData, spacing_m: float) -> list[SamplePoint]:
    """Densifica a amostragem do IDW: cada polígono de inclusão recebe um
    grid regular de pontos espaçados de `spacing_m` metros, todos rotulados
    com a dose da zona. Polígonos de exclusão (rate=0) não entram.

    Quando `spacing_m` é pequeno em relação ao tamanho das zonas (ex. 5-10 m
    em zonas de 1 ha), a densidade local fica comparável a uma campanha real
    de amostragem de solo em GIS — o IDW deixa de exibir o "olho-de-boi
    grande" que aparece quando há só um centroide por zona.

    Zonas pequenas demais para conter qualquer ponto do grid recebem ao
    menos o centroide, garantindo que cada zona contribua com >=1 amostra.
    """
    if spacing_m <= 0:
        return centroids_from_zones(kml)
    out: list[SamplePoint] = []
    for z in kml.zones:
        if z.rate <= 0:
            continue
        xs = [p[0] for p in z.coords_xy]
        ys = [p[1] for p in z.coords_xy]
        xmin, ymin = min(xs), min(ys)
        xmax, ymax = max(xs), max(ys)
        # Centraliza o grid no bbox da zona: calcula quantos passos cabem,
        # mede a largura efetivamente ocupada e empurra o primeiro ponto
        # por metade da sobra. Antes o grid começava colado em (xmin, ymin)
        # e a margem direita/superior era sempre maior — visível no ABCD,
        # onde cada zona é um quadrado regular.
        def _grid_start(lo: float, hi: float, step: float) -> float:
            span = hi - lo
            if span <= 0:
                return lo
            n = int(math.floor(span / step)) + 1
            used = (n - 1) * step
            return lo + (span - used) / 2.0
        x0 = _grid_start(xmin, xmax, spacing_m)
        y0 = _grid_start(ymin, ymax, spacing_m)
        added = 0
        gy = y0
        while gy <= ymax + 1e-9:
            gx = x0
            while gx <= xmax + 1e-9:
                if point_in_polygon(gx, gy, z.coords_xy):
                    out.append(SamplePoint(label=z.label, rate=z.rate, x=gx, y=gy))
                    added += 1
                gx += spacing_m
            gy += spacing_m
        # Fallback: zonas menores que o passo do grid recebem o centroide,
        # exceto quando já contêm uma marca externa (a marca interna do KML
        # já representa a zona — ver _zone_contains_sample).
        if added == 0 and not _zone_contains_sample(z, kml.samples):
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
