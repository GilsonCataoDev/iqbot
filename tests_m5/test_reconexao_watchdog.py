"""Regressão da trava de 2026-08-27: WebSocket caiu e o bot não reconectou.

O que aconteceu: às 10:31 o socket fechou ("Connection is already closed").
A sonda de conexão rodava a cada 60s e usava get_server_timestamp(), que na
iqoptionapi é só `return self.api.timesync.server_timestamp` — uma property em
cache que devolve o último valor recebido e NUNCA levanta exceção. A sonda
respondia "vivo" para sempre, _reconectar() nunca era chamado, e o watchdog
apenas imprimia alerta. Resultado: 25min parado sem operar.

Estes testes travam as duas correções.
"""
import time
import unittest

from iqoption_m5.mercado_iq import MercadoIQ


class ApiFake:
    """Reproduz o comportamento real da iqoptionapi que causou o bug."""

    def __init__(self, conectado=True, atraso_timestamp=0.0):
        self.conectado = conectado
        self.atraso_timestamp = atraso_timestamp
        self.reconectou = False

    def check_connect(self):
        return self.conectado

    def get_server_timestamp(self):
        # Igual ao real: property em cache. Não levanta erro com socket morto,
        # só congela no último valor recebido.
        return time.time() - self.atraso_timestamp


def _mercado_com(api):
    import threading
    m = MercadoIQ.__new__(MercadoIQ)   # sem __init__: não queremos conectar
    m._api = api
    m._lock_api = threading.RLock()
    m._lock_reconexao = threading.Lock()
    return m


class TestSondaDeVida(unittest.TestCase):
    def test_socket_fechado_e_detectado(self):
        m = _mercado_com(ApiFake(conectado=False))
        self.assertFalse(m.conexao_viva())

    def test_timestamp_congelado_e_detectado(self):
        """Half-open: check_connect diz True mas não chega mais tráfego."""
        m = _mercado_com(ApiFake(conectado=True, atraso_timestamp=600.0))
        self.assertFalse(m.conexao_viva())

    def test_conexao_saudavel_passa(self):
        m = _mercado_com(ApiFake(conectado=True, atraso_timestamp=2.0))
        self.assertTrue(m.conexao_viva())

    def test_timestamp_sozinho_nao_serve_de_sonda(self):
        """O bug original: com socket morto, o timestamp ainda 'funciona'.

        Se alguém voltar a usar só get_server_timestamp() como sonda, este
        teste falha — é exatamente o cenário que travou o bot por 25min.
        """
        api = ApiFake(conectado=False, atraso_timestamp=0.0)

        # A sonda antiga: não levanta e devolve um valor fresquinho, mesmo com
        # o socket fechado. Sozinha, ela diria "conexão ok".
        self.assertIsInstance(api.get_server_timestamp(), float)

        # A sonda nova enxerga o socket fechado.
        self.assertFalse(_mercado_com(api).conexao_viva())


class TestReconexao(unittest.TestCase):
    def test_reconecta_quando_sonda_acusa_morto(self):
        api = ApiFake(conectado=False)
        m = _mercado_com(api)
        chamou = []
        m._reconectar = lambda: chamou.append(True)

        self.assertTrue(m.reconectar_se_necessario())
        self.assertEqual(len(chamou), 1, "deveria ter reconectado")

    def test_nao_reconecta_a_toa_quando_saudavel(self):
        m = _mercado_com(ApiFake(conectado=True, atraso_timestamp=1.0))
        chamou = []
        m._reconectar = lambda: chamou.append(True)

        self.assertTrue(m.reconectar_se_necessario())
        self.assertEqual(chamou, [], "não deveria reconectar com socket vivo")

    def test_forcar_reconecta_mesmo_com_sonda_ok(self):
        """Caminho do watchdog: o loop travou, reconecta sem perguntar."""
        m = _mercado_com(ApiFake(conectado=True, atraso_timestamp=1.0))
        chamou = []
        m._reconectar = lambda: chamou.append(True)

        self.assertTrue(m.reconectar_se_necessario(forcar=True))
        self.assertEqual(len(chamou), 1)

    def test_falha_de_reconexao_retorna_false(self):
        m = _mercado_com(ApiFake(conectado=False))

        def _falha():
            raise RuntimeError("IQ fora do ar")

        m._reconectar = _falha
        self.assertFalse(m.reconectar_se_necessario())


class TestWatchdogAgeSobreConexao(unittest.TestCase):
    def test_watchdog_aceita_mercado_e_forca_reconexao(self):
        """O watchdog precisa receber o mercado, senão volta a ser só print."""
        import inspect
        from iqoption_m5 import app

        params = list(inspect.signature(app._watchdog).parameters)
        self.assertIn("mercado", params,
                      "_watchdog sem acesso ao mercado não consegue destravar nada")

        fonte = inspect.getsource(app._watchdog)
        self.assertIn("forcar=True", fonte,
                      "watchdog deve forçar reconexão, não apenas alertar")


if __name__ == "__main__":
    unittest.main()


class TestDeadlockDeReconexao(unittest.TestCase):
    """Regressão do 2o ato do bug (2026-08-27 11:18).

    A primeira correção (sonda de vida) não bastou: quando o socket morre, a
    thread do loop principal fica pendurada DENTRO de uma chamada da
    iqoptionapi segurando o _lock_api. A thread de reconexão de 60s então
    bloqueava esperando esse mesmo lock e nunca rodava. O bot congelava de vez,
    sem nem imprimir o "[CONEXAO] Reconectando".
    """

    def _mercado_com_lock_preso(self):
        import threading
        api = ApiFake(conectado=False)
        m = _mercado_com(api)
        m._TIMEOUT_LOCK_RECONEXAO = 0.2   # teste rápido

        preso = threading.Event()
        liberar = threading.Event()

        def _thread_pendurada():
            with m._lock_api:      # simula get_realtime_candles travado
                preso.set()
                liberar.wait(timeout=30)

        t = threading.Thread(target=_thread_pendurada, daemon=True)
        t.start()
        self.assertTrue(preso.wait(timeout=5), "setup do teste falhou")
        return m, liberar

    def test_reconecta_mesmo_com_lock_preso(self):
        m, liberar = self._mercado_com_lock_preso()
        chamou = []
        m._reconectar = lambda: chamou.append(True)
        try:
            ok = m.reconectar_se_necessario(forcar=True)
        finally:
            liberar.set()

        self.assertTrue(ok)
        self.assertEqual(len(chamou), 1,
                         "reconexão nao rodou: voltou o deadlock de 2026-08-27")

    def test_nao_fica_pendurado_esperando_o_lock(self):
        """O ponto do bug: a chamada tem que RETORNAR, não travar junto."""
        import time as _t
        m, liberar = self._mercado_com_lock_preso()
        m._reconectar = lambda: None
        try:
            inicio = _t.monotonic()
            m.reconectar_se_necessario(forcar=True)
            gasto = _t.monotonic() - inicio
        finally:
            liberar.set()

        self.assertLess(gasto, 5.0,
                        f"ficou {gasto:.1f}s esperando o lock — deadlock de volta")

    def test_reconexao_com_extras_nao_reentra_no_lock_preso(self):
        """M5/M15/H1 do Lab precisam reabrir mesmo com snapshot antigo preso."""
        m, liberar = self._mercado_com_lock_preso()
        m.config = type("Config", (), {"ativos": ("EURUSD",)})()
        m._streams_extras = {900: 120}
        m._cache_atualizado = 0.0
        m._nova_conexao = lambda: object()
        m._iniciar_streams = lambda: None
        extras_abertos = []
        m._iniciar_timeframes_extras_sem_lock = (
            lambda extras, recarregar_historico=True:
            extras_abertos.append((extras, recarregar_historico))
        )
        try:
            inicio = time.monotonic()
            self.assertTrue(m.reconectar_se_necessario(forcar=True))
            gasto = time.monotonic() - inicio
        finally:
            liberar.set()

        self.assertLess(gasto, 5.0, "reconexão dos extras ficou presa no lock antigo")
        self.assertEqual(extras_abertos, [({900: 120}, False)])
