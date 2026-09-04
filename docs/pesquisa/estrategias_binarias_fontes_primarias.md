# Estratégias em opções binárias: o que é verificável

Pesquisa realizada em 31/08/2026. Objetivo: orientar o monitor sem tratar promessas de win rate como evidência.

## Conclusão curta

Não encontrei uma estratégia pública, auditada e replicável que comprove vantagem consistente para o varejo em binárias de curto prazo. Reguladores descrevem o produto como pagamento fixo ou zero, com perda potencial de todo o valor investido e retorno esperado estruturalmente negativo quando o payout não compensa a probabilidade.

Portanto, os “bigs” verificáveis não aparecem como operadores de sinais M5/M15 em plataformas varejistas. A prática profissional documentada é: definir risco antes da entrada, controlar exposição, considerar volatilidade/evento, usar instrumentos regulados e validar em simulador/out-of-sample. Isso é gestão de processo, não uma fórmula mágica de CALL/PUT.

## O que as fontes primárias confirmam

### Produto e matemática

- A CFTC/SEC definem a binária como uma proposição sim/não: no vencimento há pagamento predeterminado ou nada. A própria orientação alerta que, com ganho menor que a perda, uma chance 50/50 produz retorno esperado negativo: [CFTC/SEC — Binary Options and Fraud](https://www.cftc.gov/LearnAndProtect/FraudAwarenessPrevention/CFTCFraudAdvisories/fraudadv_binaryoptions.html).
- A ESMA identificou “structural expected negative return”, conflitos de interesse e perdas da maioria dos clientes de varejo como razões para proibir a comercialização a investidores de varejo na União Europeia: [ESMA — análise de intervenção](https://www.esma.europa.eu/sites/default/files/library/esma50-162-214_product_intervention_analysis_binary_options.pdf).
- A FCA confirmou proibição permanente da venda, marketing e distribuição de binárias a consumidores de varejo no Reino Unido, citando perdas inesperadas e risco de dano ao consumidor: [FCA — banimento permanente](https://www.fca.org.uk/news/statements/fca-confirms-permanent-ban-sale-binary-options-retail-consumers).

### O que profissionais documentam sobre risco e evento

- O CME recomenda estabelecer antes de cada operação: tamanho da posição, perda máxima por trade, perda máxima diária, alavancagem pretendida e exposição total; também recomenda saber o stop e o capital em risco antes de enviar a ordem: [CME — Risk Management and Your Trade Plan](https://www.cmegroup.com/education/courses/building-a-trade-plan/risk-management-and-your-trade-plan) e [CME — Trade and Risk Management](https://www.cmegroup.com/education/courses/trade-and-risk-management).
- Para risco de evento, o CME orienta avaliar atividade intradiária, volume de opções e volatilidade histórica versus implícita; isso apoia um filtro de notícia/volatilidade, mas não prova a direção do próximo candle: [CME — Measuring and Managing Event Risk](https://www.cmegroup.com/education/featured-reports/videos/measuring-and-managing-event-risk).
- A Nadex, uma exchange regulada nos EUA, descreve contratos de evento, possibilidade de encerrar antes do vencimento e uso de conta demo. Isso é diferente de prometer que uma entrada curta terá vantagem: [Nadex — How to Trade Event Contracts](https://website-prod.nadex.com/learning/how-to-trade-event-contracts/).

## Estratégias alegadas versus práticas testáveis

| Alegação comum | Situação | Como tratar no bot |
|---|---|---|
| “75–90% de acerto” em M1/M5 | Não é evidência sem dados, custos/payout, amostra e período fora da amostra | Rejeitar até obter histórico auditável e forward test |
| Martingale/dobrar após loss | Aumenta rapidamente a exposição e não cria edge | Bloquear no monitor |
| Entrar no primeiro candle da notícia | Alta volatilidade, spread/slippage e direção incerta | Marcar risco; esperar reação e reteste |
| Pullback em suporte/resistência | Hipótese plausível, não garantia | Testar por ativo, sessão, volatilidade e expiração |
| Rompimento + reteste | Hipótese testável | Exigir fechamento/retorno ao nível e registrar atraso |
| RSI/Bollinger em extremo | Pode funcionar em range e falhar em tendência | Separar regime de mercado; não misturar amostras |
| IA lendo manchete | Ajuda a resumir contexto; não prevê o resultado | Usar como segunda opinião, nunca como gatilho único |

## Especificação recomendada para o monitor

1. Registrar cada sinal mesmo quando não houver entrada: ativo, horário UTC, sessão, notícia, preço, direção, nível, expiração, payout e motivo de descarte.
2. Avaliar separadamente expirações de 5, 15 e 30 minutos; não comparar diretamente M15 com M5.
3. Separar mercado normal e OTC; separar ativo e horário. Não misturar resultados.
4. Para notícia: classificar impacto e surpresa (actual versus forecast), aguardar 1–3 candles, medir retorno/volume e somente então sinalizar “a favor” se houver rompimento e reteste. Se houver candle oposto forte ou retorno ao range, bloquear.
5. Calcular o breakeven por payout: `p = 1 / (1 + payout_decimal)`. Com payout de 85%, o mínimo é 54,05% antes de outros custos.
6. Exigir amostra mínima, teste fora da amostra e relatório por ativo/horário. Um resultado positivo em poucos sinais deve ser tratado como exploratório.
7. Manter prática/manual até haver edge estável; não liberar automação com base apenas em win rate de marketing.

## O que não foi encontrado

As fontes oficiais consultadas não oferecem uma receita de entrada M5/M15 que “os grandes usam” e que seja diretamente aplicável à binária varejista. Profissionais de derivativos usam estruturas de opções, hedge, volatilidade e controle de portfólio; isso não equivale a uma CALL/PUT fixa de plataforma varejista. Qualquer adaptação deve ser validada no próprio ativo, payout, feed e regra de liquidação da IQ Option.

## Fontes adicionais de prática profissional

- [CME — Option Strategies](https://www.cmegroup.com/education/courses/option-strategies)
- [CME — Trading Simulator](https://www.cmegroup.com/education/courses/curriculum-all-about-options)
- [CFTC — Binary Options Fraud](https://www.cftc.gov/BinaryOptionsFraud/index.htm)

