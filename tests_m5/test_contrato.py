"""A apuracao tem de bater com a opcao que existiu, nao com uma parecida."""
import pandas as pd
import pytest

from iqoption_m5.contrato import desfecho_binario, marco_vencimento, preco_em


def _serie(precos, inicio="2026-09-10 14:00:00", freq="1min"):
    idx = pd.date_range(inicio, periods=len(precos), freq=freq)
    return pd.DataFrame(
        {"Open": precos, "High": precos, "Low": precos, "Close": precos}, index=idx
    )


class TestMarcoVencimento:
    def test_pedido_de_15min_cai_no_quarto_de_hora_mais_proximo(self):
        # 14:02 + 15min = 14:17; marcos 14:15 (2min antes) e 14:30 (13 depois)
        assert marco_vencimento(pd.Timestamp("2026-09-10 14:02"), 15) == \
            pd.Timestamp("2026-09-10 14:15")

    def test_alvo_mais_perto_do_marco_seguinte_alonga_a_opcao(self):
        # 14:09 + 15min = 14:24; 14:30 esta a 6min e 14:15 a 9min
        assert marco_vencimento(pd.Timestamp("2026-09-10 14:09"), 15) == \
            pd.Timestamp("2026-09-10 14:30")

    def test_caso_medido_em_producao_14h09_pedindo_30min(self):
        """Registrado no executor: 14:09 pedindo 30min expirou 14:45."""
        assert marco_vencimento(pd.Timestamp("2026-09-10 14:09"), 30) == \
            pd.Timestamp("2026-09-10 14:45")

    def test_duracao_real_varia_embora_o_pedido_seja_sempre_15min(self):
        """É por isso que medir 3 velas M5 fixas nao reproduz o contrato."""
        duracoes = {
            (marco_vencimento(pd.Timestamp(f"2026-09-10 14:{m:02d}"), 15)
             - pd.Timestamp(f"2026-09-10 14:{m:02d}")).total_seconds() / 60
            for m in range(0, 15)
        }
        assert duracoes != {15.0}
        assert min(duracoes) < 15 < max(duracoes)

    def test_marco_colado_na_compra_pula_para_o_seguinte(self):
        # 14:14:30 + 15min = 14:29:30 -> marco 14:30 fica a 30s da compra
        venc = marco_vencimento(pd.Timestamp("2026-09-10 14:14:30"), 15)
        assert (venc - pd.Timestamp("2026-09-10 14:14:30")).total_seconds() >= 60


class TestPrecoEm:
    def test_usa_o_ultimo_preco_negociado_ate_o_instante(self):
        serie = _serie([1.10, 1.11, 1.12])
        assert preco_em(serie, pd.Timestamp("2026-09-10 14:01:30")) == 1.11

    def test_antes_do_inicio_da_serie_nao_inventa_preco(self):
        assert preco_em(_serie([1.10, 1.11]), pd.Timestamp("2026-09-10 13:00")) is None

    def test_depois_do_fim_da_serie_nao_extrapola(self):
        """Extrapolar produziria desfecho inventado, indistinguivel de medido."""
        assert preco_em(_serie([1.10, 1.11]), pd.Timestamp("2026-09-10 15:00")) is None


class TestDesfechoBinario:
    def test_call_ganha_quando_o_preco_no_vencimento_esta_acima_do_strike(self):
        serie = _serie([1.10] * 3 + [1.15] * 30)
        d = desfecho_binario(serie, "call", pd.Timestamp("2026-09-10 14:02"), 15)

        assert d["resultado"] == "win"
        assert d["strike"] == 1.10
        assert d["vencimento"] == pd.Timestamp("2026-09-10 14:15")

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
