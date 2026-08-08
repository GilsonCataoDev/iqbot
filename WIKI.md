# IQ Option M5 — Wiki Técnica Completa

> Última atualização: 2026-08-08

---

## Índice

1. [Visão geral](#1-visão-geral)
2. [Arquitetura](#2-arquitetura)
3. [Módulos](#3-módulos)
4. [Loop principal e timing de velas](#4-loop-principal-e-timing-de-velas)
5. [Estratégias](#5-estratégias)
6. [Gerenciamento de risco](#6-gerenciamento-de-risco)
7. [Mercado OTC vs normal](#7-mercado-otc-vs-normal)
8. [Gráfico em tempo real](#8-gráfico-em-tempo-real)
9. [Banco de dados](#9-banco-de-dados)
10. [Configuração — referência completa](#10-configuração--referência-completa)
11. [Como rodar e testar](#11-como-rodar-e-testar)
12. [Bugs corrigidos — histórico](#12-bugs-corrigidos--histórico)
13. [Problemas comuns](#13-problemas-comuns)

---

## 1. Visão geral

Bot de binary options para a IQ Option em conta **PRACTICE**. Analisa candles M5 de até 10 ativos simultaneamente, exibe um painel web local em tempo real e envia ordens automaticamente quando a estratégia e o gerenciador de risco aprovam. O único protocolo de conexão é o WebSocket não oficial (`iqoptionapi`).

**Ativos padrão**

| Tipo | Ativos |
|---|---|
| Normal | `EURUSD`, `GBPUSD`, `USDJPY`, `AUDCAD`, `EURGBP` |
| OTC (sintético) | `EURUSD-OTC`, `GBPUSD-OTC`, `USDJPY-OTC`, `AUDCAD-OTC`, `EURGBP-OTC` |

**Fluxo resumido**

```
IQ Option API ──► mercado_iq.py ──► app.py (loop principal)
                                       ├── estrategia.py  → Decisao
                                       ├── risco.py       → Autorizacao
                                       ├── executor.py    → ordem WebSocket
                                       ├── registro.py    → SQLite
                                       └── grafico.py     → JSON → navegador
```

---

## 2. Arquitetura

### Camadas

```
┌─────────────────────────────────────────────────────────┐
│  rodar_iqoption_m5.py   (entry point CLI)               │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│  app.py  —  loop principal sequencial                    │
│  • fase 1: snapshot de todos os ativos                   │
│  • fase 2: _avaliar_ativo() para cada ativo              │
│  • fase 3: sleep anti-spinlock com retry inteligente     │
└──┬──────────┬──────────┬──────────┬──────────┬──────────┘
   │          │          │          │          │
   ▼          ▼          ▼          ▼          ▼
mercado  estrategia   risco    executor   grafico
_iq.py    .py         .py       .py        .py
```

### Threads

| Thread | Papel |
|---|---|
| Principal | Loop de avaliação sequencial (sem ThreadPoolExecutor) |
| `executor._processar` | Daemon por ordem: aguarda resultado sem bloquear o loop |
| `grafico-m5` | HTTP server do painel web (daemon) |
| `_grafico_rt_worker` | Atualiza candle em formação a cada 1 s (daemon) |
| `reconectar_se_necessario` | Teste de conexão a cada 60 s (daemon) |
| `_ia_candle` | Consulta IA em background ao fechar candle (daemon) |

### Modelos de dados (modelos.py)

```python
SnapshotMercado  # dados ao vivo: candles, payout, mercado_aberto, timestamp_servidor
Decisao          # sinal gerado: ativo, direcao, preco, candle_hora, motivo, detalhes
Autorizacao      # resultado do risco: permitida, motivo
ResultadoOrdem   # resultado gravado: id_ordem, lucro, resultado_bruto
OperacaoPendente # ordem sem resultado ainda (sobreviveu reinício)
ResumoRisco      # estado resumido: lucro, perdas, encerrado, etc.
EstadoPersistido # estado que persiste no SQLite entre reinícios
```

---

## 3. Módulos

### `mercado_iq.py`
Adaptador sobre `iqoptionapi`. Responsável por:
- Conexão e autenticação WebSocket
- `snapshot(ativo)` → `SnapshotMercado` com candles, payout e abertura
- `_obter_abertura_turbo()`: verifica se o ativo está disponível para negociação
  - **OTC**: usa apenas o campo `enabled` (ignora `is_suspended` que a IQ marca incorretamente)
  - **Normal**: usa `enabled AND NOT is_suspended`
- `comprar()` e `aguardar_resultado()` via `buyv3`
- Buffer de candles por ativo; candle em formação é `index[-1]`, último fechado é `index[-2]`

### `estrategia.py`
Indicadores e avaliação de sinais. Todas as avaliações usam o candle **fechado** (`iloc[-2]`), nunca o candle em formação.

Métodos principais:
- `calcular_indicadores(candles, ativo)` → DataFrame com indicadores. Cache invalidado quando `candles.index[-2]` muda.
- `avaliar_todas(ativo, indicadores)` → lista de `Decisao`. Avalia em `len(df) - 2` (último fechado).
- `sinais_historicos(ativo, candles)` → lista de `Decisao` para o gráfico (retroativo).
- `possivel_entrada(ativo, candles)` → alerta de próxima entrada provável.

Indicadores calculados: BB (Bollinger), EMA micro/macro, RSI, ATR, MACD, TendenciaMacro, suporte/resistência, Fibonacci.

### `risco.py`
Único ponto de autorização. Estado thread-safe com `threading.Lock`.

**Fluxo de autorização** (`avaliar` → `reservar`):
1. `_encerrado` → bloqueia tudo
2. `_ordens_abertas` contém o ativo → `ordem_ja_aberta`
3. Mercado fechado → `mercado_fechado`
4. Payout abaixo do mínimo → `payout_indisponivel`
5. `horario_bloqueado` → `horario_bloqueado`
6. Cooldown pós-ordem → `cooldown_pos_ordem`
7. Circuit breaker → `circuit_breaker`
8. Entrada fora da janela → `entrada_atrasada`
9. Permite → `autorizada`

**Ordens abertas**: `_ordens_abertas: set[str]` — um conjunto por ativo (não flag global), permitindo ordens simultâneas em ativos diferentes.

`registrar_resultado(lucro, ativo)` remove o ativo do conjunto e atualiza lucro/perdas.

### `executor.py`
Envia a ordem em thread daemon e aguarda resultado. Em caso de rejeição pela IQ (`buy_recusado`), chama `risco.cancelar_reserva(ativo)` para liberar o slot do ativo.

### `registro.py`
SQLite em `dados/iqoption_m5_practice.sqlite3`. Tabelas:
- `operacoes`: todas as ordens enviadas com status e lucro
- `decisoes`: todos os sinais avaliados (inclusive bloqueados)

Métodos úteis:
- `estado_hoje()` → `EstadoPersistido` para reinicializar o risco
- `stats_globais()` → contagem e winrate do dia separados por OTC / normal
- `operacoes_grafico(ativo)` → últimas operações para exibição no gráfico

### `grafico.py`
Serve um painel web via HTTP local (`127.0.0.1:8767`). Escreve JSONs atomicamente em `%LOCALAPPDATA%\IQOptionM5\grafico_web\iqoption_m5\`. A escrita atômica usa `os.replace()` para evitar que o navegador leia arquivo parcial.

### `alerta.py`
Detecta padrões de reversão intracandle (família "vela extrema"). **Não envia ordem**: apenas informa o usuário. Baseado na triagem walk-forward.

### `noticias.py`
Baixa o calendário ForexFactory semanalmente. Avisa quando há divulgação de alto/médio impacto nas próximas 20 minutos para as moedas do par. Ativos OTC não recebem aviso (preço sintético não reage a macro).

### `ia.py`
Análise contextual via API de IA. Roda em thread daemon por candle. O parecer pode bloquear a entrada se a IA discordar com confiança média/alta, exceto quando confirmação de notícia favorece o sinal.

### `recuperacao.py`
Ao reiniciar, busca ordens com `status='aberta'` no SQLite e consulta resultado na IQ. Ordens com mais de 10 minutos sem resposta viram `perda_tecnica`.

---

## 4. Loop principal e timing de velas

### Fluxo por iteração

```
while not encerrado:
    1. Snapshots de todos os ativos (sequencial)
    2. _avaliar_ativo() para cada ativo (sequencial)
    3. Cálculo do sleep anti-spinlock
    4. sleep()
```

### Janela de entrada

O bot acorda cerca de **5 segundos após o fechamento** de cada candle e avalia o sinal no candle que acabou de fechar. A janela de entrada padrão é de **45 segundos** (`entrada_max_segundos_no_candle`).

```
Candle A fecha ──► segundo 5 de B: avalia A, entra em B (janela: 0–45s)
                   segundo 290 de B: tick de pré-fechamento (fora da janela)
                   segundo 5 de C: avalia B, entra em C se houver sinal
```

### Sleep anti-spinlock

```python
seg_restantes = timeframe_segundos - seg_no_candle
if seg_restantes > 15:
    sono = seg_restantes - 10   # acorda 10s antes do fechamento
else:
    sono = seg_restantes + 5    # acorda 5s após o fechamento
sono = max(3.0, sono)
```

Se `_retry_ativos` não estiver vazio, o sleep é limitado a 5 s para permitir retry dentro da janela.

### Proteções de timing (bugs corrigidos)

**1. Slot preservado fora da janela**
Se o loop acorda no segundo 290 (fora dos 45 s), a avaliação retorna sem commitar `ultimo_candle_processado`, para não bloquear a avaliação correta no segundo 5 do próximo candle.

**2. Lag de buffer da API**
Quando o loop acorda no segundo 5, o stream WebSocket pode ainda não ter atualizado `index[-2]` para o candle recém-fechado. O bot detecta esse lag comparando `candle_fechado.timestamp()` com o timestamp esperado (`ts_servidor - seg_no_candle - timeframe`). Se houver lag, agenda retry em 5 s.

**3. Bloqueio temporário não commita slot**
Se o risco retorna `mercado_fechado` ou `payout_indisponivel` (bloqueios temporários), o slot `ultimo_candle_processado` **não é commitado**, e o ativo entra em `_retry_ativos`. O loop volta em 5 s para tentar novamente dentro da janela. Só commita quando há execução, bloqueio definitivo (IA, encerrado, par inválido), ou ausência de sinal.

### `_retry_ativos` — mecanismo de retry

```python
_retry_ativos: set[str]  # populado em _avaliar_ativo quando bloqueio é temporário

# No sleep section:
if _retry_ativos:
    sono = min(sono, 5.0)
_retry_ativos.clear()
```

---

## 5. Estratégias

### Arquitetura de avaliação

Todas as estratégias são avaliadas via `_avaliar_todas_estrategias(ativo, df, indice)` onde `indice = len(df) - 2` (último candle fechado). Cada estratégia retorna `Decisao | None`.

### Estratégias ativas

| Estratégia | Setup | Condições |
|---|---|---|
| `reversao_bollinger_rsi` | Bollinger reversal | Preço fura banda + RSI extremo + mercado lateral (ATR abaixo da mediana) |
| `pullback_tendencia_m5` | Pullback Fibonacci | Tendência EMA 50 + retração 38,2–61,8% + RSI 35–65 + candle de confirmação |
| `sr_rejeicao` | Suporte/resistência | Toque em pivô + candle de rejeição (mecha) |
| `macd_crossover` | MACD crossover | Cruzamento MACD × Signal com ATR válido |
| `pin_bar` | Pin bar | Mecha ≥ 2× corpo em nível de S/R |
| `fibo_sr_retracao` | Fibonacci + SR | Confluência Fibonacci e pivô na mesma região |

### Estratégias opcionais (desativadas por padrão)

| Campo config | Estratégia |
|---|---|
| `engulfing_sr_ativo` | Engulfing em S/R |
| `divergencia_rsi_ativo` | Divergência RSI |
| `bollinger_squeeze_ativo` | Bollinger Squeeze |

### Cache de indicadores

`calcular_indicadores` cacheia o DataFrame por ativo usando `candles.index[-2]` como chave. A cache é invalidada quando o último candle fechado muda ou quando o número de candles muda — garantindo que o dado em formação nunca "contamina" o cache.

---

## 6. Gerenciamento de risco

### Motivos de bloqueio

| Motivo | Tipo | Reseta slot? |
|---|---|---|
| `encerrado_hoje` | Definitivo | Sim |
| `ordem_ja_aberta` | Definitivo | Sim |
| `mercado_fechado` | **Temporário** | **Não** (retry) |
| `payout_indisponivel` | **Temporário** | **Não** (retry) |
| `horario_bloqueado` | Definitivo | Sim |
| `cooldown_pos_ordem` | Definitivo | Sim |
| `circuit_breaker` | Definitivo | Sim |
| `entrada_atrasada` | Definitivo | Preservado (fora da janela) |
| `meta_diaria_atingida` | Definitivo | Sim |
| `limite_diario` | Definitivo | Sim |
| `max_perdas` | Definitivo | Sim |
| `stop_diario` | Definitivo | Sim |
| `piso_banca_atingido` | Definitivo | Sim |
| `drawdown_maximo` | Definitivo | Sim |

### Ordens simultâneas

`_ordens_abertas: set[str]` permite múltiplas ordens simultâneas desde que em ativos diferentes. `EURUSD-OTC` aberta não bloqueia `GBPUSD-OTC`. A verificação é `ativo in _ordens_abertas`.

### Kill switch

Crie o arquivo `kill_switch.json` na raiz do projeto com `{"ativo": false}` para encerrar o loop imediatamente sem `Ctrl+C`.

---

## 7. Mercado OTC vs normal

### Problema histórico

A IQ Option marca `is_suspended=True` em ativos OTC mesmo quando estão disponíveis para negociação. Usar esse campo causava `mercado_fechado` em ativos que o painel da IQ mostrava como abertos.

### Solução atual

```python
# mercado_iq.py — _obter_abertura_turbo()
enabled = bool(detalhe.get("enabled", False))
is_otc_entry = nome.endswith("-op") or any(c.upper().endswith("-OTC") for c in candidatos)

if is_otc_entry:
    aberto = enabled          # OTC: ignora is_suspended
else:
    aberto = enabled and not bool(detalhe.get("is_suspended", False))
```

Para OTC, somente `enabled` é confiável. A rejeição real vem do `buyv3` da API.

### Stats separados por tipo

`registro.stats_globais()` retorna winrate e lucro do dia separados por `otc` e `normal`, exibidos no painel gráfico.

---

## 8. Gráfico em tempo real

### Arquitetura

```
app.py ──► grafico_fila (Queue) ──► thread escritora ──► JSON no disco
                                                           ▲
                _grafico_rt_worker (daemon, 1 s) ──────────┘
                (só atualiza OHLC do candle em formação)
```

O `_grafico_rt_worker` atualiza apenas o candle em formação (`sn.candles.iloc[-1]`) sem recalcular indicadores, evitando sobrecarga e falhas silenciosas.

### Tecnologia

- **Frontend**: HTML + CSS Grid + [LightweightCharts 4.1.3](https://tradingview.github.io/lightweight-charts/)
- **Temas**: CSS custom properties com `@media (prefers-color-scheme: dark)` + toggle via `data-theme`
- **Atualização**: polling JavaScript a cada 2 s nos JSONs servidos localmente

### Painel de stats (ENTRADAS HOJE)

Exibe contadores separados por OTC e mercado normal:
- Entradas / Wins
- Winrate (barra colorida: verde ≥ 60%, amarelo ≥ 45%, vermelho < 45%)
- Lucro acumulado do dia

### Toggle de ativos

Cada ativo pode ser desativado no painel via checkbox. A seleção é salva em `ativos_toggle.json` e lida pelo loop a cada iteração (cooldown de 3 s no cache).

---

## 9. Banco de dados

### Arquivo

`iqoption_m5/dados/iqoption_m5_practice.sqlite3` (separado por conta)

### Tabela `operacoes`

| Campo | Tipo | Descrição |
|---|---|---|
| `id_ordem` | TEXT PK | ID da IQ Option |
| `ativo` | TEXT | Ex: `GBPUSD-OTC` |
| `direcao` | TEXT | `call` / `put` |
| `setup` | TEXT | Nome da estratégia |
| `valor` | REAL | Entrada em USD |
| `payout` | REAL | Ex: 0.86 |
| `lucro` | REAL | Resultado líquido |
| `status` | TEXT | `aberta` / `finalizada` |
| `enviada_em` | TEXT | ISO datetime |
| `encerrada_em` | TEXT | ISO datetime |

### Tabela `decisoes`

Registra todos os sinais avaliados, incluindo os bloqueados, com motivo de autorização. Útil para análise de funil.

---

## 10. Configuração — referência completa

Arquivo: `iqoption_m5/config.py` — dataclass `Configuracao` (frozen).

### Conta e ativos

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `conta` | `"PRACTICE"` | `"PRACTICE"` ou `"REAL"` |
| `confirmo_conta_real` | `False` | Trava extra para conta real |
| `ativos` | 10 pares | Ativos monitorados |
| `pares_validados` | `()` | Se não vazio, só esses pares enviam ordens |

### Execução e valor

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `executar_ordens` | `True` | `False` = modo silencioso (só loga) |
| `valor_por_ordem` | `1.0` | Valor fixo em USD |
| `valor_percentual_banca` | `0.0` | Se > 0, substitui `valor_por_ordem` (ex: 0.03 = 3%) |
| `alavancagem_pyramid` | `False` | Próxima entrada = valor + lucro da última |
| `alavancagem_maximo` | `0.0` | Teto da alavancagem (0 = sem teto) |
| `banca_inicial` | `0.0` | Banca de referência para gestão |
| `piso_banca` | `0.0` | Para permanentemente se banca ≤ piso |

### Timing

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `timeframe_segundos` | `300` | M5 = 300, M1 = 60 |
| `expiracao_minutos` | `5` | Expiração das ordens |
| `entrada_max_segundos_no_candle` | `45` | Janela de entrada após abertura |
| `limite_candles` | `120` | Candles mantidos no buffer |

### Limites diários

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `max_operacoes_dia` | `9999` | Máximo de ordens por dia |
| `max_perdas_consecutivas` | `9999` | Perdas seguidas antes de parar |
| `stop_diario` | `-9999.0` | Prejuízo diário que para o bot |
| `parar_por_perdas` | `False` | Ativa bloqueio por perdas consecutivas |
| `parar_por_prejuizo` | `False` | Ativa stop diário |
| `meta_diaria` | `0.0` | Lucro que encerra o dia (0 = desativado) |

### Proteções avançadas

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `drawdown_maximo_percentual` | `0.0` | Para quando drawdown ≥ N% do pico |
| `circuit_breaker_max_perdas` | `0` | Perdas seguidas para ativar cooldown |
| `circuit_breaker_cooldown_minutos` | `60` | Duração do cooldown |
| `cooldown_pos_ordem_segundos` | `0.0` | Pausa após cada resultado |
| `horario_bloqueado` | `((0,0),(7,0))` | Janela UTC bloqueada |
| `payout_minimo` | `0.75` | Payout mínimo aceito |

### Indicadores

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `bb_periodo` | `20` | Período Bollinger Bands |
| `bb_desvio` | `2.0` | Desvios padrão das bandas |
| `rsi_periodo` | `14` | Período RSI |
| `rsi_sobrevendido` | `35.0` | Nível de sobrevenda |
| `rsi_sobrecomprado` | `65.0` | Nível de sobrecompra |
| `ema_micro_periodo` | `9` | EMA curta |
| `ema_macro_periodo` | `50` | EMA longa (define tendência) |
| `macd_fast` | `6` | MACD linha rápida |
| `macd_slow` | `16` | MACD linha lenta |
| `macd_signal` | `9` | MACD sinal |
| `atr_periodo` | `14` | ATR período |
| `atr_regime_janela` | `50` | Janela para mediana do ATR (filtro de regime) |
| `atr_max_multiplo_mediana` | `2.0` | Bloqueia se ATR > N × mediana |

---

## 11. Como rodar e testar

### Instalação

```bat
INSTALAR_DEPENDENCIAS.bat
```

### Iniciar bot (M5)

```bat
INICIAR_IQ_M5.bat
```

Ou via linha de comando:
```bash
python rodar_iqoption_m5.py
python rodar_iqoption_m5.py --m1        # timeframe M1
python rodar_iqoption_m5.py --real      # conta real (exige config extra)
```

### Testes automatizados

```bat
TESTAR_IQ_M5.bat
```

```bash
python -m pytest tests_m5/ -q           # 106 testes, ~12s
python -m pytest tests_m5/ -v           # verbose
```

Cobertura dos testes:
- Estratégia (sinais, indicadores, edge cases)
- Risco (autorização, limites, persistência)
- Executor (falha da API, resultado)
- Gráfico (montagem de dados, JSON)
- Mercado IQ (abertura OTC/normal, parsing de candles)
- Painel HTML (estrutura, elementos)
- Alerta (reversão curta, notícias)
- Backtest (simulação, relatório)
- Investigação (comparação de setups)
- Configuração (timeframe, validação)

### Ver resultados

```bat
VER_RESULTADOS_M5.bat
```

### Backtest

```bash
python rodar_backtest_m5.py
python rodar_backtest_m5.py --candles 20000
python rodar_backtest_m5.py --offline
python rodar_backtest_m5.py --offline --validar    # walk-forward
python rodar_backtest_m5.py --ativos EURUSD-OTC --payout 0.92
```

---

## 12. Bugs corrigidos — histórico

### Bug 1 — Slot commitado fora da janela de entrada

**Sintoma**: bot entrava "na vela seguinte" — avaliava candle A no segundo 290, commitava o slot, e quando o segundo 5 da próxima vela chegava, o slot de A já estava ocupado, então o bot avaliava B e entrava na C.

**Causa**: `ultimo_candle_processado[ativo] = candle_fechado` era executado incondicionalmente, mesmo quando `segundo_no_candle > entrada_max_segundos_no_candle`.

**Fix**: retorno antecipado antes do commit quando fora da janela — "slot preservado".

---

### Bug 2 — OTC marcado como mercado fechado incorretamente

**Sintoma**: `GBPUSD-OTC` e `AUDCAD-OTC` recebiam `risco=mercado_fechado` mesmo com o ativo disponível na plataforma.

**Causa**: `is_suspended=True` é usado pela IQ Option de forma inconsistente em ativos OTC mesmo quando negociáveis.

**Fix**: ativos OTC usam apenas `enabled` para determinar abertura; `is_suspended` é ignorado para OTC.

---

### Bug 3 — Ordens bloqueando outros ativos (global lock)

**Sintoma**: ordem aberta em `EURUSD-OTC` bloqueava `GBPUSD-OTC`.

**Causa**: `_ordem_aberta: bool` era global — qualquer ordem aberta bloqueava todos os ativos.

**Fix**: substituído por `_ordens_abertas: set[str]` — lock por ativo. `EURUSD-OTC` aberta não afeta `GBPUSD-OTC`.

---

### Bug 4 — Slot commitado em bloqueio temporário

**Sintoma**: `mercado_fechado` ou `payout_indisponivel` commitavam o slot, fazendo o bot pular a janela de entrada e só avaliar na próxima vela.

**Causa**: `ultimo_candle_processado[ativo] = candle_fechado` ocorria antes da avaliação de risco, independente do resultado.

**Fix**: slot só é commitado se houve execução ou bloqueio definitivo. Bloqueios temporários (`mercado_fechado`, `payout_indisponivel`) não commitam o slot e ativam retry em 5 s via `_retry_ativos`.

---

### Bug 5 — Lag de buffer da API (bug raiz do "vela seguinte")

**Sintoma**: bot acordava no segundo 5 do novo candle, mas o stream WebSocket ainda não tinha atualizado `index[-2]` para o candle recém-fechado. Avaliava o candle anterior (lag), commitava o slot incorreto, e só processava o candle correto na próxima iteração.

**Causa**: lag de stream da API entre o fechamento do candle e a atualização do buffer local.

**Fix**: verificação explícita de lag antes de qualquer avaliação. Se `candle_fechado.timestamp() < ts_esperado - 1`, o ativo entra em `_retry_ativos` e o loop retorna em 5 s.

---

### Bug 6 — Chart RT worker sobrecarregava a thread

**Sintoma**: gráfico não atualizava em tempo real.

**Causa**: `_grafico_rt_worker` chamava `estrategia.calcular_indicadores()` para cada ativo a cada segundo, falhando silenciosamente.

**Fix**: o worker só extrai `sn.candles.iloc[-1]` (OHLC do candle em formação) e atualiza apenas o último ponto do array de candles, sem recalcular indicadores.

---

## 13. Problemas comuns

| Mensagem / Sintoma | Causa provável | Solução |
|---|---|---|
| `mercado=fechado` | Ativo normal fora do horário | Usar aba OTC; verificar `horario_bloqueado` |
| `payout=indisponível` | API não confirmou payout ainda | Aguardar; o bot retenta automaticamente |
| `operacao_pendente_banco` | Ordem sem resultado do reinício anterior | Reiniciar pelo bat; bot recupera automaticamente |
| Gráfico não abre | Porta 8767 ocupada ou erro no servidor | Copiar URL do terminal no navegador |
| Aba travada / dados antigos | Snapshot falhou para esse ativo | Loop tenta novamente na próxima iteração |
| `entrou na vela seguinte` | Lag de buffer da API | Corrigido (Bug 5); verificar logs por `buffer desatualizado` |
| `buffer desatualizado (Ns atrás)` | Stream lento para esse ativo | Normal; bot retenta em 5 s automaticamente |
| `buyv3 recusado` | IQ Option rejeitou a ordem | Mercado pode estar suspenso no momento; reserva cancelada automaticamente |
| Bot parou com `piso_banca_atingido` | Banca caiu abaixo do piso configurado | Revisão manual necessária antes de reiniciar |
