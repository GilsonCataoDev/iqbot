# Organização do projeto

Os arquivos `.bat` e os módulos executáveis principais permanecem na raiz para manter os atalhos atuais funcionando.

- `forex/relatorios/` — pesquisas e relatórios de Forex.
- Os módulos `backtest_forex_*.py` permanecem na raiz porque os testes e imports existentes dependem desses nomes.
- `bots/` — reservado para versões futuras dos bots; os lançadores atuais continuam na raiz.
- `binarias/` — reservado para documentação/recursos de binárias; os lançadores atuais continuam na raiz.
- `dados/bancos/` — bancos SQLite das contas e timeframes.
- `dados/logs/` — logs de execução.
- `docs/guias/` — README, Wiki e documentação técnica.
- `scripts/utilitarios/` — reservado para utilitários que não são lançados diretamente pelos BATs.

Para iniciar, continue usando os BATs na raiz. O monitor Forex permanece em `monitor_forex.py` e o seu lançador em `MONITOR_FOREX.bat`.
