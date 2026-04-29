# Simulador VRA — Tese de Mestrado POLI/USP

Simulação Python do algoritmo de aplicação em taxa variável (VRA) descrito na dissertação **"Aplicação em Taxa Variável na Agricultura Familiar com Zonas de Manejo no Google Earth"** (Edson Casagrande, PPG Engenharia de Computação, EP-USP, orientação Prof. Carlos Eduardo Cugnasca).

Lê um KML do Google Earth nas convenções da Tab. 4 (cap 6 §6.3 da tese), calcula dose por coordenada GNSS via IDW (cap 5 §5.4), simula um trator percorrendo o talhão com velocidade variável conforme o relevo, e mostra split-screen pygame com mapa de zonas + pintura progressiva. Reproduz numericamente a Tab. 6 do cap 7 §7.3 (acurácia sob variação de velocidade, erro ≤±5%).

## Instalação

```bash
python -m pip install -r requirements.txt
```

(Se preferir venv: `python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt`.)

## Uso

```bash
# Testes unitários (deve mostrar 9/9 vra_engine + 11/11 terrain passando)
python -m pytest tests/ -v

# Modo boustrophedon (apresentação): trator vai-e-volta com velocidade modulada pelo relevo
python -m src.main data/ensaio_abcd.kml --mode boustrophedon

# Modo aleatório (teste unitário visual): pontos GPS sorteados, mapa emerge por amostragem
python -m src.main data/ensaio_abcd.kml --mode random

# Talhão maior com 7 zonas cobrindo a legenda canônica da fig:vra
python -m src.main data/talhao_completo.kml --mode boustrophedon
```

Tecla `S` na janela: salva snapshot manual em `docs/`. Snapshots automáticos em 25%, 50% e 100% da cobertura também ficam em `docs/`. Ao final, o relatório de erro por zona é impresso no console e gravado em `docs/relatorio_erro.csv`.

## Parâmetros do declive

```bash
# Sem bossa central, só rampa uniforme 6%
python -m src.main data/ensaio_abcd.kml --decline-x 0.06 --bump-h 0

# Sem declive, só bossa de 3 m no centro
python -m src.main data/ensaio_abcd.kml --decline-x 0 --bump-h 3
```

## Convenções KML adotadas

Conforme Tab. 4 da dissertação (cap 6 §6.3):

| Feature KML | Nome (`<name>`) | Significado |
| --- | --- | --- |
| `Polygon` | `Label=Rate` | Zona de inclusão com dose `Rate` kg/ha |
| `Polygon` | `Label=0` | Zona de exclusão (não aplicar) |
| `Polygon` | `Field=Rate` | Talhão completo, dose-base `Rate` |
| `Point` | `Label=Rate:Radius` | Região circular (ex.: `Cupinzeiro=0:3m`) |
| `Point` | `Label=Rate` | Ponto de amostra para IDW |

## Colormap

Sete cores discretas espelhando a `fig:vra` do cap 1 da tese (`images/VRA - Adubo.jpg`):

| Cor | Dose (kg/ha) |
| --- | --- |
| Verde escuro | 50 |
| Verde claro | 60 |
| Amarelo | 70 |
| Laranja | 80 |
| Vermelho claro | 85 |
| Vermelho médio | 90 |
| Vermelho escuro | 100 |

Doses intermediárias (ex.: 75) são mapeadas por vizinho mais próximo da escala.

## Modelo de declive e velocidade

Perfil de altitude analítico:

```text
Z(x,y) = a·x + b·y + Σ h·exp(−r²/2σ²)
```

Velocidade do trator modulada pelo gradiente projetado sobre a direção de movimento:

```text
v = clip(v_nom − α·∇Z·ĥ, v_min, v_max)
```

Defaults: `v_nom=5.0`, `v_min=1.5`, `v_max=7.0` m/s, `α=50` m/s por (m/m).

## Limitações

- KML `data/ensaio_abcd.kml` usa zonas explícitas com lookup direto; a interpolação IDW é exercitada apenas em pontos de amostra esparsos (não cobertos nesta primeira entrega).
- Erro de aplicação por zona modelado de forma agregada como ruído gaussiano truncado ±5% — aproximação que representa latência do controlador, atuador e GNSS sem modelar cada componente isoladamente.
- Rotação/curvatura do trator simplificadas: o vetor heading muda discretamente entre faixas, sem dinâmica de viragem.
