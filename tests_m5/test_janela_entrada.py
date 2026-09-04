"""Testes determinísticos para §11: janela de entrada por setup.

Verifica a lógica de filtro por segundo_no_candle sem precisar do loop completo
do app.py. O filtro é trivial (comparação simples), então os testes validam
a configuração e a corretude da lógica isolada.
"""
from datetime import datetime

import pandas as pd

from iqoption_m5.config import Configuracao, configuracao_scalping_h1, configuracao_scalping_m15
from iqoption_m5.modelos import Decisao, SnapshotMercado
from iqoption_m5.risco import GerenciadorRisco


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

class TestJanelaEntradaConfig:
    def test_default_none(self):
        c = Configuracao()
        assert c.janela_entrada_por_setup is None

    def test_custom_dict(self):
        jep = {"sr_rejeicao": 30, "pullback_confluencia": 180}
        c = Configuracao(janela_entrada_por_setup=jep)
        assert c.janela_entrada_por_setup["sr_rejeicao"] == 30
        assert c.janela_entrada_por_setup["pullback_confluencia"] == 180

    def test_setup_nao_configurado_nao_bloqueado(self):
        """Setup sem janela explícita não deve ser bloqueado pela lógica."""
        jep = {"sr_rejeicao": 30}
        c = Configuracao(janela_entrada_por_setup=jep)
        # pullback não está no dict → janela_setup=None → não bloqueia
        assert c.janela_entrada_por_setup.get("pullback_confluencia") is None

    def test_validar_nao_quebra_com_janela(self):
        """Configuração com janela_entrada_por_setup não quebra na validação."""
        c = Configuracao(janela_entrada_por_setup={"sr_rejeicao": 30})
        c.validar()  # não deve levantar

    def test_validar_nao_quebra_sem_janela(self):
        c = Configuracao()
        c.validar()

    def test_m15_liga_janela_intracandle_por_toque(self):
        c = configuracao_scalping_m15()

        assert c.entrada_intracandle_por_toque_ativo
        assert c.janela_entrada_por_setup["sr_rejeicao"] == 540
        # Pullback pode acontecer no meio da vela; até 10min ainda sobram 5min
        # para a opção expirar no fechamento da própria vela.
        assert c.janela_entrada_por_setup["pullback_confluencia"] == 600

    def test_h1_liga_janela_intracandle_por_toque(self):
        c = configuracao_scalping_h1()

        assert c.entrada_intracandle_por_toque_ativo
        assert c.janela_entrada_por_setup["sr_rejeicao"] == 2400
        assert c.janela_entrada_por_setup["pullback_confluencia"] == 2400


# ---------------------------------------------------------------------------
# Lógica do filtro (isolada do app.py)
# ---------------------------------------------------------------------------

def _aplica_filtro(janela_por_setup: dict | None, setup: str, segundo: int) -> bool:
    """Replica a lógica de §11 do app.py — retorna True se deve cancelar."""
    if not janela_por_setup:
        return False
    janela_setup = janela_por_setup.get(setup)
    if janela_setup is None:
        return False
    return segundo > janela_setup


class TestJanelaFiltroLogica:
    def test_none_nunca_bloqueia(self):
        assert not _aplica_filtro(None, "sr_rejeicao", 250)

    def test_dict_vazio_nunca_bloqueia(self):
        assert not _aplica_filtro({}, "sr_rejeicao", 250)

    def test_setup_ausente_nao_bloqueia(self):
        assert not _aplica_filtro({"pullback": 60}, "sr_rejeicao", 200)

    def test_dentro_da_janela_passa(self):
        assert not _aplica_filtro({"sr_rejeicao": 30}, "sr_rejeicao", 30)

    def test_na_borda_passa(self):
        # segundo == janela: 30 > 30 é False → não bloqueia
        assert not _aplica_filtro({"sr_rejeicao": 30}, "sr_rejeicao", 30)

    def test_fora_da_janela_bloqueia(self):
        assert _aplica_filtro({"sr_rejeicao": 30}, "sr_rejeicao", 31)

    def test_multiplos_setups_independentes(self):
        jep = {"sr_rejeicao": 30, "pullback_confluencia": 180}
        # sr_rejeicao em 200s → fora da janela
        assert _aplica_filtro(jep, "sr_rejeicao", 200)
        # pullback em 180s → na borda → passa
        assert not _aplica_filtro(jep, "pullback_confluencia", 180)
        # pullback em 181s → fora
        assert _aplica_filtro(jep, "pullback_confluencia", 181)
        # macd (não configurado) em 200s → não bloqueia
        assert not _aplica_filtro(jep, "macd_crossover", 200)

    def test_janela_zero_bloqueia_tudo_exceto_segundo_zero(self):
        jep = {"sr_rejeicao": 0}
        assert not _aplica_filtro(jep, "sr_rejeicao", 0)
        assert _aplica_filtro(jep, "sr_rejeicao", 1)


class TestRiscoJanelaIntracandle:
    def _snapshot(self, segundo_no_candle: int) -> SnapshotMercado:
        ts = 1_800_000_000
        ts = ts - (ts % 900) + segundo_no_candle
        candles = pd.DataFrame(
            {"Open": [1.0], "High": [1.1], "Low": [0.9], "Close": [1.0], "Volume": [1.0]},
            index=pd.date_range("2026-01-01", periods=1, freq="15min"),
        )
        return SnapshotMercado("EURUSD", candles, 0.85, True, ts)

    def test_setup_intracandle_passa_no_meio_da_vela(self):
        config = configuracao_scalping_m15()
        decisao = Decisao(
            "EURUSD",
            "call",
            1.0,
            datetime.now(),
            "sr_rejeicao_m15",
            detalhes={"setup": "sr_rejeicao"},
        )

        autorizacao = GerenciadorRisco(config).avaliar(self._snapshot(360), decisao)

        assert autorizacao.permitida

    def test_pullback_confluencia_passa_no_meio_da_vela(self):
        config = configuracao_scalping_m15()
        decisao = Decisao(
            "EURUSD",
            "call",
            1.0,
            datetime.now(),
            "pullback_m15",
            detalhes={"setup": "pullback_confluencia"},
        )

        # 600s = 10min, o limite da janela (a comparacao e inclusiva).
        autorizacao = GerenciadorRisco(config).avaliar(self._snapshot(600), decisao)

        assert autorizacao.permitida

    def test_pullback_confluencia_bloqueado_passando_dos_10min(self):
        """Depois de 10min sobra pouca vela pra opcao respirar — e a expiracao
        e o fechamento dessa mesma vela."""
        config = configuracao_scalping_m15()
        decisao = Decisao(
            "EURUSD",
            "call",
            1.0,
            datetime.now(),
            "pullback_m15",
            detalhes={"setup": "pullback_confluencia"},
        )

        autorizacao = GerenciadorRisco(config).avaliar(self._snapshot(601), decisao)

        assert not autorizacao.permitida
        assert autorizacao.motivo == "entrada_atrasada"

    def test_pullback_simples_continua_bloqueado_no_meio_da_vela(self):
        config = configuracao_scalping_m15()
        decisao = Decisao(
            "EURUSD",
            "call",
            1.0,
            datetime.now(),
            "pullback_m15",
            detalhes={"setup": "pullback"},
        )

        autorizacao = GerenciadorRisco(config).avaliar(self._snapshot(600), decisao)

        assert not autorizacao.permitida
        assert autorizacao.motivo == "entrada_atrasada"
