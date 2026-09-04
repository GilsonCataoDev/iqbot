from __future__ import annotations

from dataclasses import replace
import sqlite3
import json

import pandas as pd

import backtest_m15
from iqoption_m5.config import configuracao_scalping_h1, configuracao_scalping_m15
from iqoption_m5.registro import RegistroSQLite


def _candles_m15() -> pd.DataFrame:
    idx = pd.date_range("2026-08-25 08:00", periods=16, freq="15min")
    valores = range(16)
    return pd.DataFrame(
        {
            "Open": valores,
            "High": [v + 0.8 for v in valores],
            "Low": [v - 0.2 for v in valores],
            "Close": [v + 0.5 for v in valores],
            "Volume": 1.0,
        },
        index=idx,
    )


def test_resample_contexto_so_libera_candle_superior_fechado() -> None:
    agregado = backtest_m15._resample_fechado(_candles_m15(), "1h")

    # O bloco 08:00-09:00 só pode aparecer às 09:00. O rótulo 08:00
    # permitiria que 08:15 enxergasse High/Low/Close de 08:30 e 08:45.
    assert agregado.index[0] == pd.Timestamp("2026-08-25 09:00")
    disponivel_0845 = agregado[agregado.index <= pd.Timestamp("2026-08-25 08:45")]
    assert disponivel_0845.empty


def test_minutos_parciais_padrao_reproduzem_janela_ao_vivo() -> None:
    assert backtest_m15._minutos_parciais_padrao(configuracao_scalping_m15()) == 1
    assert backtest_m15._minutos_parciais_padrao(configuracao_scalping_h1()) == 5


def test_candle_parcial_aceita_janela_m15_de_um_minuto() -> None:
    inicio = pd.Timestamp("2026-08-25 08:00")
    m1 = pd.DataFrame(
        {"Open": [1.0], "High": [1.2], "Low": [0.9], "Close": [1.1], "Volume": [10]},
        index=[inicio],
    )

    parcial = backtest_m15._candle_parcial_m1(m1, inicio, 1)

    assert parcial is not None
    assert parcial["Close"] == 1.1


def test_setups_inconclusivos_nao_executam() -> None:
    """Setups sem edge provado ficam desligados; os reprovados nunca voltam.

    pin_bar_sr saiu desta lista em 2026-08-26: backtest sem lookahead deu
    n=235 WR=70.2% IC95%=[64%,76%], IC inteiro acima do breakeven. Está ligado
    como SONDA (ver test_pin_bar_sr_e_sonda_em_stake_neutro).
    """
    m15 = configuracao_scalping_m15()
    h1 = configuracao_scalping_h1()

    # engulfing_sr: n=23, IC=[49%,84%] cruza o breakeven — amostra insuficiente.
    assert not m15.engulfing_sr_ativo
    # Reprovados por IC95% inteiro abaixo do breakeven de 54.1%.
    assert not m15.divergencia_rsi_ativo      # n=606 WR=49.0% IC=[45%,53%]
    assert not m15.bollinger_squeeze_ativo    # n=210 WR=38.6% IC=[32%,45%]
    assert not m15.anti_martingale_ativo
    assert not h1.anti_martingale_ativo
    assert not h1.engulfing_sr_ativo


def test_h1_pin_bar_sr_continua_sonda_em_stake_neutro() -> None:
    """No M15 a sonda foi substituída pelo plano forex->binária."""
    m15 = configuracao_scalping_m15()
    h1 = configuracao_scalping_h1()

    assert not m15.pin_bar_sr_ativo
    assert h1.pin_bar_sr_ativo
    assert h1.multiplicador_por_setup == {"sr_rejeicao": 1.0, "pin_bar_sr": 1.0}
    assert all(v == 1.0 for v in h1.multiplicador_por_setup.values())
    assert (h1.expiracao_por_setup or {}).get("pin_bar_sr") == 120


def test_m15_e_h1_practice_com_estrategias_separadas() -> None:
    m15 = configuracao_scalping_m15()
    h1 = configuracao_scalping_h1()

    for config in (m15, h1):
        assert config.conta == "PRACTICE"
        assert config.executar_ordens
        assert config.noticia_confirmada_ativo

    assert m15.forex_reteste_m15_ativo
    assert not m15.pullback_confluencia_ativo
    assert not m15.breakout_reteste_ativo
    assert h1.pullback_confluencia_ativo
    assert h1.breakout_reteste_ativo


def test_m15_e_h1_operam_os_mesmos_7_pares_validados() -> None:
    """Os 4 pares novos entraram com backtest próprio por timeframe.

    USDCHF teve o melhor WR no M15 (82.7%) mas ficou de fora: na IQ só existe
    como OTC, e estes perfis são de mercado normal.
    """
    esperados = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "EURJPY")
    for config in (configuracao_scalping_m15(), configuracao_scalping_h1()):
        assert config.ativos == esperados
        assert "USDCHF" not in config.ativos
        assert not any("OTC" in a for a in config.ativos)


def test_practice_continua_sem_limite_diario() -> None:
    config = replace(configuracao_scalping_m15(), max_operacoes_dia=0)
    assert config.max_operacoes_dia == 0


def test_registro_cria_campanha_com_configuracao(tmp_path) -> None:
    config = replace(configuracao_scalping_m15(), pasta_dados=tmp_path)
    registro = RegistroSQLite(tmp_path / "campanha.sqlite3", config=config)

    with sqlite3.connect(registro.caminho) as db:
        linha = db.execute(
            "SELECT config_hash, timeframe, janela_entrada_segundos FROM campanhas"
        ).fetchone()

    assert linha == (config.config_hash, 900, 60)


def test_relatorio_reproduzivel_exclui_reversao_sem_m1(tmp_path) -> None:
    config = configuracao_scalping_m15()
    df = pd.DataFrame(
        [
            {"ativo": "EURUSD", "setup": "sr_rejeicao", "res": "ganho", "rev_parcial": True},
            {"ativo": "EURUSD", "setup": "sr_rejeicao", "res": "ganho", "rev_parcial": False},
            {"ativo": "EURUSD", "setup": "pullback", "res": "perda", "rev_parcial": False},
        ]
    )
    destino = tmp_path / "validacao.json"

    backtest_m15.salvar_relatorio_reproduzivel(df, config, 0.85, 1, True, destino)
    relatorio = json.loads(destino.read_text(encoding="utf-8"))

    assert relatorio["total_sinais"] == 3
    assert relatorio["sinais_validos"] == 2
    assert relatorio["config_hash"] == config.config_hash
    assert relatorio["walk_forward"]["janelas"] == 0
    assert relatorio["alpha_bonferroni"] == 0.01666667
