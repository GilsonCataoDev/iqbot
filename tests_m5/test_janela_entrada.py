"""Testes determinísticos para §11: janela de entrada por setup.

Verifica a lógica de filtro por segundo_no_candle sem precisar do loop completo
do app.py. O filtro é trivial (comparação simples), então os testes validam
a configuração e a corretude da lógica isolada.
"""
from iqoption_m5.config import Configuracao


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
