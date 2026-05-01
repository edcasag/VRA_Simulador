"""Gera CSV ground truth de trajetoria para validacao cruzada Python <-> ESP32.

Carrega um KML, gera trajetoria boustrofedon com perfil de velocidade
variavel (declive + bossas), calcula dose_at() em cada ponto e exporta
CSV ponto-a-ponto.

O CSV resultante e consumido pelo modo BUILD_SIM do POC ESP32
(VRA_Controlador) para validar que dose_at (Python double) e
LogicaHierarquica::dose (ESP32 float) produzem o mesmo resultado em
~milhares de pontos com IDW interpolado.

Uso:
    python _python/scripts/export_trajectory_csv.py \\
        --kml _python/data/ensaio_abcd.kml \\
        --out-csv _esp32/data/trajetoria_ensaio_abcd.csv \\
        --width-m 20.0 --step-m 1.0 --gnss-noise-m 0.0 --seed 42
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path

# Permite rodar standalone sem instalar pacote.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.kml_parser import parse_kml
from src.terrain import default_params
from src.tractor_sim import boustrophedon
from src.vra_engine import dose_at


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--kml", required=True, help="Caminho para o arquivo KML de entrada")
    parser.add_argument("--out-csv", required=True, help="Caminho do CSV de saida")
    parser.add_argument("--width-m", type=float, default=20.0, help="Largura do implemento (m)")
    parser.add_argument("--step-m", type=float, default=1.0, help="Passo de amostragem ao longo da faixa (m)")
    parser.add_argument("--gnss-noise-m", type=float, default=0.0,
                        help="Desvio-padrao do ruido GPS (0 = sem ruido, recomendado para validacao cruzada)")
    parser.add_argument("--seed", type=int, default=42, help="Seed do RNG (so afeta o ruido GPS)")
    parser.add_argument("--headland", choices=["on", "off"], default="off",
                        help="Cabeceira (passada do perimetro) antes do boustrofedon")
    args = parser.parse_args()

    kml = parse_kml(args.kml)
    bbox = kml.bbox()

    field_coords = kml.field_polygon.coords_xy if kml.field_polygon else None
    exclusion_polys = [z.coords_xy for z in kml.zones if z.rate == 0]
    exclusion_circs = [(c.x, c.y, c.radius_m) for c in kml.circles if c.rate == 0]

    terrain = default_params(bbox)

    rng = random.Random(args.seed)
    samples = boustrophedon(
        bbox, terrain,
        width_m=args.width_m,
        step_m=args.step_m,
        gnss_noise_m=args.gnss_noise_m,
        paint_offset_back_m=0.0,
        rng=rng,
        field_polygon=field_coords,
        exclusion_polygons=exclusion_polys,
        exclusion_circles=exclusion_circs,
        headland=(args.headland == "on"),
    )

    out_path = Path(args.out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_linhas = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow([
            "idx", "t_s", "x_m", "y_m", "vel_mps",
            "heading_x", "heading_y", "spreading", "dose_alvo_kg_ha",
        ])
        for idx, s in enumerate(samples):
            hx, hy = (s.heading if s.heading is not None else (0.0, 0.0))
            v = s.v if s.v is not None else 0.0
            d = dose_at(s.x, s.y, kml)
            writer.writerow([
                idx, f"{s.t:.4f}", f"{s.x:.4f}", f"{s.y:.4f}", f"{v:.4f}",
                f"{hx:.6f}", f"{hy:.6f}", 1 if s.spreading else 0, f"{d:.6f}",
            ])
            n_linhas += 1

    bytes_csv = out_path.stat().st_size
    print(f"OK: {n_linhas} linhas escritas em {out_path} ({bytes_csv/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
