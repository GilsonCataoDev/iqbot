# IQ Option Bot

Ferramenta de pesquisa e validação de estratégias para IQ Option (opções binárias M5/M15/H1 e um laboratório de CFD Forex separado). Analisa candles, compara candidatos, abre um painel local e registra sinais. **O modo padrão é PESQUISA: monitora os ativos e não envia ordens.** O envio em `PRACTICE` ou `REAL` exige perfil e confirmação explícitos.

Credenciais nunca ficam no código: são lidas de `IQ_OPTION_EMAIL` / `IQ_OPTION_SENHA` (via `.env.bat`, que é ignorado pelo git) ou digitadas no terminal.

## Perfis ativos

| Perfil | Timeframe | Setups validados | Status |
|---|---|---|---|
| **M15** | 15 min | `sr_rejeicao`, `pin_bar_sr`, `fibo_sr_retracao` | Ativo, edge positivo provado |
| **H1** | 60 min | mesmos setups do M15 | Ativo, edge positivo provado |
| M1 | 1 min | — | Pausado (edge negativo em todos os setups testados) |
| Swing/CFD | D1+H4+H1 | `pullback_tendencia`, `sr_rejeicao`, `breakout_reteste`, `divergencia_rsi_h4` | Somente monitor visual — sinal automático tem edge negativo provado |

## Começar em 3 passos (M15/H1)

1. Execute `INSTALAR_DEPENDENCIAS.bat` uma vez.
2. Execute `TESTAR_M15_E_H1_PRACTICE.bat` (conta PRACTICE, os dois timeframes juntos) ou `TESTAR_SCALPING_M15_PRACTICE.bat` / rode `python rodar_iqoption_m5.py --scalping-h1-practice --confirmo` para um só.
3. Informe o e-mail e a senha da IQ Option no terminal, ou defina `IQ_OPTION_EMAIL`/`IQ_OPTION_SENHA` antes de rodar.

O M5 original (perfil de pesquisa/pausado) continua disponível: `INICIAR_IQ_M5.bat` / `PESQUISAR_IQ_M5.bat`. Pela linha de comando, o perfil PRACTICE M5 é `python rodar_iqoption_m5.py --practice --confirmo`.

## M5 ou M1

`INICIAR_IQ_M5.bat` usa candles de 5 minutos com expiração de 5 minutos. `INICIAR_IQ_M1.bat` usa candles de 1 minuto com expiração de 1 minuto; pela linha de comando é `python rodar_iqoption_m5.py --m1`.

No M1 a janela de entrada cai de 15 para 5 segundos, porque 15 segundos seriam um quarto de uma vela de um minuto.

Rode um de cada vez. O limite diário de operações é controlado dentro do processo, então dois monitores abertos poderiam enviar mais ordens do que o combinado.

**A triagem walk-forward mediu apenas M5.** Os números daquele estudo não valem para M1: a vela de um minuto tem mais ruído e o spread pesa proporcionalmente muito mais. Trate M1 como coleta nova, do zero.

O navegador abrirá automaticamente. A senha não fica gravada no projeto e não aparece enquanto é digitada.

## Ativos acompanhados

- Mercado normal: `EURUSD`, `GBPUSD` e `USDJPY`.
- OTC: `EURUSD-OTC`, `GBPUSD-OTC` e `USDJPY-OTC`.

No perfil PRACTICE com execução, o limite de cinco operações é compartilhado entre todos os ativos.

## O que aparece no gráfico

- Candles M5 e volume.
- Bandas de Bollinger, EMA curta e EMA longa.
- RSI, suporte, resistência e Fibonacci.
- Aviso de possível entrada e marcações de CALL/PUT.
- Pullbacks de tendência em Fibonacci e/ou suporte/resistência.
- Sinais bloqueados e resultados das operações PRACTICE.

## Alertas explicados e notícias

Além dos sinais que o robô opera, o painel mostra três cartões acima do gráfico:

- **Alerta**: aponta uma vela de corpo anormal (a família de reversão curta, única que sobreviveu à triagem walk-forward com purga) e diz em português por que apontou. Marca `entrada` quando o corpo passa de 1,5x o ATR e `aproximando` a partir de 1,0x. Mostra também **onde a entrada aconteceria**, com uma linha grossa no gráfico: contínua quando o preço já é conhecido (a abertura da vela em formação) e tracejada quando ainda é estimativa. Esse alerta **não envia ordem**: ele existe para você decidir.
- **Por que o robô sinalizou**: traduz a decisão da estratégia que opera de verdade — qual banda foi furada, qual nível o pullback tocou, como o RSI se moveu.
- **Notícias do dia**: divulgações macro de alto e médio impacto das moedas do par, com quantos minutos faltam. Quando falta menos de 20 minutos para um evento, o alerta ganha um aviso destacado.

O alerta de reversão sugere **expiração de 30 minutos**, que foi a testada na triagem. A expiração que o robô usa nas ordens continua sendo a de `config.py`; são coisas diferentes e o painel avisa isso.

Ativos `-OTC` nunca recebem aviso de notícia: o preço deles é gerado por algoritmo da corretora, então divulgação econômica real não o move.

A fonte do calendário é o feed público semanal do ForexFactory, sem cadastro. Sem internet, a ferramenta usa o último calendário salvo em `iqoption_m5\dados\calendario_economico.json`; sem nenhum dos dois, ela segue funcionando e apenas deixa de avisar.

## Estratégias de entrada (M15/H1 — ativas)

Medidas com `backtest_m15.py --m1-minutos 8` (reconstrói o candle parcial via M1 para remover o viés de lookahead dos setups de reversão). Breakeven a 85% de payout é 54,1%.

| Setup | WR medido | Edge |
|---|---|---|
| `sr_rejeicao` | ~80% | +25.9pp |
| `fibo_sr_retracao` | 75.5% | +21.4pp |
| `pin_bar_sr` | 74.6% | +20.5pp |
| `retracao_intracandle` | 50.4% | **-3.7pp — desligado** |
| `pullback` / `pullback_confluencia` | ~49% | **negativo — desligado** |
| `macd_crossover` | 33% | **negativo — desligado** |

O sizing por ordem é proporcional ao edge de cada setup (`multiplicador_por_setup` em `config.py`). Uma 2ª entrada na mesma direção enquanto outro par tiver posição aberta é bloqueada (`bloquear_direcao_paralela`) para evitar triplicar a mesma exposição quando os setups disparam juntos em pares correlacionados.

## Estratégias antigas (M5 — perfil de pesquisa)

1. `reversao_bollinger_rsi`: retorno para dentro da Banda de Bollinger com confirmação do RSI, somente em mercado lateral.
2. `pullback`: recuo a favor da tendência que toca Fibonacci de 38,2% a 61,8% e/ou um pivô de suporte/resistência, seguido por candle de confirmação e RSI entre 35 e 65.

No gráfico, `PULLBACK FIBO+SUPORTE` ou `PULLBACK FIBO+RESISTÊNCIA` indica confluência. O relatório separa os resultados das duas estratégias.

## Proteções

- Perfil PESQUISA observa estratégias candidatas e nunca envia ordens.
- Envio em `PRACTICE` exige `--practice --confirmo`.
- Valor de 1 unidade por ordem.
- Payout mínimo de 80%.
- Entrada somente no começo de um novo candle M5.
- Uma operação aberta por vez.
- Máximo de cinco operações por dia.
- Três perdas consecutivas ou prejuízo diário de 5 unidades encerram a sessão.

Essas regras reduzem risco, mas não garantem lucro. OTC e mercado normal têm comportamentos diferentes; acompanhe os resultados separadamente antes de considerar qualquer mudança.

## Resultados e testes

- Execute `VER_RESULTADOS_M5.bat` para consultar o histórico.
- Execute `TESTAR_IQ_M5.bat` para verificar o projeto sem enviar ordens reais.
- O banco PRACTICE fica em `iqoption_m5\dados\iqoption_m5_practice.sqlite3`.

## Investigação de estratégias alternativas

- Execute `python investigar_estrategias_m5.py` para repetir a triagem local sem conectar à IQ e sem enviar ordens.
- O experimento compara impulso, reversão curta, rompimentos e bounce em níveis, com expirações de 5, 15 e 30 minutos.
- A melhor hipótese atual foi reversão de um candle extremo no `GBPUSD` normal com expiração de 30 minutos, mas ainda não passou pelo intervalo simultâneo exigido para ser considerada viável.
- Consulte `RELATORIO_ESTRATEGIAS_M5.md` para os números e `PESQUISA_ESTRATEGIAS_VIAVEIS.md` para a evidência primária.

## Backtest

`RODAR_BACKTEST_M5.bat` mede a estratégia contra o histórico da IQ Option sem enviar nenhuma ordem. Ele usa a mesma `EstrategiaReversaoM5` do robô, então o que aparece no relatório é a lógica que roda ao vivo.

O backtest usa `configuracao_pesquisa_m5()`: 10 ativos e estratégias candidatas habilitadas. O perfil não aplica limite diário nem filtro de horário, para que a amostra possa ser comparada posteriormente por hora. Os limites de operação PRACTICE/REAL não reduzem a amostra de pesquisa.

O relatório traz a taxa de acerto com intervalo de confiança de 95% comparada ao breakeven do payout, separada por ativo, estratégia, direção, fatores de confluência e hora do dia. A linha final aponta as horas que ficaram abaixo do breakeven — candidatas a bloqueio.

Opções úteis:

```
python rodar_backtest_m5.py --candles 20000     histórico maior
python rodar_backtest_m5.py --offline           usa o cache, não baixa nada
python rodar_backtest_m5.py --ativos EURUSD-OTC  um ativo só
python rodar_backtest_m5.py --payout 0.92       ajusta o breakeven
python rodar_backtest_m5.py --spread-pips 0.0002 --slippage-pips 0.0001
python rodar_backtest_m5.py --offline --validar  valida os filtros fora da amostra
```

`--validar` divide o histórico em treino (70% inicial) e teste (30% final), escolhe os filtros olhando somente o treino e mede o resultado no período escondido. Serve para separar vantagem real de coincidência: um filtro escolhido depois de ver todos os dados quase sempre parece bom no próprio período em que foi escolhido.

O relatório também executa `BacktestRealista`. Informe `--spread-pips` e `--slippage-pips` com valores observados na conta; deixar ambos em zero mostra apenas o efeito do payout.

O histórico baixado fica em `iqoption_m5\dados\historico\`, então depois da primeira execução o `--offline` permite testar ideias sem esperar download.

Cada linha do relatório vale para o ativo daquela linha. Os ativos `-OTC` têm preço sintético gerado pela corretora: o resultado de um `-OTC` não vale para o par real de mesmo nome, nem o contrário.

## Swing / CFD Forex (`iqoption_swing/`)

Bot separado para operar CFD (margem/alavancagem) analisando D1 (tendência) + H4 (estrutura, Fibonacci, S/R, RSI) + H1 (confirmação do candle na direção da tendência).

```
python rodar_swing_practice.py         # modo monitor — só exibe sinais (recomendado)
python backtest_swing.py               # mede a estratégia no histórico H1
```

**⚠️ Sinal automático com edge negativo provado** (`backtest_swing.py`, cache `dados/bt_h1.pkl`, ~4 meses de H1 em 12 pares): WR 18.4% contra breakeven de 33.3% no R:R 2.0 usado (edge -14.9pp, IC95% inteiro abaixo do breakeven, -102R em 228 trades). Todos os 4 setups (`pullback_tendencia`, `sr_rejeicao`, `breakout_reteste`, `divergencia_rsi_h4`) são negativos ou neutros — nenhum sobrevive.

Use `rodar_swing_practice.py` apenas como **apoio visual** (tendência multi-timeframe, zonas de Fibonacci, S/R) para decisão manual — não siga o card de entrada/SL/TP que ele imprime. `rodar_swing_exec_practice.py` (execução automática) nunca chegou a enviar uma ordem: a compra de CFD via `buy_order`/`get_instruments("forex")` trava com timeout na lib `iqoptionapi`, que parece não suportar bem o produto CFD atual da IQ.

O laboratório antigo de simulação pura (`rodar_forex_simulacao.py`) continua disponível e não chama `buy_order`.

## Problemas comuns

- `mercado=fechado`: o ativo normal não está negociando; escolha uma aba OTC quando ela estiver disponível.
- `payout=indisponível`: nenhuma ordem será enviada até a IQ confirmar o payout.
- O gráfico não abre: copie no navegador o endereço mostrado depois de `Gráfico M5 aberto:`.
- Uma aba fica momentaneamente atrasada: o painel mantém o último candle válido e tenta novamente sem apagar o ativo.
- `operacao_pendente_banco`: abra novamente pelo iniciador correspondente. A ferramenta procura o resultado no histórico. Se a corretora não responder, a operação fica como `resultado_desconhecido`: o risco reserva a perda máxima, mas lucro e win rate não inventam uma perda confirmada.
- Resultado gravado antes da expiração real: a apuração só consulta a IQ **depois** que a opção expira (nunca antes) — se ainda assim um resultado parecer errado, rode `python reapurar_resultados.py --tf m15 h1 --conta PRACTICE --dry-run` pra conferir contra o histórico real da corretora.
- Para encerrar: pressione `Ctrl + C` no terminal.

## Arquivos principais

- `rodar_iqoption_m5.py`: inicia a ferramenta (M5/M15/H1/M1, via flags).
- `backtest_m15.py`: mede os setups M15/H1 no histórico (`--m1-minutos 8` remove o viés de lookahead das reversões).
- `reapurar_resultados.py`: corrige win/loss gravados errado contra o histórico real da IQ.
- `rodar_swing_practice.py` / `rodar_swing_exec_practice.py`: bot swing CFD (monitor / execução).
- `backtest_swing.py`: mede a estratégia swing no histórico H1.
- `rodar_forex_simulacao.py`: laboratório Forex/CFD antigo, só simulação.
- `iqoption_m5\config.py`: ativos, estratégias, limites e perfis (M5/M15/H1/M1).
- `iqoption_m5\estrategia.py`: cálculo dos sinais das opções binárias.
- `iqoption_m5\executor.py`: envio de ordens, sizing por setup, retry.
- `iqoption_m5\mercado_iq.py`: conexão, candles e apuração de resultado na IQ.
- `iqoption_m5\risco.py`: bloqueios, limite diário e concentração de direção.
- `iqoption_m5\grafico.py`: painel local (M5/M15/H1).
- `iqoption_m5\registro.py`: persistência SQLite das operações.
- `iqoption_m5\alerta.py`: alertas explicados, sem enviar ordem.
- `iqoption_m5\noticias.py`: calendário econômico do dia.
- `iqoption_swing\`: estratégia, executor e mercado do bot swing CFD.
- `DOCUMENTACAO_TECNICA.md`: arquitetura e manutenção.
