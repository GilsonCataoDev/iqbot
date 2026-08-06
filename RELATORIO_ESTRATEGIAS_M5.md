# Investigação de estratégias M5
Triagem walk-forward em três janelas futuras por ativo, payout assumido de 85%. Os parâmetros são escolhidos somente no passado de cada janela. O critério final usa intervalo simultâneo com correção de Bonferroni para os testes realizados.

- Combinações avaliadas: 48
- Candidatos promissores: 0
- Regra de aprovação: mínimo de 300 operações fora da amostra, lucro positivo em todas as três janelas e piso do intervalo simultâneo acima do breakeven.

## Resultado

Nenhuma família cumpriu o critério de viabilidade.

## Melhores resultados fora da amostra

| ativo | familia | operacoes | acerto | lucro | folds_positivos | payout_exigido |
|---|---|---|---|---|---|---|
| GBPUSD | reversao_candle | 376 | 59.04% | 34.7 | 3 | 69.37% |
| EURUSD | reversao_candle | 366 | 56.01% | 13.25 | 2 | 78.54% |
| GBPUSD | nivel_anterior_bounce | 175 | 57.71% | 11.85 | 2 | 73.27% |
| USDJPY | reversao_zscore | 1035 | 54.4% | 6.55 | 2 | 83.84% |
| EURUSD | reversao_zscore | 1074 | 54.19% | 2.7 | 1 | 84.54% |
| USDJPY-OTC | nivel_anterior_breakout | 169 | 53.85% | -0.65 | 1 | 85.71% |
| EURUSD-OTC | nivel_anterior_breakout | 144 | 52.08% | -5.25 | 1 | 92.0% |
| EURUSD | nivel_anterior_bounce | 144 | 51.39% | -7.1 | 2 | 94.59% |

## Melhor hipótese para observação prospectiva

`reversao_candle` em `GBPUSD` obteve 59.04% em 376 operações, lucro simulado de +34.70 unidades e lucro positivo em 3/3 janelas. Parâmetros escolhidos em cada janela: `{'limiar_atr': 1.5, 'expiracao': 6} | {'limiar_atr': 1.5, 'expiracao': 6} | {'limiar_atr': 1.5, 'expiracao': 6}`. Mesmo assim, o piso do intervalo simultâneo ficou abaixo do breakeven; portanto é um candidato de coleta PRACTICE, não uma estratégia comprovada.

## Limitações

Cada ativo contém 20.000 candles, mas o histórico cobre apenas cerca de dois a três meses. Os ativos normais e OTC começam em datas diferentes, e o volume dos OTC é sempre zero. O histórico também não contém o payout real de cada entrada. Passar nesta triagem autoriza apenas coleta prospectiva em PRACTICE, nunca conta real.
