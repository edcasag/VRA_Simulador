"""Acumula dose aplicada por zona e gera relatório (Tab. 6 cap 7 §7.3).

Modelo:
  dose_aplicada[zona] += dose_alvo[zona] · largura_aplicacao · v(t) · Δt · (1 + ε)

onde ε ~ N(0, 0.025²) truncado em ±0.05 representa agregadamente latência do
controlador, atuador e GNSS. Aproximação simplificada documentada no README.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path

from .i18n import t
from .kml_parser import KmlData, Polygon
from .vra_engine import point_in_polygon


@dataclass
class ZoneAccumulator:
    label: str
    rate_alvo: float
    area_ha: float
    massa_aplicada_kg: float = 0.0
    area_coberta_m2: float = 0.0


def polygon_area_m2(coords: list[tuple[float, float]]) -> float:
    """Área de um polígono fechado em m² (fórmula do shoelace)."""
    n = len(coords)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


class CoverageReport:
    def __init__(
        self,
        kml: KmlData,
        width_m: float = 3.0,
        noise_std: float = 0.025,
        noise_clip: float = 0.05,
        seed: int = 42,
        lang: str = "pt",
    ) -> None:
        self.zones: list[Polygon] = [z for z in kml.zones if z.rate > 0]
        self.width_m = width_m
        self.noise_std = noise_std
        self.noise_clip = noise_clip
        self.lang = lang
        self.rng = random.Random(seed)
        self.acc: dict[str, ZoneAccumulator] = {
            z.label: ZoneAccumulator(
                label=z.label,
                rate_alvo=z.rate,
                area_ha=polygon_area_m2(z.coords_xy) / 10_000.0,
            )
            for z in self.zones
        }
        self.last_t: float | None = None

    def update(self, x: float, y: float, t: float, v: float | None) -> None:
        """Acumula dose conforme o trator passa pelo ponto (x,y)."""
        if v is None or v <= 0:
            return
        if self.last_t is None:
            self.last_t = t
            return
        dt = max(t - self.last_t, 0.0)
        self.last_t = t
        if dt <= 0:
            return
        zone = self._find_zone(x, y)
        if zone is None:
            return
        eps = self.rng.gauss(0.0, self.noise_std)
        eps = max(-self.noise_clip, min(self.noise_clip, eps))
        delta_area_m2 = self.width_m * v * dt
        delta_kg = zone.rate * (1.0 + eps) * delta_area_m2 / 10_000.0
        acc = self.acc[zone.label]
        acc.massa_aplicada_kg += delta_kg
        acc.area_coberta_m2 += delta_area_m2

    def _find_zone(self, x: float, y: float) -> Polygon | None:
        for z in self.zones:
            if point_in_polygon(x, y, z.coords_xy):
                return z
        return None

    def rows(self) -> list[dict[str, float | str]]:
        out: list[dict[str, float | str]] = []
        for label, acc in self.acc.items():
            if acc.area_coberta_m2 < 1e-3:
                aplicado = 0.0
            else:
                aplicado = acc.massa_aplicada_kg / (acc.area_coberta_m2 / 10_000.0)
            erro_pct = (
                100.0 * (aplicado - acc.rate_alvo) / acc.rate_alvo if acc.rate_alvo else 0.0
            )
            out.append(
                {
                    "zona": label,
                    "alvo_kg_ha": round(acc.rate_alvo, 2),
                    "aplicado_kg_ha": round(aplicado, 2),
                    "erro_pct": round(erro_pct, 2),
                    "area_ha": round(acc.area_ha, 4),
                    "cobertura_pct": round(
                        100.0 * acc.area_coberta_m2 / max(acc.area_ha * 10_000.0, 1e-9), 1
                    ),
                }
            )
        return out

    def render_console(self) -> str:
        h_zone = t(self.lang, "tbl_zone")
        h_target = t(self.lang, "tbl_target")
        h_applied = t(self.lang, "tbl_applied")
        h_error = t(self.lang, "tbl_error")
        h_cov = t(self.lang, "tbl_coverage")
        header = f"{h_zone:<5}| {h_target:^12} | {h_applied:^16} | {h_error:^6} | {h_cov:^11}"
        sep = "-" * 5 + "|" + "-" * 14 + "|" + "-" * 18 + "|" + "-" * 8 + "|" + "-" * 13
        lines = [header, sep]
        for r in self.rows():
            lines.append(
                f"{r['zona']:<5}| {r['alvo_kg_ha']:>12} | {r['aplicado_kg_ha']:>16} | "
                f"{r['erro_pct']:>+6} | {r['cobertura_pct']:>11}"
            )
        return "\n".join(lines)

    def write_csv(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.rows()
        if not rows:
            return
        header_map = {
            "zona": t(self.lang, "tbl_zone"),
            "alvo_kg_ha": t(self.lang, "tbl_target"),
            "aplicado_kg_ha": t(self.lang, "tbl_applied"),
            "erro_pct": t(self.lang, "tbl_error"),
            "area_ha": t(self.lang, "tbl_area"),
            "cobertura_pct": t(self.lang, "tbl_coverage"),
        }
        translated_rows = [{header_map[k]: v for k, v in r.items()} for r in rows]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(translated_rows[0].keys()))
            writer.writeheader()
            writer.writerows(translated_rows)
