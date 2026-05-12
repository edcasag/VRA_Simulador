"""Ponto de entrada do simulador VRA.

Uso:
    python -m src.main data/ensaio_abcd.kml
    python -m src.main data/talhao_completo.kml
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from .i18n import t
from .kml_parser import parse_kml
from .launcher import run_launcher, should_show_launcher
from .terrain import GaussianBump, default_params
from .tractor_sim import boustrophedon, should_use_headland
from .visualization import run
from .vra_engine import (
    DEFAULT_IDW_RADIUS_M,
    IdwParams,
    centroids_from_zones,
    dose_at,
    dose_at_idw_pure,
    grid_samples_from_zones,
    point_in_polygon,
    samples_from_zones_count,
)


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
        nargs="?",
        default=None,
        help="Path to the .kml file describing the field zones (e.g. "
        "data/ensaio_abcd.kml). Optional: if omitted, the launcher offers a "
        "dropdown with the example KMLs in data/.",
    )
    p.add_argument(
        "--lang",
        choices=["pt", "en"],
        default="pt",
        help="Language for the on-screen UI, the in-window report panel and the "
        "CSV column headers. pt = Portuguese (default, used by the SBIAGRO 2025 paper); "
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
        "--no-launcher",
        action="store_true",
        help="Skip the interactive launcher window even when no other flag is "
        "provided (useful for batch/CI runs).",
    )
    p.add_argument(
        "--mode",
        choices=["boustrophedon"],
        default="boustrophedon",
        help="Trajectory style. 'boustrophedon' (default): back-and-forth strips "
        "with U-turns and optional headland pass.",
    )
    p.add_argument(
        "--method",
        choices=["zones", "idw"],
        default="zones",
        help="Prescription method. 'zones' (default): hierarchical decision over "
        "polygons / circles / sample points (the proposed method). 'idw': pure "
        "IDW interpolation over polygon centroids, ignoring inclusion polygons, "
        "exclusion polygons / circles and Field=Rate. Used for the comparison "
        "Zones vs IDW suggested by the advisor.",
    )
    p.add_argument(
        "--idw-power",
        type=float,
        default=2.0,
        help="IDW exponent N (weights = 1/d^N). Range 0.5–5.0. N=2.0 (default) "
        "is the classical inverse-square; N→0.5 yields a near-uniform field "
        "(global mean); N→5.0 emphasizes the bull's-eye effect (each sample "
        "dominates its neighborhood). Ignored when --method=zones.",
    )
    p.add_argument(
        "--idw-radius-m",
        type=float,
        default=DEFAULT_IDW_RADIUS_M,
        help=f"IDW search radius in meters (default: {DEFAULT_IDW_RADIUS_M:g}). "
        "Samples beyond this distance do not contribute to the interpolated "
        "rate. Ignored when --method=zones.",
    )
    p.add_argument(
        "--idw-samples",
        type=int,
        default=0,
        help="Approximate total number of IDW samples, distributed across "
        "all inclusion polygons proportionally to area (each zone gets ~ "
        "n × zone_area / total_area samples). 0 (default) = use only one "
        "centroid per zone (sparse). Higher values mimic a real GIS soil-"
        "sampling campaign (e.g., 50 = sparse field survey; 500 = dense "
        "research-grade sampling). Each sample inherits the zone's rate. "
        "Ignored when --method=zones. Takes precedence over --idw-grid-m.",
    )
    p.add_argument(
        "--idw-grid-m",
        type=float,
        default=0.0,
        help="(Advanced) Grid spacing in meters for densifying samples "
        "within each zone. Alternative to --idw-samples for users who want "
        "exact spacing control. 0 (default) = inactive. Ignored when "
        "--idw-samples > 0 or --method=zones.",
    )
    p.add_argument(
        "--tractor-speed-kmh",
        type=float,
        default=6.0,
        help="Nominal tractor speed in km/h on flat terrain (default: 6.0). "
        "Min/max saturations are scaled proportionally (~30%% / 150%% of nominal).",
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
        "--strip-overlap",
        type=float,
        default=0.0,
        help="Fraction of overlap between adjacent boustrophedon strips "
        "(0.0 to 0.5). 0.0 (default) = strips adjacent without overlap. "
        "Higher values (e.g. 0.10) eliminate small gaps from irregular field "
        "edges and GNSS drift, at the cost of double-counting some area in "
        "the per-zone applied mass. The actual swath width (--width-m) is "
        "unchanged; only the spacing between strips.",
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

    # Launcher interativo: aparece se nenhuma flag foi passada (apenas o KML
    # posicional opcional). Permite escolher exemplo KML, idioma, modo
    # apresentação, cabeceira, velocidades via UI.
    if should_show_launcher():
        updated = run_launcher(args)
        if updated is None:
            # Usuário fechou a janela sem confirmar
            return
        args = updated

    if args.kml is None:
        sys.stderr.write(
            "Erro: nenhum arquivo KML especificado. Passe um caminho como "
            "argumento posicional ou rode sem flags para abrir o launcher.\n"
        )
        sys.exit(1)

    # Validação do N do IDW (range 0.5–5.0 conforme decisão de 2026-05-07)
    if not (0.5 <= args.idw_power <= 5.0):
        sys.stderr.write(
            f"Erro: --idw-power deve estar em [0.5, 5.0]; recebido {args.idw_power}.\n"
        )
        sys.exit(1)

    kml = parse_kml(args.kml)
    bbox = kml.bbox()

    terrain = default_params(bbox)
    terrain.a = args.decline_x
    terrain.b = args.decline_y
    # Velocidade nominal escolhida pelo usuário (ou 6 km/h padrão); saturações
    # mantidas proporcionais (~30% / 150% da nominal).
    v_nom_kmh = args.tractor_speed_kmh
    terrain.v_nom = v_nom_kmh / 3.6
    terrain.v_min = (v_nom_kmh * 0.3) / 3.6
    terrain.v_max = (v_nom_kmh * 1.5) / 3.6
    terrain.alpha = 13.3
    if args.bump_h:
        cx = 0.5 * (bbox[0] + bbox[2])
        cy = 0.5 * (bbox[1] + bbox[3])
        sigma = 0.20 * max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        terrain.bumps = [GaussianBump(h=args.bump_h, x0=cx, y0=cy, sigma=sigma)]
    else:
        terrain.bumps = []

    field_coords = kml.field_polygon.coords_xy if kml.field_polygon else None
    # Trajetória do trator independe do método de prescrição: na realidade
    # física, o trator desvia da sede, do lago e das pedras seja qual for o
    # algoritmo de cálculo de dose. A diferença entre Zonas e IDW está
    # somente na DOSE aplicada ao longo da mesma trajetória — não no
    # planejamento do caminho.
    exclusion_polys: list[list[tuple[float, float]]] = [
        z.coords_xy for z in kml.zones if z.rate == 0
    ]
    exclusion_circs: list[tuple[float, float, float]] = [
        (c.x, c.y, c.radius_m) for c in kml.circles if c.rate == 0
    ]

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
        strip_overlap=args.strip_overlap,
    )

    # Função de dose injetada na visualização e no acumulador de cobertura.
    # 'zones' usa a hierarquia padrão; 'idw' usa apenas amostras (centroides
    # dos polígonos de inclusão) com o N escolhido pelo usuário.
    # `clip_polys` define a "área demarcada" — onde aplicação faz sentido.
    # Quando há `Field` no KML, é o talhão. Quando não há (ex.: ABCD), é a
    # união das zonas de inclusão. Em ambos os métodos, dose=0 fora dessa
    # área (independente do método de interpolação).
    # Polígonos da "área demarcada" (sem tolerância — clip estrito). Quando
    # há `Field` no KML, é o talhão. Quando não há (ex.: ABCD), é a união
    # das zonas de inclusão.
    clip_polys: list[list[tuple[float, float]]] = (
        [kml.field_polygon.coords_xy]
        if kml.field_polygon
        else [z.coords_xy for z in kml.zones if z.rate > 0]
    )

    def _inside_clip(x: float, y: float) -> bool:
        return any(point_in_polygon(x, y, p) for p in clip_polys)

    if args.method == "idw":
        # --idw-samples (didático) tem precedência sobre --idw-grid-m (avançado).
        if args.idw_samples > 0:
            zone_samples = samples_from_zones_count(kml, args.idw_samples)
        elif args.idw_grid_m > 0:
            zone_samples = grid_samples_from_zones(kml, args.idw_grid_m)
        else:
            zone_samples = centroids_from_zones(kml)
        # Inclui também as amostras IDW externas do KML (Placemarks com
        # número solto, fora das zonas) — comparação justa contra zonas,
        # que também usam essas amostras como fallback hierárquico.
        idw_samples = zone_samples + list(kml.samples)
        # Casa o raio efetivo do IDW com o tamanho do talhão para que pontos
        # longe das amostras (cantos do Sítio Palmar p. ex.) ainda recebam
        # uma interpolação válida em vez de 0 (que pintaria cinza). O flag
        # --idw-radius-m continua sendo aceito para experimentos didáticos
        # de truncamento (raio < bbox_diag).
        bbox_diag = math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1])
        eff_radius = max(args.idw_radius_m, bbox_diag * 2.0)
        idw_params = IdwParams(power=args.idw_power, radius_m=eff_radius)

        def dose_fn(x: float, y: float) -> float:
            # Clip rígido pela área demarcada: fora do talhão (ou da união
            # das zonas, no ABCD) o operador desliga a aplicação. IDW puro
            # não pode "vazar" cor para fora dessa área.
            if not _inside_clip(x, y):
                return 0.0
            return dose_at_idw_pure(x, y, idw_samples, idw_params)

        # Composição das amostras: N da zona (centroides ou grid) + M externas.
        n_zone = len(zone_samples)
        n_external = len(kml.samples)
        if args.idw_samples > 0:
            method_label = (
                f"IDW N={args.idw_power:g} | {n_zone} grid + "
                f"{n_external} externas = {n_zone + n_external} amostras"
            )
        elif args.idw_grid_m > 0:
            method_label = (
                f"IDW N={args.idw_power:g} | grid {args.idw_grid_m:g} m: "
                f"{n_zone} amostras + {n_external} externas = "
                f"{n_zone + n_external}"
            )
        else:
            method_label = (
                f"IDW N={args.idw_power:g} | {n_zone} centroides + "
                f"{n_external} externas = {n_zone + n_external} amostras"
            )
    else:
        idw_samples = []
        idw_params = IdwParams()

        def dose_fn(x: float, y: float) -> float:
            # Clip rígido pela área demarcada também no modo Zonas — quando
            # não há `Field` no KML (ABCD), dose_at usa só a hierarquia de
            # polígonos e devolve 0 fora de qualquer zona; o clip explícito
            # apenas reforça e mantém simetria com o modo IDW.
            if not _inside_clip(x, y):
                return 0.0
            return dose_at(x, y, kml)

        # Para zonas, "amostras utilizadas" = centroides das zonas com rate>0
        # (aplicando o filtro de marca interna) + amostras IDW externas
        # (usadas como fallback na hierarquia). Mantém a contagem coerente
        # com o modo IDW: zonas que têm marca dentro não entram como
        # centroide.
        n_zone = len(centroids_from_zones(kml))
        n_external = len(kml.samples)
        method_label = (
            f"Zonas | {n_zone} centroides + {n_external} externas "
            f"= {n_zone + n_external} amostras"
        )

    # Saída em subpasta por método (e por N quando IDW), evitando que rodadas
    # consecutivas sobrescrevam snapshots uns dos outros — facilita a
    # comparação posterior na tese. CSV usa nome com data+hora+KML, então
    # múltiplas rodadas no mesmo método não se sobrescrevem.
    if args.method == "idw":
        if args.idw_samples > 0:
            method_subdir = (
                f"idw_p{args.idw_power:g}_n{args.idw_samples}".replace(".", "_")
            )
        elif args.idw_grid_m > 0:
            method_subdir = (
                f"idw_p{args.idw_power:g}_grid{args.idw_grid_m:g}m".replace(".", "_")
            )
        else:
            method_subdir = f"idw_p{args.idw_power:g}_centroides".replace(".", "_")
    else:
        method_subdir = "zones"
    docs_dir = args.docs_dir / "simulator-reports" / method_subdir
    docs_dir.mkdir(parents=True, exist_ok=True)

    report = run(
        kml=kml,
        terrain=terrain,
        samples=samples,
        mode_label=method_label,
        width_m=args.width_m,
        cell_m=args.cell_m,
        docs_dir=docs_dir,
        snapshots_at_pct=(25, 50, 100),
        snapshot_prefix=args.snapshot_prefix,
        speed_factor=args.speed_factor,
        paint_offset_back_m=args.paint_offset_back_m,
        start_paused=args.paused_start,
        lang=args.lang,
        dose_fn=dose_fn,
        method=args.method,
        idw_samples=idw_samples,
        idw_params=idw_params,
    )

    print()
    print(report.render_console())
    # Nome do CSV: report-YYYY-MM-DD-HHMM-<kml-stem>.csv para nunca
    # sobrescrever rodadas anteriores. Espaços do nome do KML viram
    # underscore para evitar problemas em shells/explorers.
    from datetime import datetime  # noqa: PLC0415 (uso pontual)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    kml_stem = Path(args.kml).stem.replace(" ", "_")
    csv_path = docs_dir / f"report-{timestamp}-{kml_stem}.csv"
    # Parâmetros do teste, escritos no header do CSV, com chaves
    # traduzidas conforme args.lang (mesma mensagem que o usuário vê
    # na tela e no terminal).
    params = {
        t(args.lang, "report_param_date"): timestamp,
        t(args.lang, "report_param_kml"): Path(args.kml).name,
        t(args.lang, "report_param_method"): method_label,
        t(args.lang, "report_param_width"): f"{args.width_m:g} m",
        t(args.lang, "report_param_noise"): f"{args.gnss_noise_m:g} m",
        t(args.lang, "report_param_lang"): args.lang.upper(),
    }
    report.write_csv(csv_path, params=params)
    print(f"\n{t(args.lang, 'report_saved')}: {csv_path}")


if __name__ == "__main__":
    main()
