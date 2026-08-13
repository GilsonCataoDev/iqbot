# Pesquisa — executor Forex/CFD intraday na IQ Option

Data da pesquisa: 13 de agosto de 2026.

## Recomendação objetiva

Não existe uma “melhor estratégia” demonstrada para todo ativo e período. A hipótese com melhor fundamento para começar o novo validador é **rompimento de nível relevante, seguido de pullback, na direção do regime**, com risco e saídas definidos por ATR. Ela é simples, não depende de indicador proprietário e permite medir separadamente sinal, execução e custos.

O motivo não é que um padrão gráfico isolado tenha lucro garantido. Dados primários do mercado cambial mostram que:

- ordens stop se concentram perto de números redondos e podem produzir movimentos rápidos e autorreforçados depois do rompimento; o efeito encontrado durou horas, não dias ([New York Fed, Staff Report 150](https://www.newyorkfed.org/research/staff_reports/sr150.html));
- take-profits tendem a favorecer rejeições antes do nível, enquanto stops tendem a intensificar a tendência depois de cruzá-lo ([New York Fed, Staff Report 125](https://www.newyorkfed.org/research/staff_reports/sr125.html));
- fluxo de ordens e retorno têm associação forte em frequências curtas, mas essa associação enfraquece em horizontes longos ([Federal Reserve, IFDP 830](https://www.federalreserve.gov/econres/ifdp/order-flow-and-exchange-rate-dynamics-in-electronic-brokerage-system-data.htm));
- a evidência histórica para regras técnicas varia por amostra e perde força em alguns períodos. Portanto, a regra abaixo é uma **hipótese a validar**, não uma promessa ([NBER Working Paper 3818](https://www.nber.org/papers/w3818)).

## Estratégia candidata: `breakout_pullback_atr`

### Universo e horários

- Começar somente com `EURUSD`, `USDJPY` e `GBPUSD`, cada um avaliado separadamente.
- Usar M15 para regime e M5 para gatilho e execução.
- Testar prioritariamente a janela líquida de **03:00–11:00 no horário de Nova York**. A literatura baseada em EBS identifica essa faixa entre as mais ativas, e a sobreposição Londres–Nova York costuma concentrar liquidez ([Federal Reserve, IFDP 862](https://www.federalreserve.gov/pubs/ifdp/2006/862/ifdp862.htm), [BIS Quarterly Review](https://www.bis.org/publ/qtrpdf/r_qt1312e.htm)).
- Armazenar tudo em UTC e converter com fuso `America/New_York`; não fixar um deslocamento porque existe horário de verão.
- Não misturar Forex normal com OTC ou opções. O instrumento pesquisado é o **CFD Forex Margin**.

### Definições sem lookahead

Todos os cálculos abaixo usam apenas candles já fechados. `ATR` significa ATR(14) de Wilder calculado até o candle anterior ao sinal.

**Regime M15 de compra:**

1. fechamento acima da média dos extremos do canal de 20 candles anteriores;
2. máxima do canal de 20 candles maior que a máxima do bloco de 20 candles imediatamente anterior;
3. mínima do canal de 20 candles maior que a mínima do bloco anterior.

Venda é o inverso. Essa definição objetiva representa topos e fundos ascendentes/descendentes sem ZigZag redesenhável.

**Rompimento M5 de compra:**

1. nível = máxima dos 12 candles M5 anteriores, excluindo o candle atual;
2. fechamento acima do nível por pelo menos `max(0,10 × ATR, 2 × spread)`;
3. corpo do candle entre `0,60 × ATR` e `2,00 × ATR`;
4. fechamento situado nos 25% superiores do range do candle.

Venda é o inverso usando a mínima anterior. O limite de 2 ATR evita perseguir um movimento já excessivamente esticado.

**Pullback e entrada:**

- Esperar de 1 a 6 candles M5 após o rompimento.
- Compra: o preço volta à zona `nível ± 0,20 × ATR`, mas fecha novamente acima do nível e acima da metade do próprio candle. Venda é o inverso.
- Cancelar se houver fechamento do outro lado do nível maior que `0,20 × ATR`, se passarem 6 candles ou se surgir notícia bloqueada.
- Entrar apenas na abertura do candle seguinte: compra no **Ask**, venda no **Bid**. Nunca usar o fechamento final do candle e simular entrada nesse mesmo preço.

### Stop, alvo e trailing

- **Stop inicial:** além do extremo do pullback, com folga de `0,10 × ATR`.
- Rejeitar a operação se a distância do stop for menor que `0,60 × ATR` ou maior que `1,80 × ATR`.
- **Risco inicial para PRACTICE:** 0,25% da equity por operação; risco simultâneo máximo de 0,50%; perda diária máxima de 1,00%.
- **Alvo inicial:** `2R`, onde `R` é a perda monetária entre entrada e stop, já incluindo spread e slippage estimado.
- Ao alcançar `+1R`, mover o stop para entrada **mais os custos reais**.
- Após `+1,5R`, testar trailing próprio de `1,5 × ATR` a partir do maior fechamento favorável; o stop nunca pode afrouxar.
- **Time stop:** encerrar depois de 12 candles M5 (60 minutos) se nem stop nem alvo forem atingidos.

O trailing deve ser uma variante experimental separada. A própria IQ Option avisa que o trailing nativo pode não estar disponível para contas Margin ([IQ Option — Trailing Stop](https://blog.iqoption.com/en/the-end-when-to-exit-a-trade/)). O executor só deve fazer trailing por alterações de stop se a interface usada confirmar suporte; caso contrário, mantenha stop e alvo fixos.

## Notícias, spread e execução

- Versão inicial: bloquear novas entradas de 15 minutos antes até 30 minutos depois de eventos de alto impacto da moeda. Testar também janela pós-notícia de 60 minutos.
- Estudos com dados transacionáveis mostram salto imediato de volatilidade e volume após divulgações; em vários eventos, o volume ficou elevado por cerca de uma hora, e payroll/GDP tiveram efeitos especialmente grandes ([Federal Reserve, IFDP 823](https://www.federalreserve.gov/Pubs/ifdp/2004/823/ifdp823.htm)).
- Bloquear entrada quando `spread > 20%` da distância até o stop ou acima do percentil 90 do spread daquele par e horário, calculado apenas com histórico anterior.
- O backtest precisa usar Bid/Ask históricos. Compra abre no Ask e fecha no Bid; venda abre no Bid e fecha no Ask.
- Acrescentar cenário de slippage adverso, inclusive `0`, `0,25 × spread` e `0,50 × spread` por execução.
- Se stop e alvo forem tocados no mesmo candle e não houver ticks, contabilizar **stop primeiro**. É a hipótese conservadora.

A política oficial informa que o spread pode ser dinâmico conforme liquidez, volatilidade, horário e tamanho; em condições rápidas, a ordem pode ser executada no primeiro preço disponível, não no solicitado ([IQ Option — General Fees](https://files.iqoption.com/storage/public/5b/6d/6c6e43a9f/general_fees.pdf), [IQ Option — Order Execution Policy](https://files.iqoption.com/storage/public/5b/97/8237ee271/order_execution_policy.pdf)). Esses custos não podem ser substituídos por um spread médio único.

## Validação que decide se a estratégia merece PRACTICE

1. Congelar previamente os parâmetros acima e manter as variações como estratégias separadas: entrada imediata, pullback, alvo fixo e trailing.
2. Usar walk-forward cronológico; sugestão inicial: 6 meses para calibração e 1 mês totalmente fora da amostra, rolando a janela. Se não houver histórico suficiente, coletar mais dados em vez de usar validação aleatória.
3. Aplicar purge/embargo de pelo menos 12 candles M5 entre treino e teste, pois uma posição pode durar esse horizonte.
4. Comparar com baselines: sempre fora do mercado, rompimento imediato sem filtro e direção aleatória com os mesmos horários/stops. Corrigir a seleção múltipla de pares, parâmetros e variantes.
5. Promover apenas se houver expectativa líquida positiva fora da amostra, pelo menos 300 operações de teste agregadas, resultado positivo na maioria dos folds, drawdown aceitável e intervalo de confiança da expectativa acima de zero. Esses limites são critérios de engenharia e devem ser revisados antes do teste, nunca depois de ver o lucro.

Métricas principais: expectativa líquida em `R/operação`, profit factor, drawdown máximo, MAE/MFE, taxa de execução, slippage, spread/R, tempo em posição e desempenho por par, sessão e regime. Win rate isolado não decide a promoção.

## Como Forex Margin funciona na IQ Option

- Na IQ Option, Forex é negociado como **CFD sobre margem**: há especulação na variação sem propriedade da moeda subjacente ([página oficial de Forex](https://iqoption.com/en/forex), [KID do CFD FX](https://files.iqoption.com/storage/public/5a/c3/aaccc7b86/key_information_document_fx_cfd.pdf)).
- A quantidade é dada em lotes. A documentação informa `1 lote = 100.000` unidades da moeda-base e exemplos de mini, micro e nano-lote. A interface documentada aceita quantidade a partir da faixa de `0,001` lote ([IQ Option — Forex on Margin](https://blog.iqoption.com/en/margin-trading-how-does-it-work/)).
- A margem é calculada pela plataforma. A fórmula publicada é `margem = lotes × 100.000 / alavancagem`. Alavancagem aumenta ganhos **e perdas** sobre a exposição total.
- Material oficial mais recente diz que a alavancagem é definida pelo ativo, região e condições de negociação; deve ser lida no painel **Info → Trading Conditions**, não codificada como constante ([IQ Option — Leverage](https://blog.iqoption.com/en/what-is-leverage-in-trading/)).
- Stop-loss e take-profit do Margin são definidos em pips relativos ao Bid/Ask de abertura e podem ser alterados durante a posição. A mesma documentação informa bloqueio de novas posições em nível de margem de 100% e fechamento automático em 50% ([IQ Option — Forex on Margin](https://blog.iqoption.com/en/margin-trading-how-does-it-work/)).
- A oferta muda por país, entidade, ativo e conta. O executor deve primeiro confirmar que a conta PRACTICE mostra a aba **Margin**, as condições do par e as operações necessárias de abrir, consultar, alterar SL/TP e fechar. Não inferir disponibilidade apenas porque opções binárias aparecem.

## Decisão de implementação

Criar um executor independente do executor M5 de opções, inicialmente em modo **observação/PRACTICE sem ordens**. A primeira entrega deve somente:

1. descobrir ativos Forex Margin realmente disponíveis na conta;
2. registrar Bid, Ask, spread, candles e condições do instrumento;
3. gerar e persistir sinais da regra `breakout_pullback_atr`;
4. simular tamanho, SL, TP, trailing e P/L usando preços executáveis;
5. comprovar num ambiente de teste que abrir, consultar, modificar e fechar uma posição Margin são suportados antes de habilitar qualquer envio.

Não reutilizar o comando de opção com `expiração=5min`: Forex Margin mantém uma posição e exige ciclo de vida próprio. A recomendação permanece **sem ordens reais** até o walk-forward líquido passar e o executor completar testes em PRACTICE.

## Experimento adicional: toque em LTA/LTB com alvo estrutural

Foi adicionada a hipótese `toque_lta_ltb`: dois fundos confirmados ascendentes formam
uma LTA e dois topos confirmados descendentes formam uma LTB. O toque exige rejeição,
corpo mínimo de 0,30 ATR, linha ainda respeitada, regime M15 alinhado e uma única entrada
por linha. O alvo é o pivô oposto formado entre os dois pontos da linha; operações com
retorno/risco inferior a 1,20 são descartadas.

No histórico local disponível, depois de spread e risco de 0,25% por posição, a regra
permaneceu negativa: EURUSD 29 operações/20,69% de acerto; GBPUSD 21/23,81%; USDJPY
32/25,00%. A amostra é pequena e não autoriza envio PRACTICE. O próximo teste válido é
walk-forward com período final intocado, sem afrouxar parâmetros depois de ver o resultado.

## Experimento adicional: correção + Fibonacci + S/R

A candidata `correcao_fibo_sr` espera um impulso de pelo menos 2 ATR alinhado ao
regime M15, seguido de correção na faixa de 50%–61,8%. A entrada exige que um pivô
anterior de suporte/resistência coincida com a zona, candle de rejeição com corpo
mínimo de 0,30 ATR e espaço até o extremo do impulso para pelo menos 1,20R.

No histórico local, a regra ficou próxima do capital inicial, mas gerou somente 5
operações no EURUSD, 4 no GBPUSD e 7 no USDJPY. Os acertos foram, respectivamente,
20,00%, 25,00% e 14,29%. Essa quantidade não permite conclusão: o resultado significa
apenas que a confluência está muito seletiva. Os parâmetros permanecem congelados até
existir mais histórico; afrouxá-los agora para aumentar a amostra seria data snooping.
