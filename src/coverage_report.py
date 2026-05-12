"""Acumula dose aplicada por zona e gera relatório (Tab. 6 cap 7 §7.3).

Modelo:
  dose_aplicada[zona] += dose_alvo(x,y) · largura_aplicacao · v(t) · Δt · (1 + ε)

onde dose_alvo(x,y) é a função do método ativo (zonas hierárquicas ou IDW puro)
e ε ~ N(0, 0.025²) truncado em ±0.05 representa agregadamente latência do
controlador, atuador e GNSS. Aproximação simplificada documentada no README.

Quando `update()` recebe um `dose_fn` (modo IDW puro), a taxa pode variar
dentro da mesma zona — a massa acumulada usa a dose interpolada localmente,
não a taxa fixa do polígono. Pontos pintados fora de qualquer zona-alvo (ex.:
sede, corredores) são contabilizados em `mass_off_zone_kg` para evidenciar o
desperdício do IDW puro vs zonas com exclusão.
"""

from __future__ import annotations

import csv
import io
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .i18n import t
from .kml_parser import (  # noqa: F401 (polygon_area_m2 re-exportada)
    CircularPoint,
    KmlData,
    Polygon,
    polygon_area_m2,
)
from .vra_engine import point_in_polygon

DoseFn = Callable[[float, float], float]


@dataclass
class ZoneAccumulator:
    label: str
    rate_alvo: float
    area_ha: float
    massa_aplicada_kg: float = 0.0
    area_coberta_m2: float = 0.0
    # Massa e área aplicadas pela correção virtual no fim da simulação
    # (preenchimento das células não cobertas pela trajetória do trator com a
    # taxa do algoritmo — sem movimento físico). Separados dos valores reais
    # do trator para o relatório distinguir os dois.
    massa_correcao_kg: float = 0.0
    area_correcao_m2: float = 0.0


class CoverageGrid:
    """Grid retangular de células (default 1 m × 1 m) alinhado ao bbox do
    talhão que registra quais células foram efetivamente pintadas pelo trator.

    Independente do método de prescrição (Zones/IDW): serve para diagnosticar
    sobras do plano de cobertura — bordas irregulares do talhão, quinas
    convexas das exclusões, deriva GNSS — e quantificar a cobertura real por
    zona, dado que o acumulador `area_coberta_m2` por zona soma rectângulos
    sobrepostos como se fossem áreas distintas (super-estima).

    Cada célula é um booleano (pintada ou não). A marcação usa um template de
    disco pré-computado e copiado em fatia da matriz numpy — rápido o
    suficiente para rodar live durante a simulação sem causar travamento.
    """

    def __init__(
        self,
        bbox: tuple[float, float, float, float],
        cell_size_m: float = 1.0,
    ) -> None:
        xmin, ymin, xmax, ymax = bbox
        self.cell_size_m = cell_size_m
        self.xmin = xmin
        self.ymin = ymin
        self.nx = int(math.ceil((xmax - xmin) / cell_size_m)) + 1
        self.ny = int(math.ceil((ymax - ymin) / cell_size_m)) + 1
        self.painted: np.ndarray = np.zeros((self.ny, self.nx), dtype=bool)
        # Marca quais células foram preenchidas pela correção virtual ao fim
        # da simulação (vs. pintadas pelo trator durante a trajetória). Usado
        # tanto pelo relatório (separar `correção_kg` de `aplicado_kg`)
        # quanto pela visualização (pintar essas células no paint_layer no
        # fim, no estilo "preencher pixels brancos").
        self.is_correction: np.ndarray = np.zeros(
            (self.ny, self.nx), dtype=bool
        )
        # Cache de templates de disco por raio em células (evita reconstruir
        # a máscara a cada chamada de mark_disk).
        self._disk_cache: dict[int, np.ndarray] = {}

    def _disk_template(self, radius_cells: int) -> np.ndarray:
        cached = self._disk_cache.get(radius_cells)
        if cached is not None:
            return cached
        size = 2 * radius_cells + 1
        yy, xx = np.indices((size, size))
        c = radius_cells
        mask = (xx - c) ** 2 + (yy - c) ** 2 <= radius_cells**2
        self._disk_cache[radius_cells] = mask
        return mask

    def mark_disk(self, x: float, y: float, radius_m: float) -> None:
        """Pinta como cobertas todas as células num disco de raio radius_m
        centrado em (x, y). Disco aproxima o swath retangular do trator —
        com amostras consecutivas a step_m=1 m, os discos se sobrepõem e
        formam uma faixa de largura ≈ width_m, fiel à pintura real."""
        ix = int((x - self.xmin) / self.cell_size_m)
        iy = int((y - self.ymin) / self.cell_size_m)
        radius_cells = max(1, int(round(radius_m / self.cell_size_m)))
        template = self._disk_template(radius_cells)
        size = 2 * radius_cells + 1
        ix0, iy0 = ix - radius_cells, iy - radius_cells
        ix1, iy1 = ix0 + size, iy0 + size
        # Recorta o template para caber no grid (bordas do bbox).
        sx0 = max(0, -ix0)
        sy0 = max(0, -iy0)
        sx1 = size - max(0, ix1 - self.nx)
        sy1 = size - max(0, iy1 - self.ny)
        gx0 = max(0, ix0)
        gy0 = max(0, iy0)
        gx1 = min(self.nx, ix1)
        gy1 = min(self.ny, iy1)
        if sx1 > sx0 and sy1 > sy0 and gx1 > gx0 and gy1 > gy0:
            self.painted[gy0:gy1, gx0:gx1] |= template[sy0:sy1, sx0:sx1]

    def coverage_in_polygon(
        self, polygon: list[tuple[float, float]]
    ) -> tuple[int, int]:
        """Devolve (n_células_pintadas, n_células_dentro_do_polígono).

        Itera apenas as células do bbox do polígono e usa point_in_polygon
        em cada centro de célula. Para 6 zonas × ~10k células × ~12 vértices
        chega a ~1 s — aceitável como pós-processamento.
        """
        if len(polygon) < 3:
            return 0, 0
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        ix0 = max(0, int((min(xs) - self.xmin) / self.cell_size_m))
        ix1 = min(self.nx, int((max(xs) - self.xmin) / self.cell_size_m) + 1)
        iy0 = max(0, int((min(ys) - self.ymin) / self.cell_size_m))
        iy1 = min(self.ny, int((max(ys) - self.ymin) / self.cell_size_m) + 1)
        n_total = 0
        n_painted = 0
        for iy in range(iy0, iy1):
            cy = self.ymin + (iy + 0.5) * self.cell_size_m
            row = self.painted[iy]
            for ix in range(ix0, ix1):
                cx = self.xmin + (ix + 0.5) * self.cell_size_m
                if point_in_polygon(cx, cy, polygon):
                    n_total += 1
                    if bool(row[ix]):
                        n_painted += 1
        return n_painted, n_total

    def total_painted_m2(self) -> float:
        """Área total pintada (qualquer lugar do bbox), em m²."""
        return float(self.painted.sum()) * (self.cell_size_m**2)


def _pip_vectorized(
    xx: np.ndarray, yy: np.ndarray, polygon: list[tuple[float, float]]
) -> np.ndarray:
    """Ray casting vetorizado: para cada par (xx[i], yy[i]) devolve True
    se está dentro do polígono. Funciona com arrays N-D do numpy."""
    n = len(polygon)
    inside = np.zeros(xx.shape, dtype=bool)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        dy = y2 - y1
        if abs(dy) < 1e-12:
            continue
        cond_y = (y1 > yy) != (y2 > yy)
        x_intersect = (x2 - x1) * (yy - y1) / dy + x1
        inside ^= cond_y & (xx < x_intersect)
    return inside


class CoverageReport:
    def __init__(
        self,
        kml: KmlData,
        width_m: float = 3.0,
        noise_std: float = 0.025,
        noise_clip: float = 0.05,
        seed: int = 42,
        lang: str = "pt",
        bbox: tuple[float, float, float, float] | None = None,
        coverage_cell_m: float = 1.0,
        is_zones_mode: bool = True,
    ) -> None:
        self.zones: list[Polygon] = [z for z in kml.zones if z.rate > 0]
        # Exclusões (Rate=0) precisam estar acessíveis aqui para classificar
        # como "fora de zona" qualquer aplicação dentro delas. No modo Zonas
        # o trator já evita esses polígonos pelo planejamento da trajetória,
        # então não há impacto. No modo IDW, porém, a trajetória cruza Sede
        # e pedras — e antes desta correção o produto pulverizado nessas
        # áreas era erroneamente atribuído à zona-fundo de inclusão que
        # contém a exclusão (ex.: Normal envolvendo Sede no Sítio Palmar),
        # mascarando o desperdício do método IDW puro.
        self.exclusion_polys: list[Polygon] = [
            z for z in kml.zones if z.rate == 0
        ]
        self.exclusion_circles: list[CircularPoint] = [
            c for c in kml.circles if c.rate == 0
        ]
        self.width_m = width_m
        self.noise_std = noise_std
        self.noise_clip = noise_clip
        self.lang = lang
        self.rng = random.Random(seed)
        # Indexa por posição na lista de zonas: tolera dois polígonos com mesmo
        # rótulo (ex.: "Poor=100" e "Poor=120" no Sítio Viçosa).
        self.acc: list[ZoneAccumulator] = [
            ZoneAccumulator(
                label=self._unique_label(i),
                rate_alvo=z.rate,
                area_ha=z.area_m2 / 10_000.0,
            )
            for i, z in enumerate(self.zones)
        ]
        # Massa aplicada FORA de qualquer zona-alvo (relevante apenas no modo
        # IDW puro, onde o trator pode pintar sobre sede/corredor).
        self.mass_off_zone_kg: float = 0.0
        self.area_off_zone_m2: float = 0.0
        self.last_t: float | None = None
        # Posição da última amostra com spreading=True processada por update().
        # Usado para detectar interrupções (curva em U entre faixas) onde
        # update() não foi chamado durante a transição. Sem isso a primeira
        # amostra da nova faixa receberia delta_area = width × v × dt
        # acumulado ao longo da curva inteira (~19 s no boustrofédico padrão),
        # inflando a massa dessa zona por uns 5-10%.
        self.last_x: float | None = None
        self.last_y: float | None = None
        # Grid de cobertura (opcional): se bbox foi passado, registra cada
        # disco pintado pelo trator num grid 2D para diagnóstico de sobras.
        self.coverage_grid: CoverageGrid | None = (
            CoverageGrid(bbox, cell_size_m=coverage_cell_m)
            if bbox is not None
            else None
        )
        self.swath_radius_m: float = width_m / 2.0
        # Última `dose_fn` recebida em update(). Usada por
        # grid_zone_stats() para integrar massa via grid (cada célula
        # entra uma vez, sem dupla contagem por sobreposição de retângulos
        # de pintura). Para Zonas, dose_fn dentro de uma zona retorna
        # rate_alvo da zona (constante); para IDW, retorna o valor
        # interpolado naquele ponto.
        self.dose_fn: DoseFn | None = None
        # Cache do grid_zone_stats. Após apply_virtual_correction o
        # estado da grid é final — disparar precompute_zone_stats() já
        # popula o cache, e ao usuário pedir o relatório (espaço) a
        # render é instantânea em vez de bloquear ~3 s no cálculo.
        self._zone_stats_cache: dict[str, dict[str, float]] | None = None
        # Caminho rápido em modo Zonas: dose_fn dentro de uma zona é
        # constante (= zone.rate), então mass_per_zone = N_painted ×
        # rate × cell_area, sem chamar dose_fn por célula. Em modo IDW
        # a dose varia por célula, então o cálculo é per-cell mesmo.
        self.is_zones_mode = is_zones_mode

    def _unique_label(self, idx: int) -> str:
        """Numera zonas sem rótulo (Z1, Z2…) e sufixa o rótulo com a dose se
        houver outra zona com o mesmo nome (ex.: dois 'Poor' com doses distintas)."""
        z = self.zones[idx]
        if not z.label:
            return f"Z{idx + 1}"
        same = [other for other in self.zones if other.label == z.label]
        if len(same) <= 1:
            return z.label
        return f"{z.label}={z.rate:g}"

    def update(
        self,
        x: float,
        y: float,
        t: float,
        v: float | None,
        dose_fn: DoseFn | None = None,
    ) -> None:
        """Acumula dose conforme o trator passa pelo ponto (x,y).

        Se `dose_fn` for fornecida, a taxa local é `dose_fn(x, y)` (modo IDW
        puro, dose variável); caso contrário usa-se `zone.rate` constante
        (modo zonas de manejo, comportamento original)."""
        if dose_fn is not None:
            self.dose_fn = dose_fn
        if v is None or v <= 0:
            return
        if self.last_t is None or self.last_x is None or self.last_y is None:
            self.last_t = t
            self.last_x = x
            self.last_y = y
            return
        dt = max(t - self.last_t, 0.0)
        dx = x - self.last_x
        dy = y - self.last_y
        displacement = math.hypot(dx, dy)
        self.last_t = t
        self.last_x = x
        self.last_y = y
        if dt <= 0:
            return
        # Detecção de interrupção: amostra muito longe da anterior indica
        # que update() não foi chamado durante uma curva em U ou trânsito
        # (spreading=False). Pula a acumulação para essa amostra "bridge"
        # — ela só serve pra atualizar last_t/last_x/last_y, e a próxima
        # amostra contínua usa um dt normal. Threshold 5 m é bem acima do
        # passo típico de 1 m mas bem abaixo de strip_spacing/arc_length.
        GAP_THRESHOLD_M = 5.0
        if displacement > GAP_THRESHOLD_M:
            return
        eps = self.rng.gauss(0.0, self.noise_std)
        eps = max(-self.noise_clip, min(self.noise_clip, eps))
        delta_area_m2 = self.width_m * v * dt
        zone_idx = self._find_zone_idx(x, y)
        # Taxa efetiva: dose_fn local (IDW) ou rate fixa da zona (Zonas).
        if dose_fn is not None:
            local_rate = dose_fn(x, y)
        elif zone_idx is not None:
            local_rate = self.zones[zone_idx].rate
        else:
            return  # Sem dose_fn nem zona: não há o que aplicar
        delta_kg = local_rate * (1.0 + eps) * delta_area_m2 / 10_000.0
        # Grid de cobertura: marca o disco pintado pelo trator. Independe da
        # classificação por zona — registra QUALQUER ponto pintado, dentro ou
        # fora de inclusões/exclusões. Após a simulação, coverage_in_polygon
        # devolve a cobertura real por zona (sem a super-estimativa do
        # acumulador rectangular).
        if self.coverage_grid is not None:
            self.coverage_grid.mark_disk(x, y, self.swath_radius_m)
        if zone_idx is None:
            # Pintura fora de zonas-alvo (corredor, sede, etc.). Acumula no
            # bucket global "off-zone" para o relatório evidenciar desperdício.
            self.mass_off_zone_kg += delta_kg
            self.area_off_zone_m2 += delta_area_m2
            return
        acc = self.acc[zone_idx]
        acc.massa_aplicada_kg += delta_kg
        acc.area_coberta_m2 += delta_area_m2

    def _find_zone_idx(self, x: float, y: float) -> int | None:
        """Devolve o índice da zona de menor área que contém (x, y) — assim
        zonas específicas (sub-zonas) prevalecem sobre zonas-fundo na contagem.

        Exclusões (polígono Sede, círculos de pedra) têm prioridade absoluta:
        se (x, y) cai dentro de uma exclusão, devolve None para que a
        aplicação seja classificada como "fora de zona" (mass_off_zone_kg),
        mesmo que uma zona de inclusão maior também contenha o ponto. Isso
        evidencia o desperdício do IDW puro, que ignora limites e aplica
        produto sobre Sede/pedras.
        """
        for z in self.exclusion_polys:
            if point_in_polygon(x, y, z.coords_xy):
                return None
        for c in self.exclusion_circles:
            if (x - c.x) ** 2 + (y - c.y) ** 2 <= c.radius_m**2:
                return None
        smallest_area: float | None = None
        smallest_idx: int | None = None
        for i, z in enumerate(self.zones):
            if point_in_polygon(x, y, z.coords_xy):
                if smallest_area is None or z.area_m2 < smallest_area:
                    smallest_area = z.area_m2
                    smallest_idx = i
        return smallest_idx

    def apply_virtual_correction(self, dose_fn: DoseFn) -> int:
        """Preenche as células ainda em False dentro de zonas de inclusão (e
        fora de exclusões) com a taxa do algoritmo no centro da célula —
        sem simular movimento de trator. Operação computacional pura,
        executada uma única vez no fim da simulação.

        Para cada célula corrigida:
        - marca self.coverage_grid.painted como True;
        - acumula em ZoneAccumulator.massa_correcao_kg e area_correcao_m2 da
          zona à qual a célula pertence (mesma lógica de _find_zone_idx).

        Retorna o número de células corrigidas (útil para log).
        """
        if self.coverage_grid is None or not self.zones:
            return 0
        grid = self.coverage_grid
        cell_size = grid.cell_size_m
        cell_area = cell_size**2
        # Bbox combinado das zonas de inclusão para limitar a varredura.
        all_xs = [p[0] for z in self.zones for p in z.coords_xy]
        all_ys = [p[1] for z in self.zones for p in z.coords_xy]
        ix0 = max(0, int((min(all_xs) - grid.xmin) / cell_size))
        ix1 = min(
            grid.nx, int((max(all_xs) - grid.xmin) / cell_size) + 1
        )
        iy0 = max(0, int((min(all_ys) - grid.ymin) / cell_size))
        iy1 = min(
            grid.ny, int((max(all_ys) - grid.ymin) / cell_size) + 1
        )
        n_corrected = 0
        for iy in range(iy0, iy1):
            cy = grid.ymin + (iy + 0.5) * cell_size
            row = grid.painted[iy]
            for ix in range(ix0, ix1):
                if row[ix]:
                    continue
                cx = grid.xmin + (ix + 0.5) * cell_size
                zone_idx = self._find_zone_idx(cx, cy)
                if zone_idx is None:
                    continue
                rate = dose_fn(cx, cy)
                if rate <= 0:
                    continue
                self.acc[zone_idx].massa_correcao_kg += (
                    rate * cell_area / 10_000.0
                )
                self.acc[zone_idx].area_correcao_m2 += cell_area
                grid.painted[iy, ix] = True
                grid.is_correction[iy, ix] = True
                n_corrected += 1
        return n_corrected

    def grid_coverage_stats(
        self,
    ) -> list[dict[str, float | str]] | None:
        """Cobertura real por zona, calculada a partir do grid de células.

        Retorna lista [{zona, painted_m2, total_m2, coverage_pct, missed_m2},
        ...] ou None se o grid não foi habilitado (bbox=None no construtor).

        Diferente do `cobertura_pct` legacy, que soma rectângulos sobrepostos
        (super-estima e pode passar de 100%), este conta cada célula 1×, o
        que dá a cobertura verdadeira — útil para diagnosticar buracos do
        plano de cobertura (sobras das faixas, cantos irregulares).
        """
        if self.coverage_grid is None:
            return None
        cell_area = self.coverage_grid.cell_size_m**2
        out: list[dict[str, float | str]] = []
        for i, z in enumerate(self.zones):
            n_painted, n_total = self.coverage_grid.coverage_in_polygon(
                z.coords_xy
            )
            painted_m2 = n_painted * cell_area
            total_m2 = n_total * cell_area
            cov_pct = 100.0 * n_painted / max(n_total, 1)
            out.append(
                {
                    "zona": self.acc[i].label,
                    "painted_m2": round(painted_m2, 1),
                    "total_m2": round(total_m2, 1),
                    "coverage_pct": round(cov_pct, 2),
                    "missed_m2": round(total_m2 - painted_m2, 1),
                }
            )
        return out

    def _build_zone_idx_map(self) -> np.ndarray:
        """Mapa (ny, nx) int16 com o índice da zona MENOR que contém cada
        célula, ou -1 se fora de qualquer zona. Vetorizado em numpy: para
        cada zona iteramos uma vez seu bbox e usamos point-in-polygon
        vetorizado. Ordem: zonas maiores primeiro (são sobrepostas pelas
        menores depois — mesma regra de "smallest area wins" do dose_at).
        Exclusões (polígonos/círculos) têm prioridade absoluta e zeram o
        mapa de volta para -1 onde acontecerem.
        """
        grid = self.coverage_grid
        ny, nx = grid.painted.shape
        cell_size = grid.cell_size_m
        cy_col = grid.ymin + (np.arange(ny) + 0.5) * cell_size
        cx_row = grid.xmin + (np.arange(nx) + 0.5) * cell_size
        zone_map = np.full((ny, nx), -1, dtype=np.int16)
        sorted_indices = sorted(
            range(len(self.zones)), key=lambda i: -self.zones[i].area_m2
        )

        def _zone_bbox_cells(coords: list[tuple[float, float]]) -> tuple[int, int, int, int]:
            xs_z = [p[0] for p in coords]
            ys_z = [p[1] for p in coords]
            ix0 = max(0, int((min(xs_z) - grid.xmin) / cell_size))
            ix1 = min(nx, int((max(xs_z) - grid.xmin) / cell_size) + 1)
            iy0 = max(0, int((min(ys_z) - grid.ymin) / cell_size))
            iy1 = min(ny, int((max(ys_z) - grid.ymin) / cell_size) + 1)
            return ix0, ix1, iy0, iy1

        for zi in sorted_indices:
            z = self.zones[zi]
            ix0, ix1, iy0, iy1 = _zone_bbox_cells(z.coords_xy)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            sub_yy = np.broadcast_to(
                cy_col[iy0:iy1, None], (iy1 - iy0, ix1 - ix0)
            )
            sub_xx = np.broadcast_to(
                cx_row[None, ix0:ix1], (iy1 - iy0, ix1 - ix0)
            )
            inside = _pip_vectorized(sub_xx, sub_yy, z.coords_xy)
            sub_map = zone_map[iy0:iy1, ix0:ix1]
            sub_map[inside] = zi
        # Exclusões (polígonos): override para -1.
        for ex in self.exclusion_polys:
            ix0, ix1, iy0, iy1 = _zone_bbox_cells(ex.coords_xy)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            sub_yy = np.broadcast_to(
                cy_col[iy0:iy1, None], (iy1 - iy0, ix1 - ix0)
            )
            sub_xx = np.broadcast_to(
                cx_row[None, ix0:ix1], (iy1 - iy0, ix1 - ix0)
            )
            inside_ex = _pip_vectorized(sub_xx, sub_yy, ex.coords_xy)
            sub_map = zone_map[iy0:iy1, ix0:ix1]
            sub_map[inside_ex] = -1
        # Exclusões (círculos): override para -1.
        for c in self.exclusion_circles:
            ix0 = max(0, int((c.x - c.radius_m - grid.xmin) / cell_size))
            ix1 = min(nx, int((c.x + c.radius_m - grid.xmin) / cell_size) + 1)
            iy0 = max(0, int((c.y - c.radius_m - grid.ymin) / cell_size))
            iy1 = min(ny, int((c.y + c.radius_m - grid.ymin) / cell_size) + 1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            sub_yy = np.broadcast_to(
                cy_col[iy0:iy1, None], (iy1 - iy0, ix1 - ix0)
            )
            sub_xx = np.broadcast_to(
                cx_row[None, ix0:ix1], (iy1 - iy0, ix1 - ix0)
            )
            in_circ = (sub_xx - c.x) ** 2 + (sub_yy - c.y) ** 2 <= c.radius_m**2
            sub_map = zone_map[iy0:iy1, ix0:ix1]
            sub_map[in_circ] = -1
        return zone_map

    def grid_zone_stats(
        self,
    ) -> dict[str, dict[str, float]] | None:
        """Estatísticas por zona via integração na grid:
            - area_m2: área exclusiva (cells atribuídas à zona menor que
              contém cada célula);
            - mass_real_kg: massa aplicada pelo trator;
            - mass_corr_kg: massa do preenchimento virtual;
            - cv_pct: coeficiente de variação das taxas aplicadas dentro
              da zona (std/média × 100). 0 = uniforme;
            - within_5pct: % de células com taxa dentro de ±5 % da
              desejada. 100 = todas dentro do alvo;
            - mape_pct: mean absolute percentage error das taxas em
              relação à taxa desejada. 0 = aplicação igual ao planejado.

        Vetorizado: pré-computa zone_idx_map e usa máscaras numpy. Em
        modo Zonas (`is_zones_mode=True`), `dose_fn` dentro da zona é
        constante (= rate), então cv/within/mape são triviais (0/100/0)
        — caminho 10×+ mais rápido. Em modo IDW coleta as taxas
        célula-a-célula via dose_fn e calcula as métricas.

        Cacheado em `_zone_stats_cache`. Use precompute_zone_stats() para
        forçar o cálculo cedo (ao fim da simulação).
        """
        if self._zone_stats_cache is not None:
            return self._zone_stats_cache
        if self.coverage_grid is None or self.dose_fn is None:
            return None
        if not self.zones:
            return {}
        grid = self.coverage_grid
        cell_size = grid.cell_size_m
        cell_area = cell_size**2
        zone_map = self._build_zone_idx_map()
        cy_col = grid.ymin + (np.arange(grid.ny) + 0.5) * cell_size
        cx_row = grid.xmin + (np.arange(grid.nx) + 0.5) * cell_size

        out: dict[str, dict[str, float]] = {}
        for zi, acc in enumerate(self.acc):
            target = self.zones[zi].rate
            zone_mask = zone_map == zi
            n_total = int(zone_mask.sum())
            area_m2 = n_total * cell_area
            entry: dict[str, float] = {
                "area_m2": area_m2,
                "mass_real_kg": 0.0,
                "mass_corr_kg": 0.0,
                "cv_pct": 0.0,
                "within_5pct": 100.0 if self.is_zones_mode else 0.0,
                "mape_pct": 0.0,
            }
            if n_total == 0:
                out[acc.label] = entry
                continue
            painted_in_zone = zone_mask & grid.painted
            if not painted_in_zone.any():
                out[acc.label] = entry
                continue
            if self.is_zones_mode:
                # Caminho rápido: rate constante por zona; CV=0,
                # within_5pct=100, MAPE=0 por construção.
                painted_tractor = painted_in_zone & ~grid.is_correction
                painted_corr = painted_in_zone & grid.is_correction
                n_tractor = int(painted_tractor.sum())
                n_corr = int(painted_corr.sum())
                entry["mass_real_kg"] = (
                    n_tractor * target * cell_area / 10_000.0
                )
                entry["mass_corr_kg"] = (
                    n_corr * target * cell_area / 10_000.0
                )
            else:
                # IDW: coleta taxa célula-a-célula via dose_fn.
                iy_arr, ix_arr = np.where(painted_in_zone)
                rates = np.fromiter(
                    (
                        self.dose_fn(float(cx_row[ix]), float(cy_col[iy]))
                        for iy, ix in zip(iy_arr, ix_arr)
                    ),
                    dtype=np.float64,
                    count=len(iy_arr),
                )
                valid = rates > 0
                if not valid.any():
                    out[acc.label] = entry
                    continue
                rates_v = rates[valid]
                corr_mask_full = grid.is_correction[iy_arr, ix_arr]
                corr_v = corr_mask_full[valid]
                entry["mass_real_kg"] = (
                    float(rates_v[~corr_v].sum()) * cell_area / 10_000.0
                )
                entry["mass_corr_kg"] = (
                    float(rates_v[corr_v].sum()) * cell_area / 10_000.0
                )
                # Métricas de variabilidade da aplicação.
                mean_rate = float(rates_v.mean())
                if mean_rate > 0:
                    entry["cv_pct"] = (
                        float(rates_v.std()) / mean_rate * 100.0
                    )
                if target > 0:
                    rel_err = np.abs(rates_v - target) / target
                    entry["mape_pct"] = float(rel_err.mean()) * 100.0
                    entry["within_5pct"] = (
                        float((rel_err <= 0.05).sum())
                        / float(len(rates_v))
                        * 100.0
                    )
            out[acc.label] = entry

        self._zone_stats_cache = out
        return out

    def precompute_zone_stats(self) -> None:
        """Força o cálculo do grid_zone_stats agora e armazena no cache.
        Chamar ao fim da simulação (após apply_virtual_correction) para
        que a render do relatório seja instantânea — sem o usuário
        sentir os 1-3 s do cálculo ao apertar espaço."""
        self._zone_stats_cache = None
        self.grid_zone_stats()

    def rows(self) -> list[dict[str, float | str]]:
        """Linha por zona: alvo (kg/ha), área (ha), planejado (kg), aplicado (kg),
        erro % (massa: gasto efetivo vs planejado) e cobertura %."""
        # Cobertura real por zona vinda do grid (mais precisa que o
        # acumulador rectangular). Se grid desabilitado, cai para o legacy.
        grid_stats = self.grid_coverage_stats()
        grid_cov_by_label: dict[str, float] = (
            {row["zona"]: float(row["coverage_pct"]) for row in grid_stats}
            if grid_stats is not None
            else {}
        )
        # Estatísticas via grid (área exclusiva, massa real, massa correção,
        # CV, ±5%, MAPE por zona). A área exclusiva exclui regiões onde
        # uma zona menor sobrepõe (Sítio Palmar: Nova/Cana dentro de
        # Normal — só conta como Normal a área que NÃO é Nova/Cana). Sem
        # isso, planejado usaria área total mas aplicado só exclusiva,
        # gerando erro artificial enorme. Cai no acumulador legacy se
        # grid não habilitado.
        grid_stats_zone = self.grid_zone_stats()
        out: list[dict[str, float | str]] = []
        for acc in self.acc:
            if grid_stats_zone is not None and acc.label in grid_stats_zone:
                s = grid_stats_zone[acc.label]
                area_ha = s["area_m2"] / 10_000.0
                massa_apl_kg = s["mass_real_kg"]
                massa_corr_kg = s["mass_corr_kg"]
                cv_pct = s["cv_pct"]
                within_5pct = s["within_5pct"]
                mape_pct = s["mape_pct"]
            else:
                area_ha = acc.area_ha
                massa_apl_kg = acc.massa_aplicada_kg
                massa_corr_kg = acc.massa_correcao_kg
                # Sem grid: assume modo Zonas (taxa constante = perfeita).
                cv_pct = 0.0
                within_5pct = 100.0
                mape_pct = 0.0
            massa_alvo_kg = acc.rate_alvo * area_ha
            massa_total_kg = massa_apl_kg + massa_corr_kg
            erro_pct = (
                100.0 * (massa_total_kg - massa_alvo_kg) / massa_alvo_kg
                if massa_alvo_kg
                else 0.0
            )
            # Prefere cobertura do grid (real, sem super-estimativa por
            # sobreposição de retângulos). Após apply_virtual_correction o
            # grid já reflete o preenchimento → cobertura ≈ 100%. Fallback
            # para o cálculo legacy quando o grid não está habilitado.
            if acc.label in grid_cov_by_label:
                cobertura_pct = round(grid_cov_by_label[acc.label], 1)
            else:
                cobertura_pct = round(
                    100.0
                    * acc.area_coberta_m2
                    / max(acc.area_ha * 10_000.0, 1e-9),
                    1,
                )
            out.append(
                {
                    "zona": acc.label,
                    "alvo_kg_ha": round(acc.rate_alvo, 2),
                    "area_ha": round(area_ha, 4),
                    "planejado_kg": round(massa_alvo_kg, 2),
                    "aplicado_kg": round(massa_apl_kg, 2),
                    "correcao_kg": round(massa_corr_kg, 2),
                    "erro_pct": round(erro_pct, 2),
                    "cobertura_pct": cobertura_pct,
                    "cv_pct": round(cv_pct, 2),
                    "within_5pct": round(within_5pct, 1),
                    "mape_pct": round(mape_pct, 2),
                }
            )
        return out

    def _totals_row(
        self, rows: list[dict[str, float | str]]
    ) -> dict[str, float | str] | None:
        """Linha agregada equivalente à row "Total" da tabela do relatório.

        Calcula os mesmos totais ponderados (massa absoluta de desvio, área
        total, médias ponderadas por área de cobertura/CV/±5%, média
        ponderada por massa planejada de MAPE) e devolve um dict com as
        mesmas chaves de `rows()`. Os campos `aplicado_kg` e `erro_pct`
        incluem a contribuição do `mass_off_zone_kg` (mesma convenção do
        render_console e do CSV de "Off-zone"). Devolve None quando não
        há massa planejada ou área agregada (ex.: KML sem zonas).
        """
        if not rows:
            return None
        total_plan = 0.0
        total_appl = 0.0
        total_corr = 0.0
        total_abs_dev = 0.0
        total_area_ha = 0.0
        sum_cov_w = 0.0
        sum_cv_w = 0.0
        sum_within_w = 0.0
        sum_mape_w = 0.0
        for r in rows:
            z_plan = float(r["planejado_kg"])
            z_appl = float(r["aplicado_kg"])
            z_corr = float(r["correcao_kg"])
            z_area = float(r["area_ha"])
            total_plan += z_plan
            total_appl += z_appl
            total_corr += z_corr
            total_abs_dev += abs(z_appl + z_corr - z_plan)
            total_area_ha += z_area
            sum_cov_w += z_area * float(r["cobertura_pct"])
            sum_cv_w += z_area * float(r["cv_pct"])
            sum_within_w += z_area * float(r["within_5pct"])
            sum_mape_w += z_plan * float(r["mape_pct"])
        if self.mass_off_zone_kg > 0:
            total_appl += self.mass_off_zone_kg
            total_abs_dev += self.mass_off_zone_kg
        if total_plan <= 0 or total_area_ha <= 0:
            return None
        return {
            "zona": "Total",
            "alvo_kg_ha": round(total_plan / total_area_ha, 2),
            "area_ha": round(total_area_ha, 4),
            "planejado_kg": round(total_plan, 2),
            "aplicado_kg": round(total_appl, 2),
            "correcao_kg": round(total_corr, 2),
            "erro_pct": round(100.0 * total_abs_dev / total_plan, 2),
            "cobertura_pct": round(sum_cov_w / total_area_ha, 2),
            "cv_pct": round(sum_cv_w / total_area_ha, 2),
            "within_5pct": round(sum_within_w / total_area_ha, 2),
            "mape_pct": round(sum_mape_w / total_plan, 2),
        }

    def render_console(self) -> str:
        """Tabela do relatório formatada com `rich.table.Table` (estilo
        HEAVY_HEAD, headers em 2 linhas via \\n, linha Total separada por
        section). Saída em texto puro com box-drawing Unicode — funciona
        tanto no terminal quanto na renderização monoespaçada do painel
        pygame.

        Para a versão CEA/SBIAGRO 2026 a apresentação visual da tabela é
        importante: bordas Unicode, alinhamento decimal e tipografia
        consistente saem prontos do rich, sem manter formatação manual
        coluna a coluna.
        """
        rows = self.rows()
        # Pré-cálculo dos totais ponderados — precisamos antes de montar
        # as colunas porque a row Total fica após `add_section()` (que
        # insere o separador horizontal entre dados e resumo).
        total_plan = 0.0
        total_appl = 0.0
        total_corr = 0.0
        total_abs_dev = 0.0
        total_area_ha = 0.0
        sum_cov_w = 0.0    # Σ area × cobertura
        sum_cv_w = 0.0     # Σ area × CV
        sum_within_w = 0.0 # Σ area × ±5%
        sum_mape_w = 0.0   # Σ planejado × MAPE
        for r in rows:
            z_plan = float(r["planejado_kg"])
            z_appl = float(r["aplicado_kg"])
            z_corr = float(r["correcao_kg"])
            z_area = float(r["area_ha"])
            total_plan += z_plan
            total_appl += z_appl
            total_corr += z_corr
            total_abs_dev += abs(z_appl + z_corr - z_plan)
            total_area_ha += z_area
            sum_cov_w += z_area * float(r["cobertura_pct"])
            sum_cv_w += z_area * float(r["cv_pct"])
            sum_within_w += z_area * float(r["within_5pct"])
            sum_mape_w += z_plan * float(r["mape_pct"])
        if self.mass_off_zone_kg > 0:
            total_appl += self.mass_off_zone_kg
            total_abs_dev += self.mass_off_zone_kg

        # Header centralizado mesmo quando os dados são alinhados à direita:
        # Text(..., justify="center") sobrescreve o `justify` do Column só
        # para o cabeçalho. Mantém os números alinhados à direita (boa
        # leitura por casa decimal) e os rótulos centrados (visual mais
        # equilibrado para artigo).
        def hdr(key: str) -> Text:
            return Text(t(self.lang, key), justify="center")

        table = Table(
            box=box.ROUNDED,
            header_style="bold",
            show_lines=False,
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column(hdr("hdr_zone"), justify="center", no_wrap=True)
        table.add_column(hdr("hdr_target"), justify="center")
        table.add_column(hdr("hdr_area"), justify="center")
        table.add_column(hdr("hdr_planned"), justify="center")
        table.add_column(hdr("hdr_applied"), justify="center")
        table.add_column(hdr("hdr_correction"), justify="center")
        table.add_column(hdr("hdr_mass_delta"), justify="center")
        table.add_column(hdr("hdr_coverage"), justify="center")
        table.add_column(hdr("hdr_cv"), justify="center")
        table.add_column(hdr("hdr_within"), justify="center")
        table.add_column(hdr("hdr_mape"), justify="center")

        for r in rows:
            table.add_row(
                str(r["zona"]),
                f"{float(r['alvo_kg_ha']):.0f}",
                f"{float(r['area_ha']):.2f}",
                f"{float(r['planejado_kg']):.1f}",
                f"{float(r['aplicado_kg']):.1f}",
                f"{float(r['correcao_kg']):.1f}",
                f"{float(r['erro_pct']):+.2f}",
                f"{float(r['cobertura_pct']):.1f}",
                f"{float(r['cv_pct']):.1f}",
                f"{float(r['within_5pct']):.1f}",
                f"{float(r['mape_pct']):.1f}",
            )
        if self.mass_off_zone_kg > 0:
            table.add_row(
                t(self.lang, "tbl_off_zone"),
                "", "", "",
                f"{self.mass_off_zone_kg:.1f}",
                "", "", "", "", "", "",
            )

        if total_plan > 0 and total_area_ha > 0:
            total_err = 100.0 * total_abs_dev / total_plan
            avg_target = total_plan / total_area_ha
            avg_cov = sum_cov_w / total_area_ha
            avg_cv = sum_cv_w / total_area_ha
            avg_within = sum_within_w / total_area_ha
            avg_mape = sum_mape_w / total_plan
            table.add_section()
            table.add_row(
                "[bold]Total[/bold]",
                f"[bold]{avg_target:.0f}[/bold]",
                f"[bold]{total_area_ha:.2f}[/bold]",
                f"[bold]{total_plan:.1f}[/bold]",
                f"[bold]{total_appl:.1f}[/bold]",
                f"[bold]{total_corr:.1f}[/bold]",
                f"[bold]{total_err:+.2f}[/bold]",
                f"[bold]{avg_cov:.1f}[/bold]",
                f"[bold]{avg_cv:.1f}[/bold]",
                f"[bold]{avg_within:.1f}[/bold]",
                f"[bold]{avg_mape:.1f}[/bold]",
            )

        # Renderização em texto puro (sem ANSI) para o painel pygame —
        # box-drawing Unicode é preservado, marcação [bold]…[/bold] some.
        # Largura fixa generosa para não comprimir colunas.
        buf = io.StringIO()
        Console(
            file=buf,
            force_terminal=False,
            color_system=None,
            width=200,
            legacy_windows=False,
        ).print(table)
        out = buf.getvalue().rstrip()
        out += "\n\n" + t(self.lang, "report_metrics_legend")
        return out

    def write_csv(
        self,
        path: str | Path,
        *,
        params: dict[str, str] | None = None,
    ) -> None:
        """Salva o relatório em CSV no formato exibido na tela.

        Estrutura do arquivo:
          1. Linha de comentário com o título do relatório.
          2. Linhas de comentário "Chave: Valor" para cada item em `params`
             (chaves já traduzidas pelo caller; útil para registrar KML,
             método, largura de aplicação, ruído GNSS, etc.).
          3. Cabeçalho da tabela (header_map) e uma row por zona, mais a
             row "Off-zone" (se houve aplicação fora) e a row "Total"
             agregada.
          4. Linha em branco.
          5. Comentários com a nota de rodapé e a legenda das métricas.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.rows()
        if not rows:
            return
        # Total agregado primeiro, antes de adicionar a row "Off-zone" (que
        # zera vários campos da linha): _totals_row já lê mass_off_zone_kg
        # do estado interno e não pode ver a row do off-zone na lista, sob
        # pena de duplicar a contribuição.
        totals = self._totals_row(rows)
        # Linha extra "Fora de zonas" no CSV quando houve aplicação fora
        # (modo IDW puro): permite ao leitor da tese quantificar o desperdício.
        if self.mass_off_zone_kg > 0:
            rows.append(
                {
                    "zona": t(self.lang, "tbl_off_zone"),
                    "alvo_kg_ha": 0,
                    "area_ha": round(self.area_off_zone_m2 / 10_000.0, 4),
                    "planejado_kg": 0,
                    "aplicado_kg": round(self.mass_off_zone_kg, 2),
                    "correcao_kg": 0,
                    "erro_pct": 0,
                    "cobertura_pct": 0,
                    "cv_pct": 0,
                    "within_5pct": 0,
                    "mape_pct": 0,
                }
            )
        # Linha Total ao fim, com o mesmo conteúdo da última row da tabela
        # do simulador (média ponderada por área de cobertura/CV/±5%, média
        # ponderada por massa planejada de MAPE, soma absoluta de desvios
        # para Δ mass %).
        if totals is not None:
            rows.append(totals)
        header_map = {
            "zona": t(self.lang, "tbl_zone"),
            "alvo_kg_ha": t(self.lang, "tbl_target"),
            "area_ha": t(self.lang, "tbl_area"),
            "planejado_kg": t(self.lang, "tbl_planned_kg"),
            "aplicado_kg": t(self.lang, "tbl_applied_kg"),
            "correcao_kg": t(self.lang, "tbl_correction_kg"),
            "erro_pct": t(self.lang, "tbl_error"),
            "cobertura_pct": t(self.lang, "tbl_coverage"),
            "cv_pct": t(self.lang, "tbl_cv_pct"),
            "within_5pct": t(self.lang, "tbl_within_5pct"),
            "mape_pct": t(self.lang, "tbl_mape_pct"),
        }
        translated_rows = [{header_map[k]: v for k, v in r.items()} for r in rows]
        with path.open("w", newline="", encoding="utf-8") as f:
            # Título da tabela (mesmo do painel pygame) como comentário
            # antes do cabeçalho. Ajuda o leitor humano e permite que
            # ferramentas como pandas ignorem com `comment="#"`.
            f.write(f"# {t(self.lang, 'report_title')}\n")
            # Parâmetros do teste (KML, método, largura, ruído, idioma)
            # também como comentário, na mesma seção do título. Caller
            # passa as chaves já traduzidas para o idioma da run.
            if params:
                for k, v in params.items():
                    f.write(f"# {k}: {v}\n")
                f.write("#\n")
            writer = csv.DictWriter(f, fieldnames=list(translated_rows[0].keys()))
            writer.writeheader()
            writer.writerows(translated_rows)
            # Após a tabela: a nota de rodapé sobre o caráter aproximado da
            # simulação e a legenda das métricas (Δ mass, CV, ±5%, MAPE),
            # nos mesmos termos exibidos na tela.
            f.write("\n")
            f.write(f"# {t(self.lang, 'report_note')}\n")
            f.write("\n")
            for line in t(self.lang, "report_metrics_legend").split("\n"):
                if line.strip():
                    f.write(f"# {line}\n")
