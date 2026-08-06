# Investigação de estratégias M5
Triagem walk-forward em três janelas futuras por ativo, payout assumido de 85%. Os parâmetros são escolhidos somente no passado de cada janela. O critério final usa intervalo simultâneo com correção de Bonferroni para os testes realizados.

**Modo purgado:** operações sobrepostas foram descartadas. Um sinal com expiração de N velas ocupa N velas; sinais em velas seguidas mediriam quase o mesmo movimento e inflariam a amostra. Aqui cada operação é independente das demais, então o intervalo de confiança é honesto.

- Combinações avaliadas: 48
- Candidatos promissores: 0
- Regra de aprovação: mínimo de 300 operações fora da amostra, lucro positivo em todas as três janelas e piso do intervalo simultâneo acima do breakeven.

## Resultado

Nenhuma família cumpriu o critério de viabilidade.

## Melhores resultados fora da amostra

| ativo | familia | operacoes | acerto | lucro | folds_positivos | payout_exigido |
|---|---|---|---|---|---|---|
| USDJPY | reversao_zscore | 570 | 57.02% | 31.25 | 3 | 75.38% |
| GBPUSD | reversao_candle | 284 | 58.1% | 21.25 | 3 | 72.12% |
| EURUSD | reversao_candle | 286 | 56.64% | 13.7 | 2 | 76.54% |
| GBPUSD | reversao_zscore | 462 | 55.41% | 11.6 | 1 | 80.47% |
| EURUSD-OTC | nivel_anterior_breakout | 111 | 56.76% | 5.55 | 3 | 76.19% |
| GBPUSD | nivel_anterior_bounce | 112 | 56.25% | 4.55 | 1 | 77.78% |
| EURUSD | reversao_zscore | 442 | 54.52% | 3.85 | 2 | 83.4% |
| USDJPY-OTC | nivel_anterior_breakout | 134 | 54.48% | 1.05 | 1 | 83.56% |

## Melhor hipótese para observação prospectiva

`reversao_zscore` em `USDJPY` obteve 57.02% em 570 operações, lucro simulado de +31.25 unidades e lucro positivo em 3/3 janelas. Parâmetros escolhidos em cada janela: `{'janela': 20, 'z': 2.0, 'expiracao': 3} | {'janela': 20, 'z': 2.0, 'expiracao': 3} | {'janela': 20, 'z': 2.0, 'expiracao': 3}`. Mesmo assim, o piso do intervalo simultâneo ficou abaixo do breakeven; portanto é um candidato de coleta PRACTICE, não uma estratégia comprovada.

## Limitações

Cada ativo contém 20.000 candles, mas o histórico cobre apenas cerca de dois a três meses. Os ativos normais e OTC começam em datas diferentes, e o volume dos OTC é sempre zero. O histórico também não contém o payout real de cada entrada. Passar nesta triagem autoriza apenas coleta prospectiva em PRACTICE, nunca conta real.
