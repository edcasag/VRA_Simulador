"""Split-screen pygame: mapa de zonas (esq.) + simulação do trator pintando (dir.).

Layout:
- Janela 1280×720, divisor central
- Painel esquerdo: mapa de zonas pintado integralmente, com legenda
- Painel direito: mesma área em cinza claro; rastro do trator pintado por dose
- Curvas de nível tracejadas no painel direito
- HUD inferior direito: posição, altitude, velocidade, dose, cobertura, tempo, modo
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Centraliza a janela do programa na tela (override via env var externa)
os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

import pygame

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
TRACTOR_IMG_PATH = ASSETS_DIR / "trator.jpg"

from .coverage_report import CoverageReport
from .kml_parser import KmlData
from .terrain import TerrainParams, altitude, contour_lines
from .tractor_sim import TractorSample
from .vra_engine import IdwParams, dose_at

# ---------- Colormap canônico (fig:vra do cap 1) ----------
COLOR_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (50.0, (0, 130, 50)),       # Verde escuro
    (60.0, (140, 200, 60)),     # Verde claro
    (70.0, (250, 230, 50)),     # Amarelo
    (80.0, (250, 150, 40)),     # Laranja
    (85.0, (240, 90, 60)),      # Vermelho claro
    (90.0, (210, 50, 50)),      # Vermelho médio
    (100.0, (160, 30, 30)),     # Vermelho escuro
]
COLOR_NAMES = [
    "Verde escuro 50",
    "Verde claro 60",
    "Amarelo 70",
    "Laranja 80",
    "Vermelho claro 85",
    "Vermelho médio 90",
    "Vermelho escuro 100",
]
GRAY_BG = (235, 235, 235)
GRAY_DARK = (110, 110, 110)
GRAY_FIELD = (200, 200, 200)


def _load_tractor_with_alpha(path: Path, target_h: int = 35, white_threshold: int = 235) -> pygame.Surface:
    """Carrega JPG do trator e converte fundo branco em alpha=0.

    JPEG não tem canal alpha e o ruído de compressão faz com que pixels "brancos"
    sejam (252, 254, 251) etc — colorkey exato não pega. Usa threshold por canal.
    """
    import numpy as np

    raw = pygame.image.load(str(path)).convert_alpha()
    arr3 = pygame.surfarray.array3d(raw)
    mask = (
        (arr3[..., 0] >= white_threshold)
        & (arr3[..., 1] >= white_threshold)
        & (arr3[..., 2] >= white_threshold)
    )
    alpha = pygame.surfarray.pixels_alpha(raw)
    alpha[mask] = 0
    del alpha  # libera o lock antes de redimensionar

    aspect = raw.get_width() / raw.get_height()
    target_w = max(2, int(round(target_h * aspect)))
    return pygame.transform.smoothscale(raw, (target_w, target_h))


def color_for_dose(rate: float) -> tuple[int, int, int]:
    """Vizinho mais próximo na escala canônica. Doses ≤0 retornam cinza."""
    if rate <= 0.5:
        return GRAY_FIELD
    best = COLOR_STOPS[0]
    best_d = abs(rate - best[0])
    for stop in COLOR_STOPS[1:]:
        d = abs(rate - stop[0])
        if d < best_d:
            best = stop
            best_d = d
    return best[1]


# ---------- Transformação coord → tela ----------
@dataclass
class Viewport:
    bbox: tuple[float, float, float, float]
    rect: pygame.Rect
    margin_px: int = 20

    def world_to_screen(self, x: float, y: float) -> tuple[int, int]:
        xmin, ymin, xmax, ymax = self.bbox
        w = self.rect.width - 2 * self.margin_px
        h = self.rect.height - 2 * self.margin_px
        sx = (x - xmin) / max(xmax - xmin, 1e-9) * w
        # y do mundo cresce para o norte; tela cresce para baixo
        sy = (1.0 - (y - ymin) / max(ymax - ymin, 1e-9)) * h
        return int(self.rect.x + self.margin_px + sx), int(self.rect.y + self.margin_px + sy)

    def cell_size_px(self, cell_m: float) -> int:
        xmin, _, xmax, _ = self.bbox
        w = self.rect.width - 2 * self.margin_px
        return max(2, int(cell_m / max(xmax - xmin, 1e-9) * w))


# ---------- Renderização ----------
def _draw_zones_filled(surf: pygame.Surface, kml: KmlData, vp: Viewport) -> None:
    surf.fill((255, 255, 255), vp.rect)
    if kml.field_polygon:
        pts = [vp.world_to_screen(*p) for p in kml.field_polygon.coords_xy]
        pygame.draw.polygon(surf, color_for_dose(kml.field_polygon.rate), pts)
    # Inclusões primeiro
    for z in kml.zones:
        if z.rate > 0:
            pts = [vp.world_to_screen(*p) for p in z.coords_xy]
            pygame.draw.polygon(surf, color_for_dose(z.rate), pts)
    # Exclusões por cima
    for z in kml.zones:
        if z.rate == 0:
            pts = [vp.world_to_screen(*p) for p in z.coords_xy]
            pygame.draw.polygon(surf, (90, 90, 90), pts)
    # Círculos
    for c in kml.circles:
        cx, cy = vp.world_to_screen(c.x, c.y)
        rpx = vp.cell_size_px(c.radius_m * 2) // 2
        col = (90, 90, 90) if c.rate == 0 else color_for_dose(c.rate)
        pygame.draw.circle(surf, col, (cx, cy), max(2, rpx))


def _draw_zone_outlines(surf: pygame.Surface, kml: KmlData, vp: Viewport) -> None:
    for z in kml.zones:
        pts = [vp.world_to_screen(*p) for p in z.coords_xy]
        pygame.draw.polygon(surf, (60, 60, 60), pts, 1)
    if kml.field_polygon:
        pts = [vp.world_to_screen(*p) for p in kml.field_polygon.coords_xy]
        pygame.draw.polygon(surf, (60, 60, 60), pts, 2)


def _draw_zone_labels(
    surf: pygame.Surface, kml: KmlData, vp: Viewport, font: pygame.font.Font
) -> None:
    for z in kml.zones:
        if z.rate <= 0:
            continue
        cx = sum(p[0] for p in z.coords_xy) / len(z.coords_xy)
        cy = sum(p[1] for p in z.coords_xy) / len(z.coords_xy)
        sx, sy = vp.world_to_screen(cx, cy)
        text = font.render(f"{z.label}={int(z.rate)}", True, (10, 10, 10))
        rect = text.get_rect(center=(sx, sy))
        bg = pygame.Surface((rect.width + 6, rect.height + 4), pygame.SRCALPHA)
        bg.fill((255, 255, 255, 200))
        surf.blit(bg, (rect.x - 3, rect.y - 2))
        surf.blit(text, rect)


def _draw_contours(
    surf: pygame.Surface,
    bbox: tuple[float, float, float, float],
    terrain: TerrainParams,
    vp: Viewport,
    spacing: float = 0.5,
) -> None:
    segs = contour_lines(bbox, terrain, spacing=spacing, grid=80)
    for seg in segs:
        if len(seg) < 2:
            continue
        p1 = vp.world_to_screen(*seg[0])
        p2 = vp.world_to_screen(*seg[1])
        pygame.draw.line(surf, (90, 90, 90), p1, p2, 1)


def _draw_legend(surf: pygame.Surface, font: pygame.font.Font, x: int, y: int) -> None:
    box_w = 230
    line_h = 22
    box_h = line_h * (len(COLOR_STOPS) + 1) + 12
    pygame.draw.rect(surf, (255, 255, 255), (x, y, box_w, box_h))
    pygame.draw.rect(surf, (50, 50, 50), (x, y, box_w, box_h), 1)
    title = font.render("Taxa de aplicação (kg/ha)", True, (10, 10, 10))
    surf.blit(title, (x + 8, y + 6))
    for i, (rate, color) in enumerate(COLOR_STOPS):
        yy = y + 10 + line_h * (i + 1)
        pygame.draw.rect(surf, color, (x + 8, yy, 22, 16))
        pygame.draw.rect(surf, (50, 50, 50), (x + 8, yy, 22, 16), 1)
        text = font.render(COLOR_NAMES[i], True, (10, 10, 10))
        surf.blit(text, (x + 36, yy))


def _draw_hud(
    surf: pygame.Surface,
    font: pygame.font.Font,
    rect: pygame.Rect,
    info: dict[str, str],
) -> None:
    """Painel HUD ancorado no canto superior esquerdo de `rect`."""
    panel_h = len(info) * 22 + 12
    panel_w = 220
    panel_x = rect.left + 12
    panel_y = rect.top + 12
    panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
    panel.fill((255, 255, 255, 220))
    pygame.draw.rect(panel, (50, 50, 50), panel.get_rect(), 1)
    surf.blit(panel, (panel_x, panel_y))
    for i, (k, v) in enumerate(info.items()):
        text = font.render(f"{k}: {v}", True, (10, 10, 10))
        surf.blit(text, (panel_x + 8, panel_y + 6 + i * 22))


# ---------- Loop principal ----------
def run(
    kml: KmlData,
    terrain: TerrainParams,
    samples: Iterator[TractorSample],
    mode_label: str,
    width_m: float = 3.0,
    cell_m: float = 1.0,
    title: str = "Simulador VRA — Tese de Mestrado POLI/USP",
    docs_dir: str | Path = "docs",
    snapshots_at_pct: tuple[int, ...] = (25, 50, 100),
    snapshot_prefix: str = "snapshot",
    speed_factor: float = 8.0,
    max_fps: int = 60,
    paint_offset_back_m: float = 1.0,
) -> CoverageReport:
    """Executa a visualização. Devolve o CoverageReport ao terminar."""
    pygame.init()
    pygame.display.set_caption(title)
    screen = pygame.display.set_mode((1280, 720))
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("Segoe UI", 14)
    big_font = pygame.font.SysFont("Segoe UI", 18, bold=True)

    bbox = kml.bbox()
    # Margem suficiente para o trator caber durante curvas em U (raio=width_m/2 + folga)
    margin = max(width_m / 2 + 5.0, 0.05 * max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    bbox = (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)

    left_rect = pygame.Rect(0, 0, 640, 720)
    right_rect = pygame.Rect(640, 0, 640, 720)
    vp_left = Viewport(bbox, left_rect)
    vp_right = Viewport(bbox, right_rect)

    # Painel esquerdo (estático)
    static_left = pygame.Surface((1280, 720))
    static_left.fill((250, 250, 250))
    pygame.draw.rect(static_left, GRAY_BG, left_rect)
    _draw_zones_filled(static_left, kml, vp_left)
    _draw_zone_outlines(static_left, kml, vp_left)
    _draw_zone_labels(static_left, kml, vp_left, big_font)
    _draw_legend(static_left, font, left_rect.x + 12, left_rect.y + 12)

    # Painel direito (fundo dinâmico): cinza-claro + contornos das zonas (esqueleto)
    static_right = pygame.Surface((640, 720))
    static_right.fill(GRAY_BG)
    # Viewport com origem (0,0) usado tanto para isolinhas como para pintura do trator
    vp_right_local = Viewport(bbox, pygame.Rect(0, 0, 640, 720))
    # Contorno claro das zonas (apenas traço, sem preenchimento)
    for z in kml.zones:
        pts = [vp_right_local.world_to_screen(*p) for p in z.coords_xy]
        pygame.draw.polygon(static_right, (180, 180, 180), pts, 1)
    # Isolinhas
    _draw_contours(static_right, bbox, terrain, vp_right_local, spacing=0.5)

    # Camada de pintura do trator (acumulada)
    paint_layer = pygame.Surface((640, 720), pygame.SRCALPHA)

    # Ícone do trator (visto de cima): branco do JPG vira transparente por threshold
    # (colorkey sozinho não funciona com JPEG por causa do ruído de compressão).
    tractor_img: pygame.Surface | None = None
    if TRACTOR_IMG_PATH.exists():
        try:
            tractor_img = _load_tractor_with_alpha(TRACTOR_IMG_PATH, target_h=35)
        except (pygame.error, OSError, ImportError):
            tractor_img = None

    report = CoverageReport(kml, width_m=width_m)
    docs_dir = Path(docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Total estimado para barras de progresso (apenas para snapshots)
    samples_list: list[TractorSample] | None = None
    if snapshots_at_pct:
        samples_list = list(samples)
        total = max(1, len(samples_list))
        sample_iter: Iterator[TractorSample] = iter(samples_list)
    else:
        total = 1
        sample_iter = samples

    paint_step_m = max(0.5, cell_m)  # profundidade longitudinal do retângulo de pintura

    def _paint_swath(s: TractorSample) -> tuple[float, float]:
        """Pinta um retângulo de paint_step_m (longitudinal) × width_m (perpendicular)
        deslocado paint_offset_back_m atrás do trator (eixo do distribuidor de discos),
        orientado pelo heading. Devolve as coordenadas do centro da pintura."""
        if s.heading is None:
            d = dose_at(s.x, s.y, kml)
            color = color_for_dose(d)
            sx, sy = vp_right_local.world_to_screen(s.x, s.y)
            sz = vp_right_local.cell_size_px(paint_step_m)
            pygame.draw.rect(
                paint_layer,
                (*color, 230),
                (sx - sz // 2, sy - sz // 2, sz, sz),
            )
            return s.x, s.y
        hx, hy = s.heading
        norm = math.hypot(hx, hy) or 1.0
        hx /= norm
        hy /= norm
        # Centro da pintura deslocado para trás do trator (no sentido -heading)
        cx = s.x - hx * paint_offset_back_m
        cy = s.y - hy * paint_offset_back_m
        d = dose_at(cx, cy, kml)
        color = color_for_dose(d)
        # Vetor perpendicular (rotação 90°)
        perp_x, perp_y = -hy, hx
        hl = paint_step_m / 2.0
        hw = width_m / 2.0
        corners_world = [
            (cx + hx * hl + perp_x * hw, cy + hy * hl + perp_y * hw),
            (cx - hx * hl + perp_x * hw, cy - hy * hl + perp_y * hw),
            (cx - hx * hl - perp_x * hw, cy - hy * hl - perp_y * hw),
            (cx + hx * hl - perp_x * hw, cy + hy * hl - perp_y * hw),
        ]
        corners_screen = [vp_right_local.world_to_screen(*p) for p in corners_world]
        pygame.draw.polygon(paint_layer, (*color, 230), corners_screen)
        return cx, cy

    running = True
    finished = False
    sim_time = 0.0
    real_time_acc = 0.0
    snapshots_done: set[int] = set()
    idx = 0

    last_sample: TractorSample | None = None
    while running:
        clock.tick(max_fps)

        if not finished:
            # Avança amostras proporcional ao speed_factor. Durante curvas em U
            # (spreading=False), reduz a 1/3 para a manobra ficar visível.
            base_steps = max(1, int(2 * speed_factor))
            in_curve = last_sample is not None and not last_sample.spreading
            steps_per_frame = max(1, base_steps // 3) if in_curve else base_steps
            for _ in range(steps_per_frame):
                try:
                    s = next(sample_iter)
                except StopIteration:
                    finished = True
                    break
                idx += 1
                last_sample = s
                sim_time = s.t
                if s.spreading:
                    paint_x, paint_y = _paint_swath(s)
                    report.update(paint_x, paint_y, s.t if s.v else sim_time, s.v)
                # Snapshots
                pct = int(100 * idx / total)
                for tgt in snapshots_at_pct:
                    if pct >= tgt and tgt not in snapshots_done:
                        snapshots_done.add(tgt)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_s:
                    pct = int(100 * idx / total)
                    path = docs_dir / f"{snapshot_prefix}_manual_{pct}pct.png"
                    pygame.image.save(screen, str(path))
                elif finished:
                    # Após terminar, qualquer tecla fecha
                    running = False
                elif event.key == pygame.K_ESCAPE:
                    running = False

        # Compose
        screen.blit(static_left, (0, 0))
        screen.blit(static_right, (640, 0))
        screen.blit(paint_layer, (640, 0))

        # Trator atual (ícone rotacionado conforme heading; círculo amarelo se sem heading
        # ou sem imagem carregada)
        if last_sample is not None:
            sx, sy = vp_right.world_to_screen(last_sample.x, last_sample.y)
            if tractor_img is not None and last_sample.heading is not None:
                hx, hy = last_sample.heading
                # Ícone com frente para cima → atan2(hy, hx) - 90° (pygame gira CCW)
                angle = math.degrees(math.atan2(hy, hx)) - 90.0
                rotated = pygame.transform.rotate(tractor_img, angle)
                rect = rotated.get_rect(center=(sx, sy))
                screen.blit(rotated, rect)
            else:
                pygame.draw.circle(screen, (0, 0, 0), (sx, sy), 6, 2)
                pygame.draw.circle(screen, (255, 255, 0), (sx, sy), 4)

        # HUD
        if last_sample is not None:
            d = dose_at(last_sample.x, last_sample.y, kml)
            z = altitude(last_sample.x, last_sample.y, terrain)
            if last_sample.v is not None:
                v_str = f"{last_sample.v * 3.6:.1f} km/h"
            else:
                v_str = "—"
            info = {
                "Pos (m)": f"({last_sample.x:.1f}, {last_sample.y:.1f})",
                "Altitude": f"{z:+.2f} m",
                "Velocidade": v_str,
                "Dose alvo": f"{d:.0f} kg/ha",
                "Cobertura": f"{100 * idx / total:.0f} %",
                "Tempo sim.": f"{sim_time:.0f} s",
            }
            # HUD ancorado em cima da zona B (NE do mapa, painel esquerdo)
            hud_anchor = pygame.Rect(330, 40, 0, 0)
            _draw_hud(screen, font, hud_anchor, info)

        pygame.draw.line(screen, (0, 0, 0), (640, 0), (640, 720), 1)

        # Banner de "concluído" centralizado, esperando tecla
        if finished:
            msg = "Simulação concluída — pressione qualquer tecla para fechar"
            text = big_font.render(msg, True, (10, 10, 10))
            tw, th = text.get_size()
            banner = pygame.Surface((tw + 40, th + 24), pygame.SRCALPHA)
            banner.fill((255, 255, 200, 235))
            pygame.draw.rect(banner, (50, 50, 50), banner.get_rect(), 2)
            screen.blit(banner, ((1280 - banner.get_width()) // 2, 12))
            screen.blit(text, ((1280 - tw) // 2, 12 + 12))

        pygame.display.flip()

        # Snapshots automáticos
        for tgt in list(snapshots_done):
            path = docs_dir / f"{snapshot_prefix}_{tgt:03d}pct.png"
            if not path.exists():
                pygame.image.save(screen, str(path))

    pygame.quit()
    return report
