"""A apuracao tem de bater com a opcao que existiu, nao com uma parecida."""
import pandas as pd

from iqoption_m5.contrato import desfecho_binario, preco_em, vencimento


def _serie(precos, inicio="2026-09-10 14:00:00", freq="1min"):
    idx = pd.date_range(inicio, periods=len(precos), freq=freq)
    return pd.DataFrame(
        {"Open": precos, "High": precos, "Low": precos, "Close": precos}, index=idx
    )


def _serie_ohlc(barras, inicio="2026-09-10 14:00:00", freq="1min"):
    """Abertura e fechamento diferentes: distingue preço do início do fim."""
    idx = pd.date_range(inicio, periods=len(barras), freq=freq)
    return pd.DataFrame(
        [{"Open": o, "High": max(o, c), "Low": min(o, c), "Close": c} for o, c in barras],
        index=idx,
    )


class TestVencimento:
    def test_expira_nos_minutos_pedidos(self):
        assert vencimento(pd.Timestamp("2026-09-10 14:02"), 15) == pd.Timestamp("2026-09-10 14:17")

    def test_nao_arredonda_para_quarto_de_hora(self):
        """Hipotese descartada contra ordens reais.

        Arredondar faria uma opcao de 15 minutos durar de 10 a 20, e derrubou
        a concordancia de 89% para 79%. As 19 ordens registram 15 minutos
        entre enviada_em e encerrada_em, sem excecao.
        """
        assert vencimento(pd.Timestamp("2026-09-10 14:09"), 15) == pd.Timestamp("2026-09-10 14:24")

    def test_a_duracao_e_sempre_a_pedida_independente_do_minuto(self):
        duracoes = {
            (vencimento(pd.Timestamp(f"2026-09-10 14:{m:02d}"), 15)
             - pd.Timestamp(f"2026-09-10 14:{m:02d}")).total_seconds() / 60
            for m in range(0, 15)
        }
        assert duracoes == {15.0}


class TestPrecoEm:
    def test_usa_o_ultimo_preco_negociado_ate_o_instante(self):
        serie = _serie([1.10, 1.11, 1.12])
        assert preco_em(serie, pd.Timestamp("2026-09-10 14:01:30")) == 1.11

    def test_antes_do_inicio_da_serie_nao_inventa_preco(self):
        assert preco_em(_serie([1.10, 1.11]), pd.Timestamp("2026-09-10 13:00")) is None

    def test_depois_do_fim_da_serie_nao_extrapola(self):
        """Extrapolar produziria desfecho inventado, indistinguivel de medido."""
        assert preco_em(_serie([1.10, 1.11]), pd.Timestamp("2026-09-10 15:00")) is None

    def test_usa_abertura_da_barra_e_nao_o_fechamento_futuro(self):
        """Numa barra M1 o Close vale para o fim dela.

        Ler o Close para uma compra aos 9 segundos devolveria o preço de 50
        segundos depois — lookahead que inverte desfechos sem deixar rastro.
        """
        serie = _serie_ohlc([(1.100, 1.190), (1.200, 1.290)])

        assert preco_em(serie, pd.Timestamp("2026-09-10 14:00:09")) == 1.100

    def test_instante_exato_da_virada_usa_a_barra_que_comeca_ali(self):
        serie = _serie_ohlc([(1.100, 1.190), (1.200, 1.290)])

        assert preco_em(serie, pd.Timestamp("2026-09-10 14:01:00")) == 1.200


class TestDesfechoBinario:
    def test_call_ganha_quando_o_preco_no_vencimento_esta_acima_do_strike(self):
        serie = _serie([1.10] * 3 + [1.15] * 30)
        d = desfecho_binario(serie, "call", pd.Timestamp("2026-09-10 14:02"), 15)

        assert d["resultado"] == "win"
        assert d["strike"] == 1.10
        assert d["vencimento"] == pd.Timestamp("2026-09-10 14:17")

    def test_put_ganha_quando_o_preco_cai(self):
        serie = _serie([1.10] * 3 + [1.05] * 30)
        assert desfecho_binario(serie, "put", pd.Timestamp("2026-09-10 14:02"), 15)["resultado"] == "win"

    def test_preco_identico_no_vencimento_e_empate_nao_perda(self):
        d = desfecho_binario(_serie([1.10] * 30), "call", pd.Timestamp("2026-09-10 14:02"), 15)

        assert d["resultado"] == "empate"

    def test_sem_dado_ate_o_vencimento_devolve_None(self):
        """Serie curta nao pode virar 'loss' silencioso."""
        assert desfecho_binario(
            _serie([1.10] * 5), "call", pd.Timestamp("2026-09-10 14:02"), 15
        ) is None

    def test_strike_e_o_preco_da_compra_nao_a_abertura_da_vela_m5(self):
        """O erro que motivou o modulo: preco move dentro da vela de 5min."""
        serie = _serie([1.100, 1.101, 1.102, 1.103, 1.104] + [1.1035] * 30)
        d = desfecho_binario(serie, "call", pd.Timestamp("2026-09-10 14:04"), 15)

        assert d["strike"] == 1.104          # preco em 14:04, nao 1.100 de 14:00
        assert d["resultado"] == "loss"      # 1.1035 < 1.104
