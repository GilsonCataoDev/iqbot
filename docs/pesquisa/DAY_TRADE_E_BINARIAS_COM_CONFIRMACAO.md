# Day trade e binárias com confirmação: hipóteses testáveis

> Pesquisa em 10/09/2026. Este guia organiza setups para **estudo manual e
> Practice**. Não é recomendação de investimento, nem promessa de lucro ou de
> taxa de acerto.

## Decisão prática

Para o Monitor Auxiliar, começar com **uma regra por vez**: contexto no M15,
zona objetiva e confirmação em candle fechado no M5. A melhor hipótese inicial
para o estudo de Fibonacci é: **impulso M15 -> retração 50%--61,8% -> rejeição
ou engolfo M5 a favor do impulso -> binária de 15 minutos**. A expiração é uma
hipótese de teste (três candles M5), não uma garantia.

Uma marcação de Fibo não é entrada. Ela só define a área onde o preço merece
atenção. O candle de confirmação decide se há sinal; sem ele, a tela deve dizer
`AGUARDAR`.

## A matemática que vem antes da estratégia

Em binárias, o resultado depende do preço no vencimento: paga um valor fixo se
a proposição for verdadeira e, em geral, perde-se o valor investido se for falsa.
É a estrutura "tudo ou nada" descrita pela [CFTC/SEC](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/fraudadv_binaryoptions.html).

Se a perda é 1 stake e o payout líquido é `p`, o acerto mínimo para não perder é:

`breakeven = 1 / (1 + p)`

| Payout líquido | Acerto mínimo |
|---:|---:|
| 70% | 58,82% |
| 80% | 55,56% |
| 85% | 54,05% |
| 90% | 52,63% |

Portanto, um setup não é aprovado porque "acerta mais de 50%". Ele precisa
superar o breakeven **no mesmo ativo, horário, feed, expiração e payout**. A
ESMA apontou retorno esperado estruturalmente negativo e conflitos de interesse
como riscos das binárias de varejo; isso é uma razão para tratá-las como produto
de risco elevado, não como renda previsível. [ESMA — análise de intervenção](https://www.esma.europa.eu/sites/default/files/library/esma35-43-1000_additional_information_on_the_agreed_product_intervention_measures_relating_to_contracts_for_differences_and_binary_options.pdf)

## Os quatro setups para comparar

| Setup | Contexto | Zona | Confirmação exigida | Uso em binária | Principal risco |
|---|---|---|---|---|---|
| **Fibo: correção a favor** | M15 com EMA 9 acima/abaixo da 21 e impulso >= 1,5 ATR | Retração 50%--61,8% do último impulso manual | M5 fecha com rejeição ou engolfo a favor; RSI não esticado | M5: 15 min; M15: 30 min, ambos como hipótese | Marcar o impulso errado ou entrar antes da reação |
| **Rompimento + reteste** | Range/S&R M15 bem definido | Nível rompido | Fechamento M15 fora; M5 volta ao nível e rejeita na direção do rompimento | M5: 15 min, somente após o reteste | Falso rompimento e notícia; não entrar no primeiro rompimento |
| **S/R + varredura** | Pivô anterior M15/M30 | Máxima ou mínima anterior | Varre o nível, fecha de volta no range e o candle seguinte rompe a vela de retorno | M5: 15 min após a confirmação | Confundir pavio comum com varredura; alvo/expiração curtos demais |
| **VWAP + tendência** | Só em mercado com volume/tick volume consistente e sessão definida | VWAP ou banda de desvio | M5 fecha retomando a VWAP e EMA 9/21 concorda | Manter em sombra até dados próprios | Na IQ, tick volume e preço de liquidação podem não reproduzir o mercado centralizado |

### 1. Fibo: correção a favor — o primeiro estudo

**CALL**:

1. No M15, marque o impulso do fundo ao topo; EMA 9 acima da 21 e ambas com inclinação de alta.
2. Espere o preço descer para 50%--61,8%. Não compre em 23,6% nem no meio do impulso.
3. No M5, aceite somente fechamento de rejeição de baixa (pavio inferior relevante e fechamento comprador) **ou** engolfo de alta.
4. Entre no começo da vela seguinte, no máximo 30 segundos após o fechamento M5, se o preço ainda estiver na zona.
5. Para binária, registre 15 min de expiração; para CFD/forex, o stop fica além do extremo da zona e o alvo no topo anterior — são operações diferentes.

Para **PUT**, inverta: topo -> fundo, tendência de baixa, correção para cima até
50%--61,8%, e rejeição/engolfo baixista no M5.

O estudo de Shanaev e Gibson encontrou evidência de poder preditivo em ações dos
EUA próximas a suporte/resistência de Fibonacci. É um *working paper*, em ações,
e não mede binárias, M5, IQ ou payout; ele só justifica testar a hipótese de forma
objetiva. [SSRN — *Can Returns Breed Like Rabbits?*](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4212430)

### 2. Rompimento + reteste

É uma forma de evitar perseguir uma vela forte. A regra não é “rompeu, entra”; é
“rompeu, **voltou**, respeitou e fechou confirmando”.

- **CALL:** M15 fecha acima da resistência; M5 testa o nível por cima; candle M5
  fecha comprador, sem fechar de volta dentro do range.
- **PUT:** inverso.
- Descarte se houver dois ou mais retestes sem reação, candle contrário muito
  forte ou notícia de alto impacto próxima.

Médias móveis e regras de rompimento têm evidência histórica em um índice de
ações, mas isso não se transfere automaticamente para pares, intraday ou
binárias. [Brock, Lakonishok & LeBaron (1992)](https://doi.org/10.1111/j.1540-6261.1992.tb04681.x)

### 3. Suporte/resistência + varredura de liquidez

O objetivo é entrar **depois** de o preço falhar em sustentar o rompimento do
nível, e não adivinhar o topo/fundo.

- Marque uma máxima/mínima M15/M30 que já tenha sido respeitada.
- O preço atravessa com pavio; depois fecha de volta para dentro do range.
- A vela seguinte no M5 precisa romper a máxima/mínima da vela de retorno.
- Só então há candidato. Se o candle volta a fechar fora do range, a hipótese de
  reversão falhou.

Esse setup deve ser tratado como classificação objetiva de preço, não como prova
de que “liquidez institucional” foi observada. A vantagem, se existir, precisa ser
medida no feed e vencimento usados.

### 4. VWAP como filtro, não como sinal isolado

VWAP é uma média ponderada por volume e é usada como benchmark de execução em
mercados centralizados. [SEC — discussão de VWAP](https://www.sec.gov/Archives/edgar/data/1132327/000119312505112007/ddefa14a.htm)

No monitor, use como contexto: acima da VWAP favorece procurar CALLs em
pullbacks; abaixo favorece PUTs. Ainda exige zona + candle confirmado. Em forex
spot não há volume centralizado; e em binária da plataforma o valor final pode
seguir feed próprio. Por isso VWAP deve ficar em **sombra** até o forward test.

## O que conta como confirmação de candle

| Padrão fechado | Serve para | Regra objetiva inicial | Não serve quando |
|---|---|---|---|
| Rejeição de alta | CALL em suporte/Fibo | pavio inferior >= corpo e fechamento no terço superior do range | candle é pequeno demais (< 0,2 ATR) |
| Rejeição de baixa | PUT em resistência/Fibo | pavio superior >= corpo e fechamento no terço inferior | candle é pequeno demais (< 0,2 ATR) |
| Engolfo de alta | CALL após correção | corpo comprador engloba o corpo anterior vendedor | ocorre fora de zona ou contra M15 |
| Engolfo de baixa | PUT após correção | inverso | ocorre fora de zona ou contra M15 |
| Vela forte | Continuação, nunca entrada sozinha | corpo >= 60% do range e >= 0,35 ATR | já percorreu grande parte do caminho até o vencimento |

Candles isolados são frágeis. Há pesquisa que encontra informação incremental em
alguns padrões, mas ela ressalta o problema da subjetividade e não prova uma
regra universal de lucro. [Lo, Mamaysky & Wang (2000)](https://www.nber.org/papers/w7613)

## Regras específicas para binárias

1. **Só candle fechado.** Intravela serve para observar, não para confirmar.
2. **Hora de entrada:** registrar o atraso entre o fechamento e a ordem. Se passar
   de 30 s no M5 ou 60 s no M15, marcar como entrada atrasada e não comparar como
   se fosse o mesmo setup.
3. **Expiração é parte da estratégia:** M5 + 15 min e M15 + 30 min são duas
   campanhas distintas. Não misturar os resultados.
4. **Payout é parte do resultado:** salvar payout mostrado no momento da entrada;
   sem ele, não existe cálculo de edge.
5. **Mercado normal e OTC separados:** não transportar conclusão de um para o
   outro. A CFTC alerta que plataformas online podem ter risco de preço,
   liquidação e fraude; conferir situação regulatória e condições da plataforma é
   parte do risco operacional. [CFTC — alerta](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/unregistered_binary_options_websites.htm)

## Como escolher uma estratégia sem “matar” uma boa cedo demais

Um backtest negativo de uma configuração específica não demonstra que todo uso
de Fibo é impossível. Demonstra apenas que **aquela definição, ativos,
expiração e período** não passou. O caminho correto é congelar uma definição e
compará-la contra uma base, sem mudar cinco filtros de uma vez.

1. Rodar as quatro hipóteses em **sombra**, sem ordem, por ativo e expiração.
2. Depois de 100 sinais resolvidos por célula, calcular lucro líquido e intervalo
   de confiança do acerto; exigir o breakeven do payout.
3. Separar treino cronológico, validação e último período fora da amostra.
4. Promover apenas a combinação que superar a base em dois blocos fora da amostra
   e manter resultado líquido positivo.
5. Só depois observar em Practice; conta real nunca é a etapa de descoberta.

Os estudos de análise técnica não convergem para uma receita imutável. Até onde
há evidência, parâmetros e resultados variam por ativo, período e custo. Em FX,
traders profissionais historicamente combinam análise técnica e fundamental em
horizontes curtos; isso descreve uso, não valida lucro. [Taylor & Allen (1992)](https://doi.org/10.1016/0261-5606(92)90048-3)

## Configuração recomendada agora no Monitor Auxiliar

Ative apenas **Fibo: correção a favor** por uma sessão e use:

- M15 para marcar o impulso;
- M5 para ler o candle;
- EMA 9/21 como direção, não entrada;
- RSI apenas para evitar um preço já esticado;
- alerta somente depois do candle M5 fechado;
- expiração de 15 min registrada como hipótese a validar.

O próximo passo é deixar o monitor registrar cada sinal desse combo com
`ativo + hora + Fibo + candle + expiração + payout + resultado`, para comparar
Fibo contra os outros três setups sem adivinhação.
