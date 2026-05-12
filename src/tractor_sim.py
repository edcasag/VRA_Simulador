"""Gerador de trajetória do trator (boustrofédico).

Faixas paralelas E↔O com curvas em U na cabeceira, velocidade modulada por
terrain.speed_at(); distribuidor desliga durante a curva (spreading=False).
Quando recebe field_polygon e exclusões, recorta cada faixa para que o trator
opere apenas dentro do talhão e desvie das construções (Sede, Horta etc.).

Assinatura: yield TractorSample(x, y, t, v, heading, spreading)
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
    v: float | None                       # m/s
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


# Limiar de detecção de quina abrupta: |cross| de duas direções consecutivas
# unitárias = sin(ângulo). sin(5°) ≈ 0.087. Abaixo disso as arestas são
# praticamente colineares (segmento de uma curva aproximada), inserir arco
# seria poluir a trajetória sem ganho visual.
_CORNER_CROSS_THRESHOLD = 0.087


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


def headland_pass(
    field_polygon: list[tuple[float, float]] | None,
    exclusion_polygons: list[list[tuple[float, float]]] | None,
    exclusion_circles: list[tuple[float, float, float]] | None,
    terrain: TerrainParams,
    width_m: float,
    step_m: float,
    gnss_noise_m: float,
    rng: random.Random,
    t_start: float = 0.0,
) -> tuple[list[TractorSample], float]:
    """Gera passada de cabeceira (perímetro) ao redor do talhão, das exclusões
    poligonais (Sede, Horta etc.) e das exclusões circulares (pedras, cupins).

    Trajetória ao longo de cada aresta:
    - Talhão: deslocada `width_m/2` para dentro.
    - Exclusões poligonais: deslocada `width_m/2` para fora (dá a volta).
    - Exclusões circulares: circunferência centrada na original, raio = r +
      width_m/2 (mesma distância "rente" das exclusões poligonais).

    Spreading=True ao longo das arestas/circunferências; spreading=False
    nos saltos. Devolve (lista de samples, t_final).
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

    def walk_arc(
        center: tuple[float, float],
        p_from: tuple[float, float],
        p_to: tuple[float, float],
        radius: float,
        ccw: bool,
    ) -> None:
        """Caminha em arco circular de p_from a p_to, ambos a `radius` de center.

        Usado nas quinas convexas externas das exclusões (Sede, etc.) para
        fechar o vão de arco que ficaria descoberto se conectássemos as duas
        arestas com um trânsito reto. spreading=True (o trator está aplicando
        produto enquanto contorna a quina).
        """
        nonlocal t
        a1 = math.atan2(p_from[1] - center[1], p_from[0] - center[0])
        a2 = math.atan2(p_to[1] - center[1], p_to[0] - center[0])
        delta = a2 - a1
        if ccw:
            while delta < -1e-9:
                delta += 2.0 * math.pi
        else:
            while delta > 1e-9:
                delta -= 2.0 * math.pi
        arc_len = abs(delta) * radius
        if arc_len < 1e-3:
            return
        n_steps = max(2, int(arc_len / step_m))
        ds = arc_len / n_steps
        for k in range(1, n_steps + 1):
            f = k / n_steps
            a = a1 + f * delta
            px = center[0] + radius * math.cos(a)
            py = center[1] + radius * math.sin(a)
            # Tangente ao arco no sentido do percurso (perpendicular ao raio).
            if ccw:
                hx, hy = -math.sin(a), math.cos(a)
            else:
                hx, hy = math.sin(a), -math.cos(a)
            v = speed_at(px, py, (hx, hy), terrain)
            t += ds / max(v, 1e-3)
            nx_n = px + rng.gauss(0.0, gnss_noise_m)
            ny_n = py + rng.gauss(0.0, gnss_noise_m)
            samples.append(
                TractorSample(
                    x=nx_n, y=ny_n, t=t, v=v, heading=(hx, hy), spreading=True
                )
            )

    def walk_circle(center: tuple[float, float], radius: float) -> None:
        """Caminha a circunferência completa centrada em `center`, raio
        `radius`, no sentido CCW. Usado para contornar exclusões circulares
        pequenas (cupins, pedras): sem isso o boustrofédico recorta um
        trecho maior que o círculo (raio + width/2) e a passada de correção
        tampa o miolo com fragmentos visualmente feios.
        """
        nonlocal t
        circumference = 2.0 * math.pi * radius
        if circumference < step_m:
            return
        n_steps = max(8, int(circumference / step_m))
        ds = circumference / n_steps
        for k in range(n_steps + 1):
            f = k / n_steps
            a = 2.0 * math.pi * f  # CCW
            px = center[0] + radius * math.cos(a)
            py = center[1] + radius * math.sin(a)
            # Tangente CCW: rotação +90° do vetor radial.
            hx, hy = -math.sin(a), math.cos(a)
            v = speed_at(px, py, (hx, hy), terrain)
            t += ds / max(v, 1e-3)
            nx_n = px + rng.gauss(0.0, gnss_noise_m)
            ny_n = py + rng.gauss(0.0, gnss_noise_m)
            samples.append(
                TractorSample(
                    x=nx_n, y=ny_n, t=t, v=v, heading=(hx, hy), spreading=True
                )
            )

    def walk_offset_contour(
        poly: list[tuple[float, float]], outward: bool
    ) -> None:
        """Caminha o contorno do polígono offsetado por `inset` (inward para
        talhão, outward para exclusões), inserindo arcos tangentes nas quinas.

        Estratégia (análoga a um trajetória CNC com fresa de raio R = inset):
        1. Calcula o polígono paralelo (offset_polygon) com cantos agudos.
        2. Em cada vértice cuja virada angular > 5°, insere arco circular de
           raio R = inset, tangente às duas arestas adjacentes. O sentido do
           arco (CCW para virada à esquerda, CW para virada à direita) mantém
           a linha do polígono sempre do mesmo lado do trator.
        3. As arestas são truncadas pelos cortes em ambas as extremidades para
           dar espaço aos arcos. Onde a aresta é curta para o corte completo,
           o raio efetivo do arco encolhe.
        """
        nonlocal t
        # Remove vértices duplicados consecutivos (anéis KML costumam fechar
        # repetindo o primeiro vértice no fim, gerando aresta degenerada que
        # faz offset_polygon devolver um ponto fora do contorno e produzir
        # virada espúria de até 90° no anel paralelo).
        clean = [poly[0]]
        for p in poly[1:]:
            if math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 1e-6:
                clean.append(p)
        if len(clean) > 1 and math.hypot(
            clean[-1][0] - clean[0][0], clean[-1][1] - clean[0][1]
        ) < 1e-6:
            clean.pop()
        if len(clean) < 3:
            return
        signed_offset = +inset if outward else -inset
        ring = offset_polygon(clean, signed_offset)
        n = len(ring)
        if n < 3:
            return
        R = inset
        # Direção e comprimento de cada aresta.
        edge_dirs: list[tuple[float, float]] = []
        edge_lens: list[float] = []
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            dx = x2 - x1
            dy = y2 - y1
            L = math.hypot(dx, dy)
            if L < 1e-6:
                edge_dirs.append((0.0, 0.0))
                edge_lens.append(0.0)
            else:
                edge_dirs.append((dx / L, dy / L))
                edge_lens.append(L)
        # Para cada vértice, calcula corte e parâmetros do arco.
        cuts: list[float] = [0.0] * n
        arc_specs: list[
            tuple[tuple[float, float], float, bool] | None
        ] = [None] * n
        for i in range(n):
            d_prev = edge_dirs[(i - 1) % n]
            d_next = edge_dirs[i]
            if d_prev == (0.0, 0.0) or d_next == (0.0, 0.0):
                continue
            cross = d_prev[0] * d_next[1] - d_prev[1] * d_next[0]
            if abs(cross) < _CORNER_CROSS_THRESHOLD:
                continue
            dot = d_prev[0] * d_next[0] + d_prev[1] * d_next[1]
            # half = α/2 onde α é o ângulo de virada (exterior). O corte ao
            # longo de cada aresta é d = R · tan(α/2) — distância da apex até
            # o ponto de tangência. Para R fixo, virada brusca (α grande) →
            # corte longo; virada suave (α pequeno) → corte curto.
            half = abs(math.atan2(cross, dot)) / 2.0
            tan_half = math.tan(half)
            d_req = R * tan_half
            # Cap pelo metade da aresta adjacente para não invadir o corte do
            # vértice vizinho. Quando o cap atua, R_eff < R.
            d = min(d_req, edge_lens[(i - 1) % n] / 2.0, edge_lens[i] / 2.0)
            if d < 1e-3:
                continue
            R_eff = d / tan_half if tan_half > 1e-9 else R
            apex = ring[i]
            cut_in = (apex[0] - d * d_prev[0], apex[1] - d * d_prev[1])
            # Lado do centro: virada à esquerda (cross > 0) → centro a +90° de
            # d_prev; virada à direita → centro a -90°. Independe de CCW/CW
            # do polígono — o sinal do cross já carrega o sentido da virada.
            if cross > 0:
                n_ctr = (-d_prev[1], d_prev[0])
                arc_dir_ccw = True
            else:
                n_ctr = (d_prev[1], -d_prev[0])
                arc_dir_ccw = False
            center = (cut_in[0] + R_eff * n_ctr[0], cut_in[1] + R_eff * n_ctr[1])
            cuts[i] = d
            arc_specs[i] = (center, R_eff, arc_dir_ccw)
        # Caminha as arestas truncadas e os arcos.
        for i in range(n):
            v1 = ring[i]
            v2 = ring[(i + 1) % n]
            d_i = edge_dirs[i]
            L_i = edge_lens[i]
            if L_i < 1e-6:
                continue
            c_in = cuts[i]
            c_out = cuts[(i + 1) % n]
            start = (v1[0] + c_in * d_i[0], v1[1] + c_in * d_i[1])
            end = (v2[0] - c_out * d_i[0], v2[1] - c_out * d_i[1])
            if L_i - c_in - c_out > 1e-3:
                walk_edge(start, end, d_i)
            spec = arc_specs[(i + 1) % n]
            if spec is not None:
                center, R_eff, arc_dir_ccw = spec
                d_next = edge_dirs[(i + 1) % n]
                arc_end = (
                    v2[0] + cuts[(i + 1) % n] * d_next[0],
                    v2[1] + cuts[(i + 1) % n] * d_next[1],
                )
                walk_arc(center, end, arc_end, R_eff, arc_dir_ccw)

    # Perímetro do talhão (offset inward = trator caminha por dentro a inset
    # da borda). Polígono paralelo com cantos suavizados por arcos tangentes
    # de raio R = inset. Substitui o esquema anterior (arestas extendidas +
    # arcos centrados nos vértices originais) que produzia descasamento entre
    # heading e movimento nas quinas convexas (efeito visual de "marcha ré").
    walk_offset_contour(field_polygon, outward=False)

    # Volta em cada exclusão poligonal (offset outward = trator passa rente,
    # a inset da borda externa). Mesma lógica do talhão, só inverte o sinal
    # do offset. A linha do polígono fica sempre do mesmo lado do trator
    # (à esquerda quando o polígono é percorrido no mesmo sentido do offset).
    for ex_poly in exclusion_polygons or []:
        if len(ex_poly) < 3:
            continue
        # Trânsito do último ponto da trajetória até o primeiro ponto do
        # contorno da exclusão (sem pintar). Calcula o ponto de entrada
        # antes de caminhar.
        ring = offset_polygon(ex_poly, +inset)
        if len(ring) >= 3 and samples:
            x1, y1 = ring[0]
            x2, y2 = ring[1] if len(ring) > 1 else ring[0]
            dx = x2 - x1
            dy = y2 - y1
            L = math.hypot(dx, dy)
            heading_in = (dx / L, dy / L) if L > 1e-6 else (1.0, 0.0)
            transit((samples[-1].x, samples[-1].y), (x1, y1), heading_in)
        walk_offset_contour(ex_poly, outward=True)

    # Volta em cada exclusão CIRCULAR (cupins, pedras). O boustrofédico
    # recorta o intervalo de cada faixa que cruza o círculo a uma distância
    # `r + width/2` do centro, deixando uma região circular-quadrada sem
    # pintura. A circulação aqui contorna o círculo a essa mesma distância,
    # cobrindo o anel [r, r+width] que o boustrofédico deixou de fora.
    for cx_ex, cy_ex, r_ex in exclusion_circles or []:
        r_outer = r_ex + inset
        if r_outer < step_m:
            continue
        # Trânsito do último ponto da trajetória até o ponto de entrada
        # (lado leste do círculo, onde a tangente CCW aponta para o norte).
        entry = (cx_ex + r_outer, cy_ex)
        if samples:
            transit(
                (samples[-1].x, samples[-1].y),
                entry,
                (0.0, 1.0),
            )
        walk_circle((cx_ex, cy_ex), r_outer)

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
    strip_overlap: float = 0.0,
) -> Iterator[TractorSample]:
    """Faixas paralelas E↔O cobrindo o bbox; velocidade vem de terrain.speed_at().

    Quando `field_polygon` é fornecido, cada faixa é recortada por ele (o trator
    só opera dentro do talhão). Cada `exclusion_polygons` ou `exclusion_circles`
    é subtraído do intervalo da faixa (o trator atravessa a área da exclusão sem
    espalhar — spreading=False).

    Args:
        width_m: largura de aplicação do trator (m). Define o tamanho real do
            swath pintado.
        step_m: passo de amostragem espacial ao longo da faixa (m).
        gnss_noise_m: desvio-padrão do ruído gaussiano nas coordenadas reportadas.
        paint_offset_back_m: distância da pintura atrás do trator (m). O gerador
            desloca o trator +offset na direção do movimento, de forma que a
            pintura caia simétrica em ambos os sentidos (sem desalinhar ida vs
            volta).
        field_polygon: contorno do talhão. None = usa bbox sem clipagem.
        exclusion_polygons: lista de polígonos a evitar (Sede, Horta etc.).
        exclusion_circles: lista de (cx, cy, r) a evitar.
        strip_overlap: fração de sobreposição entre faixas (0.0 a 0.5). 0 = faixas
            adjacentes, sem overlap (deixa pequenas sobras nas bordas e quinas).
            0.10 = a faixa N+1 começa a width_m × 0.9 da faixa N, equivalente a
            10% de sobreposição — prática comum em GPS auto-steer real para
            evitar gaps. A largura do swath em si (`width_m`) não muda; só o
            avanço entre faixas.
    """
    rng = rng or random.Random(42)
    _, ymin, _, ymax = bbox
    margin = width_m * 0.5
    # Avanço entre faixas após o overlap (mantém width_m como o swath real).
    strip_overlap = max(0.0, min(0.5, strip_overlap))
    strip_spacing_m = width_m * (1.0 - strip_overlap)
    # Raio da curva em U casa com o avanço, para a próxima faixa começar
    # exatamente a strip_spacing_m de distância.
    radius = strip_spacing_m / 2.0
    arc_length = math.pi * radius
    arc_n = max(40, int(arc_length * 3 / step_m))
    t = 0.0

    # Cabeceira (passada do perímetro): cobre o anel externo irregular do talhão
    # e dá a volta nas exclusões poligonais. Para evitar sobreposição entre
    # cabeceira e boustrofédico interno, pré-processa os polígonos com
    # offset_polygon: encolhe o talhão por width_m (área já coberta pela
    # cabeceira) e expande exclusões poligonais por width_m (zona já contornada
    # pela cabeceira). Exclusões circulares também são contornadas pela
    # cabeceira (volta na circunferência a r + width_m/2 do centro).
    if headland:
        head_samples, t = headland_pass(
            field_polygon,
            exclusion_polygons,
            exclusion_circles,
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
        # Vale para polígonos E círculos (ambos agora têm cabeceira).
        excl_expand = max(width_m - overlap_m, 0.5 * width_m)
        clip_excl_poly: list[list[tuple[float, float]]] = [
            offset_polygon(ex, +excl_expand) if len(ex) >= 3 else list(ex)
            for ex in (exclusion_polygons or [])
        ]
        clip_excl_circ: list[tuple[float, float, float]] = [
            (cx, cy, r + excl_expand)
            for cx, cy, r in (exclusion_circles or [])
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
    # Tolerância na decisão "ainda cabe mais uma faixa?" — sem ela, fields
    # com bbox que difere do nominal por pouco (ex.: ABCD onde projeção
    # equiretangular dá 99.998 m em vez de 100 m exatos) deixam a faixa
    # final fora por uns mm. step_m/2 = 0.5 m default cobre erros de
    # projeção e desalinhamento de strip_spacing sem permitir faixas
    # muito além da borda do campo.
    bound_tol = step_m * 0.5

    while y <= ymax - margin + bound_tol:
        intervals = _strip_intervals(
            y, bbox, clip_field, clip_excl_poly, clip_excl_circ, step_m,
        )
        if not intervals:
            # Faixa inteira fora do talhão (ou totalmente excluída) — pula sem U-turn
            y += strip_spacing_m
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
        next_y = y + strip_spacing_m
        if next_y <= ymax - margin + bound_tol and last_x is not None:
            # Verifica se a próxima faixa tem intervalo no x atual; se não, pula a
            # curva em U (o trator vai em linha reta no transito até o próximo segmento)
            next_intervals = _strip_intervals(
                next_y, bbox, clip_field, clip_excl_poly, clip_excl_circ, step_m,
            )
            if next_intervals:
                # Lookahead 1 faixa para evitar marcha ré: se a próxima
                # faixa começa adiante no sentido atual (ex.: talhão alarga
                # em direção a essa direção), estende a faixa atual com um
                # transit reto (sem pintar) até alinhar com o início da
                # próxima. Daí o U-turn cai exatamente no início da próxima
                # — sem o trator ter que andar de marcha ré após a curva.
                next_direction = -direction
                if next_direction > 0:
                    # Próxima vai E: começa pelo intervalo mais à esquerda.
                    pa_next_first = min(pa for pa, _ in next_intervals)
                    x_start_next = pa_next_first + paint_offset_back_m
                else:
                    # Próxima vai W: começa pelo intervalo mais à direita.
                    pb_next_first = max(pb for _, pb in next_intervals)
                    x_start_next = pb_next_first - paint_offset_back_m
                if direction > 0 and x_start_next > last_x:
                    extend_xs = _frange(
                        last_x + step_m, x_start_next, step_m
                    )
                elif direction < 0 and x_start_next < last_x:
                    extend_xs = _frange(
                        last_x - step_m, x_start_next, -step_m
                    )
                else:
                    extend_xs = []
                for x in extend_xs:
                    heading = (1.0 * direction, 0.0)
                    v = speed_at(x, y, heading, terrain)
                    t += step_m / max(v, 1e-3)
                    yield TractorSample(
                        x=x, y=y, t=t, v=v, heading=heading, spreading=False
                    )
                    last_x = x

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
