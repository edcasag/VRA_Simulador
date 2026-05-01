"""Geradores de trajetória do trator.

Duas variantes:
- boustrophedon: faixas paralelas E↔O com curvas em U na cabeceira, velocidade
  modulada por terrain.speed_at(); distribuidor desliga durante a curva (spreading=False).
  Quando recebe field_polygon e exclusões, recorta cada faixa para que o trator opere
  apenas dentro do talhão e desvie das construções (Sede, Horta etc.).
- uniform_random: pontos GPS uniformemente sorteados (teste unitário visual)

Assinatura comum: yield TractorSample(x, y, t, v, heading, spreading)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterator

from .terrain import TerrainParams, speed_at


@dataclass
class TractorSample:
    x: float
    y: float
    t: float                              # tempo simulado (s)
    v: float | None                       # m/s — None no modo random
    heading: tuple[float, float] | None   # vetor unitário ou None
    spreading: bool = True                # False durante curva em U ou trânsito por exclusão


# ---------- Helpers de geometria para clipagem das faixas ----------

def _horizontal_intersections(
    y0: float, polygon: list[tuple[float, float]]
) -> list[float]:
    """Retorna os x onde a linha horizontal y=y0 cruza as arestas do polígono, ordenados."""
    xs: list[float] = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        # Conta cruzamento se y0 estiver entre y1 e y2 (regra meio-aberta para evitar
        # contagem dupla nos vértices).
        if (y1 <= y0 < y2) or (y2 <= y0 < y1):
            t = (y0 - y1) / (y2 - y1)
            xs.append(x1 + t * (x2 - x1))
    xs.sort()
    return xs


def _polygon_horizontal_intervals(
    y0: float, polygon: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Intervalos (x_in, x_out) onde a horizontal y=y0 está dentro do polígono."""
    xs = _horizontal_intersections(y0, polygon)
    return [(xs[i], xs[i + 1]) for i in range(0, len(xs) - 1, 2)]


def _circle_horizontal_interval(
    y0: float, cx: float, cy: float, r: float
) -> tuple[float, float] | None:
    """Intervalo onde a horizontal y=y0 corta o círculo (cx, cy, r); None se não corta."""
    dy = y0 - cy
    if abs(dy) >= r:
        return None
    dx = math.sqrt(r * r - dy * dy)
    return (cx - dx, cx + dx)


def _subtract_intervals(
    intervals: list[tuple[float, float]],
    excluded: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Remove cada intervalo `excluded` da lista `intervals`."""
    result = [(a, b) for a, b in intervals]
    for ex_a, ex_b in excluded:
        if ex_a >= ex_b:
            continue
        new_result: list[tuple[float, float]] = []
        for a, b in result:
            if ex_b <= a or ex_a >= b:
                # Sem sobreposição
                new_result.append((a, b))
            elif ex_a <= a and ex_b >= b:
                # Exclusão cobre todo o intervalo
                pass
            elif ex_a <= a:
                # Sobrepõe à esquerda
                new_result.append((ex_b, b))
            elif ex_b >= b:
                # Sobrepõe à direita
                new_result.append((a, ex_a))
            else:
                # Exclusão no meio: divide em dois
                new_result.append((a, ex_a))
                new_result.append((ex_b, b))
        result = new_result
    return result


def _strip_intervals(
    y0: float,
    bbox: tuple[float, float, float, float],
    field_polygon: list[tuple[float, float]] | None,
    exclusion_polygons: list[list[tuple[float, float]]] | None,
    exclusion_circles: list[tuple[float, float, float]] | None,
    step_m: float,
) -> list[tuple[float, float]]:
    """Calcula os intervalos x onde a faixa y=y0 pode aplicar.

    O caller é responsável por pré-processar os polígonos com `offset_polygon`
    para incluir as folgas necessárias (encolhimento do talhão, expansão das
    exclusões). Esta função só faz a clipagem geométrica das faixas.
    """
    xmin, _, xmax, _ = bbox
    if field_polygon is not None:
        intervals = _polygon_horizontal_intervals(y0, field_polygon)
    else:
        intervals = [(xmin, xmax)]
    for ex in exclusion_polygons or []:
        ex_intervals = _polygon_horizontal_intervals(y0, ex)
        intervals = _subtract_intervals(intervals, ex_intervals)
    for cx, cy, r in exclusion_circles or []:
        ci = _circle_horizontal_interval(y0, cx, cy, r)
        if ci is not None:
            intervals = _subtract_intervals(intervals, [ci])
    intervals = [(a, b) for a, b in intervals if b - a > step_m]
    return intervals


# ---------- Cabeceira (headland pass) ----------

def _polygon_signed_area(poly: list[tuple[float, float]]) -> float:
    """Shoelace padrão em coordenadas y-cresce-para-cima:
    positivo = anti-horário (CCW), negativo = horário (CW)."""
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def offset_polygon(
    poly: list[tuple[float, float]], offset: float
) -> list[tuple[float, float]]:
    """Polígono deslocado: offset > 0 expande (afasta do interior), offset < 0
    encolhe (aproxima do interior). Implementado por interseção das arestas
    deslocadas — para cada vértice, encontra o ponto onde as duas arestas
    adjacentes deslocadas se encontram.

    Funciona perfeitamente para polígonos convexos. Em quinas côncavas pode
    gerar pequenas auto-interseções, mas é suficiente para talhões de fazenda
    (onde a curvatura é suave). Não introduz dependência externa (shapely)."""
    n = len(poly)
    if n < 3 or abs(offset) < 1e-9:
        return list(poly)

    ccw = _polygon_signed_area(poly) > 0
    # Normal "para dentro" do polígono em relação a cada aresta
    inward_sign = 1.0 if ccw else -1.0
    # shift > 0 quando queremos encolher; shift < 0 quando queremos expandir
    shift = -offset

    # Pré-computa direção e normal-para-dentro de cada aresta
    edges_n: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        ex = x2 - x1
        ey = y2 - y1
        L = math.hypot(ex, ey)
        if L < 1e-9:
            edges_n.append(((0.0, 0.0), (0.0, 0.0)))
            continue
        ex /= L
        ey /= L
        nx = inward_sign * (-ey)
        ny = inward_sign * ex
        edges_n.append(((ex, ey), (nx, ny)))

    result: list[tuple[float, float]] = []
    for i in range(n):
        prev_e = (i - 1) % n
        cur_e = i
        d1, n1 = edges_n[prev_e]
        d2, n2 = edges_n[cur_e]
        Vi = poly[i]
        # Pontos das arestas deslocadas que passam por V_i
        p1x = Vi[0] + shift * n1[0]
        p1y = Vi[1] + shift * n1[1]
        p2x = Vi[0] + shift * n2[0]
        p2y = Vi[1] + shift * n2[1]
        # Resolve P1 + t*d1 = P2 + s*d2
        det = d1[0] * (-d2[1]) - d1[1] * (-d2[0])
        if abs(det) < 1e-9:
            # Arestas colineares — apenas usa o ponto deslocado
            result.append((p1x, p1y))
            continue
        dx = p2x - p1x
        dy = p2y - p1y
        t = (dx * (-d2[1]) - dy * (-d2[0])) / det
        result.append((p1x + t * d1[0], p1y + t * d1[1]))

    return result


def _inset_polygon_edges(
    poly: list[tuple[float, float]], inset: float, outward: bool = False
) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    """Para cada aresta do polígono, devolve (origem_inset, destino_inset, direção).

    Cada aresta é deslocada `inset` metros para dentro do polígono (ou para fora
    se outward=True, útil para contornar exclusões). Não computa interseções
    nas quinas: gera arestas isoladas que o caller concatena com pequenos
    saltos. Para contornos suaves o resultado é visualmente bom.
    """
    if len(poly) < 3 or inset <= 0:
        return []
    # Em y-up: polígono CCW (área > 0) tem o interior à esquerda de cada aresta;
    # CW (área < 0) tem o interior à direita.
    ccw = _polygon_signed_area(poly) > 0
    # Rotação +90° em y-up: (dx, dy) -> (-dy, dx) = normal à esquerda
    # Rotação -90° em y-up: (dx, dy) -> (dy, -dx) = normal à direita
    inward_sign = 1.0 if ccw else -1.0
    if outward:
        inward_sign = -inward_sign
    edges: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]] = []
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        ex = x2 - x1
        ey = y2 - y1
        L = math.hypot(ex, ey)
        if L < 1e-6:
            continue
        ex /= L
        ey /= L
        # Normal "para dentro" (rotação +90° ou -90° do edge direction)
        nx = inward_sign * (-ey)
        ny = inward_sign * ex
        ov1 = (x1 + inset * nx, y1 + inset * ny)
        ov2 = (x2 + inset * nx, y2 + inset * ny)
        edges.append((ov1, ov2, (ex, ey)))
    return edges


def headland_pass(
    field_polygon: list[tuple[float, float]] | None,
    exclusion_polygons: list[list[tuple[float, float]]] | None,
    terrain: TerrainParams,
    width_m: float,
    step_m: float,
    gnss_noise_m: float,
    rng: random.Random,
    t_start: float = 0.0,
) -> tuple[list[TractorSample], float]:
    """Gera passada de cabeceira (perímetro) ao redor do talhão e das exclusões
    poligonais (Sede, Horta etc.).

    Trajetória ao longo de cada aresta:
    - Talhão: deslocada `width_m/2` para dentro.
    - Exclusões poligonais: deslocada `width_m/2` para fora (dá a volta).

    Exclusões circulares (cupins, pedras pequenas) NÃO recebem volta — o
    boustrofédico já as desvia bem na Estratégia B e o ganho seria mínimo.

    Spreading=True ao longo das arestas; spreading=False nos saltos.
    Devolve (lista de samples, t_final).
    """
    if field_polygon is None or len(field_polygon) < 3:
        return [], t_start

    samples: list[TractorSample] = []
    t = t_start
    inset = width_m / 2.0

    def walk_edge(
        ov1: tuple[float, float], ov2: tuple[float, float], heading: tuple[float, float]
    ) -> None:
        nonlocal t
        dx = ov2[0] - ov1[0]
        dy = ov2[1] - ov1[1]
        L = math.hypot(dx, dy)
        if L < step_m:
            return
        n_steps = max(1, int(L / step_m))
        for k in range(n_steps + 1):
            f = k / n_steps
            px = ov1[0] + f * dx
            py = ov1[1] + f * dy
            v = speed_at(px, py, heading, terrain)
            t += step_m / max(v, 1e-3)
            nx_n = px + rng.gauss(0.0, gnss_noise_m)
            ny_n = py + rng.gauss(0.0, gnss_noise_m)
            samples.append(
                TractorSample(
                    x=nx_n, y=ny_n, t=t, v=v, heading=heading, spreading=True
                )
            )

    def transit(
        p_from: tuple[float, float],
        p_to: tuple[float, float],
        heading: tuple[float, float],
    ) -> None:
        """Pequeno trânsito entre arestas (spreading=False)."""
        nonlocal t
        dx = p_to[0] - p_from[0]
        dy = p_to[1] - p_from[1]
        L = math.hypot(dx, dy)
        if L < 1e-3:
            return
        n_steps = max(1, int(L / step_m))
        for k in range(1, n_steps + 1):
            f = k / n_steps
            px = p_from[0] + f * dx
            py = p_from[1] + f * dy
            v = speed_at(px, py, heading, terrain)
            t += L / n_steps / max(v, 1e-3)
            samples.append(
                TractorSample(x=px, y=py, t=t, v=v, heading=heading, spreading=False)
            )

    # Perímetro do talhão (inset para dentro)
    edges = _inset_polygon_edges(field_polygon, inset, outward=False)
    if edges:
        prev_end: tuple[float, float] | None = None
        for ov1, ov2, heading in edges:
            if prev_end is not None:
                transit(prev_end, ov1, heading)
            walk_edge(ov1, ov2, heading)
            prev_end = ov2
        # Fecha o anel
        if prev_end is not None:
            transit(prev_end, edges[0][0], edges[0][2])

    # Volta em cada exclusão poligonal (outset para fora — trator passa rente)
    for ex_poly in exclusion_polygons or []:
        if len(ex_poly) < 3:
            continue
        ex_edges = _inset_polygon_edges(ex_poly, inset, outward=True)
        if not ex_edges:
            continue
        # Trânsito do último ponto da trajetória até a primeira aresta da exclusão
        if samples:
            transit(
                (samples[-1].x, samples[-1].y),
                ex_edges[0][0],
                ex_edges[0][2],
            )
        prev_end_ex: tuple[float, float] | None = None
        for ov1, ov2, heading in ex_edges:
            if prev_end_ex is not None:
                transit(prev_end_ex, ov1, heading)
            walk_edge(ov1, ov2, heading)
            prev_end_ex = ov2
        # Fecha o anel da exclusão
        if prev_end_ex is not None:
            transit(prev_end_ex, ex_edges[0][0], ex_edges[0][2])

    return samples, t


def should_use_headland(
    field_polygon: list[tuple[float, float]] | None,
    threshold_vertices: int = 5,
) -> bool:
    """Critério auto: cabeceira faz sentido quando o talhão tem contorno
    irregular (pelo menos 5 vértices). Sem talhão (ABCD, talhao_completo)
    ou com talhão retangular (4 vértices) o boustrofédico cobre direto."""
    if field_polygon is None:
        return False
    return len(field_polygon) >= threshold_vertices


# ---------- Gerador principal ----------

def boustrophedon(
    bbox: tuple[float, float, float, float],
    terrain: TerrainParams,
    width_m: float = 3.0,
    step_m: float = 1.0,
    gnss_noise_m: float = 0.5,
    paint_offset_back_m: float = 0.0,
    rng: random.Random | None = None,
    field_polygon: list[tuple[float, float]] | None = None,
    exclusion_polygons: list[list[tuple[float, float]]] | None = None,
    exclusion_circles: list[tuple[float, float, float]] | None = None,
    headland: bool = False,
) -> Iterator[TractorSample]:
    """Faixas paralelas E↔O cobrindo o bbox; velocidade vem de terrain.speed_at().

    Quando `field_polygon` é fornecido, cada faixa é recortada por ele (o trator
    só opera dentro do talhão). Cada `exclusion_polygons` ou `exclusion_circles`
    é subtraído do intervalo da faixa (o trator atravessa a área da exclusão sem
    espalhar — spreading=False).

    Args:
        width_m: largura de aplicação do trator (m), define espaçamento entre faixas
        step_m: passo de amostragem espacial ao longo da faixa (m)
        gnss_noise_m: desvio-padrão do ruído gaussiano nas coordenadas reportadas
        paint_offset_back_m: distância da pintura atrás do trator (m). O gerador desloca
            o trator +offset na direção do movimento, de forma que a pintura caia
            simétrica em ambos os sentidos (sem desalinhar ida vs volta).
        field_polygon: contorno do talhão. None = usa bbox sem clipagem.
        exclusion_polygons: lista de polígonos a evitar (Sede, Horta etc.).
        exclusion_circles: lista de (cx, cy, r) a evitar.
    """
    rng = rng or random.Random(42)
    _, ymin, _, ymax = bbox
    margin = width_m * 0.5
    radius = width_m / 2.0
    arc_length = math.pi * radius
    arc_n = max(40, int(arc_length * 3 / step_m))
    t = 0.0

    # Cabeceira (passada do perímetro): cobre o anel externo irregular do talhão
    # e dá a volta nas exclusões poligonais. Para evitar sobreposição entre
    # cabeceira e boustrofédico interno, pré-processa os polígonos com
    # offset_polygon: encolhe o talhão por width_m (área já coberta pela
    # cabeceira) e expande exclusões poligonais por width_m (zona já contornada
    # pela cabeceira). Exclusões circulares não são contornadas pela cabeceira;
    # ainda assim recebem expansão por width_m/2 para o swath não invadi-las.
    if headland:
        head_samples, t = headland_pass(
            field_polygon,
            exclusion_polygons,
            terrain,
            width_m,
            step_m,
            gnss_noise_m,
            rng,
            t_start=t,
        )
        for s in head_samples:
            yield s
        # Encolhe o talhão por um pouco menos que width_m para deixar uma
        # pequena sobreposição intencional com a cabeceira. Em quinas agudas
        # o offset polygonal cria pequenos gaps; um operador humano passa de
        # novo. 1 m é suficiente para preencher gaps típicos sem custo grande.
        overlap_m = 1.0
        inner_inset = max(width_m - overlap_m, 0.5 * width_m)
        clip_field = (
            offset_polygon(field_polygon, -inner_inset)
            if field_polygon and len(field_polygon) >= 3
            else field_polygon
        )
        # Idem para exclusões: contrai a expansão da exclusão por overlap_m,
        # garantindo que o trator interno passe rente à área já contornada.
        excl_expand = max(width_m - overlap_m, 0.5 * width_m)
        clip_excl_poly: list[list[tuple[float, float]]] = [
            offset_polygon(ex, +excl_expand) if len(ex) >= 3 else list(ex)
            for ex in (exclusion_polygons or [])
        ]
        clip_excl_circ: list[tuple[float, float, float]] = [
            (cx, cy, r + margin) for cx, cy, r in (exclusion_circles or [])
        ]
    else:
        # Sem cabeceira: pré-processa só as exclusões com a meia-largura
        # (sem encolher o talhão — strips podem estourar pra fora, over-spray
        # off-field é o comportamento real de máquinas sem section control).
        clip_field = field_polygon
        clip_excl_poly = [
            offset_polygon(ex, +margin) if len(ex) >= 3 else list(ex)
            for ex in (exclusion_polygons or [])
        ]
        clip_excl_circ = [
            (cx, cy, r + margin) for cx, cy, r in (exclusion_circles or [])
        ]

    y = ymin + margin
    direction = 1  # +1: O→L; -1: L→O
    last_x: float | None = None

    while y <= ymax - margin + 1e-9:
        intervals = _strip_intervals(
            y, bbox, clip_field, clip_excl_poly, clip_excl_circ, step_m,
        )
        if not intervals:
            # Faixa inteira fora do talhão (ou totalmente excluída) — pula sem U-turn
            y += width_m
            direction *= -1
            last_x = None
            continue

        # Ordena por direção: O→L sobe x, L→O desce x
        if direction > 0:
            intervals.sort(key=lambda iv: iv[0])
        else:
            intervals.sort(key=lambda iv: -iv[0])

        for i_idx, (pa, pb) in enumerate(intervals):
            # Range no espaço do trator (offset do distribuidor compensado)
            if direction > 0:
                xs = _frange(
                    pa + paint_offset_back_m, pb + paint_offset_back_m, step_m
                )
            else:
                xs = _frange(
                    pb - paint_offset_back_m, pa - paint_offset_back_m, -step_m
                )

            # Trânsito sobre a exclusão até o início deste intervalo (spreading=False)
            if last_x is not None and i_idx > 0 and xs:
                target = xs[0]
                if direction > 0 and target > last_x + step_m:
                    transit = _frange(last_x + step_m, target - step_m, step_m)
                elif direction < 0 and target < last_x - step_m:
                    transit = _frange(last_x - step_m, target + step_m, -step_m)
                else:
                    transit = []
                for x in transit:
                    heading = (1.0 * direction, 0.0)
                    v = speed_at(x, y, heading, terrain)
                    t += step_m / max(v, 1e-3)
                    yield TractorSample(
                        x=x, y=y, t=t, v=v, heading=heading, spreading=False
                    )
                    last_x = x

            # Aplicação ao longo do intervalo (spreading=True)
            for x in xs:
                heading = (1.0 * direction, 0.0)
                v = speed_at(x, y, heading, terrain)
                dt = step_m / max(v, 1e-3)
                t += dt
                nx = x + rng.gauss(0.0, gnss_noise_m)
                ny = y + rng.gauss(0.0, gnss_noise_m)
                yield TractorSample(
                    x=nx, y=ny, t=t, v=v, heading=heading, spreading=True
                )
                last_x = x

        # Curva em U para entrar na próxima faixa (sem pintar)
        next_y = y + width_m
        if next_y <= ymax - margin + 1e-9 and last_x is not None:
            # Verifica se a próxima faixa tem intervalo no x atual; se não, pula a
            # curva em U (o trator vai em linha reta no transito até o próximo segmento)
            next_intervals = _strip_intervals(
                next_y, bbox, clip_field, clip_excl_poly, clip_excl_circ, step_m,
            )
            if next_intervals:
                cx_arc = last_x
                cy_arc = y + radius
                seg_len = arc_length / arc_n
                for i in range(1, arc_n + 1):
                    frac = i / arc_n
                    if direction > 0:
                        ang = -math.pi / 2 + math.pi * frac
                        hx = -math.sin(ang)
                        hy = math.cos(ang)
                    else:
                        ang = -math.pi / 2 - math.pi * frac
                        hx = math.sin(ang)
                        hy = -math.cos(ang)
                    px = cx_arc + radius * math.cos(ang)
                    py = cy_arc + radius * math.sin(ang)
                    v = speed_at(px, py, (hx, hy), terrain)
                    t += seg_len / max(v, 1e-3)
                    yield TractorSample(
                        x=px, y=py, t=t, v=v, heading=(hx, hy), spreading=False
                    )

        y = next_y
        direction *= -1


def uniform_random(
    bbox: tuple[float, float, float, float],
    n_samples: int = 4000,
    rng: random.Random | None = None,
) -> Iterator[TractorSample]:
    """Pontos uniformemente sorteados no bbox. Sem velocidade nem heading."""
    rng = rng or random.Random(42)
    xmin, ymin, xmax, ymax = bbox
    for _ in range(n_samples):
        x = rng.uniform(xmin, xmax)
        y = rng.uniform(ymin, ymax)
        yield TractorSample(x=x, y=y, t=0.0, v=None, heading=None)


def _frange(start: float, stop: float, step: float) -> list[float]:
    """Range com passo float, inclui o stop quando passo o atinge."""
    out: list[float] = []
    x = start
    if step > 0:
        while x <= stop + 1e-9:
            out.append(x)
            x += step
    else:
        while x >= stop - 1e-9:
            out.append(x)
            x += step
    return out
