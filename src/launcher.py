"""Tela inicial de configuração (launcher) em tkinter.

Aparece quando o simulador é invocado sem flags na linha de comando, oferecendo
ao usuário um conjunto enxuto de opções (idioma, modo apresentação, cabeceira,
velocidade) com defaults sensatos para click-and-run. Se o usuário passar
qualquer flag (--lang, --paused-start, etc.) ou --no-launcher, o launcher é
pulado e a CLI aplica os valores diretamente.

Tkinter é parte da stdlib (sem dep nova). Os widgets ttk seguem o tema nativo
do sistema operacional, então a janela do launcher fica visualmente integrada
ao Windows / macOS / Linux do usuário.
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .paths import DATA_DIR

SIM_SPEED_VALUES = {"slow": 0.2, "medium": 0.3, "fast": 1.0}
TRACTOR_SPEED_KMH = {"slow": 4.0, "medium": 6.0, "fast": 8.0}


def _list_kmls() -> list[Path]:
    """Lista os .kml de exemplo encontrados em data/ (ordenado por nome)."""
    if not DATA_DIR.exists():
        return []
    return sorted(DATA_DIR.glob("*.kml"))


def run_launcher(args: argparse.Namespace) -> argparse.Namespace | None:
    """Abre a tela de configuração e devolve `args` atualizado, ou None se o
    usuário fechar a janela sem confirmar."""
    root = tk.Tk()
    root.title("VRA_Simulador")
    root.geometry("620x760")
    root.resizable(False, False)
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass

    # Lista de KMLs de exemplo na pasta data/
    kml_paths = _list_kmls()
    kml_names = [p.name for p in kml_paths]
    # Pré-seleciona o KML passado via CLI (se houver), senão o primeiro da lista
    initial_kml = ""
    if args.kml is not None:
        cli_path = Path(args.kml).resolve()
        for p in kml_paths:
            if p.resolve() == cli_path:
                initial_kml = p.name
                break
        if not initial_kml:
            initial_kml = str(args.kml)
    elif kml_names:
        initial_kml = kml_names[0]

    # Estado tkinter (mutável, ligado aos widgets)
    kml_var = tk.StringVar(value=initial_kml)
    lang_var = tk.StringVar(value=args.lang or "pt")
    paused_var = tk.BooleanVar(value=True)
    headland_var = tk.BooleanVar(value=True)
    mode_var = tk.StringVar(value="boustrophedon")
    method_var = tk.StringVar(value=getattr(args, "method", None) or "zones")
    idw_power_var = tk.DoubleVar(value=getattr(args, "idw_power", 2.0))
    idw_grid_var = tk.DoubleVar(value=getattr(args, "idw_grid_m", 0.0))
    tractor_speed_var = tk.StringVar(value="medium")
    sim_speed_var = tk.StringVar(value="medium")
    started = {"ok": False}

    # Título
    title = ttk.Label(
        root,
        text="VRA_Simulador — Configuração / Settings",
        font=("Segoe UI", 14, "bold"),
    )
    title.pack(pady=(14, 10))

    main_frame = ttk.Frame(root, padding=(20, 0))
    main_frame.pack(fill="both", expand=True)

    # Exemplo de KML
    ttk.Label(
        main_frame, text="Arquivo KML / KML file", font=("Segoe UI", 10, "bold")
    ).pack(anchor="w")
    kml_row = ttk.Frame(main_frame)
    kml_row.pack(anchor="w", padx=20, pady=(2, 12), fill="x")
    kml_combo = ttk.Combobox(
        kml_row,
        textvariable=kml_var,
        values=kml_names,
        width=42,
    )
    kml_combo.pack(side="left", padx=(0, 6))

    def browse_kml() -> None:
        path = filedialog.askopenfilename(
            title="Selecione um arquivo KML / Select a KML file",
            filetypes=[("KML files", "*.kml"), ("All files", "*.*")],
            initialdir=str(DATA_DIR) if DATA_DIR.exists() else None,
        )
        if path:
            kml_var.set(path)

    ttk.Button(
        kml_row, text="Procurar... / Browse...", command=browse_kml
    ).pack(side="left")

    # Idioma
    ttk.Label(main_frame, text="Idioma / Language", font=("Segoe UI", 10, "bold")).pack(
        anchor="w"
    )
    lang_frame = ttk.Frame(main_frame)
    lang_frame.pack(anchor="w", pady=(2, 12), padx=20)
    ttk.Radiobutton(lang_frame, text="Português", variable=lang_var, value="pt").pack(
        side="left", padx=(0, 24)
    )
    ttk.Radiobutton(lang_frame, text="English", variable=lang_var, value="en").pack(
        side="left"
    )

    # Opções
    ttk.Label(main_frame, text="Opções / Options", font=("Segoe UI", 10, "bold")).pack(
        anchor="w"
    )
    ttk.Checkbutton(
        main_frame,
        text="Iniciar pausado com slides / Start paused with slides",
        variable=paused_var,
    ).pack(anchor="w", padx=20, pady=(2, 2))
    ttk.Checkbutton(
        main_frame,
        text="Cabeceira automática / Auto headland pass",
        variable=headland_var,
    ).pack(anchor="w", padx=20, pady=(0, 12))

    # Estilo de traçado
    ttk.Label(
        main_frame,
        text="Estilo de traçado / Trajectory style",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w")
    for value, label in [
        ("boustrophedon", "Boustrofédico (vai-e-volta) / Boustrophedon"),
        ("random", "Pontos aleatórios / Random — apenas teste visual de IDW"),
    ]:
        ttk.Radiobutton(
            main_frame, text=label, variable=mode_var, value=value
        ).pack(anchor="w", padx=20, pady=1)

    # Método de prescrição (Zonas vs IDW puro) — comparação sugerida pelo orientador
    ttk.Label(
        main_frame,
        text="Método de prescrição / Prescription method",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(8, 0))
    for value, label in [
        ("zones", "Zonas de Manejo (Google Earth) / Management Zones"),
        ("idw", "IDW puro (somente amostras, sem zonas) / Pure IDW"),
    ]:
        ttk.Radiobutton(
            main_frame, text=label, variable=method_var, value=value
        ).pack(anchor="w", padx=20, pady=1)

    # Slider de potência N (0.5-5.0) com value inline + presets na mesma linha.
    idw_row = ttk.Frame(main_frame)
    idw_row.pack(anchor="w", padx=20, pady=(2, 0), fill="x")
    idw_label = ttk.Label(idw_row, text="Potência N do IDW / IDW exponent N:")
    idw_label.pack(side="left")
    idw_value_label = ttk.Label(idw_row, text=f"{idw_power_var.get():.1f}", width=4)
    idw_value_label.pack(side="left", padx=(8, 0))

    def _on_power_change(value: str) -> None:
        idw_value_label.configure(text=f"{float(value):.1f}")

    slider_row = ttk.Frame(main_frame)
    slider_row.pack(anchor="w", padx=20, pady=(0, 0), fill="x")
    idw_slider = ttk.Scale(
        slider_row,
        from_=0.5,
        to=5.0,
        orient="horizontal",
        variable=idw_power_var,
        command=_on_power_change,
        length=320,
    )
    idw_slider.pack(side="left")
    presets_row = slider_row  # presets compartilham a linha do slider
    for preset in (0.5, 1.0, 2.0, 3.0, 5.0):
        ttk.Button(
            presets_row,
            text=f"N={preset:g}",
            width=5,
            command=lambda v=preset: (
                idw_power_var.set(v),
                idw_value_label.configure(text=f"{v:.1f}"),
            ),
        ).pack(side="left", padx=1)

    # Espaçamento do grid de amostras dentro de cada zona (0 = só centroide).
    # Spinbox permite que o usuário digite o valor ou use as setas.
    grid_row = ttk.Frame(main_frame)
    grid_row.pack(anchor="w", padx=20, pady=(2, 0), fill="x")
    grid_label = ttk.Label(
        grid_row,
        text="Espaçamento do grid (m): / Grid spacing (m):",
    )
    grid_label.pack(side="left")
    grid_spin = ttk.Spinbox(
        grid_row,
        from_=0,
        to=200,
        increment=5,
        textvariable=idw_grid_var,
        width=6,
    )
    grid_spin.pack(side="left", padx=(8, 8))
    grid_hint = ttk.Label(
        grid_row,
        text="0 = só centroides, 50 m = grid moderado, 10 m = denso (GIS-like)",
        foreground="#666",
        font=("Segoe UI", 8),
    )
    grid_hint.pack(side="left")

    def _sync_idw_state(*_args: object) -> None:
        state = "normal" if method_var.get() == "idw" else "disabled"
        idw_slider.state(["!disabled"] if state == "normal" else ["disabled"])
        idw_label.configure(state=state)
        idw_value_label.configure(state=state)
        for child in presets_row.winfo_children():
            if child is not idw_slider:
                child.configure(state=state)
        grid_label.configure(state=state)
        grid_spin.configure(state=state)
        grid_hint.configure(state=state)

    method_var.trace_add("write", _sync_idw_state)
    _sync_idw_state()

    # Velocidade do trator (real, em km/h)
    ttk.Label(
        main_frame,
        text="Velocidade do trator / Tractor speed",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(8, 0))
    for value, label in [
        ("slow", "4 km/h (terreno difícil / heavy terrain)"),
        ("medium", "6 km/h (padrão / standard)"),
        ("fast", "8 km/h (terreno limpo / clean field)"),
    ]:
        ttk.Radiobutton(
            main_frame, text=label, variable=tractor_speed_var, value=value
        ).pack(anchor="w", padx=20, pady=1)

    # Velocidade da simulação (animação)
    ttk.Label(
        main_frame,
        text="Velocidade da animação / Animation speed",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(8, 0))
    for value, label in [
        ("slow", "Lenta / Slow (didática)"),
        ("medium", "Média / Medium"),
        ("fast", "Rápida / Fast"),
    ]:
        ttk.Radiobutton(
            main_frame, text=label, variable=sim_speed_var, value=value
        ).pack(anchor="w", padx=20, pady=1)

    # Botão Iniciar
    def confirm() -> None:
        if not kml_var.get().strip():
            messagebox.showerror(
                "VRA_Simulador",
                "Selecione um arquivo KML antes de iniciar.\n\n"
                "Please select a KML file before starting.",
            )
            return
        started["ok"] = True
        root.destroy()

    btn_frame = ttk.Frame(root)
    btn_frame.pack(pady=14)
    btn = ttk.Button(btn_frame, text="Iniciar / Start", command=confirm, width=20)
    btn.pack()
    btn.focus_set()

    # Atalhos de teclado
    root.bind("<Return>", lambda _e: confirm())
    root.bind("<Escape>", lambda _e: root.destroy())

    # Centraliza
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    root.mainloop()

    if not started["ok"]:
        return None

    # Resolve a seleção do combobox para o caminho real (data/<arquivo>.kml)
    selected = kml_var.get().strip()
    matched_path: Path | None = None
    for p in kml_paths:
        if p.name == selected:
            matched_path = p
            break
    args.kml = matched_path if matched_path else Path(selected)

    args.lang = lang_var.get()
    args.paused_start = paused_var.get()
    args.headland = "auto" if headland_var.get() else "off"
    args.mode = mode_var.get()
    args.method = method_var.get()
    # Slider devolve float; arredonda para 1 casa para casar com os labels.
    args.idw_power = round(float(idw_power_var.get()), 1)
    # Spinbox pode vir como string se o usuário digitar; fallback p/ 0 em
    # caso de valor inválido.
    try:
        args.idw_grid_m = max(0.0, float(idw_grid_var.get()))
    except (tk.TclError, ValueError):
        args.idw_grid_m = 0.0
    args.tractor_speed_kmh = TRACTOR_SPEED_KMH[tractor_speed_var.get()]
    args.speed_factor = SIM_SPEED_VALUES[sim_speed_var.get()]
    return args


def should_show_launcher() -> bool:
    """Decide se o launcher deve aparecer. True quando o usuário não passou
    nenhuma flag (apenas o KML posicional). --no-launcher força False."""
    argv = sys.argv[1:]
    if "--no-launcher" in argv:
        return False
    return not any(a.startswith("-") for a in argv)
