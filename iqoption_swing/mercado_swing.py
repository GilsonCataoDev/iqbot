from __future__ import annotations

import concurrent.futures
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


class MercadoSwing:
    """Conexão IQ Option para swing: busca D1/H4/H1 sob demanda (sem stream contínuo)."""

    TF_D1 = 86400
    TF_H4 = 14400
    TF_H1 = 3600

    def __init__(self, config: SwingConfig):
        self.config = config
        self._api: IQ_Option | None = None
        self._lock = threading.RLock()
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

    def _atualizar_cache(self) -> None:
        try:
            lucros = self._api.get_all_profit()
        except Exception as e:
            print(f"[SWING] payout indisponível: {e!r}")
            lucros = {}
        # IDs binários via get_all_init_v2 (sempre disponível)
        try:
            init_v2 = self._api.get_all_init_v2()
            for secao in ("binary", "turbo"):
                for aid, info in (init_v2.get(secao, {}).get("actives", {}).items()):
                    nome_raw = info.get("name", "")
                    nome = nome_raw.split(".")[-1].upper() if "." in nome_raw else nome_raw.upper()
                    if nome in self.config.ativos:
                        self._ids[nome] = int(aid)
                        self._abertos[nome] = info.get("enabled", False) and not info.get("is_suspended", True)
        except Exception as e:
            print(f"[SWING] IDs binários erro: {e!r}")
        for ativo in self.config.ativos:
            entrada = lucros.get(ativo) or lucros.get(ativo.lower())
            if isinstance(entrada, dict):
                val = entrada.get("binary") or entrada.get("turbo")
                self._payouts[ativo] = float(val) if isinstance(val, (int, float)) else None
            else:
                self._payouts[ativo] = None
            if ativo not in self._abertos:
                self._abertos[ativo] = True  # fallback: assume aberto
        if self._ids:
            print(f"[SWING] IDs binários: {self._ids}")
        # IDs forex via get_instruments("forex") com timeout (API tem loop infinito interno)
        print("[SWING] Buscando IDs forex via get_instruments (timeout 12s)...")
        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _fut = _pool.submit(self._api.get_instruments, "forex")
        try:
            ins_data = _fut.result(timeout=12)
            instruments = ins_data.get("instruments", []) if isinstance(ins_data, dict) else []
            for detail in instruments:
                nome = str(detail.get("name", "")).upper()
                if nome in self.config.ativos:
                    id_fx = detail.get("id")
                    if id_fx:
                        self._ids_forex[nome] = int(id_fx)
            if self._ids_forex:
                print(f"[SWING] IDs forex: {self._ids_forex}")
            else:
                nomes_disp = [d.get("name") for d in instruments[:10]]
                print(f"[SWING] Nenhum ID forex p/ {self.config.ativos}. Disponíveis: {nomes_disp}")
        except concurrent.futures.TimeoutError:
            print("[SWING] get_instruments('forex') TIMEOUT — conta PRACTICE pode não suportar CFD")
        except Exception as e:
            print(f"[SWING] get_instruments('forex') erro: {e!r}")
        finally:
            _pool.shutdown(wait=False)
        if not self._ids_forex:
            print("[SWING] IDs forex não encontrados — buy_order tentará com nome string")
        self._cache_ts = time.time()

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

    def _buscar(self, ativo: str, tf: int, n: int) -> pd.DataFrame:
        with self._lock:
            ts = self._api.get_server_timestamp()
            dados = self._api.get_candles(ativo, tf, n, ts)
        return self._para_df(dados)

    def candles_d1(self, ativo: str) -> pd.DataFrame:
        return self._buscar(ativo, self.TF_D1, self.config.d1_num_candles)

    def candles_h4(self, ativo: str) -> pd.DataFrame:
        return self._buscar(ativo, self.TF_H4, self.config.h4_num_candles)

    def candles_h1(self, ativo: str) -> pd.DataFrame:
        return self._buscar(ativo, self.TF_H1, self.config.h1_num_candles)

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

    def reconectar_se_necessario(self) -> bool:
        with self._lock:
            try:
                self._api.get_server_timestamp()
                return True
            except Exception:
                try:
                    self._api = self._nova_conexao()
                    self._atualizar_cache()
                    return True
                except Exception:
                    return False
