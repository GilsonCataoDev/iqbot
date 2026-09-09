# EMA 9/20 e EMA 9/21: melhorias que podem ser testadas

> Status: proposta de pesquisa. Nenhum filtro abaixo está aprovado para conta real.

## Decisão

Manter a EMA como **gatilho de pullback**, e não como previsão. A regra-base continua sendo: tendência pelas médias, toque na faixa e fechamento de rejeição a favor. Testar somente cinco filtros adicionais, isoladamente, por ativo, timeframe e tipo de mercado.

Não há evidência acadêmica que valide especificamente “EMA 9/20 (ou 9/21) + pavio” em forex M5/M15 ou em binárias. Estudos amplos encontram resultados dependentes de período e de custos; isso impede transformar parâmetros populares em promessa de acerto.

## Os 5 filtros candidatos

| # | Filtro objetivo | Regra candidata | O que ele tenta remover | Teste pré-definido |
|---|---|---|---|---|
| 1 | Tendência do timeframe acima | CALL somente se M5/M15 e H1 tiverem EMA9 acima da EMA20/21 e inclinação da EMA longa no mesmo sentido; PUT no inverso. | Pullback contra uma tendência maior. | Comparar `sem H1` vs `H1 alinhado`; não ajustar as EMAs durante o teste. |
| 2 | Primeiro reteste após impulso | Aceitar o toque somente se houve impulso anterior >= 1 ATR em até 3 velas e no máximo 1 toque anterior à faixa nas últimas 5 velas. | Mercado serrilhado e médias sendo atravessadas repetidamente. | Comparar base vs primeiro reteste; registrar `impulso_atr` e `toques_anteriores`. |
| 3 | Qualidade da vela de confirmação | Após tocar a faixa, exigir corpo >= 0,35 ATR e fechamento no último 25% do range, a favor da operação. | Pavios sem continuação e candles de indecisão. | Testar apenas três cortes: corpo >= 0,25 / 0,35 / 0,50 ATR. |
| 4 | RSI como estado de pullback | Em alta, RSI14 entre 40–55; em baixa, 45–60. O RSI não é sinal sozinho. | Entrar quando o movimento já está esticado ou sem retração. | Comparar `sem RSI` vs faixa atual; manter os limites fixos em cada fold. |
| 5 | Volatilidade e evento | ATR14 da vela deve ficar entre os percentis 25–85 do **mesmo ativo e mesma hora UTC**. Separar também os sinais dentro de -30/+30 min de notícia de alto impacto; não inferir direção pela notícia. | Horas paradas, picos anormais e execução instável próxima a evento. | Comparar cada grupo separadamente: ATR normal, ATR baixo, ATR alto, pré/pós-notícia. Sessão é atributo de análise, não um bloqueio global. |

Os filtros 2 e 3 já aparecem parcialmente na variante experimental `ema920_prime`; ela deve continuar em sombra até superar a linha-base em validação fora da amostra.

## Como medir sem se enganar

1. **Congelar a linha-base:** EMA9/20 fechado e EMA9/21+RSI fechado, com a regra atual de toque/rejeição. Nada de mudar mais de um parâmetro por vez.
2. **Separar as células:** ativo, M5/M15, normal/OTC, setup, sessão/hora, expiração e payout. Para forex, salvar spread real/estimado. Não misturar resultado de binária com retorno de forex.
3. **Walk-forward cronológico:** usar os primeiros 60% dos candles para treino, 20% para escolher entre os três cortes previamente listados e os últimos 20% como holdout bloqueado. Repetir em janelas rolantes; o último período nunca decide os parâmetros.
4. **Métrica primária:** binárias = lucro líquido usando o payout efetivo e `breakeven = 1 / (1 + payout_decimal)`; forex = resultado líquido após spread. WR é apenas métrica secundária.
5. **Regra de promoção:** o filtro só sai da sombra se tiver pelo menos 100 sinais resolvidos na célula, lucro líquido positivo e superar a base em dois holdouts cronológicos consecutivos. Caso contrário, permanece pesquisa.

## Por que estes filtros são hipóteses — não provas

- Um estudo com mais de 21 mil regras técnicas em FX encontrou previsibilidade e lucro variando por período e moeda, mesmo com proteção contra data snooping e validação fora da amostra. Ele não estabelece uma EMA curta vencedora para M5/M15. [Hsu, Taylor & Wang (2016), *Journal of International Economics*](https://doi.org/10.1016/j.jinteco.2016.03.012)
- Outro estudo de regras de média móvel em 18 moedas encontrou queda de lucros ajustados ao risco ao longo das décadas e resultados próximos de zero nos anos 1990. Isso reforça o uso de validação temporal, não de um parâmetro eterno. [Olson (2004), *Journal of Banking & Finance*](https://doi.org/10.1016/S0378-4266(02)00399-0)
- Em análise intradiária com custos observados, a maior parte do lucro técnico desapareceu após custos. Por isso payout e spread precisam entrar no resultado antes de comparar filtros. [Krauss, Do & Huck (2016), *Finance Research Letters*](https://doi.org/10.1016/j.frl.2016.04.014)
- A volatilidade intradiária em câmbio tem padrão horário forte, o que sustenta normalizar ATR por horário, e não concluir que uma faixa fixa de ATR serve para todas as sessões. [Andersen & Bollerslev (1997), *Journal of Empirical Finance*](https://doi.org/10.1016/S0927-5398(97)00004-2)
- O BIS descreve liquidez tipicamente maior na abertura de Londres e na sobreposição Londres–Nova York. Isso justifica registrar sessão/liquidez; não determina a direção de uma vela. [BIS (2013), *Anatomy of the global FX market*](https://www.bis.org/publications/qr-201312/anatomy-global-fx-market-through-lens-2013-triennial-survey)

## Limites importantes

- Esses estudos são principalmente de spot/futuros e, em muitos casos, dados diários; não validam automaticamente a liquidação, o feed, o atraso ou o payout da IQ Option.
- OTC deve ser sempre uma amostra separada. Um resultado em mercado normal não é transferível para OTC.
- O filtro de notícia classifica risco de execução/volatilidade. Sem `actual` versus `forecast` e confirmação do preço, ele não escolhe CALL ou PUT.

## Próxima campanha recomendada

Rodar em sombra primeiro os filtros 1–5 sobre os três setups atuais, sem combinar filtros novos. Ao final de cada semana, gerar a tabela `ativo × timeframe × setup × filtro × hora UTC × payout × resultado` e só então selecionar **uma** combinação para uma nova campanha isolada.
