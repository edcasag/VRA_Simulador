"""Ponto de entrada do simulador VRA.

Uso:
    python -m src.main data/ensaio_abcd.kml
    python -m src.main data/talhao_completo.kml
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .coverage_report import polygon_area_m2
from .i18n import t
from .kml_parser import KmlData, parse_kml
from .terrain import GaussianBump, default_params
from .tractor_sim import boustrophedon, should_use_headland
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
        "--headland",
        choices=["auto", "on", "off"],
        default="auto",
        help="Headland (perimeter) pass before the boustrophedon. The tractor "
        "first traces the field perimeter and the polygon exclusions (Sede etc.), "
        "then fills the interior. Improves coverage of irregular boundaries. "
        "auto = on when the field polygon has >= 5 vertices (irregular shape); "
        "off otherwise. on / off force the behavior.",
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


def _print_kml_summary(kml: KmlData, lang: str, width_m: float) -> None:
    """Imprime sumário das features do KML antes de iniciar a simulação."""
    print(t(lang, "summary_title"))
    if kml.field_polygon is not None:
        area_ha = polygon_area_m2(kml.field_polygon.coords_xy) / 10_000.0
        info = t(lang, "summary_field_fmt").format(
            vertices=len(kml.field_polygon.coords_xy), area_ha=area_ha
        )
        print(f"  {t(lang, 'summary_field')}: {info}")
    else:
        print(f"  {t(lang, 'summary_field')}: {t(lang, 'summary_field_none')}")

    inclusions = [z for z in kml.zones if z.rate > 0]
    exclusions = [z for z in kml.zones if z.rate == 0]
    inc_desc = ", ".join(f"{z.label}={z.rate:g}" for z in inclusions) or "—"
    exc_desc = ", ".join(z.label for z in exclusions) or "—"
    print(f"  {t(lang, 'summary_inclusion')} ({len(inclusions)}): {inc_desc}")
    print(f"  {t(lang, 'summary_exclusion')} ({len(exclusions)}): {exc_desc}")
    print(f"  {t(lang, 'summary_circles')}: {len(kml.circles)}")
    print(f"  {t(lang, 'summary_samples')}: {len(kml.samples)}")

    bbox = kml.bbox()
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    n_strips = max(1, int(h / max(width_m, 1e-6)))
    print(
        f"  {t(lang, 'summary_bbox')}: {w:.0f} x {h:.0f} m  "
        f"(~{n_strips} {t(lang, 'summary_strips')})"
    )
    print()


def main() -> None:
    args = parse_args()
    kml = parse_kml(args.kml)
    bbox = kml.bbox()

    _print_kml_summary(kml, args.lang, args.width_m)

    terrain = default_params(bbox)
    terrain.a = args.decline_x
    terrain.b = args.decline_y
    # Velocidade nominal típica de trator agrícola distribuidor: 6 km/h ≈ 1.667 m/s.
    # Saturações: 1.8 km/h em subida íngreme; 9 km/h em descida.
    # alpha=13.3 -> 5% de subida derruba ~1.2 km/h; 10% satura em v_min.
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

    field_coords = kml.field_polygon.coords_xy if kml.field_polygon else None
    exclusion_polys = [z.coords_xy for z in kml.zones if z.rate == 0]
    exclusion_circs = [(c.x, c.y, c.radius_m) for c in kml.circles if c.rate == 0]

    if args.headland == "auto":
        headland = should_use_headland(field_coords)
    else:
        headland = args.headland == "on"

    samples = boustrophedon(
        bbox,
        terrain,
        width_m=args.width_m,
        gnss_noise_m=args.gnss_noise_m,
        paint_offset_back_m=args.paint_offset_back_m,
        field_polygon=field_coords,
        exclusion_polygons=exclusion_polys,
        exclusion_circles=exclusion_circs,
        headland=headland,
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
    print(f"\n{t(args.lang, 'report_note')}")
    csv_path = args.docs_dir / "relatorio_erro.csv"
    report.write_csv(csv_path)
    print(f"\n{t(args.lang, 'report_saved')}: {csv_path}")


if __name__ == "__main__":
    main()
