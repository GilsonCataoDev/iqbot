# Plano de organização — Lab EMA e Monitor de Mercado

## Entrega implementada em 04/09/2026

- Lab EMA organizado em **Agora, Entradas, Estudos e Dados**, mantendo o gráfico
  sempre disponível e as preferências salvas no navegador.
- Monitor organizado em **Agora, Gráfico, Entradas, Estudos e Dados**, com filtros
  de tipo, resultado e ativo.
- Histórico do Monitor migrado automaticamente de JSON para SQLite; o JSON ficou
  apenas como espelho de compatibilidade.
- Saúde dos dados visível nos dois painéis e alerta do Monitor restrito a entrada
  validada, sem bip para estudo sombra.
- Exportação conjunta para XLSX por `EXPORTAR_DADOS_PAINEIS.bat`.

As fases de Groq rastreável, intervalo de confiança e promoção automática por
amostra continuam deliberadamente fora desta entrega: exigem validação futura e
não devem influenciar entradas antes de provar ganho fora da amostra.

## Objetivo

Transformar as telas em ferramentas de decisão simples, sem misturar entrada real,
operação PRACTICE, bloqueio e estudo sombra. O usuário deve responder em poucos
segundos: **há entrada? em qual ativo? por quê? onde entra? quando desiste?**

## Produtos e responsabilidades

### Lab EMA — execução e pesquisa controlada

- Opera somente em PRACTICE enquanto uma combinação não estiver aprovada.
- Mantém M5 e M15 como trilhas separadas.
- Mostra ordens enviadas, bloqueadas e sombras em áreas diferentes.
- Compara estratégia, ativo, direção, horário e timeframe.
- Nunca promove uma estratégia automaticamente por win rate de amostra pequena.

### Monitor de Mercado — auxílio de decisão

- Não envia ordens.
- Prioriza tendência, estrutura, zona de interesse, gatilho, invalidação e alvo.
- Mostra somente uma recomendação principal por ativo.
- Mantém Fibo, fluxo, ORB/FVG e liquidez como estudos independentes.
- Registra e simula cada plano para validar TP, SL, não execução e expiração.

## Linguagem única de estados

| Estado | Significado | Cor |
|---|---|---|
| ENTRAR | Todos os requisitos operacionais foram satisfeitos | Verde |
| PREPARAR | Preço aproximando da zona; ainda falta gatilho | Azul |
| AGUARDAR | Contexto existe, mas preço está longe | Cinza |
| BLOQUEADO | Havia setup, mas uma proteção impediu a entrada | Vermelho |
| ESTUDO | Sinal sombra; nunca deve parecer ordem | Amarelo |
| ENCERRADO | Resultado já conhecido | Verde/vermelho pelo resultado |

“Sinal” deixa de ser usado sozinho. Toda ocorrência deve dizer **sinal de estudo**,
**entrada autorizada**, **ordem enviada** ou **operação encerrada**.

## Estrutura comum das duas telas

### 1. Agora

- Faixa superior com horário BRT, conexão, conta e atualização.
- Ranking de ativos: ENTRAR primeiro, depois PREPARAR, AGUARDAR e BLOQUEADO.
- Cartão principal com direção, timeframe, validade e qualidade da leitura.
- Plano objetivo: zona de entrada, gatilho, invalidação, TP/expiração e risco.
- Checklist curto com no máximo cinco condições.
- Aviso sonoro apenas ao mudar para PREPARAR ou ENTRAR.

### 2. Gráfico

- Candles vivos e escala estável.
- Camadas independentes: EMAs, S/R, Fibo, entradas, sombras e bloqueios.
- Padrão limpo: sombras e bloqueios desligados ao abrir.
- Fibo ancorada na perna estrutural, com origem e extremo identificados.
- Entrada, stop/invalidação e alvo usam cores e nomes iguais nos dois sistemas.
- Clique numa marca abre o dossiê correspondente sem fechar sozinho.

### 3. Entradas

- Separar “abertas”, “encerradas”, “falharam no envio” e “não executadas”.
- Filtros por data, ativo, timeframe, setup, direção, horário e resultado.
- Cada linha mostra: horário BRT, preço, expiração/TP-SL, resultado, lucro e motivo.
- Dossiê preserva indicadores, candle, contexto, latência e bloqueios daquele momento.
- Exportação para XLSX; o banco continua sendo a fonte oficial.

### 4. Estudos

- Uma aba por setup, sem misturar resultados.
- M5 e M15 obrigatoriamente separados.
- Mostrar amostra, wins, losses, equals, desconhecidos, WR, lucro/EV e intervalo de confiança.
- Comparar sombra versus ordem PRACTICE para detectar viés de seleção e execução.
- Selo de maturidade: INSUFICIENTE, OBSERVAR, CANDIDATA ou APROVADA.

### 5. Dados e saúde

- Total recebido, salvo, enviado, resolvido e pendente.
- Último candle por ativo e atraso da atualização.
- Ordens sem resultado, falhas de envio e registros legados sem timeframe.
- Versão da estratégia/configuração associada a toda decisão.
- Botão de diagnóstico gera relatório sem alterar ordens ou resultados.

## Tela específica do Lab EMA

### Resumo operacional

- P&L PRACTICE do dia, ordens abertas e exposição atual.
- Duas abas fixas: **M5** e **M15**.
- Cartões de setup mostram resultado real e sombra em colunas diferentes.
- Histórico padrão exibe somente ordens enviadas; sombras aparecem ao ativar “Pesquisa”.

### Dossiê de entrada

- Por que entrou: toque/rejeição, alinhamento EMA, RSI, tendência maior e horário.
- Por que poderia falhar: entrada atrasada, mercado lateral, candle esticado,
  conflito de direção, notícia, payout ou obstáculo próximo.
- Captura das últimas velas e dos valores dos indicadores no instante do sinal.
- Resultado explicado como associação observada, nunca como causa comprovada.

## Tela específica do Monitor de Mercado

### Cartão de decisão

- **Viés:** compra, venda ou neutro.
- **Estrutura:** tendência/range e último topo/fundo relevante.
- **Localização:** suporte, resistência, Fibo e distância em ATR.
- **Plano:** esperar zona, exigir gatilho, invalidar em X e buscar Y.
- **Notícia:** sem risco, aproximação, janela bloqueada ou resultado publicado.

### Estudos do Monitor

- Fibo, fluxo, ORB/FVG e liquidez ficam em cartões recolhidos.
- Apenas o melhor estudo do ativo aparece no resumo; os demais ficam na aba Pesquisa.
- O simulador distingue: entrada não tocada, TP primeiro, SL primeiro,
  mesma vela ambígua, expiração e posição ainda aberta.
- Resultados nunca são somados entre métodos diferentes.

## Contrato único de dados

Todo evento deve possuir:

- `evento_id`, `origem` (lab/monitor) e `modo` (practice/sombra/leitura);
- ativo, timeframe, setup, direção e versão da configuração;
- horário UTC armazenado e horário BRT apenas para exibição;
- candle do sinal, preço observado, entrada, invalidação/SL, alvo/TP e expiração;
- estado da decisão, motivo principal e checklist;
- ID da ordem quando enviada;
- resultado, lucro ou resultado em R, além da origem do resultado;
- snapshot dos indicadores e contexto de notícia.

O Monitor deve migrar o histórico JSON para SQLite. JSON permanece apenas como
arquivo de atualização da página; XLSX é exportação, não fonte de verdade.

## Uso de IA/Groq

- A matemática continua local e determinística.
- O Groq recebe somente candidatos calculados e responde: ACEITAR, REJEITAR ou INCERTO.
- A resposta deve citar conflitos: estrutura, entrada esticada, alvo bloqueado,
  notícia ou confluência insuficiente.
- A IA nunca inventa preços, altera resultado, envia ordem ou promove estratégia.
- Falha/timeout da IA não bloqueia a atualização da página.

## Plano de implementação

### Fase 1 — confiança nos dados

1. Definir o contrato único e a versão dos eventos.
2. Separar resultados reais, sombras e estudos nas consultas.
3. Corrigir registros legados com timeframe zero sem inventar informação.
4. Migrar o histórico do Monitor de JSON para SQLite.
5. Criar testes de reconciliação entre decisão, ordem e resultado.

**Aceite:** totais da tela conciliam com o banco e nenhum desconhecido vira loss.

### Fase 2 — navegação e visual

1. Criar o mesmo cabeçalho, ranking e cores nos dois sistemas.
2. Implementar abas Agora, Gráfico, Entradas, Estudos e Dados.
3. Separar M5/M15 e real/sombra no Lab.
4. Recolher estudos secundários no Monitor.
5. Tornar o layout responsivo e persistir filtros do usuário.

**Aceite:** uma entrada e seu motivo são encontrados em até três cliques.

### Fase 3 — dossiê e aprendizado

1. Salvar snapshot completo do momento da decisão.
2. Exibir sequência sinal → envio → vencimento → resultado.
3. Adicionar comparações por ativo, horário, setup e contexto.
4. Gerar XLSX com abas Resumo, Entradas, Sombras, Estudos e Problemas.
5. Medir diferenças entre sombra e execução PRACTICE.

**Aceite:** toda entrada pode ser auditada sem depender do gráfico atual.

### Fase 4 — alertas e Groq

1. Alertas distintos para PREPARAR, ENTRAR e BLOQUEADO.
2. Silêncio por ativo/setup para evitar repetição.
3. Segunda opinião Groq somente quando existe plano calculado.
4. Salvar resposta, modelo, horário e latência da IA.
5. Medir se o filtro da IA melhora resultados fora da amostra.

**Aceite:** IA opcional, rastreável e incapaz de executar operações.

### Fase 5 — promoção de estratégias

1. Definir amostra mínima antes de exibir porcentagem como confiável.
2. Validar por ativo, timeframe e sessão em período futuro.
3. Considerar payout, falha de envio e latência.
4. Aprovar poucas combinações; demais continuam em sombra.
5. Criar checklist obrigatório antes de qualquer modo REAL.

**Aceite:** nenhuma estratégia vai para REAL apenas por resultado agregado do Lab.

## Situação dos dados em 04/09/2026

### Lab EMA

- 1.393 decisões, 222 tentativas de operação e 1.012 simulações sombra.
- 210 operações finalizadas e 12 falhas de envio.
- M5 PRACTICE: EMA9/20 61,0% em 136 operações; EMA9/21 RSI intravela
  70,6% em 17; EMA9/21 RSI fechado 63,6% em 11.
- M15 PRACTICE está abaixo: 42,9% EMA9/20 e 44,4% intravela; amostras menores.
- As sombras M5 ficam perto de 45–48%, bem abaixo das ordens selecionadas.
  Essa diferença exige auditoria antes de concluir que a seleção melhora o WR.

### Monitor de Mercado

- 238 eventos: 105 Fibo, 69 fluxo, 37 falso rompimento, 3 liquidez e 1 ORB/FVG.
- Apenas 4 registros são entradas validadas; os demais são estudos.
- Simulações misturadas hoje: 38 TP, 122 SL, 47 pendentes, 15 expiradas,
  8 não executadas e 8 ambíguas.
- A tela precisa mostrar essas amostras separadas por método para evitar uma
  conclusão errada sobre o conjunto.

## Ordem recomendada

Começar pela **Fase 1**. Melhorar primeiro o visual sem reconciliar os dados só
deixaria números conflitantes mais bonitos.
