# Plano aplicado ao monitor de binárias

## Regras do modo experimental

- Ativo normal separado de OTC.
- Entrada somente após toque em zona e reação confirmada.
- Rompimento só vale após reteste; não perseguir candle esticado.
- Expiração M5: 15 minutos (3 candles).
- Payout e breakeven registrados em cada teste.
- Sem martingale e sem aumento automático de stake.
- Risco máximo recomendado: 0,5–1% da banca por operação.
- Notícia de alto impacto bloqueia novas entradas por 20 minutos.
- Toda combinação de ativo, horário, setup e expiração é validada fora da amostra.

## Critério para liberar

Só liberar uma combinação quando houver pelo menos 200 operações decididas,
win rate acima do breakeven do payout e intervalo de confiança inferior ainda
acima do breakeven. Amostras menores ficam como observação.

## Estado atual

Os testes M5/M15 existentes ainda não atingem esse critério. O monitor permanece
em alerta/PRACTICE; a IA pode explicar o contexto, mas não abre ordens.
