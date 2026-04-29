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

HEADER_H = 36

from .coverage_report import CoverageReport
from .i18n import t
from .kml_parser import KmlData
from .terrain import TerrainParams, altitude, contour_lines
from .tractor_sim import TractorSample
from .vra_engine import IdwParams, dose_at

# ---------- Colormap dinâmico ----------
# Paleta âncora (verde escuro -> vermelho escuro). Os stops do colormap
# são interpolados nessa paleta conforme o range de doses do KML.
_COLORMAP_ANCHORS: list[tuple[int, int, int]] = [
    (0, 130, 50),       # Verde escuro
    (140, 200, 60),     # Verde claro
    (250, 230, 50),     # Amarelo
    (250, 150, 40),     # Laranja
    (240, 90, 60),      # Vermelho claro
    (210, 50, 50),      # Vermelho médio
    (160, 30, 30),      # Vermelho escuro
]
GRAY_BG = (235, 235, 235)
GRAY_DARK = (110, 110, 110)
GRAY_FIELD = (200, 200, 200)


def _interpolate_anchors(frac: float) -> tuple[int, int, int]:
    """Interpola linearmente entre as N âncoras na posição frac em [0,1]."""
    n = len(_COLORMAP_ANCHORS)
    pos = max(0.0, min(1.0, frac)) * (n - 1)
    i = int(pos)
    if i >= n - 1:
        return _COLORMAP_ANCHORS[-1]
    s = pos - i
    a = _COLORMAP_ANCHORS[i]
    b = _COLORMAP_ANCHORS[i + 1]
    return (
        int(a[0] + s * (b[0] - a[0])),
        int(a[1] + s * (b[1] - a[1])),
        int(a[2] + s * (b[2] - a[2])),
    )


@dataclass
class Colormap:
    """Lista de stops (rate, RGB) cobrindo o range de doses do KML."""
    stops: list[tuple[float, tuple[int, int, int]]]

    def color_for_dose(self, rate: float) -> tuple[int, int, int]:
        """Cor do stop mais próximo. Doses <= 0.5 retornam cinza (exclusão/fora)."""
        if rate <= 0.5:
            return GRAY_FIELD
        best_rate, best_col = self.stops[0]
        best_d = abs(rate - best_rate)
        for stop_rate, stop_col in self.stops[1:]:
            d = abs(rate - stop_rate)
            if d < best_d:
                best_rate, best_col = stop_rate, stop_col
                best_d = d
        return best_col

    def labels(self) -> list[str]:
        """Rótulos dos stops em kg/ha (ex.: '75 kg/ha', '88 kg/ha')."""
        return [f"{int(round(r))} kg/ha" for r, _ in self.stops]


def colormap_from_kml(kml: KmlData, n_stops: int = 8) -> Colormap:
    """Gera N stops uniformemente distribuídos entre min e max das doses > 0
    do KML (zonas de inclusão, círculos de inclusão e amostras com taxa > 0).
    Doses 0 representam exclusões e não entram no cálculo de min/max."""
    rates: list[float] = []
    rates += [z.rate for z in kml.zones if z.rate > 0]
    rates += [c.rate for c in kml.circles if c.rate > 0]
    rates += [s.rate for s in kml.samples if s.rate > 0]
    if not rates:
        # KML sem doses > 0; cai para o range histórico do ensaio A/B/C/D
        min_r, max_r = 50.0, 100.0
    else:
        min_r, max_r = min(rates), max(rates)
        if max_r - min_r < 1.0:
            # Dose única; padding de ±10% para a paleta não colapsar
            pad = max(min_r * 0.1, 5.0)
            min_r -= pad
            max_r += pad
    n = max(2, n_stops)
    stops: list[tuple[float, tuple[int, int, int]]] = []
    for i in range(n):
        frac = i / (n - 1)
        rate = min_r + frac * (max_r - min_r)
        stops.append((rate, _interpolate_anchors(frac)))
    return Colormap(stops=stops)


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
def _draw_zones_filled(
    surf: pygame.Surface, kml: KmlData, vp: Viewport, colormap: Colormap
) -> None:
    surf.fill((255, 255, 255), vp.rect)
    if kml.field_polygon:
        pts = [vp.world_to_screen(*p) for p in kml.field_polygon.coords_xy]
        pygame.draw.polygon(surf, colormap.color_for_dose(kml.field_polygon.rate), pts)
    # Inclusões primeiro
    for z in kml.zones:
        if z.rate > 0:
            pts = [vp.world_to_screen(*p) for p in z.coords_xy]
            pygame.draw.polygon(surf, colormap.color_for_dose(z.rate), pts)
    # Exclusões por cima
    for z in kml.zones:
        if z.rate == 0:
            pts = [vp.world_to_screen(*p) for p in z.coords_xy]
            pygame.draw.polygon(surf, (90, 90, 90), pts)
    # Círculos
    for c in kml.circles:
        cx, cy = vp.world_to_screen(c.x, c.y)
        rpx = vp.cell_size_px(c.radius_m * 2) // 2
        col = (90, 90, 90) if c.rate == 0 else colormap.color_for_dose(c.rate)
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
    inclusion_idx = 0
    for z in kml.zones:
        if z.rate <= 0:
            continue
        inclusion_idx += 1
        label = z.label or f"Z{inclusion_idx}"
        cx = sum(p[0] for p in z.coords_xy) / len(z.coords_xy)
        cy = sum(p[1] for p in z.coords_xy) / len(z.coords_xy)
        sx, sy = vp.world_to_screen(cx, cy)
        text = font.render(f"{label} = {int(z.rate)} kg/ha", True, (0, 0, 0))
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


def _draw_legend(
    surf: pygame.Surface,
    font: pygame.font.Font,
    x: int,
    y: int,
    colormap: Colormap,
    lang: str,
) -> None:
    box_w = 230
    line_h = 22
    box_h = line_h * (len(colormap.stops) + 1) + 12
    pygame.draw.rect(surf, (255, 255, 255), (x, y, box_w, box_h))
    pygame.draw.rect(surf, (50, 50, 50), (x, y, box_w, box_h), 1)
    title = font.render(t(lang, "legend_title"), True, (0, 0, 0))
    surf.blit(title, (x + 8, y + 6))
    labels = colormap.labels()
    for i, (_rate, color) in enumerate(colormap.stops):
        yy = y + 10 + line_h * (i + 1)
        pygame.draw.rect(surf, color, (x + 8, yy, 22, 16))
        pygame.draw.rect(surf, (50, 50, 50), (x + 8, yy, 22, 16), 1)
        text = font.render(labels[i], True, (0, 0, 0))
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
        text = font.render(f"{k}: {v}", True, (0, 0, 0))
        surf.blit(text, (panel_x + 8, panel_y + 6 + i * 22))


def _format_intro_slides(
    slides_template: list[dict[str, object]], kml: KmlData, width_m: float
) -> list[dict[str, object]]:
    """Substitui placeholders {n_zones}, {min_rate}, {max_rate}, {width_m} nas
    linhas e títulos dos slides com dados do KML atual."""
    inclusion_rates = [z.rate for z in kml.zones if z.rate > 0]
    fmt = {
        "n_zones": len(inclusion_rates),
        "min_rate": min(inclusion_rates) if inclusion_rates else 0.0,
        "max_rate": max(inclusion_rates) if inclusion_rates else 0.0,
        "width_m": width_m,
    }
    out: list[dict[str, object]] = []
    for s in slides_template:
        out.append(
            {
                "title": str(s["title"]).format(**fmt),
                "lines": [str(line).format(**fmt) for line in s["lines"]],  # type: ignore[union-attr]
                "duration_s": s["duration_s"],
            }
        )
    return out


def _draw_intro_slide(
    screen: pygame.Surface,
    title_font: pygame.font.Font,
    body_font: pygame.font.Font,
    footer_font: pygame.font.Font,
    slide: dict[str, object],
    idx: int,
    total: int,
    elapsed: float,
    lang: str,
) -> None:
    """Painel central com um slide da introdução (título + corpo + footer com progresso)."""
    title_str = str(slide["title"])
    body_lines: list[str] = list(slide["lines"])  # type: ignore[arg-type]
    duration = float(slide["duration_s"])  # type: ignore[arg-type]

    title_surf = title_font.render(title_str, True, (0, 0, 0))
    body_surfs = [body_font.render(ln, True, (0, 0, 0)) for ln in body_lines]
    footer_str = t(lang, "slide_footer").format(idx=idx + 1, total=total)
    footer_surf = footer_font.render(footer_str, True, (60, 60, 60))

    line_h = body_font.get_linesize() + 4
    body_h = max(len(body_surfs), 1) * line_h
    body_w = max(
        [title_surf.get_width(), footer_surf.get_width()]
        + [s.get_width() for s in body_surfs]
    )
    panel_w = body_w + 100
    panel_h = title_surf.get_height() + 24 + body_h + 24 + footer_surf.get_height() + 32 + 14
    panel_x = (1280 - panel_w) // 2
    panel_y = (720 - panel_h) // 2

    panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
    panel.fill((255, 255, 245, 240))
    pygame.draw.rect(panel, (50, 50, 50), panel.get_rect(), 2)

    # Título
    panel.blit(title_surf, ((panel_w - title_surf.get_width()) // 2, 18))
    body_y = 18 + title_surf.get_height() + 18
    for i, surf in enumerate(body_surfs):
        panel.blit(surf, (50, body_y + i * line_h))

    # Barra de progresso (tempo do slide)
    bar_y = body_y + body_h + 18
    bar_w = panel_w - 80
    pygame.draw.rect(panel, (220, 220, 220), (40, bar_y, bar_w, 6))
    progress = max(0.0, min(1.0, elapsed / max(duration, 0.001)))
    pygame.draw.rect(panel, (90, 130, 90), (40, bar_y, int(bar_w * progress), 6))

    # Footer
    panel.blit(
        footer_surf,
        ((panel_w - footer_surf.get_width()) // 2, panel_h - footer_surf.get_height() - 14),
    )
    screen.blit(panel, (panel_x, panel_y))


def _draw_ready_banner(
    screen: pygame.Surface, big_font: pygame.font.Font, lang: str
) -> None:
    """Banner final indicando que o usuário pode iniciar."""
    text = big_font.render(t(lang, "ready_banner"), True, (0, 0, 0))
    tw, th = text.get_size()
    panel_w = tw + 80
    panel_h = th + 40
    panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
    panel.fill((255, 255, 200, 245))
    pygame.draw.rect(panel, (50, 50, 50), panel.get_rect(), 2)
    panel.blit(text, ((panel_w - tw) // 2, (panel_h - th) // 2))
    screen.blit(panel, ((1280 - panel_w) // 2, (720 - panel_h) // 2))


def _draw_speed_hint(
    screen: pygame.Surface,
    font: pygame.font.Font,
    left_rect: pygame.Rect,
    legend_height: int,
    speed_factor: float,
    lang: str,
) -> None:
    """Painel pequeno abaixo da legenda mostrando a velocidade atual da
    simulação e o atalho +/− para ajustá-la em tempo real."""
    text = t(lang, "speed_hint").format(speed=speed_factor)
    surf = font.render(text, True, (10, 10, 10))
    panel_w = surf.get_width() + 16
    panel_h = surf.get_height() + 10
    panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
    panel.fill((255, 255, 255, 230))
    pygame.draw.rect(panel, (50, 50, 50), panel.get_rect(), 1)
    panel.blit(surf, (8, 4))
    x = left_rect.x + 12
    y = left_rect.y + 12 + legend_height + 6
    screen.blit(panel, (x, y))


def _draw_press_space_for_report_banner(
    screen: pygame.Surface, big_font: pygame.font.Font, lang: str
) -> None:
    """Banner discreto no rodapé pedindo ESPAÇO para ver o relatório.
    Posicionado no rodapé para não cobrir o resultado pintado da simulação."""
    text = big_font.render(t(lang, "press_space_for_report"), True, (0, 0, 0))
    tw, th = text.get_size()
    panel_w = tw + 80
    panel_h = th + 28
    panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
    panel.fill((255, 255, 200, 245))
    pygame.draw.rect(panel, (50, 50, 50), panel.get_rect(), 2)
    panel.blit(text, ((panel_w - tw) // 2, (panel_h - th) // 2))
    # Rodapé inferior centro (mantém os painéis pintados visíveis)
    screen.blit(panel, ((1280 - panel_w) // 2, 720 - panel_h - 20))


def _draw_report_panel(
    screen: pygame.Surface,
    mono_font: pygame.font.Font,
    big_font: pygame.font.Font,
    note_font: pygame.font.Font,
    report_lines: list[str],
    lang: str,
) -> None:
    """Painel central com o relatório de aplicação por zona ao final da simulação."""
    title_str = t(lang, "report_title")
    footer_str = t(lang, "report_footer")
    note_str = t(lang, "report_note")
    line_h = 22
    title = big_font.render(title_str, True, (0, 0, 0))
    footer = big_font.render(footer_str, True, (60, 60, 60))
    note = note_font.render(note_str, True, (10, 10, 10))
    rendered_lines = [mono_font.render(ln, True, (0, 0, 0)) for ln in report_lines]
    body_w = max(ln.get_width() for ln in rendered_lines)
    panel_w = max(
        title.get_width(), footer.get_width(), note.get_width(), body_w
    ) + 60
    panel_h = (
        title.get_height()
        + 16
        + len(rendered_lines) * line_h
        + 12
        + note.get_height()
        + 16
        + footer.get_height()
        + 40
    )
    panel_x = (1280 - panel_w) // 2
    panel_y = (720 - panel_h) // 2
    panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
    panel.fill((255, 255, 245, 240))
    pygame.draw.rect(panel, (50, 50, 50), panel.get_rect(), 2)
    panel.blit(title, ((panel_w - title.get_width()) // 2, 16))
    body_y = 16 + title.get_height() + 16
    for i, ln_surf in enumerate(rendered_lines):
        panel.blit(ln_surf, (30, body_y + i * line_h))
    note_y = body_y + len(rendered_lines) * line_h + 12
    panel.blit(note, ((panel_w - note.get_width()) // 2, note_y))
    panel.blit(
        footer,
        ((panel_w - footer.get_width()) // 2, panel_h - footer.get_height() - 16),
    )
    screen.blit(panel, (panel_x, panel_y))


# ---------- Loop principal ----------
def run(
    kml: KmlData,
    terrain: TerrainParams,
    samples: Iterator[TractorSample],
    mode_label: str,
    width_m: float = 3.0,
    cell_m: float = 1.0,
    title: str | None = None,
    docs_dir: str | Path = "docs",
    snapshots_at_pct: tuple[int, ...] = (25, 50, 100),
    snapshot_prefix: str = "snapshot",
    speed_factor: float = 8.0,
    max_fps: int = 60,
    paint_offset_back_m: float = 1.0,
    start_paused: bool = False,
    lang: str = "pt",
) -> CoverageReport:
    """Executa a visualização. Devolve o CoverageReport ao terminar."""
    pygame.init()
    pygame.display.set_caption(title or t(lang, "header_title"))
    screen = pygame.display.set_mode((1280, 720))
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("Segoe UI", 14)
    big_font = pygame.font.SysFont("Segoe UI", 18, bold=True)
    mono_font = pygame.font.SysFont("Consolas,Courier New,monospace", 16)
    # Nota do relatório (bold para destacar do corpo da tabela)
    note_font = pygame.font.SysFont("Segoe UI", 16, bold=True)
    # Fontes maiores para os slides de introdução, mais fáceis de ler à distância
    slide_title_font = pygame.font.SysFont("Segoe UI", 32, bold=True)
    slide_body_font = pygame.font.SysFont("Segoe UI", 22, bold=True)

    # Colormap dinâmico baseado no range de doses do KML (zero é exclusão e
    # não conta para min/max).
    colormap = colormap_from_kml(kml)

    bbox = kml.bbox()
    # Margem suficiente para o trator caber durante curvas em U (raio=width_m/2 + folga)
    margin = max(width_m / 2 + 5.0, 0.05 * max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    bbox = (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)

    left_rect = pygame.Rect(0, HEADER_H, 640, 720 - HEADER_H)
    right_rect = pygame.Rect(640, HEADER_H, 640, 720 - HEADER_H)
    vp_left = Viewport(bbox, left_rect)
    vp_right = Viewport(bbox, right_rect)

    # Painel esquerdo (estático)
    static_left = pygame.Surface((1280, 720))
    static_left.fill((250, 250, 250))
    pygame.draw.rect(static_left, GRAY_BG, left_rect)
    _draw_zones_filled(static_left, kml, vp_left, colormap)
    _draw_zone_outlines(static_left, kml, vp_left)
    _draw_zone_labels(static_left, kml, vp_left, big_font)
    _draw_legend(static_left, font, left_rect.x + 12, left_rect.y + 12, colormap, lang)
    # Mesma fórmula de altura usada em _draw_legend (line_h * (n + 1) + 12)
    legend_height = 22 * (len(colormap.stops) + 1) + 12

    # Painel direito (fundo dinâmico): cinza-claro + contornos das zonas (esqueleto)
    static_right = pygame.Surface((640, 720))
    static_right.fill(GRAY_BG)
    # Viewport com origem (0, HEADER_H) — reserva a faixa do cabeçalho
    vp_right_local = Viewport(bbox, pygame.Rect(0, HEADER_H, 640, 720 - HEADER_H))
    # Contorno claro das zonas (apenas traço, sem preenchimento)
    for z in kml.zones:
        pts = [vp_right_local.world_to_screen(*p) for p in z.coords_xy]
        pygame.draw.polygon(static_right, (180, 180, 180), pts, 1)
    # Isolinhas
    _draw_contours(static_right, bbox, terrain, vp_right_local, spacing=0.5)

    # Cabeçalho fixo no topo da janela (sempre visível)
    static_header = pygame.Surface((1280, HEADER_H))
    static_header.fill((245, 245, 245))
    pygame.draw.line(static_header, (50, 50, 50), (0, HEADER_H - 1), (1280, HEADER_H - 1), 1)
    header_text = big_font.render(t(lang, "header_title"), True, (0, 0, 0))
    static_header.blit(
        header_text,
        ((1280 - header_text.get_width()) // 2, (HEADER_H - header_text.get_height()) // 2),
    )

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

    report = CoverageReport(kml, width_m=width_m, lang=lang)
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
            color = colormap.color_for_dose(d)
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
        color = colormap.color_for_dose(d)
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
    paused = start_paused
    sim_time = 0.0
    real_time_acc = 0.0
    snapshots_done: set[int] = set()
    idx = 0
    report_lines: list[str] | None = None
    show_report = False  # após finished, pressionar ESPAÇO para abrir o painel

    # Estado da introdução (slides exibidos enquanto pausado, antes da simulação).
    # Placeholders nas linhas dos slides são preenchidos com dados do KML atual.
    intro_slides = _format_intro_slides(
        t(lang, "intro_slides"), kml, width_m
    ) if start_paused else []
    intro_idx = 0
    intro_slide_start_ms = pygame.time.get_ticks() if intro_slides else 0

    last_sample: TractorSample | None = None
    while running:
        clock.tick(max_fps)

        if not finished and not paused:
            # Avança amostras proporcional ao speed_factor. Durante curvas em U
            base_steps = max(1, int(2 * speed_factor))
            in_curve = last_sample is not None and not last_sample.spreading
            steps_per_frame = max(1, base_steps // 1) if in_curve else base_steps
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
                    if not show_report:
                        # Primeira tecla após terminar: ESPAÇO abre o relatório,
                        # qualquer outra fecha.
                        if event.key == pygame.K_SPACE:
                            show_report = True
                        else:
                            running = False
                    else:
                        # Relatório já visível: qualquer tecla fecha.
                        running = False
                elif event.key == pygame.K_SPACE:
                    paused = not paused
                elif event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key in (
                    pygame.K_PLUS, pygame.K_KP_PLUS, pygame.K_EQUALS
                ):
                    speed_factor = min(speed_factor * 1.5, 30.0)
                elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    speed_factor = max(speed_factor / 1.5, 0.05)

        # Compose
        screen.blit(static_left, (0, 0))
        screen.blit(static_right, (640, 0))
        screen.blit(paint_layer, (640, 0))

        # Painel da velocidade da simulação (atual + atalho +/−), abaixo da legenda
        _draw_speed_hint(screen, font, left_rect, legend_height, speed_factor, lang)

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
                t(lang, "hud_pos"): f"({last_sample.x:.1f}, {last_sample.y:.1f})",
                t(lang, "hud_altitude"): f"{z:+.2f} m",
                t(lang, "hud_speed"): v_str,
                t(lang, "hud_dose"): f"{d:.0f} kg/ha",
                t(lang, "hud_coverage"): f"{100 * idx / total:.0f} %",
                t(lang, "hud_time"): f"{sim_time:.0f} s",
            }
            # HUD ancorado em cima da zona B (NE do mapa, painel esquerdo)
            hud_anchor = pygame.Rect(330, 40, 0, 0)
            _draw_hud(screen, font, hud_anchor, info)

        pygame.draw.line(screen, (0, 0, 0), (640, HEADER_H), (640, 720), 1)

        # Cabeçalho fixo (sempre por cima)
        screen.blit(static_header, (0, 0))

        # Snapshots automáticos: salvos ANTES dos banners/painel, para capturar
        # apenas o resultado pintado (sem cobrir com elementos de UI)
        for tgt in list(snapshots_done):
            path = docs_dir / f"{snapshot_prefix}_{tgt:03d}pct.png"
            if not path.exists():
                pygame.image.save(screen, str(path))

        # Slides introdutórios enquanto pausado, depois banner "Pronto"
        if paused and not finished:
            if intro_slides and intro_idx < len(intro_slides):
                # Avança automaticamente após duration_s
                elapsed_s = (pygame.time.get_ticks() - intro_slide_start_ms) / 1000.0
                slide = intro_slides[intro_idx]
                if elapsed_s >= float(slide["duration_s"]):  # type: ignore[arg-type]
                    intro_idx += 1
                    intro_slide_start_ms = pygame.time.get_ticks()
                if intro_idx < len(intro_slides):
                    _draw_intro_slide(
                        screen,
                        slide_title_font,
                        slide_body_font,
                        font,
                        intro_slides[intro_idx],
                        intro_idx,
                        len(intro_slides),
                        elapsed_s,
                        lang,
                    )
                else:
                    _draw_ready_banner(screen, slide_title_font, lang)
            else:
                _draw_ready_banner(screen, slide_title_font, lang)

        # Quando a simulação termina, primeiro mostra um banner pedindo ESPAÇO
        # (deixando o resultado pintado totalmente visível). Depois de
        # pressionar ESPAÇO, abre o painel central com o relatório.
        if finished:
            if not show_report:
                _draw_press_space_for_report_banner(screen, big_font, lang)
            else:
                if report_lines is None:
                    report_lines = report.render_console().split("\n")
                _draw_report_panel(
                    screen, mono_font, big_font, note_font, report_lines, lang
                )

        pygame.display.flip()

    pygame.quit()
    return report
