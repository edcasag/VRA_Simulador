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

from .kml_parser import KmlData, parse_kml
from .paths import DATA_DIR
from .vra_engine import centroids_from_zones, grid_samples_from_zones

SIM_SPEED_VALUES = {"slow": 0.2, "medium": 0.3, "fast": 1.0}
TRACTOR_SPEED_KMH = {"slow": 4.0, "medium": 6.0, "fast": 8.0}

# Textos do launcher por idioma. O idioma inicial vem de _detect_os_lang();
# o radio "Idioma / Language" permite override manual em tempo de execução,
# disparando _apply_lang() (reaplica os textos a todos os widgets registrados).
LAUNCHER_TEXTS: dict[str, dict[str, str]] = {
    "title": {
        "pt": "VRA_Simulador — Configuração",
        "en": "VRA_Simulador — Settings",
    },
    "kml_section": {"pt": "Arquivo KML", "en": "KML file"},
    "browse_btn": {"pt": "Procurar...", "en": "Browse..."},
    "browse_dialog": {
        "pt": "Selecione um arquivo KML",
        "en": "Select a KML file",
    },
    "lang_section": {"pt": "Idioma", "en": "Language"},
    "options_section": {"pt": "Opções", "en": "Options"},
    "paused_check": {
        "pt": "Iniciar pausado com slides",
        "en": "Start paused with slides",
    },
    "headland_check": {
        "pt": "Cabeceira automática",
        "en": "Auto headland pass",
    },
    "tractor_section": {
        "pt": "Velocidade do trator",
        "en": "Tractor speed",
    },
    "tractor_slow": {
        "pt": "4 km/h (terreno difícil)",
        "en": "4 km/h (heavy terrain)",
    },
    "tractor_medium": {"pt": "6 km/h (padrão)", "en": "6 km/h (standard)"},
    "tractor_fast": {
        "pt": "8 km/h (terreno limpo)",
        "en": "8 km/h (clean field)",
    },
    "anim_section": {
        "pt": "Velocidade da animação",
        "en": "Animation speed",
    },
    "anim_slow": {"pt": "Lenta (didática)", "en": "Slow (didactic)"},
    "anim_medium": {"pt": "Média", "en": "Medium"},
    "anim_fast": {"pt": "Rápida", "en": "Fast"},
    "method_section": {
        "pt": "Método de prescrição",
        "en": "Prescription method",
    },
    "method_zones": {
        "pt": "Zonas de Manejo (Google Earth)",
        "en": "Management Zones (Google Earth)",
    },
    "method_idw": {
        "pt": "IDW puro (somente amostras, sem zonas)",
        "en": "Pure IDW (samples only, no zones)",
    },
    "idw_power_label": {
        "pt": "Potência N do IDW:",
        "en": "IDW exponent N:",
    },
    "grid_section": {"pt": "Espaçamento do grid", "en": "Grid spacing"},
    "grid_label": {"pt": "Espaçamento (m):", "en": "Spacing (m):"},
    "start_btn": {"pt": "Iniciar", "en": "Start"},
    "kml_invalid": {"pt": "(KML inválido)", "en": "(invalid KML)"},
    "count_centroides": {"pt": "amostras", "en": "samples"},
    "count_grid": {"pt": "amostras", "en": "samples"},
    "count_externas": {"pt": "do KML", "en": "from KML"},
    "msg_kml_required": {
        "pt": "Selecione um arquivo KML antes de iniciar.",
        "en": "Please select a KML file before starting.",
    },
}


def _detect_os_lang() -> str:
    """Retorna 'pt' se o SO está em Português (qualquer região), 'en' c.c.

    No Windows usa GetUserDefaultUILanguage() (idioma da UI escolhido pelo
    usuário, não o do sistema). Nos demais SOs cai para o locale da stdlib.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            # Primary language ID = bits 0-9 do LCID. Português = 0x16.
            if (lcid & 0x3FF) == 0x16:
                return "pt"
            return "en"
        except (OSError, AttributeError):
            pass
    try:
        import locale

        code = (locale.getlocale()[0] or "").lower()
        if code.startswith("pt"):
            return "pt"
    except Exception:
        pass
    return "en"


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
    root.geometry("960x540")
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

    # Idioma inicial: detectado a partir do Windows / locale do SO; o radio
    # "Idioma / Language" abaixo permite override manual em tempo real.
    default_lang = _detect_os_lang()

    # Estado tkinter (mutável, ligado aos widgets)
    kml_var = tk.StringVar(value=initial_kml)
    lang_var = tk.StringVar(value=default_lang)
    paused_var = tk.BooleanVar(value=False)
    headland_var = tk.BooleanVar(value=True)
    mode_var = tk.StringVar(value="boustrophedon")
    method_var = tk.StringVar(value=getattr(args, "method", None) or "zones")
    idw_power_var = tk.DoubleVar(value=getattr(args, "idw_power", 2.0))
    idw_grid_var = tk.DoubleVar(value=getattr(args, "idw_grid_m", 0.0))
    tractor_speed_var = tk.StringVar(value="medium")
    sim_speed_var = tk.StringVar(value="medium")
    started = {"ok": False}

    # Registro i18n: pares (widget, chave) -> LAUNCHER_TEXTS[chave][lang].
    # _apply_lang() reescreve text= em todos quando lang_var muda.
    i18n_widgets: list[tuple[object, str]] = []

    def _t(key: str) -> str:
        return LAUNCHER_TEXTS[key][lang_var.get()]

    def _register(widget: object, key: str) -> None:
        i18n_widgets.append((widget, key))

    # Título
    title = ttk.Label(root, font=("Segoe UI", 14, "bold"))
    title.pack(pady=(10, 6))
    _register(title, "title")

    # Layout 16:9 em duas colunas: esquerda = setup geral (KML, idioma,
    # opções, traçado, velocidades); direita = método de prescrição e
    # parâmetros do IDW (slider N, espaçamento de grid, contagem de amostras).
    main_frame = ttk.Frame(root, padding=(20, 0))
    main_frame.pack(fill="both", expand=True)
    main_frame.columnconfigure(0, weight=1, uniform="cols")
    main_frame.columnconfigure(1, weight=1, uniform="cols")
    left_col = ttk.Frame(main_frame)
    left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    right_col = ttk.Frame(main_frame)
    right_col.grid(row=0, column=1, sticky="nsew")

    # === COLUNA ESQUERDA: setup geral ===

    # Exemplo de KML
    kml_header = ttk.Label(left_col, font=("Segoe UI", 10, "bold"))
    kml_header.pack(anchor="w")
    _register(kml_header, "kml_section")
    kml_row = ttk.Frame(left_col)
    kml_row.pack(anchor="w", padx=20, pady=(2, 8), fill="x")
    kml_combo = ttk.Combobox(
        kml_row,
        textvariable=kml_var,
        values=kml_names,
        width=24,
    )
    kml_combo.pack(side="left", padx=(0, 6))

    def browse_kml() -> None:
        path = filedialog.askopenfilename(
            title=_t("browse_dialog"),
            filetypes=[("KML files", "*.kml"), ("All files", "*.*")],
            initialdir=str(DATA_DIR) if DATA_DIR.exists() else None,
        )
        if path:
            kml_var.set(path)

    browse_btn = ttk.Button(kml_row, command=browse_kml)
    browse_btn.pack(side="left")
    _register(browse_btn, "browse_btn")

    # Idioma — labels dos radios são fixos ("Português"/"English") porque
    # representam os idiomas em si.
    lang_header = ttk.Label(left_col, font=("Segoe UI", 10, "bold"))
    lang_header.pack(anchor="w")
    _register(lang_header, "lang_section")
    lang_frame = ttk.Frame(left_col)
    lang_frame.pack(anchor="w", pady=(2, 8), padx=20)
    ttk.Radiobutton(lang_frame, text="Português", variable=lang_var, value="pt").pack(
        side="left", padx=(0, 24)
    )
    ttk.Radiobutton(lang_frame, text="English", variable=lang_var, value="en").pack(
        side="left"
    )

    # Opções
    opt_header = ttk.Label(left_col, font=("Segoe UI", 10, "bold"))
    opt_header.pack(anchor="w")
    _register(opt_header, "options_section")
    paused_check = ttk.Checkbutton(left_col, variable=paused_var)
    paused_check.pack(anchor="w", padx=20, pady=(2, 2))
    _register(paused_check, "paused_check")
    headland_check = ttk.Checkbutton(left_col, variable=headland_var)
    headland_check.pack(anchor="w", padx=20, pady=(0, 8))
    _register(headland_check, "headland_check")

    # Velocidade do trator (real, em km/h)
    tractor_header = ttk.Label(left_col, font=("Segoe UI", 10, "bold"))
    tractor_header.pack(anchor="w", pady=(6, 0))
    _register(tractor_header, "tractor_section")
    for value, key in [
        ("slow", "tractor_slow"),
        ("medium", "tractor_medium"),
        ("fast", "tractor_fast"),
    ]:
        r = ttk.Radiobutton(left_col, variable=tractor_speed_var, value=value)
        r.pack(anchor="w", padx=20, pady=1)
        _register(r, key)

    # Velocidade da simulação (animação)
    anim_header = ttk.Label(left_col, font=("Segoe UI", 10, "bold"))
    anim_header.pack(anchor="w", pady=(6, 0))
    _register(anim_header, "anim_section")
    for value, key in [
        ("slow", "anim_slow"),
        ("medium", "anim_medium"),
        ("fast", "anim_fast"),
    ]:
        r = ttk.Radiobutton(left_col, variable=sim_speed_var, value=value)
        r.pack(anchor="w", padx=20, pady=1)
        _register(r, key)

    # === COLUNA DIREITA: método de prescrição e parâmetros do IDW ===

    # Método de prescrição (Zonas vs IDW puro) — comparação sugerida pelo orientador
    method_header = ttk.Label(right_col, font=("Segoe UI", 10, "bold"))
    method_header.pack(anchor="w")
    _register(method_header, "method_section")
    for value, key in [
        ("zones", "method_zones"),
        ("idw", "method_idw"),
    ]:
        r = ttk.Radiobutton(right_col, variable=method_var, value=value)
        r.pack(anchor="w", padx=20, pady=1)
        _register(r, key)

    # Potência N (0.5-5.0, default 2.0): Spinbox com presets em passo 0.5
    # — mesma estética do controle de espaçamento do grid abaixo, para o
    # usuário ter um padrão visual consistente nas opções do IDW.
    idw_row = ttk.Frame(right_col)
    idw_row.pack(anchor="w", padx=20, pady=(10, 0), fill="x")
    idw_label = ttk.Label(idw_row)
    idw_label.pack(side="left")
    _register(idw_label, "idw_power_label")
    idw_presets = (
        "0.5", "1.0", "1.5", "2.0", "2.5", "3.0", "3.5", "4.0", "4.5", "5.0",
    )
    idw_spin = ttk.Spinbox(
        idw_row,
        values=idw_presets,
        textvariable=idw_power_var,
        width=6,
    )
    idw_spin.pack(side="left", padx=(8, 0))

    # Espaçamento do grid de amostras dentro de cada zona (0 = só centroide).
    # Spinbox com presets: passo fino nas faixas pequenas para ver a transição
    # didática; passos maiores nas faixas mais espaçadas.
    grid_header = ttk.Label(right_col, font=("Segoe UI", 10, "bold"))
    grid_header.pack(anchor="w", pady=(10, 0))
    _register(grid_header, "grid_section")
    grid_row = ttk.Frame(right_col)
    grid_row.pack(anchor="w", padx=20, pady=(2, 0), fill="x")
    grid_label = ttk.Label(grid_row)
    grid_label.pack(side="left")
    _register(grid_label, "grid_label")
    grid_presets = (
        "0", "5", "10", "15", "20", "25", "30", "40", "50",
        "75", "100", "150", "200",
    )
    grid_spin = ttk.Spinbox(
        grid_row,
        values=grid_presets,
        textvariable=idw_grid_var,
        width=6,
    )
    grid_spin.pack(side="left", padx=(8, 8))
    # Label dinâmico que mostra quantas amostras o método/grid escolhido vai
    # gerar para o KML atual: "= 6 centroides + 7 externas = 13 amostras".
    grid_count_label = ttk.Label(
        right_col,
        text="",
        foreground="#0066cc",
        font=("Segoe UI", 9),
    )
    grid_count_label.pack(anchor="w", padx=20, pady=(2, 0))

    # Cache dos KMLs já parseados (evita reler o XML a cada mudança).
    kml_cache: dict[str, KmlData] = {}

    def _resolve_kml_path(name: str) -> Path | None:
        if not name:
            return None
        for p in kml_paths:
            if p.name == name:
                return p
        # Caminho explícito digitado pelo usuário
        candidate = Path(name)
        return candidate if candidate.exists() else None

    def _update_count(*_args: object) -> None:
        path = _resolve_kml_path(kml_var.get().strip())
        if path is None:
            grid_count_label.configure(text="")
            return
        path_key = str(path)
        if path_key not in kml_cache:
            try:
                kml_cache[path_key] = parse_kml(path)
            except Exception:
                grid_count_label.configure(text=_t("kml_invalid"))
                return
        kml_data = kml_cache[path_key]
        n_external = len(kml_data.samples)
        if method_var.get() == "zones":
            # centroids_from_zones aplica o filtro "marca interna substitui
            # centroide": zonas com marca dentro saem da contagem. Mantém a
            # contagem consistente com o modo IDW (mesmo KML, mesmo número).
            samples = centroids_from_zones(kml_data)
            n_zone = len(samples)
            label = f"   = {n_zone} {_t('count_centroides')}"
        else:
            try:
                grid_m = float(idw_grid_var.get())
            except (ValueError, tk.TclError):
                grid_m = 0.0
            if grid_m > 0:
                samples = grid_samples_from_zones(kml_data, grid_m)
                n_zone = len(samples)
                label = f"   = {n_zone} {_t('count_grid')}"
            else:
                samples = centroids_from_zones(kml_data)
                n_zone = len(samples)
                label = f"   = {n_zone} {_t('count_centroides')}"
        if n_external > 0:
            total = n_zone + n_external
            label += f" + {n_external} {_t('count_externas')} = {total}"
        grid_count_label.configure(text=label)

    def _sync_idw_state(*_args: object) -> None:
        state = "normal" if method_var.get() == "idw" else "disabled"
        idw_label.configure(state=state)
        idw_spin.configure(state=state)
        grid_label.configure(state=state)
        grid_spin.configure(state=state)
        grid_count_label.configure(state=state)

    def _apply_lang(*_args: object) -> None:
        lang = lang_var.get()
        for w, key in i18n_widgets:
            try:
                w.configure(text=LAUNCHER_TEXTS[key][lang])
            except tk.TclError:
                pass
        # Strings dinâmicas (contagem de amostras, "(KML inválido)") também
        # precisam ser regeneradas no idioma novo.
        _update_count()

    # Botão Iniciar
    def confirm() -> None:
        if not kml_var.get().strip():
            messagebox.showerror("VRA_Simulador", _t("msg_kml_required"))
            return
        started["ok"] = True
        root.destroy()

    btn_frame = ttk.Frame(root)
    btn_frame.pack(pady=10)
    btn = ttk.Button(btn_frame, command=confirm, width=20)
    btn.pack()
    _register(btn, "start_btn")
    btn.focus_set()

    # Atalhos de teclado
    root.bind("<Return>", lambda _e: confirm())
    root.bind("<Escape>", lambda _e: root.destroy())

    # Traces e aplicação inicial dos textos / estado dos widgets de IDW.
    kml_var.trace_add("write", _update_count)
    method_var.trace_add("write", _update_count)
    idw_grid_var.trace_add("write", _update_count)
    method_var.trace_add("write", _sync_idw_state)
    lang_var.trace_add("write", _apply_lang)
    _apply_lang()
    _sync_idw_state()

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
