from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from getpass import getpass

import pandas as pd
from iqoptionapi.stable_api import IQ_Option

from .config_swing import SwingConfig


class MercadoSwingIndisponivel(RuntimeError):
    pass


# --- captura da resposta 'underlying-list' -----------------------------------
# A lib (ws/client.py) so trata mensagens cujo nome ela conhece; 'underlying-list'
# nao esta na lista, entao a resposta seria descartada silenciosamente. Por isso
# interceptamos o on_message uma unica vez. Feio, mas a alternativa e forkar a lib.
_LOCK_UNDERLYING = threading.Lock()
_UNDERLYING: dict[str, list] = {}
_HOOK_INSTALADO = False


def _instalar_hook_underlying() -> None:
    """PRECISA rodar antes de qualquer conexao.

    ws/client.py faz `websocket.WebSocketApp(..., on_message=self.on_message)`:
    isso congela o metodo BOUND no momento da construcao. Patchar a classe
    depois de conectar nao tem efeito nenhum — o socket ja guardou a referencia
    antiga. Por isso a instalacao acontece no import deste modulo, la embaixo.
    """
    global _HOOK_INSTALADO
    if _HOOK_INSTALADO:
        return
    import iqoptionapi.ws.client as _wsclient

    _original = _wsclient.WebsocketClient.on_message

    def _com_underlying(self, message):
        try:
            m = json.loads(str(message))
            if m.get("name") == "underlying-list":
                with _LOCK_UNDERLYING:
                    _UNDERLYING["items"] = (m.get("msg") or {}).get("items") or []
        except Exception:
            pass
        return _original(self, message)

    _wsclient.WebsocketClient.on_message = _com_underlying
    _HOOK_INSTALADO = True


# Instala no import: qualquer conexao criada depois daqui ja nasce com o hook.
_instalar_hook_underlying()


class MercadoSwing:
    """Conexão IQ Option para swing: busca D1/H4/H1 sob demanda (sem stream contínuo)."""

    TF_D1 = 86400
    TF_H4 = 14400
    TF_H1 = 3600

    def __init__(self, config: SwingConfig):
        self.config = config
        self._api: IQ_Option | None = None
        self._lock = threading.RLock()
        # Separado do _lock: a reconexão precisa rodar mesmo com uma thread
        # pendurada segurando _lock numa chamada morta (ver reconectar_se_necessario).
        self._lock_reconexao = threading.Lock()
        self._email = ""
        self._senha = ""
        self._payouts: dict[str, float | None] = {}
        self._abertos: dict[str, bool] = {}
        self._ids: dict[str, int] = {}          # IDs binários (fallback)
        self._ids_forex: dict[str, int] = {}    # IDs forex CFD
        self._cache_ts = 0.0

    # -------------------------------------------------------------------------
    # Conexão
    # -------------------------------------------------------------------------

    def _credenciais(self) -> tuple[str, str]:
        if self._email and self._senha:
            return self._email, self._senha
        email = self.config.email or os.environ.get("IQ_OPTION_EMAIL", "").strip()
        senha = self.config.senha or os.environ.get("IQ_OPTION_SENHA", "").strip()
        if not email:
            email = input("Email da IQ Option: ").strip()
        if not senha:
            senha = getpass("Senha da IQ Option: ").strip()
        if not email or not senha:
            raise RuntimeError("Email ou senha vazios.")
        self._email, self._senha = email, senha
        return email, senha

    def _nova_conexao(self) -> IQ_Option:
        email, senha = self._credenciais()
        api = IQ_Option(email, senha)
        ok, motivo = api.connect()
        if not ok:
            raise MercadoSwingIndisponivel(f"Falha de conexão: {motivo}")
        api.change_balance(self.config.conta)
        time.sleep(0.8)
        return api

    def iniciar(self) -> None:
        self.config.validar()
        with self._lock:
            self._api = self._nova_conexao()
            self._atualizar_cache()
            print(f"[SWING] Conectado — conta={self.config.conta} | ativos={self.config.ativos}")

    # -------------------------------------------------------------------------
    # Cache de mercado (payout + abertura)
    # -------------------------------------------------------------------------

    @staticmethod
    def _payout_de(detalhe: dict) -> float | None:
        """Payout a partir de option.profit.commission, mesma conta que a lib faz."""
        opcao = detalhe.get("option")
        if not isinstance(opcao, dict):
            return None
        lucro = opcao.get("profit")
        if not isinstance(lucro, dict):
            return None
        comissao = lucro.get("commission")
        if not isinstance(comissao, (int, float)):
            return None
        return (100.0 - float(comissao)) / 100.0

    def _atualizar_cache(self) -> None:
        # Payout sai do get_all_init_v2, NUNCA do get_all_profit(): este usa o
        # endpoint v1 (get_all_init), que a IQ parou de responder — fica em
        # busy-wait de 30s e retenta pra sempre, sem nunca levantar exceção.
        # IDs binários via get_all_init_v2 (sempre disponível)
        payout_secao: dict[str, dict[str, float]] = {"binary": {}, "turbo": {}}
        try:
            init_v2 = self._api.get_all_init_v2()
            for secao in ("binary", "turbo"):
                for aid, info in (init_v2.get(secao, {}).get("actives", {}).items()):
                    nome_raw = info.get("name", "")
                    nome = nome_raw.split(".")[-1].upper() if "." in nome_raw else nome_raw.upper()
                    # v2 devolve "EURUSD-op" pro mercado normal; o payout é o mesmo.
                    base = nome[:-3] if nome.endswith("-OP") else nome
                    valor = self._payout_de(info)
                    if valor is not None and base in self.config.ativos:
                        payout_secao[secao][base] = valor
                    if nome in self.config.ativos:
                        self._ids[nome] = int(aid)
                        self._abertos[nome] = info.get("enabled", False) and not info.get("is_suspended", True)
        except Exception as e:
            print(f"[SWING] IDs binários erro: {e!r}")
        for ativo in self.config.ativos:
            val = payout_secao["binary"].get(ativo)
            if val is None:
                val = payout_secao["turbo"].get(ativo)
            self._payouts[ativo] = val
            if ativo not in self._abertos:
                self._abertos[ativo] = True  # fallback: assume aberto
        if self._ids:
            print(f"[SWING] IDs binários: {self._ids}")
        # IDs forex. NAO usar get_instruments() da lib: a IQ aposentou o
        # 'get-instruments' e responde "Invalid contract" em qualquer versao,
        # inclusive pra crypto. O timeout que isso gerava era lido aqui como
        # "a conta nao suporta CFD" — diagnostico errado; a conta suporta.
        # Endpoint vivo (verificado 27/08/2026): 46 pares forex, 44 abertos.
        print("[SWING] Buscando IDs forex (marginal-forex-instruments)...")
        try:
            self._ids_forex = self._buscar_ids_forex()
            if self._ids_forex:
                print(f"[SWING] IDs forex: {self._ids_forex}")
            else:
                print(f"[SWING] Nenhum ID forex para {self.config.ativos} na resposta da IQ.")
        except Exception as e:
            print(f"[SWING] falha ao buscar IDs forex: {e!r}")
        if not self._ids_forex:
            # Sem os IDs nao da pra montar ordem de margem. E o buy_order da lib
            # tambem nao serve: ele fala 'place-order-temp', igualmente aposentado.
            print("[SWING] Sem IDs forex — execucao de margem indisponivel.")
        self._cache_ts = time.time()

    def _buscar_ids_forex(self, timeout: float = 10.0) -> dict[str, int]:
        """active_id de cada par via 'marginal-forex-instruments.get-underlying-list'.

        A resposta vem em duas mensagens: um ACK 'result' e depois a
        'underlying-list' com os items — por isso o hook no on_message.
        """
        _instalar_hook_underlying()
        with _LOCK_UNDERLYING:
            _UNDERLYING.pop("items", None)
        self._api.api.send_websocket_request(
            "sendMessage",
            {
                "name": "marginal-forex-instruments.get-underlying-list",
                "version": "1.0",
                "body": {},
            },
            "swing-forex-ids",
        )
        limite = time.time() + timeout
        itens = None
        while time.time() < limite:
            with _LOCK_UNDERLYING:
                itens = _UNDERLYING.get("items")
            if itens is not None:
                break
            time.sleep(0.2)
        if itens is None:
            return {}
        ids: dict[str, int] = {}
        for item in itens:
            if not isinstance(item, dict) or item.get("is_suspended"):
                continue
            nome = str(item.get("name", "")).upper()
            active_id = item.get("active_id")
            if nome in self.config.ativos and active_id:
                ids[nome] = int(active_id)
        return ids

    def _cache_se_preciso(self) -> None:
        if time.time() - self._cache_ts > 300:
            self._atualizar_cache()

    # -------------------------------------------------------------------------
    # Candles
    # -------------------------------------------------------------------------

    @staticmethod
    def _para_df(dados) -> pd.DataFrame:
        if not dados:
            raise MercadoSwingIndisponivel("IQ não retornou candles.")
        df = pd.DataFrame(dados)
        df["timestamp"] = pd.to_datetime(df["from"], unit="s")
        df = df.rename(columns={"open": "Open", "close": "Close", "min": "Low", "max": "High"})
        if "Volume" not in df.columns:
            df["Volume"] = 0
        return df[["timestamp", "Open", "High", "Low", "Close", "Volume"]].set_index("timestamp").sort_index()

    @staticmethod
    def _somente_fechados(
        candles: pd.DataFrame, timeframe_segundos: int, timestamp_servidor: float
    ) -> pd.DataFrame:
        limite = pd.to_datetime(timestamp_servidor, unit="s")
        fechamentos = candles.index + pd.to_timedelta(timeframe_segundos, unit="s")
        return candles.loc[fechamentos <= limite]

    def _buscar(
        self, ativo: str, tf: int, n: int, somente_fechados: bool = False
    ) -> pd.DataFrame:
        with self._lock:
            ts = self._api.get_server_timestamp()
            dados = self._api.get_candles(ativo, tf, n + int(somente_fechados), ts)
        df = self._para_df(dados)
        if somente_fechados:
            df = self._somente_fechados(df, tf, ts).tail(n)
        return df

    def candles_d1(self, ativo: str) -> pd.DataFrame:
        return self._buscar(ativo, self.TF_D1, self.config.d1_num_candles, True)

    def candles_h4(self, ativo: str) -> pd.DataFrame:
        return self._buscar(ativo, self.TF_H4, self.config.h4_num_candles, True)

    def candles_h1(self, ativo: str) -> pd.DataFrame:
        return self._buscar(ativo, self.TF_H1, self.config.h1_num_candles, True)

    def candles_h1_recente(self, ativo: str, n: int = 8) -> pd.DataFrame:
        """Busca só os últimos N candles H1 — rápido, para refresh de tick."""
        return self._buscar(ativo, self.TF_H1, n)

    # -------------------------------------------------------------------------
    # Payout e estado
    # -------------------------------------------------------------------------

    def payout(self, ativo: str) -> float | None:
        self._cache_se_preciso()
        return self._payouts.get(ativo)

    def aberto(self, ativo: str) -> bool:
        self._cache_se_preciso()
        return self._abertos.get(ativo, False)

    def timestamp_servidor(self) -> int:
        with self._lock:
            return int(self._api.get_server_timestamp())

    # -------------------------------------------------------------------------
    # Expiração calendarizada
    # -------------------------------------------------------------------------

    @staticmethod
    def _ts_fim_dia() -> int:
        agora = datetime.now(timezone.utc)
        fim = agora.replace(hour=23, minute=59, second=59, microsecond=0)
        if fim <= agora + timedelta(minutes=30):
            fim += timedelta(days=1)
        return int(fim.timestamp())

    @staticmethod
    def _ts_fim_semana() -> int:
        agora = datetime.now(timezone.utc)
        dias_ate_sexta = (4 - agora.weekday()) % 7
        if dias_ate_sexta == 0 and agora.hour >= 22:
            dias_ate_sexta = 7
        sexta = agora + timedelta(days=dias_ate_sexta)
        fim = sexta.replace(hour=23, minute=59, second=59, microsecond=0)
        return int(fim.timestamp())

    def timestamp_expiracao(self, setup: str) -> int:
        regra = self.config.expiracao_por_setup.get(setup, "fim_semana")
        if regra == "fim_dia":
            return self._ts_fim_dia()
        return self._ts_fim_semana()

    # -------------------------------------------------------------------------
    # Compra com expiração por timestamp
    # -------------------------------------------------------------------------

    def comprar(
        self, valor: float, ativo: str, direcao: str, ts_expiracao: int
    ) -> tuple[bool, object]:
        with self._lock:
            id_ativo = self._ids.get(ativo)
            print(f" [DIAG-SW] {ativo}: id={id_ativo} | dir={direcao.upper()} | exp={ts_expiracao}")
            try:
                ok, id_ordem = self._api.buy_by_raw_expirationtime(
                    valor, id_ativo if id_ativo else ativo, direcao, ts_expiracao
                )
                if ok:
                    print(f" [DIAG-SW] {ativo}: buy_by_raw SUCESSO id={id_ordem}")
                    return True, id_ordem
                print(f" [DIAG-SW] {ativo}: buy_by_raw recusado: {id_ordem}")
            except Exception as e:
                print(f" [DIAG-SW] {ativo}: buy_by_raw ERRO: {e!r}")
            # fallback: tenta buy normal com minutos aproximados
            try:
                agora = self.timestamp_servidor()
                minutos = max(1, (ts_expiracao - agora) // 60)
                ok, id_ordem = self._api.buy(valor, ativo, direcao, minutos)
                if ok:
                    print(f" [DIAG-SW] {ativo}: buy() fallback SUCESSO id={id_ordem}")
                    return True, id_ordem
                print(f" [DIAG-SW] {ativo}: buy() fallback recusado: {id_ordem}")
                return False, id_ordem
            except Exception as e:
                print(f" [DIAG-SW] {ativo}: buy() fallback ERRO: {e!r}")
                return False, str(e)

    # -------------------------------------------------------------------------
    # Compra Forex CFD
    # -------------------------------------------------------------------------

    def comprar_forex(
        self,
        ativo: str,
        direcao: str,   # "buy" | "sell"
        valor_usd: float,
        alavancagem: int,
        sl_price: float,
        tp_price: float,
    ) -> tuple[bool, object]:
        """Abre posição CFD forex na IQ Option."""
        id_forex = self._ids_forex.get(ativo)
        # fallback: nome string (alguns builds aceitam)
        ativo_api = id_forex if id_forex else ativo
        print(
            f" [DIAG-FX] {ativo}: dir={direcao.upper()} | val={valor_usd:.2f} | "
            f"alavancagem={alavancagem}x | sl={sl_price:.5f} | tp={tp_price:.5f} | "
            f"id_forex={id_forex}"
        )
        if id_forex is None:
            print(f" [DIAG-FX] {ativo}: ID forex não encontrado — tentando com nome string")
        try:
            def _chamar():
                return self._api.buy_order(
                    instrument_type="forex",
                    instrument_id=ativo_api,
                    side=direcao,
                    amount=valor_usd,
                    leverage=alavancagem,
                    type="market",
                    limit_price=None,
                    stop_price=None,
                    stop_lose_kind="price",
                    stop_lose_value=sl_price,
                    take_profit_kind="price",
                    take_profit_value=tp_price,
                )
            # Não usa `with` para evitar deadlock no shutdown quando buy_order_id nunca chega
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(_chamar)
            try:
                ok, dados = future.result(timeout=15)
            except concurrent.futures.TimeoutError:
                print(f" [DIAG-FX] {ativo}: buy_order TIMEOUT (15s) — sem resposta da IQ")
                pool.shutdown(wait=False)
                return False, "timeout"
            finally:
                pool.shutdown(wait=False)
            print(f" [DIAG-FX] {ativo}: buy_order retornou ok={ok} dados={dados!r}")
            if ok:
                id_pos = dados.get("id") if isinstance(dados, dict) else dados
                print(f" [DIAG-FX] {ativo}: SUCESSO id={id_pos}")
                return True, id_pos
            return False, dados
        except Exception as e:
            print(f" [DIAG-FX] {ativo}: buy_order ERRO: {e!r}")
            return False, str(e)

    def preco_atual_forex(self, ativo: str) -> float | None:
        """Retorna preço de fechamento do último candle M1 como proxy do preço atual."""
        try:
            df = self._buscar(ativo, 60, 2)
            if df.empty:
                return None
            return float(df["Close"].iloc[-1])
        except Exception:
            return None

    def fechar_posicao_forex(self, id_posicao: object) -> bool:
        """Fecha posição CFD pelo ID."""
        try:
            with self._lock:
                ok, _ = self._api.close_position(id_posicao)
            return bool(ok)
        except Exception as e:
            print(f" [DIAG-FX] fechar_posicao ERRO: {e!r}")
            return False

    # -------------------------------------------------------------------------
    # Resultado de ordem pendente
    # -------------------------------------------------------------------------

    def verificar_resultado(self, id_ordem: str) -> str | None:
        """Verifica resultado de ordem pelo histórico IQ. Retorna 'win','loss','draw' ou None."""
        try:
            with self._lock:
                historico = self._api.get_optioninfo_v2(10)
            if not isinstance(historico, dict):
                return None
            for item in historico.get("positions", []):
                if str(item.get("id", "")) == str(id_ordem):
                    return str(item.get("win", ""))
        except Exception:
            pass
        return None

    # Ver MercadoIQ.conexao_viva: mesma armadilha, mesma correção.
    _MAX_ATRASO_TIMESTAMP = 90.0

    def conexao_viva(self) -> bool:
        """Liveness real do WebSocket.

        get_server_timestamp() NÃO serve de sonda sozinho: é property em cache
        que nunca levanta exceção com o socket morto. Foi o que travou o bot
        M15 por 25min em 2026-08-27 (ver iqoption_m5/mercado_iq.py).
        """
        try:
            if not self._api.check_connect():
                return False
        except Exception:
            return False
        try:
            ts = float(self._api.get_server_timestamp())
        except Exception:
            return False
        if ts <= 0:
            return False
        return abs(time.time() - ts) <= self._MAX_ATRASO_TIMESTAMP

    _TIMEOUT_LOCK_RECONEXAO = 5.0

    def reconectar_se_necessario(self, forcar: bool = False) -> bool:
        """Ver MercadoIQ.reconectar_se_necessario: NÃO espera o _lock
        indefinidamente, senão a reconexão nunca roda quando o loop está
        pendurado segurando esse lock — o deadlock de 2026-08-27.
        """
        if not forcar and self.conexao_viva():
            return True

        tem_lock = self._lock.acquire(timeout=self._TIMEOUT_LOCK_RECONEXAO)
        extra = "" if tem_lock else " (_lock preso — reconectando por fora)"
        try:
            with self._lock_reconexao:
                if not forcar and self.conexao_viva():
                    return True
                try:
                    print(f"[SWING] Reconectando (socket morto){extra}...")
                    self._api = self._nova_conexao()
                    self._atualizar_cache()
                    print("[SWING] Reconexão concluída.")
                    return True
                except Exception as e:
                    print(f"[SWING] Reconexão FALHOU: {e!r}")
                    return False
        finally:
            if tem_lock:
                self._lock.release()
