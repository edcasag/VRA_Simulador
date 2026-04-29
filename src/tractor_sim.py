"""Geradores de trajetória do trator.

Duas variantes:
- boustrophedon: faixas paralelas E↔O com curvas em U na cabeceira, velocidade
  modulada por terrain.speed_at(); distribuidor desliga durante a curva (spreading=False)
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
    spreading: bool = True                # False durante curva em U


def boustrophedon(
    bbox: tuple[float, float, float, float],
    terrain: TerrainParams,
    width_m: float = 3.0,
    step_m: float = 1.0,
    gnss_noise_m: float = 0.5,
    paint_offset_back_m: float = 0.0,
    rng: random.Random | None = None,
) -> Iterator[TractorSample]:
    """Faixas paralelas E↔O cobrindo o bbox; velocidade vem de terrain.speed_at().

    Args:
        width_m: largura de aplicação do trator (m), define espaçamento entre faixas
        step_m: passo de amostragem espacial ao longo da faixa (m)
        gnss_noise_m: desvio-padrão do ruído gaussiano nas coordenadas reportadas
        paint_offset_back_m: distância da pintura atrás do trator (m). O gerador desloca
            o trator +offset na direção do movimento, de forma que a pintura caia
            simétrica em ambos os sentidos (sem desalinhar ida vs volta).
    """
    rng = rng or random.Random(42)
    xmin, ymin, xmax, ymax = bbox
    margin = width_m * 0.5
    radius = width_m / 2.0
    arc_length = math.pi * radius
    # Mais amostras no arco para que a curva apareça em detalhe (mais devagar visualmente)
    arc_n = max(40, int(arc_length * 3 / step_m))
    y = ymin + margin
    direction = 1  # +1: O→L; -1: L→O
    t = 0.0
    last_x: float | None = None
    while y <= ymax - margin + 1e-9:
        # Compensa o offset da pintura: o trator avança +offset na direção atual,
        # e a pintura (deslocada -offset atrás dele) cai no range simétrico desejado.
        if direction > 0:
            xs = _frange(
                xmin + margin + paint_offset_back_m,
                xmax - margin + paint_offset_back_m,
                step_m,
            )
        else:
            xs = _frange(
                xmax - margin - paint_offset_back_m,
                xmin + margin - paint_offset_back_m,
                -step_m,
            )
        for x in xs:
            heading = (1.0 * direction, 0.0)
            v = speed_at(x, y, heading, terrain)
            dt = step_m / max(v, 1e-3)
            t += dt
            nx = x + rng.gauss(0.0, gnss_noise_m)
            ny = y + rng.gauss(0.0, gnss_noise_m)
            yield TractorSample(x=nx, y=ny, t=t, v=v, heading=heading, spreading=True)
            last_x = x

        # Curva em U para entrar na próxima faixa (sem pintar)
        next_y = y + width_m
        if next_y <= ymax - margin + 1e-9 and last_x is not None:
            cx_arc = last_x
            cy_arc = y + radius
            seg_len = arc_length / arc_n
            for i in range(1, arc_n + 1):
                frac = i / arc_n
                if direction > 0:
                    # Anti-horário, arco passa pelo lado leste (+x do bbox)
                    ang = -math.pi / 2 + math.pi * frac
                    hx = -math.sin(ang)
                    hy = math.cos(ang)
                else:
                    # Horário, arco passa pelo lado oeste (-x do bbox)
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
