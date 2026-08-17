import os
import threading
import time
from getpass import getpass

import pandas as pd
from iqoptionapi.stable_api import IQ_Option

from .config import Configuracao
from .modelos import SnapshotMercado


class MercadoIndisponivel(RuntimeError):
    pass


class MercadoIQ:
    """Adapter que esconde protocolo, cache, streams e reconexão da IQ."""

    def __init__(self, config: Configuracao):
        self.config = config
        self._api = None
        self._buffers: dict[str, pd.DataFrame] = {}
        self._lock_api = threading.RLock()
        self._lock_buffers = threading.Lock()
        self._mercado_aberto: dict[str, bool] = {}
        self._payouts: dict[str, float | None] = {}
        self._ids_ativos: dict[str, int] = {}
        self._cache_atualizado = 0.0
        self._email = ""
        self._senha = ""

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
            raise RuntimeError("Email ou senha ficaram vazios.")
        self._email, self._senha = email, senha
        return self._email, self._senha

    def _nova_conexao(self):
        email, senha = self._credenciais()
        api = IQ_Option(email, senha)
        conectado, motivo = api.connect()
        if not conectado:
            raise MercadoIndisponivel(f"Falha ao conectar na IQ Option: {motivo}")
        api.change_balance(self.config.conta)
        # A troca de saldo é assíncrona (WebSocket). Aguarda e tenta confirmar.
        time.sleep(0.8)
        try:
            modo = str(api.get_balance_mode()).upper()
            if self.config.conta.upper() not in modo:
                print(
                    f" [WARN] change_balance({self.config.conta!r}) pode não ter surtido efeito "
                    f"(modo atual: {modo!r}). Verifique antes de operar dinheiro real."
                )
        except Exception:
            pass
        return api

    @staticmethod
    def _candles_para_df(dados) -> pd.DataFrame:
        if not dados:
            raise MercadoIndisponivel("IQ Option não retornou candles.")
        df = pd.DataFrame(dados)
        obrigatorias = {"from", "open", "close", "min", "max"}
        if not obrigatorias.issubset(df.columns):
            raise MercadoIndisponivel("Resposta de candles incompleta.")
        df["timestamp"] = pd.to_datetime(df["from"], unit="s")
        df = df.rename(
            columns={"open": "Open", "close": "Close", "min": "Low", "max": "High", "volume": "Volume"}
        )
        if "Volume" not in df:
            df["Volume"] = 0
        return df[["timestamp", "Open", "High", "Low", "Close", "Volume"]].set_index("timestamp").sort_index()

    def _historico(self, ativo: str) -> pd.DataFrame:
        timestamp = self._api.get_server_timestamp()
        dados = self._api.get_candles(
            ativo,
            self.config.timeframe_segundos,
            self.config.limite_candles,
            timestamp,
        )
        return self._candles_para_df(dados)

    def _iniciar_streams(self) -> None:
        for ativo in self.config.ativos:
            self._api.start_candles_stream(
                ativo,
                self.config.timeframe_segundos,
                self.config.limite_candles,
            )

    def conectar_somente_leitura(self):
        """Conexão crua para ferramentas que apenas leem histórico (backtest)."""
        return self._nova_conexao()

    def iniciar(self) -> None:
        self.config.validar()
        with self._lock_api:
            self._api = self._nova_conexao()
            for ativo in self.config.ativos:
                self._buffers[ativo] = self._historico(ativo)
            self._iniciar_streams()
            self._atualizar_cache_forcado()

    def _obter_abertura_turbo(self) -> dict[str, bool]:
        abertos = {ativo: False for ativo in self.config.ativos}
        try:
            dados = self._api.get_all_init_v2()
            if not isinstance(dados, dict):
                print(f" [mercado] get_all_init_v2 retornou tipo inesperado: {type(dados)}")
                dados = {}
            # Busca em turbo e binary (OTC aparece em qualquer uma dependendo da hora)
            secoes = {}
            for secao in ("turbo", "binary"):
                parte = dados.get(secao, {}).get("actives", {})
                if isinstance(parte, dict):
                    secoes.update(parte)
            if not secoes:
                print(f" [mercado] get_all_init_v2 sem actives. Chaves: {list(dados.keys())}")
            nomes_vistos = []
            resolvidos = set()
            for chave, detalhe in secoes.items():
                if not isinstance(detalhe, dict):
                    continue
                nome = str(detalhe.get("name", "")).split(".", 1)[-1]
                nomes_vistos.append(nome)
                candidatos = {nome}
                if nome.endswith("-op"):
                    base = nome[: -len("-op")]
                    candidatos.add(base)
                    candidatos.add(f"{base}-OTC")
                enabled = bool(detalhe.get("enabled", False))
                is_otc_entry = nome.endswith("-op") or any(c.upper().endswith("-OTC") for c in candidatos)
                # OTC: is_suspended é frequentemente incorreto — a IQ marca suspended
                # quando o mercado regular equivalente está aberto, mas o OTC pode
                # continuar disponível. Usa só enabled; a rejeição real vem do buyv3.
                if is_otc_entry:
                    aberto = enabled
                else:
                    aberto = enabled and not bool(detalhe.get("is_suspended", False))
                for candidato in candidatos:
                    if candidato in abertos:
                        abertos[candidato] = aberto
                        resolvidos.add(candidato)
                    try:
                        self._ids_ativos[candidato] = int(chave)
                    except (TypeError, ValueError):
                        pass
            faltando = [a for a in self.config.ativos if a not in resolvidos]
            # OTC é sintético e opera 24/7 — se a API não retornou, assume aberto
            for ativo_faltante in faltando:
                if ativo_faltante.upper().endswith("-OTC"):
                    ultimo_estado = self._mercado_aberto.get(ativo_faltante, True)
                    abertos[ativo_faltante] = ultimo_estado
                    resolvidos.add(ativo_faltante)
                    print(f" [mercado] {ativo_faltante} ausente na resposta, mantendo estado anterior: {'aberto' if ultimo_estado else 'fechado'}")
            faltando = [a for a in self.config.ativos if a not in resolvidos]
            if faltando:
                print(f" [mercado] ativos NAO encontrados na resposta da IQ: {faltando}")
                for ativo_faltante in faltando:
                    parecidos = [n for n in nomes_vistos if ativo_faltante.split("-")[0] in n]
                    print(f" [mercado] nomes parecidos com '{ativo_faltante}': {parecidos or 'NENHUM'}")
        except Exception as erro:
            print(f" [mercado] falha ao consultar abertura turbo, marcando tudo como fechado: {erro!r}")
        return abertos

    def _atualizar_cache_forcado(self) -> None:
        novos_abertos = self._obter_abertura_turbo()
        try:
            lucros = self._api.get_all_profit()
        except Exception as erro:
            print(f" [mercado] falha ao consultar payout (get_all_profit): {erro!r}")
            lucros = {}
        novos_payouts = {}
        faltando_payout = []
        for ativo in self.config.ativos:
            # Para OTC (ex: "EURUSD-OTC") a API retorna a chave como "EURUSD-op".
            # Tentamos: nome-exato → base+"-op" → base → nome+"-op" → variantes lowercase.
            base = ativo[:-4] if ativo.upper().endswith("-OTC") else ativo
            entrada = None
            for chave in (ativo, f"{base}-op", base, f"{ativo}-op",
                          ativo.lower(), f"{base.lower()}-op", base.lower()):
                candidato = lucros.get(chave)
                if isinstance(candidato, dict):
                    entrada = candidato
                    break
            # Tenta "turbo" primeiro (opção de 5 min); fallback para "binary"
            if isinstance(entrada, dict):
                valor = entrada.get("turbo") if entrada.get("turbo") is not None else entrada.get("binary")
            else:
                valor = None
            novos_payouts[ativo] = float(valor) if isinstance(valor, (int, float)) else None
            if novos_payouts[ativo] is None:
                faltando_payout.append(ativo)
        if faltando_payout and lucros:
            print(f" [mercado] payout indisponivel pra: {faltando_payout}")
            for ativo_faltante in faltando_payout:
                busca = ativo_faltante.split("-")[0].lower()
                parecidos = [k for k in lucros.keys() if busca in k.lower()]
                print(f" [mercado] chaves de payout parecidas com '{ativo_faltante}': {parecidos or 'NENHUM'}")
        self._mercado_aberto = novos_abertos
        self._payouts = novos_payouts
        self._cache_atualizado = time.time()

    def _atualizar_cache_se_preciso(self) -> None:
        if time.time() - self._cache_atualizado >= self.config.cache_mercado_segundos:
            self._atualizar_cache_forcado()

    def _reconectar(self) -> None:
        api_nova = self._nova_conexao()
        api_antiga = self._api
        self._api = api_nova
        self._iniciar_streams()
        self._cache_atualizado = 0.0
        try:
            if api_antiga:
                for ativo in self.config.ativos:
                    api_antiga.stop_candles_stream(ativo, self.config.timeframe_segundos)
        except Exception:
            pass

    def _snapshot_uma_vez(self, ativo: str) -> SnapshotMercado:
        if ativo not in self.config.ativos:
            raise ValueError(f"Ativo fora da configuração: {ativo}")
        self._atualizar_cache_se_preciso()

        with self._lock_api:
            bruto = self._api.get_realtime_candles(ativo, self.config.timeframe_segundos)
            timestamp_servidor = int(self._api.get_server_timestamp())

        linhas = [
            {
                "from": ts,
                "open": candle["open"],
                "close": candle["close"],
                "min": candle["min"],
                "max": candle["max"],
                "volume": candle.get("volume", 0),
            }
            for ts, candle in bruto.items()
        ]

        with self._lock_buffers:
            if linhas:
                recentes = self._candles_para_df(linhas)
                combinado = pd.concat([self._buffers[ativo], recentes])
                combinado = combinado[~combinado.index.duplicated(keep="last")]
                self._buffers[ativo] = combinado.sort_index().tail(self.config.limite_candles)
            if ativo not in self._buffers or len(self._buffers[ativo]) < 3:
                raise MercadoIndisponivel(f"Sem candles suficientes para {ativo}.")
            buffer_local = self._buffers[ativo].copy()

        if self._mercado_aberto.get(ativo, False):
            ultimo = pd.Timestamp(buffer_local.index[-1])
            ultimo_epoch = int(ultimo.tz_localize("UTC").timestamp()) if ultimo.tzinfo is None else int(ultimo.timestamp())
            if timestamp_servidor - ultimo_epoch > self.config.timeframe_segundos * 2:
                raise MercadoIndisponivel(f"Stream de {ativo} está atrasado.")

        return SnapshotMercado(
            ativo=ativo,
            candles=buffer_local,
            payout=self._payouts.get(ativo),
            mercado_aberto=self._mercado_aberto.get(ativo, False),
            timestamp_servidor=timestamp_servidor,
        )

    def snapshot(self, ativo: str) -> SnapshotMercado:
        try:
            return self._snapshot_uma_vez(ativo)
        except Exception as erro:
            raise MercadoIndisponivel(f"Falha ao ler snapshot de {ativo}: {erro}") from erro

    def reconectar_se_necessario(self) -> bool:
        """Tenta reconectar se a conexão parece morta. Thread-safe."""
        with self._lock_api:
            try:
                self._api.get_server_timestamp()
                return True
            except Exception:
                try:
                    self._reconectar()
                    return True
                except Exception:
                    return False

    def comprar(self, valor: float, ativo: str, direcao: str, expiracao_minutos: int) -> tuple[bool, object]:
        """Tenta comprar com retry e logging detalhado de diagnóstico."""
        with self._lock_api:
            id_fresco = self._ids_ativos.get(ativo)
            payout = self._payouts.get(ativo)
            aberto = self._mercado_aberto.get(ativo, False)
            print(f" [DIAG] {ativo}: id={id_fresco}, payout={payout}, aberto={aberto}, valor={valor}, exp={expiracao_minutos}")

            # Tentativa 1: buyv3 com ID numérico (método original, mais confiável)
            if id_fresco is not None:
                print(f" [DIAG] {ativo}: tentando buyv3 com id={id_fresco}...")
                try:
                    resultado, id_ordem = self._comprar_com_id(valor, id_fresco, direcao, expiracao_minutos)
                    if resultado:
                        print(f" [DIAG] {ativo}: buyv3 SUCESSO id={id_ordem}")
                        return True, id_ordem
                    else:
                        print(f" [DIAG] {ativo}: buyv3 recusado: {id_ordem}")
                        if isinstance(id_ordem, str) and "not available" in id_ordem.lower():
                            return False, id_ordem
                except Exception as e:
                    print(f" [DIAG] {ativo}: buyv3 ERRO: {e!r}")

            # Tentativa 2: buy() oficial com string do ativo
            print(f" [DIAG] {ativo}: tentando buy() oficial...")
            try:
                resultado, id_ordem = self._api.buy(valor, ativo, direcao, expiracao_minutos)
                if resultado:
                    print(f" [DIAG] {ativo}: buy() SUCESSO id={id_ordem}")
                    return True, id_ordem
                else:
                    print(f" [DIAG] {ativo}: buy() recusado: {id_ordem}")
            except Exception as e:
                print(f" [DIAG] {ativo}: buy() ERRO: {e!r}")

        # Tentativa 3: lock liberado durante o sleep para não bloquear snapshots
        print(f" [DIAG] {ativo}: retry após 1s...")
        time.sleep(1.0)
        with self._lock_api:
            if id_fresco is not None:
                try:
                    resultado, id_ordem = self._comprar_com_id(valor, id_fresco, direcao, expiracao_minutos)
                    if resultado:
                        print(f" [DIAG] {ativo}: retry buyv3 SUCESSO id={id_ordem}")
                        return True, id_ordem
                except Exception:
                    pass
            try:
                resultado, id_ordem = self._api.buy(valor, ativo, direcao, expiracao_minutos)
                if resultado:
                    print(f" [DIAG] {ativo}: retry buy() SUCESSO id={id_ordem}")
                    return True, id_ordem
                else:
                    print(f" [DIAG] {ativo}: retry buy() recusado: {id_ordem}")
                    return False, id_ordem
            except Exception as e:
                print(f" [DIAG] {ativo}: retry buy() ERRO: {e!r}")
                return False, str(e)

    def _comprar_com_id(self, valor: float, id_ativo: int, direcao: str, expiracao_minutos: int) -> tuple[bool, object]:
        api_baixo_nivel = self._api.api
        req_id = f"buy_{int(time.time()*1000)}"
        api_baixo_nivel.buy_multi_option = {}
        api_baixo_nivel.result = None
        api_baixo_nivel.buyv3(valor, id_ativo, direcao, expiracao_minutos, req_id)
        limite = time.monotonic() + 5.0
        id_ordem = None
        while api_baixo_nivel.result is None or id_ordem is None:
            info = api_baixo_nivel.buy_multi_option.get(req_id, {})
            if "message" in info:
                return False, info["message"]
            id_ordem = info.get("id")
            if time.monotonic() >= limite:
                return False, "timeout_buyv3"
            time.sleep(0.02)
        return api_baixo_nivel.result, api_baixo_nivel.buy_multi_option[req_id]["id"]

    def aguardar_resultado(self, id_ordem: object, expiracao_minutos: int | None = None) -> object:
        if not self.config.confiar_resultado_automatico:
            return None
        minutos = expiracao_minutos if expiracao_minutos is not None else self.config.expiracao_minutos
        espera_inicial = max(0.0, minutos * 60 - 30)
        time.sleep(espera_inicial)
        limite = time.monotonic() + 120
        while time.monotonic() < limite:
            resultado = self.consultar_resultado(id_ordem, timeout_segundos=6.0)
            if resultado is not None:
                return resultado
            time.sleep(3.0)
        return None

    def resultado_por_candle(
        self,
        ativo: str,
        direcao: str,
        preco_entrada: float,
        candle_hora,
        timeout_segundos: float,
    ) -> str | None:
        """Verifica o resultado da opção binária no fechamento do candle de expiração.

        A ordem é enviada na abertura do candle `candle_hora` e expira no fechamento
        desse mesmo candle (5 min depois). Esperamos o próximo candle aparecer no
        buffer como prova de que o candle de expiração já fechou, e usamos o Close
        do próprio candle de expiração (não do seguinte).
        """
        alvo = pd.Timestamp(candle_hora)
        limite = time.monotonic() + timeout_segundos
        while time.monotonic() < limite:
            with self._lock_buffers:
                buffer = self._buffers.get(ativo)
            if buffer is not None and alvo in buffer.index:
                posicao = buffer.index.get_loc(alvo)
                if isinstance(posicao, int):
                    # Se existe um candle posterior, o candle 'alvo' já fechou
                    if posicao < len(buffer.index) - 1:
                        fechamento = float(buffer.iloc[posicao]["Close"])
                        # Compara fechamento vs preco_entrada (resultado da opção binária),
                        # não vs abertura do candle (que pode diferir do preço de entrada).
                        if abs(fechamento - preco_entrada) < 1e-8:
                            return "equal"
                        venceu = (fechamento > preco_entrada) if direcao == "call" else (fechamento < preco_entrada)
                        print(f" [DIAG-RES] {ativo}: expiracao={alvo}, entrada={preco_entrada:.5f}, "
                              f"fechamento={fechamento:.5f}, "
                              f"direcao={direcao}, resultado={'win' if venceu else 'loss'}")
                        return "win" if venceu else "loss"
                    # Se não existe posterior, o candle ainda está se formando
            time.sleep(2.0)
        print(f" [DIAG-RES] {ativo}: timeout após {timeout_segundos}s esperando fechamento de {alvo}")
        return None

    _CAMPOS_CONFIRMA_OPCAO = ("active_id", "amount", "deposit", "win", "expired", "direction")

    @classmethod
    def _parece_registro_de_opcao(cls, dados: dict) -> bool:
        return any(campo in dados for campo in cls._CAMPOS_CONFIRMA_OPCAO)

    @classmethod
    def _localizar_ordem_historico(cls, dados, id_ordem: str) -> dict | None:
        if isinstance(dados, dict):
            identificadores = (
                dados.get("id"),
                dados.get("order_id"),
                dados.get("external_id"),
                dados.get("option_id"),
            )
            ids_compostos = dados.get("order_ids", [])
            bate_id = any(str(valor) == id_ordem for valor in identificadores if valor is not None) or (
                isinstance(ids_compostos, list)
                and any(str(valor) == id_ordem for valor in ids_compostos)
            )
            if bate_id and cls._parece_registro_de_opcao(dados):
                return dados
            for valor in dados.values():
                encontrada = cls._localizar_ordem_historico(valor, id_ordem)
                if encontrada is not None:
                    return encontrada
        elif isinstance(dados, list):
            for valor in dados:
                encontrada = cls._localizar_ordem_historico(valor, id_ordem)
                if encontrada is not None:
                    return encontrada
        return None

    @staticmethod
    def _extrair_lucro_historico(ordem: dict) -> object | None:
        # Rejeita opções ainda abertas — a IQ pode retornar win="win" antes de expirar
        status = str(ordem.get("status", "")).strip().lower()
        if status in {"open", "pending", "in-progress", "in_progress", "active"}:
            return None
        for chave in ("pnl_net", "net_profit"):
            if ordem.get(chave) is not None:
                try:
                    return float(ordem[chave])
                except (TypeError, ValueError):
                    pass
        resultado = str(ordem.get("win", ordem.get("status", ""))).strip().lower()
        if resultado in {"win", "won", "loss", "loose", "equal", "draw"}:
            return resultado
        return None

    def consultar_resultado(
        self, id_ordem: object, timeout_segundos: float = 6.0
    ) -> object | None:
        api_baixo_nivel = self._api.api
        api_baixo_nivel.get_options_v2_data = None
        api_baixo_nivel.get_options_v2(500, "binary,turbo")
        limite = time.monotonic() + timeout_segundos
        while api_baixo_nivel.get_options_v2_data is None:
            if time.monotonic() >= limite:
                return None
            time.sleep(0.05)
        ordem = self._localizar_ordem_historico(
            api_baixo_nivel.get_options_v2_data, str(id_ordem)
        )
        return None if ordem is None else self._extrair_lucro_historico(ordem)

    def fechar(self) -> None:
        with self._lock_api:
            if not self._api:
                return
            for ativo in self.config.ativos:
                try:
                    self._api.stop_candles_stream(ativo, self.config.timeframe_segundos)
                except Exception:
                    pass
