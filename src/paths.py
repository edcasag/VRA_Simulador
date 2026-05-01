"""Localiza as pastas data/ e assets/ de forma uniforme em dev e em bundle.

Em modo script, `Path(__file__).resolve().parent.parent` aponta para `_python/`
e DATA_DIR/ASSETS_DIR resolvem para os arquivos versionados no repositório.
Em bundle PyInstaller, o mesmo caminho resolve para a pasta MEIPASS de
extração temporária — o `.spec` deve declarar `datas=[('data', 'data'),
('assets', 'assets')]` para que os arquivos sejam bundleados na mesma posição
relativa.
"""

from __future__ import annotations

from pathlib import Path


def app_dir() -> Path:
    """Raiz da aplicação. _python/ em dev; MEIPASS em bundle."""
    return Path(__file__).resolve().parent.parent


DATA_DIR = app_dir() / "data"
ASSETS_DIR = app_dir() / "assets"
