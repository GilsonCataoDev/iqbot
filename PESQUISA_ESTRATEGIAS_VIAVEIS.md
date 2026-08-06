# Pesquisa de estratégias intradiárias viáveis para M5 na IQ Option

**Data da pesquisa:** 2 de agosto de 2026  
**Escopo:** sinais em EUR/USD e USD/JPY, candles M5, opções de retorno fixo; mercado normal e OTC avaliados separadamente.

## Resposta curta

Não encontrei uma estratégia pública com evidência confiável de lucro fora da amostra em **opções binárias M5**. Há, porém, três hipóteses com fundamento empírico suficiente para uma investigação nova e pequena:

1. **Reversão curta** depois de um deslocamento anormal.
2. **Bounce em suporte/resistência definido antes da operação**, com prioridade para números redondos.
3. **Continuação depois do rompimento confirmado desse mesmo nível**.

Volatilidade, sessão e notícias devem ser tratados primeiro como **filtros de risco/regime**, não como previsão de CALL ou PUT. Momentum de prazo longo tem evidência em FX, mas isso não valida momentum M5. Fibonacci, isoladamente, não tem evidência primária robusta localizada para esse horizonte.

A linha mais defensável e barata é testar essas três hipóteses em **EURUSD e USDJPY normais, de segunda a sexta, na conta PRACTICE**. OTC deve ser outro experimento, com outra base e outra conclusão.

## O que a evidência realmente permite afirmar

| Família | Evidência primária relevante | O que ela não prova | Prioridade |
|---|---|---|---:|
| Reversão curta | Em EUR/USD, USD/JPY e EUR/JPY interdealer, retornos de um minuto tiveram primeiro lag negativo, entre -0,08 e -0,15. Só variáveis defasadas explicaram cerca de 1% a 3% da variação do retorno. [Federal Reserve — *Rise of the Machines*](https://www.federalreserve.gov/pubs/ifdp/2009/980/ifdp980.htm) | Não demonstra lucro, acerto acima do breakeven binário nem persistência em candles da IQ. | 1 |
| Bounce em suporte/resistência | Níveis publicados por seis firmas ajudaram a prever interrupções intradiárias, mas o poder variou por par e firma. O estudo mediu bounce após 15 e 30 minutos. [Federal Reserve Bank of New York — *Support for Resistance*](https://www.newyorkfed.org/research/epr/00v06n2/0007osle.html) | Não testou a nossa forma automática de criar níveis, não provou lucro líquido e não foi um teste de opção binária M5. | 1 |
| Rompimento/continuação | Ordens de um grande banco se concentravam em números redondos; take-profits favoreciam reversão perto de níveis, enquanto stop-losses intensificavam o movimento depois do rompimento. [FRBNY — *Currency Orders and Exchange-Rate Dynamics*](https://www.newyorkfed.org/research/staff_reports/sr125.html) | É explicação microestrutural, não receita pronta nem validação fora da amostra na IQ. | 1 |
| Momentum/tendência | Regras intradiárias escolhidas em treino mostraram previsibilidade fora da amostra quando custo era zero, mas não produziram excesso de retorno com custos e horários realistas. [St. Louis Fed — Neely e Weller](https://fraser.stlouisfed.org/files/docs/publications/frbsl_wp/1999-016.pdf) | Evidência diária ou de futuros em horizontes longos não transfere automaticamente para uma decisão de cinco minutos. | 2 |
| Volatilidade | Retornos cambiais de cinco minutos exibem forte periodicidade intradiária, efeito de anúncios e persistência de volatilidade. [NBER — Andersen e Bollerslev](https://www.nber.org/papers/w5783) | Prever o tamanho do próximo movimento não prevê sua direção. | Filtro |
| Sessão/horário | EUR/USD e USD/JPY têm mais volume entre aproximadamente 8h e 12h de Nova York; volume e volatilidade mudam ao longo do dia. [Federal Reserve — dados EBS](https://www.federalreserve.gov/pubs/ifdp/2004/823/ifdp823.htm) | Uma “hora boa” para liquidez não é necessariamente uma hora com taxa de acerto direcional maior. | Filtro |
| Notícias | Anúncios geram saltos de preço e picos de volatilidade; o componente surpresa costuma ser incorporado em poucos minutos, enquanto volume/volatilidade podem permanecer elevados. [Federal Reserve — dados EBS](https://www.federalreserve.gov/pubs/ifdp/2004/823/ifdp823.htm), [Federal Reserve — mercados globais](https://www.federalreserve.gov/pubs/ifdp/2006/871/default.htm) | Saber o horário sem conhecer a surpresa não entrega antecipadamente a direção. | Evitar primeiro |

### Leitura honesta

Os resultados acima justificam **experimentos**, não dinheiro real. O estudo intradiário mais diretamente relacionado encontrou justamente que previsibilidade estatística podia existir sem lucro depois de fricções. Esse é o risco central: um efeito pequeno em FX spot pode ser real e ainda ficar abaixo do acerto exigido pelo payout fixo.

Suporte/resistência merece novo teste, mas com uma mudança importante em relação ao projeto anterior: **bounce e breakout são regimes opostos**. Somá-los como “confluências” dilui a hipótese. Primeiro é preciso decidir, por regras observáveis antes da entrada, se houve rejeição do nível ou rompimento confirmado.

## Matemática do payout fixo

A CFTC descreve a opção binária como um resultado tudo-ou-nada: o acerto paga um valor fixo ou percentual anunciado; o erro normalmente perde todo o valor aplicado. [CFTC — alerta sobre opções binárias fora de bolsa](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/beware_of_off_exchange_binary_options.htm)

Para uma aposta de 1 unidade, payout líquido `b` e probabilidade de acerto `p`:

```text
EV = p × b − (1 − p)
EV = p × (1 + b) − 1
breakeven: p* = 1 / (1 + b)
```

| Payout líquido | Acerto mínimo sem margem de segurança |
|---:|---:|
| 70% | 58,82% |
| 80% | 55,56% |
| 82% | 54,95% |
| 85% | 54,05% |

Exemplo: com payout de 82%, 53,8% de acerto tem expectativa de aproximadamente **-2,08% por unidade apostada**:

```text
0,538 × 0,82 − 0,462 = -0,02084
```

Quando o payout muda a cada ativo e instante, não existe um único breakeven calculado pela média simples. O backtest deve usar, operação por operação:

```text
PnL_i = acerto_i × payout_i − erro_i
EV estimado = média(PnL_i)
```

Empate/reembolso vale zero e deve ser armazenado separadamente. Operações rejeitadas, payout indisponível, latência e diferença entre o preço visto e o preço aceito também devem ficar no histórico. **Taxa de acerto sozinha não é métrica suficiente.**

Martingale não altera esse EV negativo; só concentra perdas raras em valores maiores.

## Por que spot não transfere diretamente para binárias

No spot, o resultado depende da amplitude favorável e desfavorável, stop, alvo, spread e duração. Uma estratégia pode lucrar acertando menos de 50% se o ganho médio superar a perda média. Na binária, uma vitória por uma fração mínima recebe o mesmo payout que uma grande alta, e um erro pequeno perde a unidade inteira. Portanto:

- evidência de retorno positivo no spot não implica acerto acima de `1/(1+payout)`;
- previsão de volatilidade não fornece direção;
- uma regra boa em 15 ou 30 minutos pode não funcionar com expiração de 5 minutos;
- preço interdealer, preço da corretora e preço de liquidação podem não coincidir.

## Mercado normal e OTC não podem ser misturados

A própria IQ Option informa que o OTC de fim de semana é um **mercado simulado**, negociado entre cliente e plataforma, com preços criados por algoritmos proprietários, padrões históricos, simulação interna de liquidez e ruído estatístico. Também reconhece que estratégias do mercado comum podem não se comportar da mesma forma no OTC. [IQ Option — metodologia oficial do OTC](https://blog.iqoption.com/pt/negociacao-otc-na-iq-option-como-negociar-titulos-no-mercado-de-venda-livre/)

Consequências:

1. Papers sobre EUR/USD real não validam `EURUSD-OTC`.
2. Notícias econômicas do mundo real não devem ser presumidas como causa dos candles OTC.
3. Uma estratégia OTC só pode ser avaliada com candles, payouts e resultados da própria IQ.
4. Treino, teste, métricas e conclusões devem ser separados entre normal e OTC.
5. Se o algoritmo gerador mudar, um padrão histórico OTC pode desaparecer sem aviso observável nos mercados externos.

A política prudente é pesquisar primeiro o mercado normal. OTC pode continuar disponível apenas como uma trilha experimental isolada, nunca como extensão automática do resultado em FX real.

## Experimento recomendado

### Universo congelado

- Ativos: `EURUSD` e `USDJPY` normais.
- Timeframe de formação: M5.
- Expirações candidatas: 1, 3 e 6 candles; cada uma conta como variante testada.
- Em candles M5 isso corresponde a 5, 15 e 30 minutos; a própria IQ informa que opções binárias podem durar de 1 minuto a 1 mês e apresenta 30 minutos como exemplo operacional. [Guia oficial da IQ Option](https://blog.iqoption.com/pt/como-negociar-opcoes-binarias-um-guia-de-a-a-z-da-iq-option/)
- Sem fim de semana; feriados e períodos sem payout ficam fora por regra operacional, não depois de ver o resultado.
- Payout mínimo deve ser definido antes do teste e o payout real deve entrar no PnL.

### Três famílias, sem misturá-las

#### A. Reversão curta

Hipótese: após um retorno M5 excepcional em relação à volatilidade recente, o próximo retorno tende parcialmente a reverter.

Definição a congelar antes do teste:

- retorno do candle anterior padronizado por uma medida de volatilidade calculada só com passado;
- entrada contrária apenas depois do fechamento do candle de choque;
- sem RSI, Fibonacci ou horário escolhido posteriormente;
- poucos limiares pré-registrados, todos contabilizados como testes.

#### B. Bounce em nível

Hipótese: o preço toca um nível conhecido previamente e fecha de volta para o lado de origem.

Níveis candidatos mais coerentes com a evidência:

- números redondos definidos por uma grade fixa do par;
- máxima/mínima do dia anterior;
- máxima/mínima de uma janela anterior encerrada antes do sinal.

O nível não pode ser reposicionado depois do toque. A entrada acontece no candle seguinte à rejeição; testar 15 minutos é especialmente relevante porque a evidência de Osler avaliou reversões em 15 e 30 minutos.

#### C. Rompimento e continuação

Hipótese: após fechar além de um nível predefinido, com deslocamento suficiente em relação à volatilidade passada, o preço continua na direção do rompimento.

O mesmo evento não pode virar bounce ou breakout conforme o resultado futuro. A classificação deve estar completa no instante da entrada.

### Filtros predefinidos

- **Volatilidade:** excluir extremos ou dividir resultados por regime; não otimizar o percentil no teste final.
- **Sessão:** usar uma janela líquida fixada por justificativa de mercado, não “as melhores horas” encontradas no histórico.
- **Notícias:** começar sem entradas cujo período de formação ou expiração atravesse uma divulgação macro relevante. O estudo da Fed mostra saltos imediatos e volatilidade elevada após divulgações. A janela exata, por exemplo `-20/+20 minutos`, é uma hipótese e também deve ser congelada.

Calendários oficiais gratuitos podem cobrir os eventos principais: [Federal Reserve/FOMC](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm), [BLS](https://www.bls.gov/schedule/), [BEA](https://www.bea.gov/news/schedule), [ECB](https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html) e [Eurostat](https://ec.europa.eu/eurostat/news/release-calendar). Para simples **evitação**, não é necessário comprar previsão de consenso.

## Validação que reduz autoengano

### 1. Separação cronológica

Usar o começo dos dados para desenvolvimento, blocos posteriores para walk-forward e preservar o último bloco como teste final intocado. Nada do teste final pode escolher ativo, hora, nível, indicador, expiração ou payout mínimo.

### 2. Walk-forward de verdade

Em cada etapa, parâmetros usam apenas o passado e são aplicados no bloco seguinte. Se sinais ou expirações se sobrepõem, a validação precisa agrupar/purgar observações próximas para evitar que praticamente o mesmo movimento apareça no treino e no teste.

### 3. Registrar todos os testes

O melhor entre muitas combinações tende a parecer bom por acaso. O *Reality Check* foi criado para testar se o melhor modelo encontrado numa busca supera um benchmark depois de considerar data snooping. [White, *Econometrica*](https://doi.org/10.1111/1468-0262.00152) A probabilidade de backtest overfitting também pode ser estimada com validação combinatória. [Bailey et al. — paper original](https://escholarship.org/uc/item/4w1110bb)

### 4. Incerteza correta

Para acerto, usar intervalo de Wilson em vez do intervalo normal ingênuo, especialmente com amostras pequenas. [NIST — intervalos binomiais](https://itl.nist.gov/div898/handbook/prc/section2/prc241.htm) Como operações do mesmo dia/regime não são independentes, o principal intervalo do **PnL médio** deve ser obtido por bootstrap em blocos de dia ou semana.

Exemplo de ordem de grandeza, não meta garantida: com payout 82%, o breakeven é 54,95%. Para detectar uma taxa verdadeira de 57% contra esse limite, com teste unilateral de 5% e poder de 80%, a aproximação binomial pede cerca de **3.600 operações independentes**. Dependência temporal e múltiplas variantes exigem mais. Por isso, algumas centenas de operações em PRACTICE podem derrubar uma estratégia ruim, mas raramente comprovam uma vantagem pequena.

### 5. Critério de avanço congelado

Uma candidata só avança para observação prospectiva se cumprir todos:

- PnL positivo usando payout histórico operação por operação;
- limite inferior ajustado de 95% do PnL médio acima de zero no teste final;
- resultado não concentrado em um único mês, ativo ou sessão;
- estabilidade em vários blocos walk-forward;
- superioridade após contabilizar todas as variantes tentadas.

Depois disso, ainda deve rodar prospectivamente na conta PRACTICE com a regra congelada. Passar nesses critérios não garante lucro futuro; apenas justifica continuar pesquisando.

## Decisão recomendada

1. **Encerrar a otimização da estratégia atual de Fibo + confluências.** O teste fora da amostra já rejeitou sua vantagem econômica.
2. **Reutilizar a infraestrutura**, mas transformar o próximo backtest numa competição pré-registrada entre reversão curta, bounce e rompimento.
3. **Começar apenas com EURUSD e USDJPY normais.** Eles têm a base acadêmica intradiária mais diretamente comparável entre as fontes examinadas.
4. **Usar volatilidade, sessão e notícias como controles.** Não selecionar horários vencedores após olhar o teste.
5. **Manter dinheiro real desligado.** A conclusão possível agora é “vale testar”, não “estratégia viável comprovada”.

## Fontes primárias principais

- Neely, C. J.; Weller, P. A. *Intraday Technical Trading in the Foreign Exchange Market*. Federal Reserve Bank of St. Louis. [PDF](https://fraser.stlouisfed.org/files/docs/publications/frbsl_wp/1999-016.pdf)
- Osler, C. L. *Support for Resistance: Technical Analysis and Intraday Exchange Rates*. Federal Reserve Bank of New York. [Artigo](https://www.newyorkfed.org/research/epr/00v06n2/0007osle.html)
- Osler, C. L. *Currency Orders and Exchange-Rate Dynamics*. Federal Reserve Bank of New York. [Staff Report](https://www.newyorkfed.org/research/staff_reports/sr125.html)
- Chaboud, A. et al. *Rise of the Machines: Algorithmic Trading in the Foreign Exchange Market*. Federal Reserve. [Artigo e dados/metodologia](https://www.federalreserve.gov/pubs/ifdp/2009/980/ifdp980.htm)
- Chaboud, A. et al. *The High-Frequency Effects of U.S. Macroeconomic Data Releases*. Federal Reserve. [Artigo](https://www.federalreserve.gov/pubs/ifdp/2004/823/ifdp823.htm)
- Andersen, T.; Bollerslev, T. *DM-Dollar Volatility: Intraday Activity Patterns...*. [NBER](https://www.nber.org/papers/w5783)
- Andersen, T. et al. *Real-Time Price Discovery in Global Stock, Bond and Foreign Exchange Markets*. [Federal Reserve](https://www.federalreserve.gov/pubs/ifdp/2006/871/default.htm)
- White, H. *A Reality Check for Data Snooping*. [Econometrica/DOI](https://doi.org/10.1111/1468-0262.00152)
- CFTC. *Beware of Off-Exchange Binary Options Trades*. [Alerta oficial](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/beware_of_off_exchange_binary_options.htm)
- IQ Option. *Negociação OTC na IQ Option*. [Metodologia oficial](https://blog.iqoption.com/pt/negociacao-otc-na-iq-option-como-negociar-titulos-no-mercado-de-venda-livre/)
