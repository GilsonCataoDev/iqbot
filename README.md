# IQ Option M5

Monitor automático para conta de treinamento (`PRACTICE`) da IQ Option. Analisa candles de 5 minutos, abre um gráfico local no navegador, marca possíveis entradas e pode executar no máximo cinco operações por dia.

## Começar em 3 passos

1. Execute `INSTALAR_DEPENDENCIAS.bat` uma vez.
2. Execute `INICIAR_IQ_M5.bat`.
3. Informe o e-mail e a senha da IQ Option no terminal.

## M5 ou M1

`INICIAR_IQ_M5.bat` usa candles de 5 minutos com expiração de 5 minutos. `INICIAR_IQ_M1.bat` usa candles de 1 minuto com expiração de 1 minuto; pela linha de comando é `python rodar_iqoption_m5.py --m1`.

No M1 a janela de entrada cai de 15 para 5 segundos, porque 15 segundos seriam um quarto de uma vela de um minuto.

Rode um de cada vez. O limite diário de operações é controlado dentro do processo, então dois monitores abertos poderiam enviar mais ordens do que o combinado.

**A triagem walk-forward mediu apenas M5.** Os números daquele estudo não valem para M1: a vela de um minuto tem mais ruído e o spread pesa proporcionalmente muito mais. Trate M1 como coleta nova, do zero.

O navegador abrirá automaticamente. A senha não fica gravada no projeto e não aparece enquanto é digitada.

## Ativos acompanhados

- Mercado normal: `EURUSD`, `GBPUSD` e `USDJPY`.
- OTC: `EURUSD-OTC`, `GBPUSD-OTC` e `USDJPY-OTC`.

O limite de cinco operações é compartilhado entre todos os ativos.

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

## Estratégias de entrada

1. `reversao_bollinger_rsi`: retorno para dentro da Banda de Bollinger com confirmação do RSI, somente em mercado lateral.
2. `pullback`: recuo a favor da tendência que toca Fibonacci de 38,2% a 61,8% e/ou um pivô de suporte/resistência, seguido por candle de confirmação e RSI entre 35 e 65.

No gráfico, `PULLBACK FIBO+SUPORTE` ou `PULLBACK FIBO+RESISTÊNCIA` indica confluência. O relatório separa os resultados das duas estratégias.

## Proteções

- Somente conta `PRACTICE`.
- Valor de 1 unidade por ordem.
- Payout mínimo de 80%.
- Entrada somente no começo de um novo candle M5.
- Uma operação aberta por vez.
- Máximo de cinco operações por dia.
- As cinco operações podem ser concluídas mesmo após perdas. Os bloqueios por perdas consecutivas e prejuízo diário ficam desativados por padrão.

Essas regras reduzem risco, mas não garantem lucro. OTC e mercado normal têm comportamentos diferentes; acompanhe os resultados separadamente antes de considerar qualquer mudança.

## Resultados e testes

- Execute `VER_RESULTADOS_M5.bat` para consultar o histórico.
- Execute `TESTAR_IQ_M5.bat` para verificar o projeto sem enviar ordens reais.
- O banco fica em `iqoption_m5\dados\iqoption_m5.sqlite3`.

## Investigação de estratégias alternativas

- Execute `python investigar_estrategias_m5.py` para repetir a triagem local sem conectar à IQ e sem enviar ordens.
- O experimento compara impulso, reversão curta, rompimentos e bounce em níveis, com expirações de 5, 15 e 30 minutos.
- A melhor hipótese atual foi reversão de um candle extremo no `GBPUSD` normal com expiração de 30 minutos, mas ainda não passou pelo intervalo simultâneo exigido para ser considerada viável.
- Consulte `RELATORIO_ESTRATEGIAS_M5.md` para os números e `PESQUISA_ESTRATEGIAS_VIAVEIS.md` para a evidência primária.

## Backtest

`RODAR_BACKTEST_M5.bat` mede a estratégia contra o histórico da IQ Option sem enviar nenhuma ordem. Ele usa a mesma `EstrategiaReversaoM5` do robô, então o que aparece no relatório é a lógica que roda ao vivo.

O relatório traz a taxa de acerto com intervalo de confiança de 95% comparada ao breakeven do payout, separada por ativo, estratégia, direção, fatores de confluência e hora do dia. A linha final aponta as horas que ficaram abaixo do breakeven — candidatas a bloqueio.

Opções úteis:

```
python rodar_backtest_m5.py --candles 20000     histórico maior
python rodar_backtest_m5.py --offline           usa o cache, não baixa nada
python rodar_backtest_m5.py --ativos EURUSD-OTC  um ativo só
python rodar_backtest_m5.py --payout 0.92       ajusta o breakeven
python rodar_backtest_m5.py --offline --validar  valida os filtros fora da amostra
```

`--validar` divide o histórico em treino (70% inicial) e teste (30% final), escolhe os filtros olhando somente o treino e mede o resultado no período escondido. Serve para separar vantagem real de coincidência: um filtro escolhido depois de ver todos os dados quase sempre parece bom no próprio período em que foi escolhido.

O histórico baixado fica em `iqoption_m5\dados\historico\`, então depois da primeira execução o `--offline` permite testar ideias sem esperar download.

Cada linha do relatório vale para o ativo daquela linha. Os ativos `-OTC` têm preço sintético gerado pela corretora: o resultado de um `-OTC` não vale para o par real de mesmo nome, nem o contrário.

## Problemas comuns

- `mercado=fechado`: o ativo normal não está negociando; escolha uma aba OTC quando ela estiver disponível.
- `payout=indisponível`: nenhuma ordem será enviada até a IQ confirmar o payout.
- O gráfico não abre: copie no navegador o endereço mostrado depois de `Gráfico M5 aberto:`.
- Uma aba fica momentaneamente atrasada: o painel mantém o último candle válido e tenta novamente sem apagar o ativo.
- `operacao_pendente_banco`: abra novamente pelo `INICIAR_IQ_M5.bat`. Após o login, a ferramenta procura a ordem no histórico sem usar a consulta antiga que pode travar. Se uma ordem vencida há mais de 10 minutos não aparecer no histórico, ela é registrada conservadoramente como perda técnica para liberar o monitor sem criar uma operação duplicada.
- Para encerrar: pressione `Ctrl + C` no terminal.

## Arquivos principais

- `rodar_iqoption_m5.py`: inicia a ferramenta.
- `rodar_backtest_m5.py`: mede a estratégia no histórico, sem enviar ordens.
- `iqoption_m5\config.py`: ativos, estratégia e limites.
- `iqoption_m5\estrategia.py`: cálculo dos sinais.
- `iqoption_m5\backtest.py`: simulação e relatório estatístico.
- `iqoption_m5\mercado_iq.py`: conexão e candles da IQ.
- `iqoption_m5\risco.py`: bloqueios e limite diário.
- `iqoption_m5\grafico.py`: painel local.
- `iqoption_m5\alerta.py`: alertas explicados, sem enviar ordem.
- `iqoption_m5\noticias.py`: calendário econômico do dia.
- `DOCUMENTACAO_TECNICA.md`: arquitetura e manutenção.
