"""Ponto de entrada do simulador VRA.

Uso:
    python -m src.main data/ensaio_abcd.kml
    python -m src.main data/talhao_completo.kml
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .kml_parser import parse_kml
from .terrain import GaussianBump, default_params
from .tractor_sim import boustrophedon
from .visualization import run


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Simulador VRA — Tese de Mestrado POLI/USP")
    p.add_argument("kml", type=Path, help="Caminho para o arquivo .kml")
    p.add_argument(
        "--width-m",
        type=float,
        default=20.0,
        help="Largura da faixa de aplicação (m). Ex.: distribuidor 10 m de cada lado → 20",
    )
    p.add_argument(
        "--cell-m",
        type=float,
        default=1.5,
        help="Profundidade longitudinal do retângulo de pintura por amostra (m). "
        "Maior que 1.0 garante leve sobreposição entre retângulos consecutivos.",
    )
    p.add_argument(
        "--paint-offset-back-m",
        type=float,
        default=1.0,
        help="Deslocamento da pintura para trás do trator (m), correspondente ao "
        "eixo do distribuidor de discos (default: 1.0 = ~metade do trator).",
    )
    p.add_argument(
        "--gnss-noise-m",
        type=float,
        default=0.0,
        help="Desvio-padrão do ruído GNSS (m). 0 = sem ruído, 0.5 = realismo de campo "
        "(mas pode introduzir falhas visuais na pintura).",
    )
    p.add_argument("--decline-x", type=float, default=0.04, help="Declive em x (m/m)")
    p.add_argument("--decline-y", type=float, default=0.0, help="Declive em y (m/m)")
    p.add_argument("--bump-h", type=float, default=2.0, help="Altura da bossa central (m)")
    p.add_argument(
        "--docs-dir", type=Path, default=Path("docs"), help="Diretório para snapshots e CSV"
    )
    p.add_argument(
        "--speed-factor",
        type=float,
        default=0.3,
        help="Fator de aceleração da simulação (0.2=bem lento, 1=médio, 3=rápido)",
    )
    p.add_argument(
        "--snapshot-prefix",
        type=str,
        default="snapshot",
        help="Prefixo para snapshots automáticos",
    )
    p.add_argument(
        "--paused-start",
        action="store_true",
        help="Inicia pausado; pressione ESPAÇO para começar (útil para iniciar a "
        "gravação de tela com a janela do pygame em foco antes da animação rodar).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    kml = parse_kml(args.kml)
    bbox = kml.bbox()

    terrain = default_params(bbox)
    terrain.a = args.decline_x
    terrain.b = args.decline_y
    # Velocidade nominal típica de trator agrícola distribuidor: 6 km/h ≈ 1.667 m/s.
    # Saturações: 1.8 km/h em subida íngreme; 9 km/h em descida.
    # alpha=13.3 → 5% de subida derruba ~1.2 km/h; 10% satura em v_min.
    terrain.v_nom = 6.0 / 3.6
    terrain.v_min = 1.8 / 3.6
    terrain.v_max = 9.0 / 3.6
    terrain.alpha = 13.3
    if args.bump_h:
        cx = 0.5 * (bbox[0] + bbox[2])
        cy = 0.5 * (bbox[1] + bbox[3])
        sigma = 0.20 * max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        terrain.bumps = [GaussianBump(h=args.bump_h, x0=cx, y0=cy, sigma=sigma)]
    else:
        terrain.bumps = []

    samples = boustrophedon(
        bbox,
        terrain,
        width_m=args.width_m,
        gnss_noise_m=args.gnss_noise_m,
        paint_offset_back_m=args.paint_offset_back_m,
    )

    report = run(
        kml=kml,
        terrain=terrain,
        samples=samples,
        mode_label="",
        width_m=args.width_m,
        cell_m=args.cell_m,
        docs_dir=args.docs_dir,
        snapshots_at_pct=(25, 50, 100),
        snapshot_prefix=args.snapshot_prefix,
        speed_factor=args.speed_factor,
        paint_offset_back_m=args.paint_offset_back_m,
        start_paused=args.paused_start,
    )

    print()
    print("=== Relatório de aplicação por zona (Tab. 6 cap 7 §7.3) ===")
    print(report.render_console())
    csv_path = args.docs_dir / "relatorio_erro.csv"
    report.write_csv(csv_path)
    print(f"\nRelatório salvo em: {csv_path}")


if __name__ == "__main__":
    main()
