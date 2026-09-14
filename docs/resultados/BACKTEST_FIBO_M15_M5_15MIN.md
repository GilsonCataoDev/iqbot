# Backtest — Fibo M15 + confirmação M5 (binárias, 15 min)

- Fonte: caches históricos locais; nenhuma ordem foi enviada.
- Entrada: próxima abertura M5 após confirmação; vencimento: 3 velas M5.
- Payout assumido: 85%; breakeven: 54,05%. Empates contam como perda (conservador).

| Ativo | Sinais | Wins | WR | IC 95% Wilson | Resultado (unid. de stake) |
| --- | ---: | ---: | ---: | --- | ---: |
| EURUSD | 242 | 158 | 65.3% | [59.1%, 71.0%] | +50.30 |
| GBPUSD | 243 | 151 | 62.1% | [55.9%, 68.0%] | +36.35 |
| USDJPY | 255 | 151 | 59.2% | [53.1%, 65.1%] | +24.35 |
| AUDUSD | 191 | 105 | 55.0% | [47.9%, 61.9%] | +3.25 |
| USDCAD | 137 | 79 | 57.7% | [49.3%, 65.6%] | +9.15 |
| EURGBP | 242 | 121 | 50.0% | [43.7%, 56.3%] | -18.15 |

| **TOTAL** | 1310 | 765 | 58.4% | [55.7%, 61.0%] | +105.25 |

## Checagem temporal

| Período | Sinais | Wins | WR | IC 95% Wilson | Resultado (unid. de stake) |
| --- | ---: | ---: | ---: | --- | ---: |
| Primeira metade | 950 | 543 | 57.2% | [54.0%, 60.3%] | +54.55 |
| Segunda metade | 360 | 222 | 61.7% | [56.5%, 66.5%] | +50.70 |

## Regra exata

1. Impulso de 5 velas M15, amplitude ≥ 1,5× a faixa média M15, EMA 9/21 e direção alinhadas.
2. Retração até 50–61,8% do impulso.
3. M5 fecha na zona com rejeição (pavio ≥ 1,5× corpo) ou engolfo na direção do impulso.
4. Entra na abertura da M5 seguinte e compara o fechamento após 15 minutos.

Resultado histórico não garante resultado futuro. Não usar em conta real sem amostra futura separada e validação por ativo.
