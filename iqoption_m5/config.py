import os
from dataclasses import dataclass, replace
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
        "AUDCAD",
        "EURGBP",
        "EURUSD-OTC",
        "GBPUSD-OTC",
        "USDJPY-OTC",
        "AUDCAD-OTC",
        "EURGBP-OTC",
    )
    conta: str = "PRACTICE"
    confirmo_conta_real: bool = False  # trava extra: precisa ser True de propósito pra operar dinheiro real
    banca_inicial: float = 0.0
    piso_banca: float = 0.0  # 0 = desativado; se >0, para tudo quando banca_inicial+lucro_total <= piso
    executar_ordens: bool = True
    executar_estrategias_nao_validadas: bool = True  # pullback/bollinger; False = so registra, so reversao_candle executa
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
    entrada_max_segundos_no_candle: int = 200
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

    macd_fast: int = 6
    macd_slow: int = 16
    macd_signal: int = 9

    # Estratégias opcionais — desativadas por padrão até validação em PRACTICE
    engulfing_sr_ativo: bool = False
    divergencia_rsi_ativo: bool = False
    bollinger_squeeze_ativo: bool = False
    divergencia_rsi_janela_pivos: int = 5
    bollinger_squeeze_percentil_janela: int = 20
    bollinger_squeeze_min_corpo_atr: float = 0.5

    meta_diaria: float = 0.0  # 0 = desativado; se >0, encerra o dia ao atingir esse lucro
    payout_minimo: float = 0.75
    bloquear_otc_real: bool = True
    cache_mercado_segundos: int = 60
    max_operacoes_dia: int = 9999
    max_perdas_consecutivas: int = 9999
    stop_diario: float = -9999.0
    parar_por_perdas: bool = False
    parar_por_prejuizo: bool = False
    cooldown_pos_ordem_segundos: float = 0.0  # 0 = desligado; >0 = bloqueia nova entrada por N segundos após resultado

    # --- Horário bloqueado (UTC) ---
    # Evita operar em janelas de baixa liquidez / mercado OTC suspenso.
    # None = sem restrição. Tupla ((h_inicio, m_inicio), (h_fim, m_fim)).
    horario_bloqueado: tuple | None = ((0, 0), (7, 0))  # 00:00-07:00 UTC

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
        if self.max_operacoes_dia < 1:
            raise RuntimeError("MAX_OPERACOES_DIA deve ser positivo.")
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


def configuracao_real_m5(base: Configuracao | None = None) -> Configuracao:
    """Perfil conta real — todos os ativos operam de verdade.

    M5 com expiração de 5 min. 10 ativos (5 reais + 5 OTC).
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
        ativos=(
            "GBPUSD", "EURUSD", "USDJPY", "AUDCAD", "EURGBP",
            "GBPUSD-OTC", "EURUSD-OTC", "USDJPY-OTC",
            "AUDCAD-OTC", "EURGBP-OTC",
        ),
        pares_validados=(),
        bloquear_otc_real=False,
        valor_por_ordem=3.5,
        valor_percentual_banca=0.03,
        max_operacoes_dia=9999,
        stop_diario=-12.0,
        meta_diaria=15.0,
        parar_por_prejuizo=True,
        banca_inicial=50.0,
        piso_banca=25.0,
        alavancagem_pyramid=False,
        alavancagem_maximo=0.0,
        executar_estrategias_nao_validadas=False,
        confiar_resultado_automatico=False,
        verificar_resultado_por_candle=True,
        porta_grafico=8769,
    )
