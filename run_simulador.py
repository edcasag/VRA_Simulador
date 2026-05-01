"""Entry point para o PyInstaller. Quando empacotado em .exe, este é o
script que o PyInstaller usa como ponto de partida. Importa o package `src`
(que tem __init__.py) e chama main() — os imports relativos dentro de src/
funcionam normalmente porque main é importado como `src.main`."""

from src.main import main

if __name__ == "__main__":
    main()
