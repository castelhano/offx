# Dica de Seleção (Calc)

Mostra, ao lado da seleção no LibreOffice Calc, um balão com a quantidade de
células selecionadas e a soma dos valores numéricos. Ex.:
`10 células  (5L × 2C)   Soma: 1.234,50`.

## Build / instalação

```bash
./build.sh                                  # gera ../dist/selectiontip.oxt
unopkg add -f ../dist/selectiontip.oxt      # instala (com o LibreOffice fechado)
unopkg remove org.offx.selectiontip         # desinstala
```

Depois de instalar, reinicie o LibreOffice.

## Como funciona

- `Jobs.xcu` executa o job no evento `onFirstVisibleTask`; o job registra um
  listener global de documentos e conecta um `ViewWatcher` a cada view do Calc.
- Em cada mudança de seleção: contagem pelos endereços dos intervalos, soma via
  `XSheetOperation.computeFunction` (código nativo do Calc, o mesmo da barra
  de status).
- O formato da soma vem de uma única célula: a primeira da seleção ou, se ela
  for texto/vazia, a seguinte (abaixo; à direita em seleção de uma linha só):
  - hora: total em horas, sem zerar em 24h (`25:30`; segundos só se houver);
  - porcentagem: 2 casas (`37,04%`);
  - moeda: formato da própria célula (`R$ 3.703,70`);
  - data: só a contagem, sem soma;
  - demais: 2 casas e separador de milhar (`1.234,50`).
- A posição vem de `VisibleArea` (1/100 mm) → `VisibleAreaOnScreen` (pixels),
  ancorada no canto inferior direito da última célula, presa à área visível.
- O balão some ao clicar, ao trocar de janela, ao selecionar uma única célula
  ou após `AUTO_HIDE_SECONDS` (0 = não some sozinho).

Configurações no topo de `python/selectiontip.py`.

## Debug

```bash
SELECTIONTIP_LOG=/tmp/selectiontip.log soffice --calc
```

## Limitações conhecidas

- Se a seleção misturar tipos (ex.: horas e valores), o total segue o formato
  da célula de referência.
- Com painéis congelados/divididos, a posição usa o painel ativo.
- Ao rolar a planilha, o balão não acompanha (some no auto-hide/no clique).
