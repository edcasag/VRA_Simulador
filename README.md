# VRA_Simulador

[![DOI](https://zenodo.org/badge/1224081407.svg)](https://doi.org/10.5281/zenodo.19893498)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://www.python.org/downloads/)

Simulador em Python da aplicação em taxa variável (VRA) com zonas de manejo lidas de um KML do Google Earth. Reproduz o comportamento de um operador humano dirigindo um trator com distribuidor de discos: faz cabeceira em talhões irregulares, contorna construções, modula a velocidade pelo declive e reporta a massa efetivamente aplicada por zona em comparação com a prescrição planejada.

Acompanha a dissertação de mestrado de Edson Casagrande na Escola Politécnica da USP (POLI/USP), orientação Prof. Carlos Eduardo Cugnasca.

> **Veja também**: [VRA_Controlador](https://github.com/edcasag/VRA_Controlador) — POC complementar em ESP32 (DOI [10.5281/zenodo.19922431](https://doi.org/10.5281/zenodo.19922431)). O Python simula a parte algorítmica idealizada (aplicação instantânea da dose-alvo); o ESP32 cobre a física do atuador (PID, planta de 1ª ordem, saturação por vazão máxima). Paridade numérica bit-a-bit validada em 2010 fixes do ensaio A/B/C/D.

![Captura do simulador rodando o Sítio Palmar](assets/screenshot_palmar.png)

> 🇬🇧 **English version**: see the section [English](#english) at the end of this document.

---

## Recursos

- Lê zonas de manejo de um KML do Google Earth (polígonos, círculos, pontos de amostra).
- Trajetória boustrofédica (vai e volta) com curvas em U, recortada pelo contorno irregular do talhão (Estratégia B).
- Cabeceira automática (passada do perímetro) em talhões com geometria irregular.
- Desvio de zonas de exclusão (construções, áreas de pousio, mata) e de pequenos obstáculos circulares (cupins, pedras).
- Velocidade do trator modulada pelo declive analítico do terreno (subida desacelera, descida acelera).
- Interpolação IDW (Inverse Distance Weighting) para pontos de amostra esparsos.
- Relatório por zona em kg planejados vs kg efetivamente aplicados, com erro %, cobertura % e linha de total.
- Interface bilíngue (pt-BR e en-US) via `--lang`.
- Snapshots automáticos da janela em 25%, 50% e 100% da cobertura, salvos como PNG.

## Pré-requisitos

- **Python 3.13** ou superior. Recomendado instalar de [python.org](https://www.python.org/downloads/) (marcar "Add python.exe to PATH" no instalador).
- Sistema operacional: Windows, Linux ou macOS.

Dependências (3 pacotes Python, ~30 MB no total):

- `pygame >= 2.5` — janela e renderização
- `numpy >= 1.24` — auxiliar para o ícone do trator
- `pytest >= 7.4` — apenas para rodar os testes

## Instalação

```bash
# Clonar o repositório
git clone https://github.com/edcasag/VRA_Simulador.git
cd VRA_Simulador

# Instalar as dependências (no Python do sistema)
python -m pip install -r requirements.txt
```

Se preferir um ambiente virtual isolado (recomendado para evitar conflito com outros projetos Python):

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install -r requirements.txt
```

## Uso rápido

```bash
# Roda direto, em português
python -m src.main data/ensaio_abcd.kml

# Em inglês, com snapshots em pasta separada (artigo CEA, etc.)
python -m src.main data/ensaio_abcd.kml --lang en --docs-dir docs/en

# Modo apresentação (pausado no início, slides explicativos, ESPAÇO inicia)
# Útil para gravar vídeo da tela
python -m src.main data/Sitio\ Palmar.kml --paused-start

# Talhão real e irregular (Sítio Palmar) — cabeceira automática ativada
python -m src.main "data/Sitio Palmar.kml"

# Ver todas as opções
python -m src.main --help
```

Com `py` launcher no Windows (caso tenha mais de uma versão de Python instalada):

```powershell
py -m src.main data/ensaio_abcd.kml
```

## Opções da CLI

| Argumento | Tipo | Default | Descrição |
|---|---|---|---|
| `kml` (posicional) | caminho | obrigatório | Arquivo `.kml` com as zonas de manejo |
| `--lang` | `pt` \| `en` | `pt` | Idioma da janela e do CSV do relatório |
| `--paused-start` | flag | desligado | Inicia pausado, mostra slides; ESPAÇO inicia |
| `--headland` | `auto` \| `on` \| `off` | `auto` | Cabeceira (perímetro). Auto: liga se talhão tem ≥5 vértices |
| `--speed-factor` | float | `0.3` | Velocidade da animação (0.2 lenta, 1 média, 3 rápida) |
| `--docs-dir` | caminho | `docs/` | Diretório para snapshots PNG e CSV |
| `--snapshot-prefix` | string | `snapshot` | Prefixo dos snapshots automáticos |
| `--width-m` | float | `20.0` | Largura da faixa de aplicação do distribuidor (m) |
| `--cell-m` | float | `1.5` | Profundidade longitudinal do retângulo de pintura (m) |
| `--paint-offset-back-m` | float | `1.0` | Distância da pintura atrás do trator (m) |
| `--gnss-noise-m` | float | `0.0` | Desvio-padrão do ruído GNSS (m) |
| `--decline-x` | float | `0.04` | Declive em x (m/m), default 4% |
| `--decline-y` | float | `0.0` | Declive em y (m/m) |
| `--bump-h` | float | `2.0` | Altura da bossa central do terreno (m) |

Teclas durante a simulação:

| Tecla | Ação |
|---|---|
| `ESPAÇO` | Pausa/retoma; avança slides na introdução; abre relatório no final |
| `S` | Salva snapshot manual da janela atual |
| `ESC` | Fecha |

## Convenção do KML

| Feature | Sintaxe (no campo `<name>`) | Significado |
|---|---|---|
| Polígono talhão | `Field=0` ou nome qualquer (ex.: `Palmar`) | Contorno do talhão. Sem tag, vira `field_polygon` com rate 0 |
| Polígono inclusão (com label) | `Good=100` | Zona de aplicação com dose 100 kg/ha |
| Polígono inclusão (sem label) | `100` | Equivalente, com label autonumerado (Z1, Z2…) |
| Polígono exclusão | `Sede=0` ou apenas `0` | Não aplicar (construção, mata, pousio) |
| Círculo (com label) | `Pedra=0:5m` | Exclusão circular de raio 5 m |
| Círculo (sem label) | `0:5m` | Idem, label autogerado |
| Ponto de amostra (IDW) | `120` | Amostra com taxa 120 kg/ha em coordenada específica |

Espaços ao redor do `=` e do `:` são tolerados (`Good = 100` funciona igual a `Good=100`).

Quando duas zonas de inclusão se sobrepõem, **vence a de menor área** (a "sub-zona específica" predomina sobre a "zona-fundo"). Útil para modelagem hierárquica.

## KMLs incluídos

| Arquivo | Conteúdo | Uso |
|---|---|---|
| `data/ensaio_abcd.kml` | 4 zonas A/B/C/D, retangulares, 1 ha cada, doses 90/75/60/100 kg/ha | Ensaio integrado da Tab. 6 da dissertação. Caso ideal: erro próximo de 0% |
| `data/talhao_completo.kml` | 7 zonas retangulares tilando a escala 50–100 kg/ha | Demonstração da legenda de cores |
| `data/Sitio Palmar.kml` | Talhão real do autor, 14 vértices irregulares, 6 zonas de inclusão, 1 polígono de exclusão (Sede), 2 círculos (cupins/pedras), 7 amostras IDW | Validação em campo real, demonstração da Estratégia B + cabeceira |

## Lendo o relatório

Ao final da simulação, pressione **ESPAÇO** para abrir o painel central com o relatório por zona. O CSV equivalente é salvo em `docs/relatorio_erro.csv`. Colunas:

| Coluna | Significado |
|---|---|
| `Zona` | Rótulo da zona (ou Z1/Z2... se sem label no KML) |
| `Alvo (kg/ha)` | Dose prescrita |
| `Área (ha)` | Área da zona |
| `Planejado (kg)` | Massa que seria usada com cobertura e dose perfeitas (alvo × área) |
| `Aplicado (kg)` | Massa efetivamente depositada na simulação |
| `Erro %` | (Aplicado − Planejado) / Planejado × 100 |
| `Cobertura %` | Fração da área da zona varrida pelo swath (pode passar de 100% por sobreposição realista nas bordas) |

A linha **Total** agrega todas as zonas (somatório de planejado e aplicado, e erro % geral da operação).

> **Nota.** Os resultados são aproximados. A operação manual real do trator é mais eficiente. O simulador é uma ferramenta didática que captura efeitos de borda e geometria, não um substituto da realidade de campo.

## Testes

```bash
python -m pytest tests/ -v
```

Cobertura atual: 9 testes do motor VRA + 11 testes do modelo de terreno.

## Como citar

Em artigos acadêmicos, use o DOI permanente do Zenodo:

```
Casagrande, E. (2026). VRA_Simulador (v1.0.0). Zenodo.
https://doi.org/10.5281/zenodo.19893498
```

Ou via BibTeX:

```bibtex
@software{casagrande2026vra,
  author       = {Casagrande, Edson},
  title        = {{VRA\_Simulador: Simulador de Aplica\c{c}\~{a}o em Taxa Vari\'{a}vel}},
  year         = 2026,
  publisher    = {Zenodo},
  version      = {v1.0.0},
  doi          = {10.5281/zenodo.19893498},
  url          = {https://doi.org/10.5281/zenodo.19893498}
}
```

Ver também `CITATION.cff` na raiz do repositório (formato padronizado, lido por GitHub e Zenodo).

## Licença

[MIT](LICENSE) — uso livre para fins acadêmicos e comerciais, com atribuição.

## Autor

**Edson Casagrande**
Mestrando em Engenharia da Computação, POLI/USP
Orientador: Prof. Carlos Eduardo Cugnasca
GitHub: [@edcasag](https://github.com/edcasag)

---

## English

VRA_Simulador is a Python simulator of variable-rate fertilizer application based on management zones read from a Google Earth KML. It reproduces what a human operator does when driving a tractor with a disc spreader: traces a perimeter pass on irregular fields, drives around buildings, modulates speed by terrain slope, and reports the actual mass applied per zone versus the prescribed amount.

It accompanies the master's dissertation by Edson Casagrande at the Polytechnic School of the University of São Paulo (POLI/USP), under Prof. Carlos Eduardo Cugnasca.

> **See also**: [VRA_Controlador](https://github.com/edcasag/VRA_Controlador) — complementary ESP32 POC (DOI [10.5281/zenodo.19922431](https://doi.org/10.5281/zenodo.19922431)). Python models the idealized algorithmic side (instant dose application); ESP32 covers the physical actuator (PID, first-order plant, max-flow saturation). Bit-for-bit numerical parity validated on 2010 fixes of the A/B/C/D experiment.

### Quick install

```bash
git clone https://github.com/edcasag/VRA_Simulador.git
cd VRA_Simulador
python -m pip install -r requirements.txt
```

Requirements: Python 3.13+, pygame, numpy, pytest.

### Quick run

```bash
# Default Portuguese UI
python -m src.main data/ensaio_abcd.kml

# English UI, snapshots in a separate folder
python -m src.main data/ensaio_abcd.kml --lang en --docs-dir docs/en

# Real irregular field with automatic headland pass
python -m src.main "data/Sitio Palmar.kml"

# Show all options
python -m src.main --help
```

### Keys during simulation

- `SPACE`: pause/resume; advance intro slides; open report at the end
- `S`: save manual snapshot of the current window
- `ESC`: close

### KML conventions

Polygon names follow `Label=Rate` for inclusion zones (e.g., `Good=100`), `Label=0` or just `0` for exclusion zones, and `Label=Rate:Radius` (e.g., `Pedra=0:5m`) for circular exclusions. Plain numbers (`120`) on points are IDW samples. See the Portuguese section above for the full table.

### Report

Each zone reports planned vs applied mass (kg), error %, and coverage %. A total line aggregates the entire field. **Note:** results are approximate; real manual tractor operation is more efficient. The simulator is a didactic tool that captures geometric and boundary effects.

### How to cite

```
Casagrande, E. (2026). VRA_Simulador (v1.0.0). Zenodo.
https://doi.org/10.5281/zenodo.19893498
```

### License

[MIT](LICENSE).
