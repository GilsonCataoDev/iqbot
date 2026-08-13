# Estratégias de movimento de preço para M5 e payoff binário

## Resumo executivo

O melhor ponto de partida não é um padrão visual isolado. Para candles de cinco minutos e resultado direcional em horizonte fixo, as hipóteses mais defensáveis são:

1. **Rompimento de range após compressão** — maior prioridade.
2. **Continuação após impulso curto, condicionada à volatilidade e ao horário**.
3. **Reversão após movimento extremo com rejeição de preço**.
4. **Rompimento ou rejeição de máxima/mínima recente**.
5. **Sequência de retornos**, testada separadamente como continuação e reversão.

A literatura dá suporte a momentum, rompimentos e suporte/resistência em alguns mercados e períodos, mas não demonstra que esses efeitos sobrevivem automaticamente em **EUR/USD M5, no candle seguinte, com payout binário**. Candlesticks puros têm evidência conflitante e devem funcionar apenas como filtro contextual. Em OTC da IQ Option, a transferência é ainda mais fraca porque as cotações são próprias da plataforma, e não preços de uma bolsa pública.

## O que a evidência realmente diz

### 1. Momentum: plausível, mas a escala temporal não transfere diretamente

Moskowitz, Ooi e Pedersen documentaram persistência do próprio retorno em futuros e forwards de moedas, índices, commodities e títulos, sobretudo em horizontes de **1 a 12 meses**, com reversão parcial posteriormente. É evidência forte para o princípio de tendência, mas não para uma previsão de cinco minutos ([artigo original, Journal of Financial Economics](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf)).

Em FX de alta frequência, a evidência é mais localizada. Hashimoto et al. estudaram transações tick a tick de USD/JPY e EUR/USD e observaram que sequências de negócios na mesma direção tinham probabilidade superior a 0,5 de continuar; sequências mais longas, porém, também antecediam reversões maiores. O efeito aparecia nos **preços negociados**, não nas cotações, o que alerta para uma possível dependência de fluxo e microestrutura que candles OHLC não capturam ([artigo original](https://doi.org/10.1080/14697681003792237)).

Há evidência de momentum intradiário em desenhos específicos de sessão. Li, Sakkas e Urquhart encontraram resultados dentro e fora da amostra em muitos dos 16 mercados desenvolvidos analisados, mais fortes sob baixa liquidez, alta volatilidade e informação discreta ([artigo original](https://doi.org/10.1016/j.finmar.2021.100619)). Gao et al. mostraram que o retorno da primeira meia hora do SPY previa o da última meia hora ([artigo original](https://doi.org/10.1016/j.jfineco.2018.05.009)). Esses desenhos capturam horário e fluxo institucional; não equivalem a prever cada candle M5 pelo anterior. Em contraponto direto, Herberger, Horn e Oehler testaram 16 estratégias de momentum em ações alemãs com retornos M5 e agregações de até 60 minutos, sem excesso positivo de momentum e com indícios de reversão entre perdedores ([working paper original](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2807405)).

Dados minuto a minuto do mercado interbancário também mostram autocorrelação negativa de primeira ordem, entre aproximadamente -0,08 e -0,15 no estudo, consistente com reversão muito curta e possivelmente bid-ask bounce/liquidez ([Federal Reserve, estudo original](https://www.federalreserve.gov/pubs/ifdp/2009/980/ifdp980.htm)). Portanto, em M5, **continuação e reversão são hipóteses concorrentes**, e o regime deve decidir qual testar.

### 2. Rompimento, range e suporte/resistência: boa hipótese de pesquisa

Brock, Lakonishok e LeBaron encontraram diferenças significativas após regras de média móvel e rompimento de faixa no DJIA diário, usando bootstrap contra vários modelos nulos ([artigo original](https://doi.org/10.1111/j.1540-6261.1992.tb04681.x)). Isso não prova Donchian em FX M5, mas sustenta uma definição objetiva de máximas e mínimas anteriores.

Para FX especificamente, Osler encontrou evidência de que níveis de suporte e resistência fornecidos por seis firmas ajudavam a prever interrupções intradiárias de tendências, variando por moeda e firma ([Federal Reserve Bank of New York, estudo original](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf)). Em dados de ordens de um grande banco, take-profits se concentravam perto desses níveis e tendiam a interromper tendências, enquanto stop-losses podiam acelerar o movimento depois do rompimento; ordens também se agrupavam em números redondos ([Federal Reserve Bank of New York, estudo original](https://www.newyorkfed.org/research/staff_reports/sr125.html)).

Isso favorece testar **duas respostas ao mesmo nível**: rejeição quando o candle toca e volta; continuação quando fecha claramente além do nível. Escolher uma delas depois de olhar todo o histórico seria data snooping, então ambas devem ser registradas previamente como modelos separados.

### 3. Movimento extremo e reversão: promissor, mas sensível à microestrutura

A autocorrelação negativa de curtíssimo prazo encontrada no mercado interbancário dá uma base para reversão após deslocamentos anormais ([Federal Reserve](https://www.federalreserve.gov/pubs/ifdp/2009/980/ifdp980.htm)). Há também reversões intradiárias previsíveis em torno de fixings institucionais de FX, explicadas por desequilíbrios de ordens; contudo, o efeito é ligado a horários específicos e ocorre em janelas de horas, não necessariamente no candle seguinte ([Bank of Canada, working paper original](https://www.bankofcanada.ca/2021/10/staff-working-paper-2021-48/)).

Logo, uma estratégia de extremo deve normalizar o movimento pela volatilidade local, controlar hora do dia e excluir notícias/fixings na análise principal. Um candle grande sozinho pode iniciar continuação, não reversão.

### 4. Candlesticks, pin bar e engulfing: não usar isoladamente

A evidência é conflitante. Caginalp e Laurent relataram poder preditivo fora da amostra para padrões de três dias em ações do S&P 500 ([artigo original](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=932984)). Em contraste, Marshall, Young e Rose, usando bootstrap e ações do DJIA, concluíram que estratégias de candlestick não agregavam valor ([artigo original](https://doi.org/10.1016/j.jbankfin.2005.08.001)). Nenhum resultado valida diretamente pin bar ou engulfing em FX M5.

Conclusão prática: **pavio e engulfing devem confirmar localização e regime**, por exemplo rejeição de uma máxima/mínima recente após retorno extremo. Testá-los sem contexto aumenta o espaço de parâmetros e o risco de falso positivo.

### 5. Compressão e expansão: a volatilidade é previsível; a direção, não necessariamente

Andersen e Bollerslev, usando retornos de cinco minutos de DEM/USD, documentaram sazonalidade intradiária pronunciada, impacto de anúncios macroeconômicos e persistência da volatilidade ([NBER, working paper original](https://www.nber.org/papers/w5783)). Isso sustenta a hipótese de que baixa amplitude recente pode ser seguida por mudança de regime de amplitude, mas **persistência de volatilidade não determina o sinal do próximo retorno**.

A compressão deve, portanto, ser apenas condição. A direção vem do rompimento confirmado pelo fechamento, e o backtest deve comparar com o mesmo rompimento sem o filtro de compressão para medir contribuição incremental.

## Cinco regras testáveis sem lookahead

### Convenções comuns

- Candle `t` só fica disponível depois de fechado.
- Sinal é calculado no fechamento de `t`; entrada usa a primeira cotação executável depois disso.
- Expiração principal: exatamente um candle M5 depois da entrada. Testar 2 e 3 candles como hipóteses separadas, nunca selecionar pelo mesmo conjunto final.
- `r_t = ln(C_t / C_{t-1})`.
- `TR_t = max(H_t-L_t, |H_t-C_{t-1}|, |L_t-C_{t-1}|)` e `ATR20_t` usa somente candles até `t`.
- Empate deve ser registrado separadamente conforme a regra real da plataforma, sem convertê-lo silenciosamente em vitória.

### Regra A — rompimento Donchian com fechamento forte

**CALL** quando:

- `C_t > max(H_{t-1} ... H_{t-N})`;
- corpo de `t` é pelo menos `0,55 × (H_t-L_t)`;
- fechamento está nos 20% superiores do candle;
- amplitude de `t` está entre `0,8` e `2,0 × ATR20_t`.

**PUT** simétrico abaixo da mínima anterior. Grade pré-declarada: `N ∈ {6, 12, 24}`. A barra de rompimento não entra no cálculo do canal. Hipótese: stop-losses e fluxo direcional sustentam o próximo candle. Principal risco: falso rompimento e seleção excessiva de `N`.

### Regra B — compressão seguida de expansão direcional

Defina `compressao_t = mediana(TR dos últimos 4 candles) / mediana(TR dos 48 candles anteriores)`.

**CALL** quando:

- `compressao_{t-1} <= 0,60`;
- `C_t` fecha acima da máxima dos quatro candles anteriores;
- `TR_t >= 1,20 × mediana(TR dos quatro candles anteriores)`;
- fechamento está nos 25% superiores de `t`.

**PUT** simétrico. Não use Bollinger, ATR e múltiplas variantes simultaneamente no primeiro teste. Hipótese: a compressão identifica o regime e o fechamento define a direção. Controle obrigatório: Regra A com `N=4` sem filtro de compressão.

### Regra C — impulso curto com continuação

**CALL** quando:

- pelo menos três dos últimos quatro retornos são positivos;
- soma `r_{t-3}...r_t >= 1,0 × sigma48_t`, onde `sigma48` é o desvio-padrão dos retornos anteriores, anualização desnecessária;
- `C_t > C_{t-4}`;
- o movimento acumulado é menor que `2,5 × sigma48_t`, evitando perseguir extremos.

**PUT** simétrico. Hipótese: persistência de fluxo em corridas curtas. Teste contraposto obrigatório: inverter o sinal mantendo exatamente a mesma seleção de eventos. Isso mostrará se o conjunto se comporta como momentum ou exaustão.

### Regra D — extremo com rejeição (pin bar contextual)

**CALL** quando:

- retorno de `t` ou deslocamento acumulado de três candles está abaixo de `-2,0 × sigma48_t`;
- `L_t < min(L_{t-1}...L_{t-12})`;
- `C_t` volta a fechar acima dessa mínima anterior;
- pavio inferior `>= 1,5 × corpo`, e fechamento fica acima do meio do candle.

**PUT** simétrico. O pin bar não é o sinal sozinho: exige extremo normalizado, varredura de nível e retorno para dentro do range. Grade inicial: limiar de extremo `{1,5; 2,0; 2,5}`; fixe a razão do pavio para limitar mineração de parâmetros.

### Regra E — decisão após sequência de candles

Após exatamente `K` candles consecutivos da mesma cor, preveja:

- **continuação**, modelo E1;
- **reversão**, modelo E2.

Use `K ∈ {3,4,5}` e estratifique antes do teste por `|soma dos retornos| / sigma48`: moderado (`0,5–1,5`) ou extremo (`>1,5`). Não misture ticks com candles: a evidência de corridas em negócios individuais não garante o mesmo fenômeno em fechamentos M5. O objetivo é descobrir se a continuação domina em sequências moderadas e a reversão em extremas, sem escolher a narrativa depois do resultado.

## Topos e fundos: quando usar e confluências

Use topos e fundos como **localização**, não como sinal isolado. A aplicação mais defensável é esperar o preço voltar a um pivô já confirmado e então classificar o evento, antes do teste, como **rejeição** ou **rompimento**. Em FX intradiário, níveis publicados de suporte/resistência interromperam tendências mais vezes do que níveis arbitrários, mas o efeito variou por moeda e fornecedor; em dados de ordens, take-profits favoreceram reversões perto dos níveis, enquanto stop-losses intensificaram movimentos depois do rompimento ([New York Fed, estudo de suporte/resistência](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf); [New York Fed, estudo de ordens](https://www.newyorkfed.org/research/staff_reports/sr125.html)). Isso sustenta a hipótese, não garante vantagem em M5 nem em OTC.

### Confluências prioritárias e regras testáveis

1. **Estrutura confirmada:** defina tendência de alta somente quando os dois últimos fundos e os dois últimos topos *já confirmados* forem ascendentes; tendência de baixa é o inverso. Na tendência de alta, teste CALL na rejeição de fundo e CALL no rompimento de topo como modelos separados; na baixa, use a simetria para PUT. Compare obrigatoriamente com a mesma regra sem filtro de tendência para medir se a estrutura realmente acrescenta valor.
2. **Tipo de reação ao nível:** rejeição CALL quando `L_t` atravessa ou chega a até `0,15 × ATR20` de um fundo confirmado, mas `C_t` fecha novamente acima do nível, acima do meio do candle e com pavio inferior pelo menos igual ao corpo; PUT simétrico no topo. Rompimento CALL quando `C_t` fecha pelo menos `0,15 × ATR20` acima do topo, nos 25% superiores do candle e com corpo `>= 55%` da amplitude; PUT simétrico. Não transforme um modelo no outro depois de ver o resultado.
3. **Volatilidade normalizada:** use distância e tolerância em ATR, não em pips fixos. Como grade inicial, aceite o candle de sinal apenas se `TR_t / ATR20` estiver entre `0,7` e `1,8`; teste a faixa previamente registrada contra a regra sem filtro. A volatilidade FX tem forte padrão intradiário e reage a anúncios, mas volatilidade por si só não informa direção ([NBER, estudo original com retornos de cinco minutos](https://www.nber.org/papers/w5783)).
4. **Número redondo:** marque antes do teste níveis terminados em `00` e `50` na escala convencional do par e crie uma variável `perto_redondo` quando a distância do pivô for `<= 0,15 × ATR20`. Teste-a como interação, não como condição obrigatória: ordens FX se concentraram em números redondos, com padrões diferentes para stops e take-profits ([New York Fed](https://www.newyorkfed.org/research/staff_reports/sr125.html)).
5. **Horário e notícia:** estratifique por blocos horários fixados previamente e reporte cada bloco, em vez de escolher posteriormente o melhor. Como teste de robustez, exclua sinais de 5 minutos antes até 20 minutos depois de divulgações macroeconômicas relevantes: em EUR/USD e USD/JPY, anúncios dos EUA elevaram abruptamente a volatilidade, que permaneceu alta por 10 a 20 minutos ([Federal Reserve, estudo original](https://www.federalreserve.gov/pubs/ifdp/2004/823/ifdp823.htm)).

### Confirmação do pivô sem olhar o futuro

Um pivô centrado com `L` candles à esquerda e `R` à direita ocorrido no candle `i` só é conhecido no fechamento de `i+R`. Com `R=2`, portanto, o topo/fundo desenhado em `i` não podia ser usado para uma entrada em `i` ou `i+1`. A documentação oficial do Pine mostra que pivôs são detectados depois de transcorridas as barras da direita e alerta que desenhá-los retroativamente no candle original pode induzir uma leitura enganosa do histórico ([TradingView, pivôs](https://www.tradingview.com/pine-script-docs/faq/techniques/); [TradingView, repainting](https://www.tradingview.com/pine-script-docs/v5/concepts/repainting/)). O ZigZag também não deve fornecer o último topo/fundo operacional: a própria MetaQuotes informa que seu último segmento pode mudar com novos preços e o recomenda para analisar movimentos já ocorridos, não para prognóstico ([MetaQuotes](https://www.mql5.com/en/code/56)).

Regra de implementação: grave separadamente `candle_do_pivo` e `candle_da_confirmacao`; o nível só pode participar de sinais a partir da confirmação. Calcule o sinal no fechamento de `t` e simule a entrada apenas na primeira cotação executável posterior. Para payoff binário líquido `q`, aprove a estratégia somente se o limite inferior do intervalo de confiança superar `1 / (1+q)`; com payout de 85%, o break-even é `54,05%`. Valide rejeição e rompimento por ativo, mercado normal/OTC, expiração e bloco horário, mantendo um teste final intocado.

## Protocolo de validação recomendado

1. **Separação temporal:** desenvolvimento, validação e teste final em blocos cronológicos; use walk-forward com embargo de pelo menos o maior horizonte/feature para evitar sobreposição informacional.
2. **Métrica correta:** além de acurácia, calcule valor esperado por unidade apostada: `EV = p × payout - (1-p)`. Com payout líquido de 80%, o ponto de equilíbrio é `p > 1/1,8 = 55,56%`; com 70%, `58,82%`.
3. **Incerteza:** reporte número de operações, intervalo de confiança da taxa de acerto, EV, pior sequência de perdas e resultados por ativo, hora, payout e trimestre. Use bootstrap em blocos, não observações embaralhadas.
4. **Data snooping:** conte todas as variantes tentadas e aplique correção para múltiplos testes/Reality Check. Sullivan, Timmermann e White mostram por que avaliar apenas a melhor regra superestima a evidência ([working paper original](https://www.fmg.ac.uk/publications/discussion-papers/data-snooping-technical-trading-rule-performance-and-bootstrap)).
5. **Teste incremental:** compare cada filtro contra sua regra-base e contra previsão incondicional por ativo/hora. Aprovar somente se o ganho permanecer no teste final intocado e em mais de uma janela temporal.

Sugestão de critério inicial de promoção para simulação prospectiva: ao menos 500 sinais no agregado, limite inferior de 95% para acurácia acima do break-even do payout observado, EV positivo em pelo menos 3 de 4 blocos walk-forward e nenhuma dependência de um único ativo ou horário. O número 500 é uma regra operacional, não garantia estatística; o tamanho necessário deve ser calculado a partir do efeito observado e da frequência de sinais.

## FX intraday versus IQ Option OTC

Nos pares FX regulares, a microestrutura, os horários e anúncios importam. A volatilidade em cinco minutos varia fortemente ao longo do dia e em torno de notícias ([Andersen e Bollerslev](https://www.nber.org/papers/w5783)); por isso, resultados devem ser estratificados por sessão e eventos programados.

No OTC da IQ Option, trate cada ativo como **outro processo de dados**. A própria plataforma informa que as cotações OTC são exclusivas, não passam por bolsa pública e podem depender de modelos/algoritmos internos; sua descrição mais recente chama o ambiente de mercado simulado com preços não ligados a dados externos em tempo real ([IQ Option, explicação oficial](https://blog.iqoption.com/en/otc-trading-on-iq-option-how-to-trade-securities-over-the-counter/)). Portanto:

- não misture `EURUSD` e `EURUSD-OTC` no mesmo modelo ou estatística;
- não use evidência acadêmica de interbancário como validação do OTC;
- valide OTC apenas com histórico capturado da própria plataforma, incluindo mudanças de regime/versão;
- mantenha holdout prospectivo, pois um algoritmo de cotação pode mudar sem aviso observável nos candles;
- confirme que os candles usados no backtest coincidem com a cotação que decide a expiração.

## Ordem recomendada de implementação

1. Regra A, rompimento Donchian com fechamento forte.
2. Regra D, extremo com rejeição contextual.
3. Regra B, compressão-expansão, sempre comparada à Regra A curta.
4. Regra C, impulso curto, comparada à sua inversão.
5. Regra E como experimento diagnóstico; não promover sem estabilidade fora da amostra.

Engulfing puro e pin bar puro devem permanecer como controles de baixa prioridade. O objetivo não é encontrar a maior taxa histórica, mas uma diferença estável sobre o break-even do payout real depois de todas as variantes testadas.

## Fontes primárias principais

- Moskowitz, Ooi e Pedersen (2012), [Time Series Momentum](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf).
- Li, Sakkas e Urquhart (2022), [Intraday Time-Series Momentum](https://doi.org/10.1016/j.finmar.2021.100619).
- Gao, Han, Li e Zhou (2018), [Market Intraday Momentum](https://doi.org/10.1016/j.jfineco.2018.05.009).
- Herberger, Horn e Oehler (2016), [Are Intraday Reversal and Momentum Trading Strategies Feasible?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2807405).
- Osler (2000), [Support for Resistance: Technical Analysis and Intraday Exchange Rates](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf).
- Osler (2001/2003), [Currency Orders and Exchange-Rate Dynamics](https://www.newyorkfed.org/research/staff_reports/sr125.html).
- Chaboud et al. (2009), [Rise of the Machines: Algorithmic Trading in the Foreign Exchange Market](https://www.federalreserve.gov/pubs/ifdp/2009/980/ifdp980.htm).
- Andersen e Bollerslev (1998), [DM-Dollar Volatility](https://www.nber.org/papers/w5783).
- Krohn, Mueller e Whelan (2021/2024), [Foreign Exchange Fixings and Returns Around the Clock](https://www.bankofcanada.ca/2021/10/staff-working-paper-2021-48/).
- Brock, Lakonishok e LeBaron (1992), [Simple Technical Trading Rules](https://doi.org/10.1111/j.1540-6261.1992.tb04681.x).
- Caginalp e Laurent (1998), [The Predictive Power of Price Patterns](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=932984).
- Marshall, Young e Rose (2006), [Candlestick Technical Trading Strategies](https://doi.org/10.1016/j.jbankfin.2005.08.001).
- Sullivan, Timmermann e White (1998/1999), [Data Snooping, Technical Trading Rule Performance, and the Bootstrap](https://www.fmg.ac.uk/publications/discussion-papers/data-snooping-technical-trading-rule-performance-and-bootstrap).

---

**Uso pretendido:** pesquisa e validação, não recomendação de investimento. Payoffs binários tornam uma pequena vantagem estatística insuficiente quando fica abaixo do break-even do payout, e perdas podem consumir integralmente o valor apostado.

## Diagnóstico binário: correção + Fibonacci + S/R

A família `correcao_fibo_sr_binaria` reutiliza o detector Forex, mas resolve o
resultado pela abertura do candle seguinte e fechamento após 1, 3 ou 6 candles.
O target estrutural não fecha a opção; por isso o retorno/risco não é filtro da
entrada binária. A triagem walk-forward não selecionou nenhum parâmetro porque
nenhum atingiu 100 ocorrências no treino.

Na regra-base congelada, apenas como diagnóstico descritivo, USDJPY teve 9–10
sinais (66,67% em 1 candle; 70,00% em 3) e EURUSD-OTC teve 9 sinais (88,89% em
1 candle; 77,78% em 3). As demais combinações foram mistas. Amostras de 7–10
operações são insuficientes e não autorizam PRACTICE; é necessário coletar mais
histórico sem alterar os parâmetros após observar esses resultados.
