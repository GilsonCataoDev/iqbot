# Documentação técnica

## Fluxo principal

`app.py` conecta à IQ, recebe os candles dos seis ativos, calcula indicadores, atualiza o painel e envia somente as decisões aprovadas por `risco.py`. O resultado é gravado no SQLite e reaparece no gráfico.

## Estratégia

A estratégia possui dois setups independentes. A reversão à média procura afastamento das Bandas de Bollinger, condição do RSI e confirmação de retorno em mercado lateral. O pullback opera a favor da tendência identificada pela EMA 50, calcula a retração do último impulso e aceita toque na faixa Fibonacci de 38,2% a 61,8% e/ou em pivô local de suporte/resistência. Depois do toque, exige candle e RSI confirmando a retomada.

O candle em formação serve para o acompanhamento visual. A decisão operacional utiliza candle fechado e só pode entrar na janela inicial do candle seguinte. Isso evita usar informação futura no sinal.

## Camadas

- `mercado_iq.py`: adapta a biblioteca não oficial `iqoptionapi` e falha de forma segura quando mercado ou payout não são confirmados.
- `estrategia.py`: indicadores, sinais históricos e alertas antecipados.
- `risco.py`: estado diário e autorização de entrada.
- `executor.py`: envio e acompanhamento da ordem.
- `registro.py`: decisões e operações no SQLite.
- `grafico.py`: publica JSONs no `%LOCALAPPDATA%\IQOptionM5`, fora do OneDrive.
- `relatorio.py`: resumo das operações gravadas.

Cada operação armazena o campo `setup`, permitindo comparar `pullback` e `reversao_bollinger_rsi` sem misturar seus resultados.

## Configuração padrão

- Timeframe e expiração: 5 minutos.
- Payout mínimo: 80%.
- Ordem: 1 unidade.
- Limite: 5 operações/dia.
- Limite ativo: 5 operações por dia. Stops por perdas consecutivas e prejuízo diário permanecem disponíveis na configuração, mas estão desativados por padrão.
- Conta: PRACTICE, validada antes da conexão.

## Verificação

Execute `TESTAR_IQ_M5.bat`. A suíte cobre estratégia, risco, persistência, gráfico, falhas da API e bloqueio de arquivos pelo OneDrive. Os testes usam objetos falsos e não enviam operações à IQ Option.

## Dependências externas

O projeto usa Python 3.12, pandas, NumPy e a biblioteca comunitária `iqoptionapi`. O gráfico usa Lightweight Charts carregado pela internet. Como a API da IQ não é oficial, mudanças do serviço podem exigir manutenção no adaptador `mercado_iq.py`.
