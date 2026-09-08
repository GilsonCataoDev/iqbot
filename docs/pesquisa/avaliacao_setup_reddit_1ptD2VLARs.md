# Avaliação do setup BOF em XAUUSD (Reddit 1ptD2VLARs)

Pesquisa realizada em 07/09/2026. O post é tratado como uma hipótese para estudo, não como evidência de lucratividade.

## Veredito curto

**Viável para acrescentar ao Monitor Mercado apenas como estudo/sinal sombra. Ainda não é viável como recomendação de entrada nem para automação.**

A ideia central — rompimento de um nível, retorno para dentro e reversão — é mensurável e tem relação plausível com a microestrutura em torno de suporte/resistência. Porém, a versão publicada deixa subjetivos quatro elementos decisivos: quais máximas/mínimas de 30 minutos são “importantes”, como medir compradores/vendedores enfraquecendo, o que conta como confirmação M5 e qual é o próximo nível-chave. Sem congelar essas definições antes do teste, o resultado fica exposto a seleção retrospectiva.

## O que a publicação realmente mostra

O [post original no Reddit](https://www.reddit.com/r/Forexstrategy/comments/1w9uaah/xauusd_5m_clean_bof_setup_caught_a_clean_breakout/) tem apenas uma imagem de um trade vencedor em XAUUSD no gráfico de 5 minutos. O título alega **1:5 R:R**, mas não fornece backtest, histórico de operações, taxa de acerto, custos, horário de execução ou resultado fora da amostra.

Em resposta nos comentários, o próprio autor descreve as regras:

1. Marcar máximas e mínimas “importantes” do M30.
2. Esperar o preço romper um desses níveis.
3. Observar se o lado que provocou o rompimento perde força e o lado oposto ganha força.
4. Esperar o preço falhar em se manter além do nível e voltar para dentro.
5. Esperar uma confirmação no M5.
6. Entrar na reversão; colocar o stop além do extremo do falso rompimento.
7. Usar o próximo nível-chave como alvo, normalmente exigindo no mínimo 1:5.

Na imagem, os valores parecem ser aproximadamente **entrada 4.385,16**, **stop 4.380,16** e **alvo 4.412,11**. Isso corresponde a cerca de 5 pontos de risco para 26,95 de retorno, ou aproximadamente **5,39R**. Esses números são uma leitura visual do print e não uma regra publicada pelo autor. Outro rótulo em 4.386,33 pode representar uma referência/linha adicional; por isso o print sozinho não determina inequivocamente o preço executado.

## O que as fontes primárias sustentam — e o que não sustentam

- Um estudo do Federal Reserve Bank of New York encontrou interrupções de tendência em níveis publicados de suporte/resistência com frequência maior que em níveis arbitrários. O efeito variou entre moedas e firmas, e as notas subjetivas de “força” dos níveis não foram confiáveis: [Support for Resistance: Technical Analysis and Intraday Exchange Rates](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf).
- Outro trabalho do mesmo banco relaciona reações e acelerações perto de números redondos ao agrupamento de ordens stop-loss e take-profit. Isso fornece uma explicação microestrutural possível para rompimentos e reversões, mas não valida este setup específico, XAUUSD ou 1:5: [Currency Orders and Exchange-Rate Dynamics](https://www.newyorkfed.org/research/staff_reports/sr125.html).
- A revisão do Federal Reserve Bank of St. Louis conclui que regras técnicas simples em câmbio tiveram retornos positivos em períodos antigos e depois perderam essa vantagem; regras mais complexas produziram resultados mais modestos. Portanto, uma relação visual plausível não implica estabilidade temporal: [Technical Analysis in the Foreign Exchange Market](https://fraser.stlouisfed.org/docs/publications/frbsl_wp/2011-001.pdf).
- Sullivan, Timmermann e White mostram por que a melhor regra escolhida entre muitas alternativas pode parecer superior por data snooping. Esse risco é diretamente relevante se forem testadas muitas definições de pivô, confirmação, buffer e alvo e depois for exibida apenas a melhor: [Data-Snooping, Technical Trading Rule Performance, and the Bootstrap](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=160330).
- Bailey et al. demonstram que desempenho simulado elevado pode aparecer após testar relativamente poucas configurações. O BOF precisa de holdout/walk-forward e registro das variantes tentadas: [The Effects of Backtest Overfitting on Out-of-Sample Performance](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659).
- A CFTC alerta que operações especulativas curtas, alavancagem e dicas anônimas em redes sociais formam uma combinação de alto risco. Também observa que a alavancagem amplia ganhos e perdas: [Understand Risks and Markets before Reacting to Internet Hype](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/CustomerAdvisory_SocialMedia_Metals.html).

Essas fontes sustentam somente a **plausibilidade de estudar** a reação em níveis. Nenhuma delas confirma a lucratividade do BOF publicado nem a promessa implícita de obter 5R com regularidade.

## Especificação mínima para um backtest honesto

Para transformar o post em uma regra reproduzível, o Monitor deve congelar uma versão antes de olhar os resultados:

1. **Ativo e contexto:** XAUUSD normal, candles M30 e M5 do mesmo feed; excluir OTC. Registrar spread e sessão.
2. **Nível M30:** pivô confirmado somente após duas velas M30 fechadas de cada lado. Um pivô não pode ser movido depois do sinal.
3. **Rompimento:** uma vela M5 ultrapassa o pivô por pelo menos `max(0,10×ATR14_M5, 2×spread)` e por no máximo `0,75×ATR14_M5`. O teto evita classificar deslocamentos grandes como pequenas varreduras.
4. **Falha:** a mesma vela ou uma das duas M5 seguintes fecha novamente do lado interno do nível M30.
5. **Confirmação:** a primeira vela M5 fechada após a falha rompe, na direção da reversão, a máxima/mínima da vela que retornou ao range. A entrada ocorre na abertura seguinte; nunca na extrema já conhecida da vela de confirmação.
6. **Stop:** além do extremo da varredura, com buffer de `max(0,10×ATR14_M5, 2×spread)`.
7. **Alvo primário:** primeiro pivô M30 confirmado e preexistente na direção do trade. Se esse alvo não oferecer pelo menos 5R, descartar.
8. **Alternativa de controle:** testar separadamente alvo fixo de 2R, 3R e 5R. Não selecionar o melhor no mesmo período usado para estimar o resultado final.
9. **Timeout:** encerrar após 12 velas M5 se nenhum stop/alvo for atingido, usando uma regra de saída congelada.
10. **Custos:** aplicar spread realista, slippage em entrada/stop e o preço correto de bid/ask. Quando stop e alvo aparecem dentro da mesma vela, classificar como ambíguo ou resolver com dados menores/ticks; não assumir o resultado favorável.

A definição de “força” não deve usar volume real/order flow porque o feed atual da IQ Option não oferece o fluxo interdealer usado nos estudos. No primeiro teste, é melhor substituí-la inteiramente pelas regras de fechamento e rompimento acima. Indicadores de corpo, pavio ou volume podem ser avaliados depois como variantes, sempre contabilizando o número de tentativas.

## Riscos de viés e erros de implementação

| Risco | Como aparece aqui | Prevenção |
|---|---|---|
| Lookahead do pivô | marcar um topo/fundo usando candles que ainda não existiam | confirmar o pivô somente após os candles da direita fecharem |
| Entrada impossível | usar no teste o fundo/topo da vela que só ficou conhecido no fechamento | entrar na abertura seguinte ou simular dados intrabar |
| Alvo retrospectivo | chamar de “próximo nível” aquele atingido no print | selecionar apenas pivô confirmado antes da entrada |
| Seleção de vencedor | post mostra um trade vencedor, sem mostrar todas as tentativas | registrar todo candidato, bloqueio, stop e expiração |
| Otimização múltipla | variar ATR, pivô, horário e R:R até algo funcionar | manter registro das variantes e usar holdout/walk-forward |
| Ambiguidade OHLC | stop e TP podem ocorrer na mesma vela M5 | usar ticks/M1 ou marcar o caso como ambíguo |
| Feed e execução | TradingView e IQ podem ter máximas, spread e horários diferentes | gerar e resolver o sinal no mesmo feed da execução |
| Notícia/slippage | ouro pode atravessar o stop em divulgações | registrar calendário, spread e slippage; analisar separadamente |
| Taxa de acerto enganosa | 5R permite baixa taxa de acerto, mas sequências de loss podem ser longas | avaliar expectativa, profit factor, drawdown e intervalo de confiança |

## Como acrescentar ao Monitor sem induzir entrada ruim

- Nome: `bof_m30_m5_xau`.
- Status inicial: **ESTUDO/SOMBRA**.
- Etapas visuais: `NÍVEL M30` → `VARREU` → `VOLTOU AO RANGE` → `CONFIRMOU M5` → `ELEGÍVEL`.
- Mostrar no gráfico apenas o pivô usado, extremo da varredura, entrada simulada, stop e alvo preexistente.
- Salvar todos os candidatos, inclusive os descartados por rompimento excessivo, R:R insuficiente, notícia, spread ou confirmação ausente.
- Resultados separados por sessão, direção, volatilidade, notícia e ano/mês.
- Critério mínimo antes de promover: pelo menos 200 sinais totais, amostra fora da otimização, expectativa positiva após custos, intervalo de confiança e drawdown aceitável. Esse número não garante validade; é apenas um piso operacional para evitar conclusão baseada em poucos casos.

## Decisão

**Acrescentar somente como estudo sombra de XAUUSD.** O conceito é suficientemente claro para criar uma primeira especificação objetiva, mas a publicação não demonstra vantagem. O primeiro objetivo deve ser descobrir a taxa-base de BOFs válidos, o custo real e a frequência com que um alvo preexistente oferece 5R — não reproduzir visualmente o vencedor mostrado.
