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
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .i18n import t
from .kml_parser import KmlData, Polygon, polygon_area_m2  # noqa: F401 (re-export)
from .vra_engine import point_in_polygon

DoseFn = Callable[[float, float], float]


@dataclass
class ZoneAccumulator:
    label: str
    rate_alvo: float
    area_ha: float
    massa_aplicada_kg: float = 0.0
    area_coberta_m2: float = 0.0


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
        if v is None or v <= 0:
            return
        if self.last_t is None:
            self.last_t = t
            return
        dt = max(t - self.last_t, 0.0)
        self.last_t = t
        if dt <= 0:
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
        zonas específicas (sub-zonas) prevalecem sobre zonas-fundo na contagem."""
        smallest_area: float | None = None
        smallest_idx: int | None = None
        for i, z in enumerate(self.zones):
            if point_in_polygon(x, y, z.coords_xy):
                if smallest_area is None or z.area_m2 < smallest_area:
                    smallest_area = z.area_m2
                    smallest_idx = i
        return smallest_idx

    def rows(self) -> list[dict[str, float | str]]:
        """Linha por zona: alvo (kg/ha), área (ha), planejado (kg), aplicado (kg),
        erro % (massa: gasto efetivo vs planejado) e cobertura %."""
        out: list[dict[str, float | str]] = []
        for acc in self.acc:
            massa_alvo_kg = acc.rate_alvo * acc.area_ha
            massa_apl_kg = acc.massa_aplicada_kg
            erro_pct = (
                100.0 * (massa_apl_kg - massa_alvo_kg) / massa_alvo_kg
                if massa_alvo_kg
                else 0.0
            )
            out.append(
                {
                    "zona": acc.label,
                    "alvo_kg_ha": round(acc.rate_alvo, 2),
                    "area_ha": round(acc.area_ha, 4),
                    "planejado_kg": round(massa_alvo_kg, 2),
                    "aplicado_kg": round(massa_apl_kg, 2),
                    "erro_pct": round(erro_pct, 2),
                    "cobertura_pct": round(
                        100.0 * acc.area_coberta_m2 / max(acc.area_ha * 10_000.0, 1e-9), 1
                    ),
                }
            )
        return out

    def render_console(self) -> str:
        h_zone = t(self.lang, "tbl_zone")
        h_target = t(self.lang, "tbl_target")
        h_area = t(self.lang, "tbl_area")
        h_plan = t(self.lang, "tbl_planned_kg")
        h_appl = t(self.lang, "tbl_applied_kg")
        h_err = t(self.lang, "tbl_error")
        h_cov = t(self.lang, "tbl_coverage")
        header = (
            f"{h_zone:<12}| {h_target:^12} | {h_area:^9} | "
            f"{h_plan:^14} | {h_appl:^14} | {h_err:^7} | {h_cov:^11}"
        )
        sep = (
            "-" * 12 + "|" + "-" * 14 + "|" + "-" * 11 + "|"
            + "-" * 16 + "|" + "-" * 16 + "|" + "-" * 9 + "|" + "-" * 13
        )
        lines = [header, sep]
        # Totais para a linha-resumo
        total_plan = 0.0
        total_appl = 0.0
        for r in self.rows():
            lines.append(
                f"{r['zona']:<12}| {r['alvo_kg_ha']:>12} | {r['area_ha']:>9} | "
                f"{r['planejado_kg']:>14} | {r['aplicado_kg']:>14} | "
                f"{r['erro_pct']:>+7} | {r['cobertura_pct']:>11}"
            )
            total_plan += float(r["planejado_kg"])
            total_appl += float(r["aplicado_kg"])
        # Linha "Fora de zonas": só aparece se houve aplicação fora (modo IDW)
        if self.mass_off_zone_kg > 0:
            off_label = t(self.lang, "tbl_off_zone")
            lines.append(
                f"{off_label:<12}| {'':>12} | {'':>9} | "
                f"{'':>14} | {self.mass_off_zone_kg:>14.2f} | "
                f"{'':>7} | {'':>11}"
            )
            total_appl += self.mass_off_zone_kg
        if total_plan > 0:
            total_err = 100.0 * (total_appl - total_plan) / total_plan
            lines.append(sep)
            lines.append(
                f"{'Total':<12}| {'':>12} | {'':>9} | "
                f"{total_plan:>14.2f} | {total_appl:>14.2f} | "
                f"{total_err:>+7.2f} | {'':>11}"
            )
        return "\n".join(lines)

    def write_csv(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.rows()
        if not rows:
            return
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
                    "erro_pct": 0,
                    "cobertura_pct": 0,
                }
            )
        header_map = {
            "zona": t(self.lang, "tbl_zone"),
            "alvo_kg_ha": t(self.lang, "tbl_target"),
            "area_ha": t(self.lang, "tbl_area"),
            "planejado_kg": t(self.lang, "tbl_planned_kg"),
            "aplicado_kg": t(self.lang, "tbl_applied_kg"),
            "erro_pct": t(self.lang, "tbl_error"),
            "cobertura_pct": t(self.lang, "tbl_coverage"),
        }
        translated_rows = [{header_map[k]: v for k, v in r.items()} for r in rows]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(translated_rows[0].keys()))
            writer.writeheader()
            writer.writerows(translated_rows)
