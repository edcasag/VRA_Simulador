"""Ponto de entrada do simulador VRA.

Uso:
    python -m src.main data/ensaio_abcd.kml
    python -m src.main data/talhao_completo.kml
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .i18n import t
from .kml_parser import parse_kml
from .terrain import GaussianBump, default_params
from .tractor_sim import boustrophedon
from .visualization import run


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m _python.src.main",
        description=(
            "VRA Simulator: visualizes the variable-rate application of a tractor "
            "moving across a field split into zones with distinct target rates. "
            "Reads zones from a KML file, simulates the boustrophedon trajectory, "
            "modulates speed by terrain slope, and produces a per-zone error "
            "report (CSV)."
        ),
        epilog=(
            "Examples:\n"
            "  # Run directly with Portuguese UI, snapshots in docs/\n"
            "  python -m _python.src.main _python/data/ensaio_abcd.kml\n"
            "\n"
            "  # Presentation mode: paused at start, intro slides explaining\n"
            "  # VRA and speed modulation; SPACE starts the simulation.\n"
            "  # Use this to record screen video with the pygame window in focus.\n"
            "  python -m _python.src.main _python/data/ensaio_abcd.kml --paused-start\n"
            "\n"
            "  # English UI, separate output dir for the CEA paper\n"
            "  python -m _python.src.main _python/data/ensaio_abcd.kml --lang en \\\n"
            "      --docs-dir _python/docs/en\n"
            "\n"
            "Keys during simulation:\n"
            "  SPACE  pause / resume (also advances intro slides in --paused-start)\n"
            "  S      save a manual snapshot of the current window\n"
            "  ESC    close\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "kml",
        type=Path,
        help="Path to the .kml file describing the field zones "
        "(e.g. _python/data/ensaio_abcd.kml).",
    )
    p.add_argument(
        "--lang",
        choices=["pt", "en"],
        default="pt",
        help="Language for the on-screen UI, the in-window report panel and the "
        "CSV column headers. pt = Portuguese (default, used by the dissertation); "
        "en = English (used by the CEA paper).",
    )
    p.add_argument(
        "--paused-start",
        action="store_true",
        help="Start in presentation mode: paused, showing intro slides about VRA "
        "and speed modulation, waiting for SPACE to begin. Useful to start a "
        "screen recording with the pygame window already focused. Without this "
        "flag the simulation starts immediately.",
    )
    p.add_argument(
        "--speed-factor",
        type=float,
        default=0.3,
        help="Simulation speed multiplier. 0.2 = very slow (didactic); 1 = medium; "
        "3 = fast (generates the report in a few seconds). Default: 0.3.",
    )
    p.add_argument(
        "--docs-dir",
        type=Path,
        default=Path("docs"),
        help="Directory where automatic PNG snapshots (at 25%%, 50%%, 100%% of the "
        "trajectory) and the per-zone error CSV are saved. Default: docs/",
    )
    p.add_argument(
        "--snapshot-prefix",
        type=str,
        default="snapshot",
        help="Filename prefix for automatic snapshots "
        "(e.g. 'snapshot_050pct.png'). Default: snapshot.",
    )
    p.add_argument(
        "--width-m",
        type=float,
        default=20.0,
        help="Application swath width (m). E.g. a disc spreader with 10 m reach on "
        "each side -> 20. Default: 20.0.",
    )
    p.add_argument(
        "--cell-m",
        type=float,
        default=1.5,
        help="Longitudinal depth (in the direction of motion) of the painted "
        "rectangle drawn at each sample (m). Values >1.0 ensure slight overlap "
        "between consecutive rectangles, avoiding visible stripes. Default: 1.5.",
    )
    p.add_argument(
        "--paint-offset-back-m",
        type=float,
        default=1.0,
        help="Distance the paint is drawn behind the tractor (m), matching the "
        "disc spreader axis. Default: 1.0 (~half the tractor length).",
    )
    p.add_argument(
        "--gnss-noise-m",
        type=float,
        default=0.0,
        help="Standard deviation of Gaussian noise applied to reported GNSS "
        "coordinates (m). 0 = no noise (clean animation); 0.5 = field realism, "
        "but may introduce small visual gaps in the paint. Default: 0.0.",
    )
    p.add_argument(
        "--decline-x",
        type=float,
        default=0.04,
        help="Uniform terrain slope along x, in m/m (4%% by default). Modulates "
        "tractor speed: uphill slows down, downhill speeds up.",
    )
    p.add_argument(
        "--decline-y",
        type=float,
        default=0.0,
        help="Uniform terrain slope along y, in m/m. Default: 0 "
        "(flat in this direction).",
    )
    p.add_argument(
        "--bump-h",
        type=float,
        default=2.0,
        help="Height (m) of the central Gaussian bump added on top of the uniform "
        "slope. Creates visible contour lines in the right panel. 0 disables "
        "the bump. Default: 2.0.",
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
        lang=args.lang,
    )

    print()
    print(t(args.lang, "report_console_title"))
    print(report.render_console())
    csv_path = args.docs_dir / "relatorio_erro.csv"
    report.write_csv(csv_path)
    print(f"\n{t(args.lang, 'report_saved')}: {csv_path}")


if __name__ == "__main__":
    main()
