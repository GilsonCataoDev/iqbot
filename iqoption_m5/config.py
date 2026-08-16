import os
from dataclasses import dataclass, field, replace
from pathlib import Path

TIMEFRAMES_SUPORTADOS = {60: "M1", 300: "M5"}


@dataclass(frozen=True)
class Configuracao:
    email: str = ""
    senha: str = ""
    ativos: tuple[str, ...] = (
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "EURUSD-OTC",
        "GBPUSD-OTC",
        "USDJPY-OTC",
    )
    conta: str = "PRACTICE"
    confirmo_conta_real: bool = False  # trava extra: precisa ser True de propósito pra operar dinheiro real
    banca_inicial: float = 0.0
    piso_banca: float = 0.0  # 0 = desativado; se >0, para tudo quando banca_inicial+lucro_total <= piso
    # Seguro por padrao: Configuracao() apenas monitora. Perfis que enviam
    # ordens precisam ativar isso explicitamente.
    executar_ordens: bool = False
    executar_estrategias_nao_validadas: bool = False  # pullback/bollinger; False = so registra, so reversao_candle executa
    pares_validados: tuple[str, ...] = ()  # vazio = todos os `ativos` podem executar; se preenchido, so esses arriscam dinheiro real
    confiar_resultado_automatico: bool = True  # False = sempre perda tecnica (historico da IQ ja mentiu "win")
    verificar_resultado_por_candle: bool = True  # True = calcula resultado pelo candle, não confia no historico da IQ
    valor_por_ordem: float = 1.0
    valor_percentual_banca: float = 0.0  # 0 = desativado; se >0, entrada = banca_atual * este percentual (ex: 0.03 = 3%)
    alavancagem_pyramid: bool = False  # proxima entrada = valor_por_ordem + lucro da ultima operacao (min 0)
    alavancagem_maximo: float = 0.0  # 0 = sem teto; senao, nunca deixa a entrada passar disso
    timeframe_segundos: int = 300
    limite_candles: int = 120
    expiracao_minutos: int = 5
    intervalo_loop_segundos: float = 2.5
    entrada_max_segundos_no_candle: int = 15
    min_segundos_ate_expiracao: int = 120  # não entra se restar < 2 min até o mark de 5min
    marcacao_tolerancia_atr: float = 2.0   # reversão: cancela se preço afastou > N×ATR do nível
    abrir_grafico: bool = True
    porta_grafico: int = 8767
    alerta_preco_tolerancia_atr: float = 0.25
    alerta_rsi_margem: float = 5.0

    bb_periodo: int = 20
    bb_desvio: float = 2.0
    rsi_periodo: int = 14
    rsi_sobrevendido: float = 35.0
    rsi_sobrecomprado: float = 65.0
    bollinger_aceitar_tendencia: bool = True
    atr_periodo: int = 14
    ema_micro_periodo: int = 9
    ema_macro_periodo: int = 50
    macro_slope_janela: int = 10
    slope_limiar_atr: float = 0.1
    atr_regime_janela: int = 50
    atr_max_multiplo_mediana: float = 2.0

    pullback_janela: int = 50
    pullback_pivo_raio: int = 2
    pullback_tolerancia_atr: float = 0.25
    pullback_amplitude_min_atr: float = 1.5
    pullback_fib_min: float = 0.382
    pullback_fib_max: float = 0.618
    pullback_rsi_min: float = 35.0
    pullback_rsi_max: float = 65.0
    pullback_slope_forte_multiplo_atr: float = 0.5  # bloqueia pullback quando |slope EMA_Macro| > N*ATR

    # --- Melhoria §8: SR Rejeição ---
    # Toggle: exige RSI sobrevendido (CALL) ou sobrecomprado (PUT) no candle que toca o nível.
    # Off por padrão — ativar após validar que melhora WR no backtest.
    sr_rejeicao_rsi_filtro: bool = False
    # 0=desativado; >0=corpo mínimo do candle de rejeição em múltiplos de ATR.
    sr_rejeicao_corpo_min_atr: float = 0.0

    # --- Melhoria §9: Pullback ---
    # 0=desativado; >0=corpo mínimo do candle de confirmação em múltiplos de ATR.
    pullback_confirmacao_corpo_atr: float = 0.0
    # Toggle: exige RSI sobrevendido no recuo (CALL) ou sobrecomprado (PUT).
    pullback_recuo_rsi_filtro: bool = False

    macd_fast: int = 6
    macd_slow: int = 16
    macd_signal: int = 9

    # Estratégias — flags globais (True = ativo por padrão)
    macd_crossover_ativo: bool = True
    sr_rejeicao_ativo: bool = True
    fibo_sr_retracao_ativo: bool = True
    reversao_candle_ativo: bool = True     # inclui reversao_confluencia

    # Estratégias opcionais — desativadas por padrão até validação em PRACTICE
    engulfing_sr_ativo: bool = False
    divergencia_rsi_ativo: bool = False
    bollinger_squeeze_ativo: bool = False
    pin_bar_sr_ativo: bool = False  # False = nunca entra em pin_bar_sr
    macd_crossover_time_ativo: bool = False        # variante: só 00h-06h UTC
    macd_crossover_tendencia_ativo: bool = False   # variante: só com TendenciaMacro alinhada
    divergencia_rsi_time_ativo: bool = False       # variante: só 11h-13h UTC
    divergencia_rsi_tendencia_ativo: bool = False  # variante: RSI extremo < 25 / > 75
    pullback_ativo: bool = False    # False = bloqueia pullback isolado (pullback_confluencia continua)
    divergencia_rsi_janela_pivos: int = 5
    bollinger_squeeze_percentil_janela: int = 20
    bollinger_squeeze_min_corpo_atr: float = 0.5

    meta_diaria: float = 0.0  # 0 = desativado; se >0, encerra o dia ao atingir esse lucro
    payout_minimo: float = 0.80
    bloquear_otc_real: bool = True
    cache_mercado_segundos: int = 60
    max_operacoes_dia: int = 5
    max_perdas_consecutivas: int = 3
    stop_diario: float = -5.0
    parar_por_perdas: bool = True
    parar_por_prejuizo: bool = True
    cooldown_pos_ordem_segundos: float = 0.0  # 0 = desligado; >0 = bloqueia nova entrada por N segundos após resultado
    cooldown_pos_ordem_por_ativo_candles: int = 0  # 0 = desligado; >0 = bloqueia N candles após ordem no mesmo ativo
    filtro_candle_entrada_atr: float = 0.0  # 0 = desligado; >0 = cancela se candle N+1 abre > N×ATR contra o sinal
    bloquear_noticia_alto_impacto: bool = False  # bloqueia entrada em janela de notícia HIGH (ativos reais)
    ia_como_filtro: bool = True  # True = IA bloqueia sinais contrários (media/alta confiança); False = só exibe parecer
    ia_filtro_exceto_setups: tuple[str, ...] = ("sr_rejeicao",)  # setups excluídos do bloqueio de IA mesmo com ia_como_filtro=True

    # --- Horário bloqueado (UTC) ---
    # Evita operar em janelas de baixa liquidez / mercado OTC suspenso.
    # None = sem restrição. Tupla ((h_inicio, m_inicio), (h_fim, m_fim)).
    horario_bloqueado: tuple | None = None  # sem restrição de horário

    # --- Filtro de horário por setup (UTC) ---
    # Mapeia nome do setup para tupla (hora_inicio, hora_fim) em UTC (0-23, inclusivo).
    # Setup só entra se hora_utc atual estiver dentro da janela.
    # Exemplo: {"sr_rejeicao": (7, 14), "macd_crossover": (0, 12)}
    # None = sem restrição por setup.
    # --- §11: Janela de entrada por setup ---
    # None = usa entrada_max_segundos_no_candle global para todos os setups.
    # Ex: {"sr_rejeicao": 30, "pullback_confluencia": 180}
    # Setups de reversão intra-candle devem ter janela menor que setups confirmados no close.
    janela_entrada_por_setup: dict | None = None

    horario_por_setup: dict | None = field(default_factory=lambda: {
        # Janelas boas (UTC) derivadas do histórico de 4 dias em PRACTICE.
        # Atualizar conforme o banco crescer.
        "macd_crossover":          (0, 12),   # 00h-12h bom; 13h-23h ruim (13h=22%, 23h=17%)
        "sr_rejeicao":             (7, 20),   # 00h=23% WR; 04h=38%; bom de 07h em diante
        "divergencia_rsi":         (5, 13),   # 05h-13h bom; 21h-23h ruins (25-38%)
        "fibo_sr_retracao":        (0, 12),   # 11h top; 01h e 10h ruins — manhã UTC segura
        "reversao_candle":         (5, 12),   # 08h-09h ótimo; 11h=0%
        "bollinger_squeeze":       (8, 16),   # 13h=100%; 01h=25%
        "pullback_confluencia":    (4, 22),   # 07h=100%; só evita 23h=0%
        "pullback":                (4, 18),   # 16h=100%; noite sem dados suficientes
        "reversao_bollinger_rsi":  (5, 15),   # poucos dados — janela conservadora
        "reversao_confluencia":    (5, 15),   # poucos dados — janela conservadora
    })

    # --- Filtro de regime de mercado ---
    # False = sem filtro (padrão). True = aplica regimes_por_setup antes de executar.
    filtro_regime_ativo: bool = False

    # Mapeia nome do setup para tupla de regimes permitidos (Regime.name).
    # Ignored quando filtro_regime_ativo=False.
    # Exemplo: {"sr_rejeicao": ("LATERAL",), "pullback": ("TENDENCIA_ALTA", "TENDENCIA_BAIXA")}
    regimes_por_setup: dict | None = field(default_factory=lambda: {
        "sr_rejeicao":             ("LATERAL",),
        "macd_crossover":          ("TENDENCIA_ALTA", "TENDENCIA_BAIXA"),
        "divergencia_rsi":         ("LATERAL", "TENDENCIA_ALTA", "TENDENCIA_BAIXA"),
        "reversao_bollinger_rsi":  ("LATERAL",),
        "reversao_candle":         ("LATERAL", "TENDENCIA_ALTA", "TENDENCIA_BAIXA"),
        "reversao_confluencia":    ("LATERAL", "TENDENCIA_ALTA", "TENDENCIA_BAIXA"),
        "pullback_confluencia":    ("TENDENCIA_ALTA", "TENDENCIA_BAIXA"),
        "pullback":                ("TENDENCIA_ALTA", "TENDENCIA_BAIXA"),
        "fibo_sr_retracao":        ("TENDENCIA_ALTA", "TENDENCIA_BAIXA"),
        "bollinger_squeeze":       ("LATERAL",),
    })

    # --- Drawdown máximo percentual ---
    # Para o bot quando (banca_pico - banca_atual) / banca_pico >= este valor.
    # 0.0 = desativado.
    drawdown_maximo_percentual: float = 0.0

    # --- Circuit breaker ---
    # Após N perdas seguidas, bloqueia por cooldown_minutos. 0 = desativado.
    circuit_breaker_max_perdas: int = 0
    circuit_breaker_cooldown_minutos: int = 60

    # --- Slippage monitor ---
    slippage_alerta_pips: float = 0.0003  # alerta se |exec - sinal| > este valor

    # --- Watchdog ---
    watchdog_timeout_minutos: float = 15.0  # alerta se nenhum ativo processado em X minutos

    # --- Retry mercado fechado (OTC) ---
    max_retry_mercado_fechado_segundos: float = 30.0  # desiste de retry de OTC fechado após N segundos; 0=sem limite

    # --- Keepalive WebSocket ---
    keepalive_intervalo_segundos: float = 30.0  # ping periódico para manter WebSocket ativo; 0=desativado

    pasta_dados: Path = Path(__file__).resolve().parent / "dados"

    @property
    def banco_sqlite(self) -> Path:
        # Banco separado por conta: dado de PRACTICE nao pode se misturar com
        # REAL, senao o piso de banca e o winrate ficam calculados errado.
        return self.pasta_dados / f"iqoption_m5_{self.conta.lower()}.sqlite3"

    @property
    def rotulo_timeframe(self) -> str:
        return TIMEFRAMES_SUPORTADOS.get(self.timeframe_segundos, f"{self.timeframe_segundos}s")

    def validar(self) -> None:
        if self.conta not in ("PRACTICE", "REAL"):
            raise RuntimeError("CONTA deve ser PRACTICE ou REAL.")
        if self.conta == "REAL" and not self.confirmo_conta_real:
            raise RuntimeError(
                "Conta REAL exige confirmo_conta_real=True de propósito. "
                "Use configuracao_real_m5() ou defina o campo explicitamente — "
                "isso existe pra ninguém ligar dinheiro real sem querer."
            )
        if self.alavancagem_pyramid and self.alavancagem_maximo <= 0:
            raise RuntimeError(
                "alavancagem_pyramid exige alavancagem_maximo > 0 configurado — "
                "sem teto, uma sequencia de vitorias pode fazer uma unica entrada "
                "ficar enorme e uma perda seguinte comer a banca inteira."
            )
        if self.conta == "REAL" and self.piso_banca <= 0:
            raise RuntimeError(
                "Conta REAL exige piso_banca > 0 configurado (limite que para o bot "
                "de vez se a banca cair demais)."
            )
        if self.timeframe_segundos not in TIMEFRAMES_SUPORTADOS:
            raise RuntimeError("Timeframe suportado: M1 (60s) ou M5 (300s).")
        if self.expiracao_minutos * 60 < self.timeframe_segundos:
            raise RuntimeError("A expiração não pode ser menor que um candle do timeframe.")
        if not 0 < self.entrada_max_segundos_no_candle < self.timeframe_segundos:
            raise RuntimeError("A janela de entrada deve caber dentro do candle.")
        if self.limite_candles < max(self.ema_macro_periodo, self.atr_regime_janela) + 3:
            raise RuntimeError("LIMITE_CANDLES é pequeno demais para os indicadores configurados.")
        if not 0 <= self.payout_minimo <= 1:
            raise RuntimeError("PAYOUT_MINIMO deve estar entre 0 e 1.")
        if self.max_operacoes_dia < 0:
            raise RuntimeError("MAX_OPERACOES_DIA nao pode ser negativo; use 0 para sem limite.")
        if not 0 < self.pullback_fib_min < self.pullback_fib_max < 1:
            raise RuntimeError("Faixa de Fibonacci do pullback é inválida.")
        if self.pullback_pivo_raio < 1 or self.pullback_janela < 10:
            raise RuntimeError("Configuração de pivôs do pullback é inválida.")
        if not 1024 <= self.porta_grafico <= 65535:
            raise RuntimeError("A porta do gráfico deve ficar entre 1024 e 65535.")


def configuracao_m1(base: Configuracao | None = None) -> Configuracao:
    """Perfil M1.

    A janela de entrada cai para 5 segundos porque em candle de 1 minuto os
    15 segundos do M5 seriam um quarto da vela — tempo suficiente para o preço
    andar bastante antes da ordem sair. O histórico dobra para o gráfico não
    ficar limitado a duas horas de tela.

    Atenção: a triagem walk-forward mediu M5. Nada do que foi medido lá vale
    automaticamente aqui; M1 tem mais ruído e o spread pesa mais.
    """
    return replace(
        base or Configuracao(),
        timeframe_segundos=60,
        expiracao_minutos=1,
        entrada_max_segundos_no_candle=5,
        limite_candles=240,
        porta_grafico=8768,
    )


def configuracao_pesquisa_m5(base: Configuracao | None = None) -> Configuracao:
    """Perfil amplo para descobrir e validar estratégias sem enviar ordens.

    Mantém candidatos experimentais e todos os pares observados. Proteções de
    execução não filtram a amostra porque ``executar_ordens`` permanece falso.
    """
    return replace(
        base or Configuracao(),
        ativos=(
            "EURUSD", "GBPUSD", "USDJPY", "AUDCAD", "EURGBP",
            "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC",
            "AUDCAD-OTC", "EURGBP-OTC",
        ),
        executar_ordens=False,
        executar_estrategias_nao_validadas=True,
        max_operacoes_dia=0,
        horario_bloqueado=None,
        horario_por_setup=None,
        entrada_max_segundos_no_candle=200,
        payout_minimo=0.75,
        engulfing_sr_ativo=True,
        divergencia_rsi_ativo=True,
        bollinger_squeeze_ativo=True,
        pin_bar_sr_ativo=True,
        macd_crossover_time_ativo=True,
        macd_crossover_tendencia_ativo=True,
        divergencia_rsi_time_ativo=True,
        divergencia_rsi_tendencia_ativo=True,
        pullback_ativo=True,
    )


def configuracao_practice_m5(base: Configuracao | None = None) -> Configuracao:
    """Perfil PRACTICE que envia ordens com limites conservadores.

    Separado de ``Configuracao()`` para que importar ou iniciar o programa sem
    uma escolha explicita nunca envie uma ordem por acidente.
    """
    return replace(
        base or Configuracao(),
        conta="PRACTICE",
        executar_ordens=True,
        executar_estrategias_nao_validadas=False,
        max_operacoes_dia=9999,
        max_perdas_consecutivas=0,
        stop_diario=-9999.0,
        parar_por_perdas=False,
        parar_por_prejuizo=False,
        payout_minimo=0.80,
        entrada_max_segundos_no_candle=25,
        # Só os 3 melhores por WR: pullback_confluencia, reversao_confluencia, reversao_bollinger_rsi
        macd_crossover_ativo=False,
        macd_crossover_time_ativo=False,
        macd_crossover_tendencia_ativo=False,
        sr_rejeicao_ativo=False,
        fibo_sr_retracao_ativo=False,
        reversao_candle_ativo=True,   # inclui reversao_confluencia — mantido
        pin_bar_sr_ativo=False,
        divergencia_rsi_ativo=False,
        divergencia_rsi_time_ativo=False,
        divergencia_rsi_tendencia_ativo=False,
        bollinger_squeeze_ativo=False,
        pullback_ativo=False,
    )


def configuracao_scalping_60(base: Configuracao | None = None) -> Configuracao:
    """Perfil scalping para banca inicial de R$60 em conta REAL.

    Entrada fixa de R$1 (≈1.7% da banca), stop diário −R$6, meta +R$5.
    Monitora 5 pares; ordens OTC permanecem bloqueadas em conta REAL.
    Proteções extras: circuit breaker, cooldown por ativo, filtro de abertura,
    bloqueio de notícia HIGH.
    """
    return replace(
        base or Configuracao(),
        email=os.environ.get("IQ_OPTION_EMAIL", ""),
        senha=os.environ.get("IQ_OPTION_SENHA", ""),
        conta="REAL",
        confirmo_conta_real=True,
        executar_ordens=True,
        ativos=(
            "EURUSD",
            "GBPUSD",
            "EURGBP",
            "EURUSD-OTC",
            "EURGBP-OTC",
        ),
        pares_validados=(),
        bloquear_otc_real=True,
        banca_inicial=60.0,
        piso_banca=40.0,
        valor_por_ordem=1.0,
        valor_percentual_banca=0.0,
        stop_diario=-6.0,
        meta_diaria=5.0,
        parar_por_prejuizo=True,
        parar_por_perdas=True,
        max_operacoes_dia=10,
        max_perdas_consecutivas=3,
        drawdown_maximo_percentual=0.20,
        circuit_breaker_max_perdas=3,
        circuit_breaker_cooldown_minutos=60,
        cooldown_pos_ordem_por_ativo_candles=2,
        filtro_candle_entrada_atr=0.3,
        bloquear_noticia_alto_impacto=True,
        alavancagem_pyramid=False,
        alavancagem_maximo=0.0,
        executar_estrategias_nao_validadas=False,
        confiar_resultado_automatico=False,
        verificar_resultado_por_candle=True,
        porta_grafico=8770,
    )


def configuracao_real_m5(base: Configuracao | None = None) -> Configuracao:
    """Perfil conta real com execução limitada aos ativos não OTC.

    M5 com expiração de 5 min. Monitora 10 ativos (5 reais + 5 OTC), mas
    os sintéticos OTC ficam bloqueados para envio de ordens reais.
    Entrada = 3% da banca (Kelly fracionado). Stop -R$12/dia, meta +R$15/dia.
    Piso de banca R$25. Resultado verificado pelo candle (não confia no
    histórico da IQ). Credenciais lidas de config.email/config.senha,
    fallback pra env vars IQ_OPTION_EMAIL/IQ_OPTION_SENHA.
    """
    return replace(
        base or Configuracao(),
        email=os.environ.get("IQ_OPTION_EMAIL", ""),
        senha=os.environ.get("IQ_OPTION_SENHA", ""),
        conta="REAL",
        confirmo_conta_real=True,
        executar_ordens=True,
        ativos=(
            "GBPUSD", "EURUSD", "USDJPY", "AUDCAD", "EURGBP",
            "GBPUSD-OTC", "EURUSD-OTC", "USDJPY-OTC",
            "AUDCAD-OTC", "EURGBP-OTC",
        ),
        pares_validados=(),
        bloquear_otc_real=True,
        valor_por_ordem=3.5,
        valor_percentual_banca=0.03,
        max_operacoes_dia=5,
        max_perdas_consecutivas=3,
        stop_diario=-12.0,
        meta_diaria=15.0,
        parar_por_prejuizo=True,
        parar_por_perdas=True,
        banca_inicial=50.0,
        piso_banca=25.0,
        alavancagem_pyramid=False,
        alavancagem_maximo=0.0,
        executar_estrategias_nao_validadas=False,
        confiar_resultado_automatico=False,
        verificar_resultado_por_candle=True,
        drawdown_maximo_percentual=0.20,
        circuit_breaker_max_perdas=3,
        circuit_breaker_cooldown_minutos=60,
        porta_grafico=8769,
    )
