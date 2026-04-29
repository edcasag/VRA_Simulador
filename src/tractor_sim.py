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
    margin: float,
    field_polygon: list[tuple[float, float]] | None,
    exclusion_polygons: list[list[tuple[float, float]]] | None,
    exclusion_circles: list[tuple[float, float, float]] | None,
    step_m: float,
) -> list[tuple[float, float]]:
    """Calcula os intervalos x onde a faixa y=y0 pode aplicar (dentro do talhão,
    fora das exclusões, com folga `margin` para o swath caber)."""
    xmin, _, xmax, _ = bbox
    if field_polygon is not None:
        intervals = _polygon_horizontal_intervals(y0, field_polygon)
    else:
        intervals = [(xmin, xmax)]
    # Encolhe a borda do talhão pela meia-largura, para o distribuidor não estourar
    intervals = [(a + margin, b - margin) for a, b in intervals if b - a > 2 * margin]
    # Subtrai exclusões poligonais (expandidas pela meia-largura)
    for ex in exclusion_polygons or []:
        ex_intervals = _polygon_horizontal_intervals(y0, ex)
        ex_intervals = [(a - margin, b + margin) for a, b in ex_intervals]
        intervals = _subtract_intervals(intervals, ex_intervals)
    # Subtrai exclusões circulares (raio expandido pela meia-largura)
    for cx, cy, r in exclusion_circles or []:
        ci = _circle_horizontal_interval(y0, cx, cy, r + margin)
        if ci is not None:
            intervals = _subtract_intervals(intervals, [ci])
    # Descarta segmentos curtos demais para uma passada útil
    intervals = [(a, b) for a, b in intervals if b - a > 2 * step_m]
    return intervals


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
    y = ymin + margin
    direction = 1  # +1: O→L; -1: L→O
    t = 0.0
    last_x: float | None = None

    while y <= ymax - margin + 1e-9:
        intervals = _strip_intervals(
            y, bbox, margin, field_polygon, exclusion_polygons, exclusion_circles, step_m
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
                next_y, bbox, margin, field_polygon,
                exclusion_polygons, exclusion_circles, step_m,
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
