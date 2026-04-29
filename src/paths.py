"""Localiza as pastas externas data/ e assets/ tanto em modo script quanto
empacotado via PyInstaller.

- Modo script (dev): assume que data/ e assets/ são irmãos de src/, ou seja,
  ficam em ../data e ../assets relativos a este arquivo.
- Modo .exe (PyInstaller --onefile): sys.frozen é True; data/ e assets/ ficam
  ao lado do executável (sys.executable.parent).
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Raiz da aplicação. _python/ em modo dev; pasta do .exe em bundle."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DATA_DIR = app_dir() / "data"
ASSETS_DIR = app_dir() / "assets"
