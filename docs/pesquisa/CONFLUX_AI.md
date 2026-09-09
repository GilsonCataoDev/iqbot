# Conflux AI: avaliação para análise de trading/Forex

Pesquisa realizada em 09/09/2026. Escopo: verificar se o produto chamado “Conflux AI” pode complementar o monitor local deste projeto. As afirmações sobre produto, preço e privacidade abaixo vêm do fornecedor ou da ficha oficial da extensão; não constituem validação independente de desempenho.

## Conclusão curta

O produto pertinente é [Conflux AI — confluxai.net](https://confluxai.net/), da Conflux AI LLC. É um analisador visual de gráficos baseado em Smart Money Concepts (SMC), voltado principalmente ao TradingView. Ele pode servir como **segunda opinião manual** para Forex/XAUUSD: analisa uma captura do gráfico e sugere viés, zonas, entrada, stop e alvos.

Não é apropriado para virar o motor local de sinais nem para enviar ordens no projeto atual. Não foi localizada API pública, webhook, feed de candles, integração oficial com IQ Option ou corretoras. Os [termos](https://confluxai.net/terms) também proíbem scripts/bots automatizados sem autorização.

Não confundir com a blockchain/token Conflux (CFX), o repositório experimental `capGoblin/conflux-ai`, nem o aplicativo geral “The Conflux”; eles são produtos distintos.

## O que o produto faz

- A [página oficial](https://confluxai.net/) informa análise de screenshots/gráficos para detectar order blocks, fair value gaps (FVG), varreduras de liquidez e mudanças de estrutura; retorna zonas de entrada, stop-loss, take-profit e uma pontuação de confluência.
- A [ficha oficial da extensão Chrome](https://chromewebstore.google.com/detail/conflux-ai-%E2%80%94-smc-analyzer/flhcohhopgdmjopapneekbejhcfaahkj) afirma funcionar em símbolos e tempos gráficos disponíveis no TradingView, incluindo Forex, ações, futuros, cripto e índices. O fluxo é: abrir o gráfico, clicar em **Analyze**, escolher Scalp/Day/Swing e receber uma leitura em cerca de um minuto.
- A extensão declara detecção de BOS, ChoCH, order blocks, FVG, liquidez, zonas premium/discount e OTE; inclui plano com entrada, SL, TP1/TP2/TP3, tamanho de posição e projeção de R. Também oferece chat sobre o gráfico e exportação de Pine Script.
- Os [indicadores oficiais](https://confluxai.net/strategy) para TradingView são: **Super Scalper HTF** (ATR/SuperTrend com confirmação SMC), **SMC Pro** (BOS/ChoCH/OB/FVG e filtro HTF) e **Pulse** (momentum + SMC). A página informa que o acesso é pago, por convite, e removido se a assinatura expirar.

## Integração e automação

Não há documentação pública encontrada para API, chave de API, webhook, alertas saindo do Conflux ou execução de trades. A integração exposta é visual: upload/colar captura, extensão que lê a aba ativa do TradingView e indicadores Pine/thinkScript.

| Necessidade deste projeto | Situação no Conflux AI |
|---|---|
| Monitorar candles/dados localmente | Não documentado |
| Receber sinal por API/webhook | Não documentado |
| Operar IQ Option | Não documentado |
| Executar ordem automaticamente | Não documentado; automação sem autorização é vedada nos termos |
| Revisar um sinal já detectado | Sim, manualmente, via screenshot/TradingView |

Uso seguro e realista: o monitor local gera um candidato, o operador abre o mesmo ativo/tempo gráfico no TradingView e pede uma análise manual ao Conflux antes de decidir. O resultado deve ser registro complementar, não gatilho de operação.

## Preço mostrado pelo fornecedor

Na [página inicial](https://confluxai.net/), os valores exibidos na data da pesquisa são:

- Indicadores: **US$ 9,99/mês**.
- AI Chart Analyzer Pro: **US$ 19,99/mês** (a página mostra US$ 39 como preço anterior), com análises “ilimitadas” sujeitas a *fair use*, extensão, indicadores, journal e calculadora de risco.
- Créditos avulsos: **10 por US$ 5** (US$ 0,50/análise) ou **50 por US$ 15** (US$ 0,30/análise).
- A [ficha da extensão](https://chromewebstore.google.com/detail/conflux-ai-%E2%80%94-smc-analyzer/flhcohhopgdmjopapneekbejhcfaahkj) também informa 3 análises gratuitas para novas contas e plano a partir de US$ 19,99/mês por 100 análises; há divergência de apresentação entre as páginas, portanto confirme no checkout antes de pagar.

Segundo os [termos](https://confluxai.net/terms), a assinatura é recorrente; o cancelamento vale ao fim do ciclo. Reembolso integral é previsto somente até 48 horas da primeira assinatura e com menos de 5 análises. Créditos não são reembolsáveis e expiram em 1 ano.

## Dados, segurança e riscos

- A [política de privacidade](https://confluxai.net/privacy) declara coleta de e-mail, dados de autenticação, uso, dispositivo, IP e localização aproximada. Gráficos são processados temporariamente; material salvo no journal inclui entradas, notas, resultados, métricas e snapshots.
- O fornecedor declara TLS em trânsito, criptografia em repouso e senhas com hash. Cita uso de Stripe, Google/OAuth, provedores de IA e analytics; dados anonimizados podem ser usados para melhorar os modelos. São declarações do fornecedor, não uma auditoria de segurança independente.
- A [ficha Chrome](https://chromewebstore.google.com/detail/conflux-ai-%E2%80%94-smc-analyzer/flhcohhopgdmjopapneekbejhcfaahkj) diz que a extensão só lê a aba do TradingView ativa após o clique em Analyze, não lê outras abas nem credenciais do TradingView, e envia a imagem ao backend para análise sem a guardar após a requisição. A ficha também declara manipulação de informações de autenticação e conteúdo do site.
- Os [termos](https://confluxai.net/terms) classificam o serviço como educacional/informativo, não como consultoria financeira. O usuário assume as decisões e perdas; a responsabilidade do fornecedor é limitada ao valor pago nos 12 meses anteriores.

## Limitações importantes

1. Não há metodologia quantitativa, histórico de performance, taxa de acerto, auditoria de código ou backtest verificável publicados nas fontes oficiais consultadas.
2. A análise depende da imagem/visualização do gráfico, portanto pode divergir do feed, spread, horário e preço de liquidação usados pela corretora.
3. SMC, confluência, SL e TP são hipóteses de análise; não são previsão nem evidência de vantagem estatística.
4. Não fornecer login, senha, token, cookie ou chave da corretora à ferramenta. A proposta oficial não necessita dessas credenciais.

## Recomendação prática

1. Se desejar testar, use as análises grátis ou créditos mínimos — sem plano anual.
2. Teste somente em conta demo/prática por uma amostra separada por ativo, sessão e tempo gráfico.
3. Registre, para cada leitura: imagem, ativo, timeframe, pontuação, direção, entrada/SL/TP propostos e resultado no feed da corretora.
4. Só considere qualquer filtro depois de medir resultado fora da amostra. Não conecte a robô, automação ou dinheiro real com base em marketing ou poucos acertos.

## Fontes primárias consultadas

- [Conflux AI — página oficial](https://confluxai.net/)
- [Conflux AI — sobre e metodologia declarada](https://www.confluxai.net/about)
- [Conflux AI — estratégia e indicadores](https://www.confluxai.net/strategy)
- [Conflux AI — Termos de Serviço](https://confluxai.net/terms)
- [Conflux AI — Política de Privacidade](https://confluxai.net/privacy)
- [Chrome Web Store — ficha oficial da extensão](https://chromewebstore.google.com/detail/conflux-ai-%E2%80%94-smc-analyzer/flhcohhopgdmjopapneekbejhcfaahkj)
