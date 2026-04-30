"""Strings PT/EN do simulador VRA.

Permite gerar imagens/relatórios em português (default, para o artigo SBIAGRO 2025)
ou inglês (para o artigo CEA), via argumento --lang em main.py.
"""

from __future__ import annotations

from typing import Any

STRINGS: dict[str, dict[str, Any]] = {
    "pt": {
        "header_title": "Simulador VRA",
        "legend_title": "Taxa de aplicação (kg/ha)",
        "ready_banner": "Pronto — pressione ESPAÇO para iniciar a simulação",
        "slide_footer": "Slide {idx} / {total}   —   ESPAÇO para iniciar",
        "press_space_for_report": "Pressione ESPAÇO para ver o relatório",
        "speed_hint": "Velocidade: {speed:.1f}×    +/− ajusta",
        "report_title": "Relatório de aplicação por zona",
        "report_footer": "Pressione qualquer tecla para fechar",
        "report_note": (
            "Resultados aproximados. A operação manual real do trator é mais eficiente."
        ),
        "report_console_title": "=== Relatório de aplicação por zona ===",
        "report_saved": "Relatório salvo em",
        "hud_pos": "Pos (m)",
        "hud_altitude": "Altitude",
        "hud_speed": "Velocidade",
        "hud_dose": "Dose alvo",
        "hud_coverage": "Cobertura",
        "hud_time": "Tempo sim.",
        "tbl_zone": "Zona",
        "tbl_target": "Alvo (kg/ha)",
        "tbl_applied": "Aplicado (kg/ha)",
        "tbl_error": "Erro %",
        "tbl_planned_kg": "Planejado (kg)",
        "tbl_applied_kg": "Aplicado (kg)",
        "tbl_coverage": "Cobertura %",
        "tbl_area": "Área (ha)",
        "summary_title": "=== Sumário do KML ===",
        "summary_field": "Talhão",
        "summary_field_none": "(não informado; bbox usado como limite)",
        "summary_field_fmt": "{vertices} vértices, área = {area_ha:.2f} ha",
        "summary_inclusion": "Zonas de inclusão",
        "summary_exclusion": "Zonas de exclusão",
        "summary_circles": "Zonas circulares",
        "summary_samples": "Pontos de amostra",
        "summary_bbox": "Bbox da trajetória",
        "summary_strips": "faixas estimadas",
        "intro_slides": [
            {
                "title": "Aplicação em Taxa Variável (VRA)",
                "lines": [
                    "{n_zones} zonas de aplicação, doses {min_rate:.0f} a {max_rate:.0f} kg/ha.",
                    "Esquerda: mapa esperado. Direita: aplicação simulada.",
                    "Trator de 2 m, distribuidor de discos de {width_m:.0f} m.",
                ],
                "duration_s": 7.0,
            },
            {
                "title": "Velocidade modulada pelo relevo",
                "lines": [
                    "Velocidade nominal 6 km/h, modulada pelo declive Z(x, y).",
                    "Subida até 1,8 km/h; descida até 9 km/h.",
                    "Controlador ajusta a vazão; erro residual ≤ ±5%.",
                ],
                "duration_s": 7.0,
            },
        ],
    },
    "en": {
        "header_title": "VRA Simulator",
        "legend_title": "Application rate (kg/ha)",
        "ready_banner": "Ready — press SPACE to start the simulation",
        "slide_footer": "Slide {idx} / {total}   —   SPACE to start",
        "press_space_for_report": "Press SPACE to see the report",
        "speed_hint": "Speed: {speed:.1f}×    +/− adjusts",
        "report_title": "Application report by zone",
        "report_footer": "Press any key to close",
        "report_note": (
            "Approximate results. Real manual tractor operation is more efficient."
        ),
        "report_console_title": "=== Application report by zone ===",
        "report_saved": "Report saved to",
        "hud_pos": "Pos (m)",
        "hud_altitude": "Altitude",
        "hud_speed": "Speed",
        "hud_dose": "Target rate",
        "hud_coverage": "Coverage",
        "hud_time": "Sim. time",
        "tbl_zone": "Zone",
        "tbl_target": "Target (kg/ha)",
        "tbl_applied": "Applied (kg/ha)",
        "tbl_error": "Error %",
        "tbl_planned_kg": "Planned (kg)",
        "tbl_applied_kg": "Applied (kg)",
        "tbl_coverage": "Coverage %",
        "tbl_area": "Area (ha)",
        "summary_title": "=== KML summary ===",
        "summary_field": "Field",
        "summary_field_none": "(not provided; bbox used as boundary)",
        "summary_field_fmt": "{vertices} vertices, area = {area_ha:.2f} ha",
        "summary_inclusion": "Inclusion zones",
        "summary_exclusion": "Exclusion zones",
        "summary_circles": "Circular zones",
        "summary_samples": "Reference samples",
        "summary_bbox": "Trajectory bbox",
        "summary_strips": "estimated strips",
        "intro_slides": [
            {
                "title": "Variable Rate Application (VRA)",
                "lines": [
                    "{n_zones} application zones, rates {min_rate:.0f} to {max_rate:.0f} kg/ha.",
                    "Left: target map. Right: simulated application.",
                    "2 m tractor with {width_m:.0f} m disc spreader.",
                ],
                "duration_s": 7.0,
            },
            {
                "title": "Speed modulated by terrain",
                "lines": [
                    "Nominal speed 6 km/h, modulated by slope Z(x, y).",
                    "Uphill down to 1.8 km/h; downhill up to 9 km/h.",
                    "Controller adjusts flow rate; residual error ≤ ±5%.",
                ],
                "duration_s": 7.0,
            },
        ],
    },
}


def t(lang: str, key: str) -> Any:
    """Devolve a string traduzida; cai em pt se a língua não existir."""
    return STRINGS.get(lang, STRINGS["pt"])[key]
