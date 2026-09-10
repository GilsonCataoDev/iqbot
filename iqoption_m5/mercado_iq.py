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


def iniciar_com_timeout(mercado, *, timeout_s: float = 45.0) -> tuple[bool, str]:
    """Não deixa a inicialização da IQ prender o processo inteiro.

    A biblioteca pode bloquear dentro de ``get_all_init_v2`` sem levantar
    exceção. A chamada roda em thread daemon para que o processo possa encerrar
    limpo e o usuário reinicie, em vez de ficar aparentando estar ativo.
    """
    erro: list[BaseException] = []

    def iniciar() -> None:
        try:
            mercado.iniciar()
        except BaseException as exc:  # devolve a falha para a thread principal
            erro.append(exc)

    thread = threading.Thread(target=iniciar, name="inicializacao-iq", daemon=True)
    thread.start()
    thread.join(max(1.0, float(timeout_s)))
    if thread.is_alive():
        return False, f"Tempo de conexão esgotado: IQ não respondeu em {timeout_s:g}s."
    if erro:
        return False, f"Falha ao iniciar a IQ: {erro[0]!r}"
    return True, ""


class MercadoIQ:
    """Adapter que esconde protocolo, cache, streams e reconexão da IQ."""

    def __init__(self, config: Configuracao):
        self.config = config
        self._api = None
        self._buffers: dict[str, pd.DataFrame] = {}
        # Streams adicionais pertencem ao laboratório multi-timeframe. Mantêm
        # buffers próprios para que M5 e M15 não sobrescrevam um ao outro.
        self._buffers_extras: dict[tuple[str, int], pd.DataFrame] = {}
        self._streams_extras: dict[int, int] = {}
        # Quando o stream WebSocket da IQ congela mas a conexão continua
        # marcada como aberta, usa get_candles pontualmente para manter a tela
        # viva. O intervalo evita uma rajada de chamadas para todos os pares.
        self._ultimo_fallback_stream: dict[tuple[str, int], float] = {}
        self._lock_api = threading.RLock()
        # Lock separado do _lock_api: a reconexão PRECISA rodar mesmo quando
        # uma thread está pendurada segurando _lock_api numa chamada morta.
        self._lock_reconexao = threading.Lock()
        self._lock_buffers = threading.Lock()
        # Sem este lock os 7 workers veem o cache vencido no mesmo instante e
        # disparam 7 refreshes simultaneos contra a IQ.
        self._lock_cache = threading.Lock()
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

    def _buscar_com_timeout(self, ativo: str, tf: int, n: int, timeout: float = 12.0) -> list:
        """get_candles com timeout — evita travar o loop quando o WebSocket cai."""
        result: list = [None]
        exc: list = [None]

        def _run() -> None:
            try:
                ts = self._api.get_server_timestamp()
                result[0] = self._api.get_candles(ativo, tf, n, ts)
            except Exception as e:
                exc[0] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise MercadoIndisponivel(f"Timeout ({timeout:.0f}s) buscando tf={tf}s para {ativo}")
        if exc[0] is not None:
            raise MercadoIndisponivel(str(exc[0]))
        if result[0] is None:
            raise MercadoIndisponivel(f"Sem dados para {ativo} tf={tf}")
        return result[0]

    def _buscar_realtime_com_timeout(
        self, ativo: str, tf: int, timeout: float = 3.0
    ) -> dict:
        """Lê o stream sem deixar a iqoptionapi prender o ciclo do Lab.

        ``get_realtime_candles`` parece local, mas pode ficar bloqueada quando
        o WebSocket entra em half-open. Sem limite, o primeiro par trava a
        atualização de todos os gráficos e impede o watchdog de receber
        progresso. A thread é daemon de propósito: uma chamada velha nunca
        pode impedir a reconexão para uma API nova.
        """
        api = self._api
        if api is None:
            raise MercadoIndisponivel("IQ não conectada.")
        result: list[dict | None] = [None]
        exc: list[Exception | None] = [None]

        def _run() -> None:
            try:
                result[0] = api.get_realtime_candles(ativo, tf)
            except Exception as erro:
                exc[0] = erro

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise MercadoIndisponivel(
                f"Timeout ({timeout:.0f}s) no stream ao vivo de {ativo} tf={tf}"
            )
        if exc[0] is not None:
            raise MercadoIndisponivel(str(exc[0]))
        if not isinstance(result[0], dict):
            raise MercadoIndisponivel(f"Stream inválido para {ativo} tf={tf}")
        return result[0]

    @staticmethod
    def _somente_fechados(
        candles: pd.DataFrame, timeframe_segundos: int, timestamp_servidor: float
    ) -> pd.DataFrame:
        """Remove o último candle superior enquanto ele ainda estiver formando."""
        limite = pd.to_datetime(timestamp_servidor, unit="s")
        fechamentos = candles.index + pd.to_timedelta(timeframe_segundos, unit="s")
        return candles.loc[fechamentos <= limite]

    def _buscar_contexto_fechado(self, ativo: str, tf: int, n: int) -> pd.DataFrame:
        # Busca um candle extra porque o mais recente normalmente ainda está aberto.
        df = self._candles_para_df(self._buscar_com_timeout(ativo, tf, n + 1))
        agora = float(self._api.get_server_timestamp())
        return self._somente_fechados(df, tf, agora).tail(n)

    def buscar_h4(self, ativo: str, n: int | None = None) -> pd.DataFrame:
        """Busca N candles H4 sem stream (leitura pontual para tendência macro)."""
        n = n if n is not None else self.config.h4_num_candles
        return self._buscar_contexto_fechado(ativo, 14400, n)

    def buscar_h1(self, ativo: str, n: int | None = None) -> pd.DataFrame:
        """Busca N candles H1 sem stream (leitura pontual para contexto H1)."""
        n = n if n is not None else self.config.h1_num_candles
        return self._buscar_contexto_fechado(ativo, 3600, n)

    def buscar_m5(self, ativo: str, n: int | None = None) -> pd.DataFrame:
        """Busca N candles M5 sem stream (leitura pontual para estrutura M5)."""
        n = n if n is not None else self.config.m5_num_candles
        return self._buscar_contexto_fechado(ativo, 300, n)

    def buscar_m15(self, ativo: str, n: int | None = None) -> pd.DataFrame:
        """Busca N candles M15 sem stream (leitura pontual para contexto M15)."""
        n = n if n is not None else self.config.m15_num_candles
        return self._buscar_contexto_fechado(ativo, 900, n)

    def _iniciar_streams(self) -> None:
        for ativo in self.config.ativos:
            try:
                self._api.start_candles_stream(
                    ativo,
                    self.config.timeframe_segundos,
                    self.config.limite_candles,
                )
            except Exception as erro:
                # Um símbolo que a IQ não disponibiliza não pode impedir os
                # demais pares de iniciar.
                print(f" [mercado] {ativo}: stream indisponível ({erro})")

    def iniciar_timeframes_extras(self, timeframes: dict[int, int]) -> None:
        """Ativa streams adicionais em uma única conexão IQ.

        Interface pequena do laboratório: ``{timeframe_em_segundos: candles}``.
        O stream principal continua intacto para os monitores atuais.
        """
        extras = {
            int(tf): int(limite)
            for tf, limite in timeframes.items()
            if int(tf) != self.config.timeframe_segundos
        }
        if not extras:
            return
        with self._lock_api:
            if self._api is None:
                raise MercadoIndisponivel("Conecte antes de iniciar timeframes extras.")
            self._iniciar_timeframes_extras_sem_lock(extras)

    def _iniciar_timeframes_extras_sem_lock(
        self, extras: dict[int, int], recarregar_historico: bool = True
    ) -> None:
        """Inicia extras sem adquirir ``_lock_api`` novamente.

        A reconexão de emergência pode ocorrer enquanto uma chamada antiga da
        IQ segura esse lock. Nesse caso, tentar chamar o método público aqui
        recriaria o deadlock e o gráfico continuaria congelado.
        """
        if self._api is None:
            raise MercadoIndisponivel("Conecte antes de iniciar timeframes extras.")
        for tf, limite in extras.items():
            for ativo in self.config.ativos:
                try:
                    chave = (ativo, tf)
                    if recarregar_historico or chave not in self._buffers_extras:
                        dados = self._buscar_com_timeout(ativo, tf, limite)
                        self._buffers_extras[chave] = self._candles_para_df(dados)
                    self._api.start_candles_stream(ativo, tf, limite)
                except Exception as erro:
                    print(f" [mercado] {ativo} tf={tf}s: stream indisponível ({erro})")
        self._streams_extras.update(extras)

    def conectar_somente_leitura(self):
        """Conexão crua para ferramentas que apenas leem histórico (backtest)."""
        return self._nova_conexao()

    def iniciar(self) -> None:
        self.config.validar()
        with self._lock_api:
            self._api = self._nova_conexao()
            for ativo in self.config.ativos:
                try:
                    # get_candles pode ficar pendurado quando um ativo OTC não
                    # existe na sessão. Limitar e isolar a falha por ativo.
                    dados = self._buscar_com_timeout(
                        ativo,
                        self.config.timeframe_segundos,
                        self.config.limite_candles,
                    )
                    self._buffers[ativo] = self._candles_para_df(dados)
                except Exception as erro:
                    print(f" [mercado] {ativo}: histórico indisponível ({erro})")
            self._iniciar_streams()
            self._atualizar_cache_forcado()

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

    @staticmethod
    def _nome_e_otc(detalhe: dict) -> tuple[str, str, bool]:
        """(nome_bruto, nome_do_ativo, é_otc) a partir do 'name' da resposta.

        A IQ distingue os mercados assim: GBPUSD-OTC = sintético OTC;
        GBPUSD-op = mercado normal. Tratar "-op" como OTC troca o instrumento
        no buyv3.
        """
        nome = str(detalhe.get("name", "")).split(".", 1)[-1].upper()
        if nome.endswith("-OTC"):
            return nome, nome, True
        if nome.endswith("-OP"):
            return nome, nome[: -len("-OP")], False
        return nome, nome, False

    def _obter_estado_mercado(self) -> tuple[dict[str, bool], dict[str, float | None]]:
        """Abertura e payout dos ativos, de uma única resposta get_all_init_v2.

        O get_all_profit() da lib usa o endpoint v1 (get_all_init), que a IQ
        parou de responder: ele fica em busy-wait de 30s e retenta pra sempre,
        sem NUNCA levantar exceção — travava a thread e enchia o log de
        "get_all_init late 30 sec". O v2 traz o mesmo option.profit.commission,
        então o payout sai daqui e o v1 não é mais tocado.
        """
        abertos = {ativo: False for ativo in self.config.ativos}
        payouts: dict[str, float | None] = {a: None for a in self.config.ativos}
        try:
            dados = self._api.get_all_init_v2()
            if not isinstance(dados, dict):
                print(f" [mercado] get_all_init_v2 retornou tipo inesperado: {type(dados)}")
                dados = {}
            # Busca em turbo e binary (OTC aparece em qualquer uma dependendo da hora)
            secoes = {}
            # Payout fica separado por seção: turbo é a preferência, binary o fallback.
            payout_secao: dict[str, dict[str, float]] = {"turbo": {}, "binary": {}}
            for secao in ("turbo", "binary"):
                parte = dados.get(secao, {}).get("actives", {})
                if not isinstance(parte, dict):
                    continue
                secoes.update(parte)
                for detalhe in parte.values():
                    if not isinstance(detalhe, dict):
                        continue
                    _, ativo_nome, _ = self._nome_e_otc(detalhe)
                    valor = self._payout_de(detalhe)
                    if valor is not None:
                        payout_secao[secao][ativo_nome] = valor
            if not secoes:
                print(f" [mercado] get_all_init_v2 sem actives. Mantendo estados anteriores.")
                # Resposta vazia — não resetar pares normais para fechado
                for ativo in self.config.ativos:
                    abertos[ativo] = self._mercado_aberto.get(ativo, ativo.upper().endswith("-OTC"))
                    payouts[ativo] = self._payouts.get(ativo)
                return abertos, payouts
            for ativo in self.config.ativos:
                valor = payout_secao["turbo"].get(ativo)
                if valor is None:
                    valor = payout_secao["binary"].get(ativo)
                payouts[ativo] = valor
            nomes_vistos = []
            resolvidos = set()
            for chave, detalhe in secoes.items():
                if not isinstance(detalhe, dict):
                    continue
                nome, candidato, is_otc_entry = self._nome_e_otc(detalhe)
                nomes_vistos.append(nome)
                enabled = bool(detalhe.get("enabled", False))
                # OTC: is_suspended é frequentemente incorreto — a IQ marca suspended
                # quando o mercado regular equivalente está aberto, mas o OTC pode
                # continuar disponível. Usa só enabled; a rejeição real vem do buyv3.
                if is_otc_entry:
                    aberto = enabled
                else:
                    aberto = enabled and not bool(detalhe.get("is_suspended", False))
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
        return abertos, payouts

    def _atualizar_cache_forcado(self) -> None:
        novos_abertos, novos_payouts = self._obter_estado_mercado()
        faltando_payout = [a for a, v in novos_payouts.items() if v is None]
        if faltando_payout:
            print(f" [mercado] payout indisponivel pra: {faltando_payout}")
        self._mercado_aberto = novos_abertos
        self._payouts = novos_payouts
        self._cache_atualizado = time.time()

    def _atualizar_cache_se_preciso(self) -> None:
        if time.time() - self._cache_atualizado < self.config.cache_mercado_segundos:
            return
        # Double-checked: sem o lock, os 7 workers passam pela checagem acima no
        # mesmo instante e disparam 7 refreshes simultaneos contra a IQ.
        with self._lock_cache:
            if time.time() - self._cache_atualizado < self.config.cache_mercado_segundos:
                return
            self._atualizar_cache_forcado()

    def _reconectar(self) -> None:
        api_nova = self._nova_conexao()
        api_antiga = self._api
        self._api = api_nova
        self._iniciar_streams()
        if self._streams_extras:
            # Reabre os dois streams sem criar uma segunda sessão IQ.
            extras = dict(self._streams_extras)
            self._streams_extras.clear()
            # Não use o método público: a reconexão pode estar acontecendo
            # justamente porque outra thread travou segurando _lock_api.
            # Os buffers anteriores continuam válidos. Baixar novamente o
            # histórico de cada ativo aqui podia segurar o lock por minutos e
            # impedir a primeira atualização do Lab após o watchdog.
            self._iniciar_timeframes_extras_sem_lock(
                extras, recarregar_historico=False
            )
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
            bruto = self._buscar_realtime_com_timeout(ativo, self.config.timeframe_segundos)
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
        tf = self.config.timeframe_segundos
        ultimo_stream = max((int(item["from"]) for item in linhas), default=0)
        stream_atrasado = (
            self._mercado_aberto.get(ativo, False)
            and (not ultimo_stream or timestamp_servidor - ultimo_stream > tf * 2)
        )

        with self._lock_buffers:
            if linhas:
                recentes = self._candles_para_df(linhas)
                combinado = pd.concat([self._buffers[ativo], recentes])
                combinado = combinado[~combinado.index.duplicated(keep="last")]
                self._buffers[ativo] = combinado.sort_index().tail(self.config.limite_candles)
            if ativo not in self._buffers or len(self._buffers[ativo]) < 3:
                raise MercadoIndisponivel(f"Sem candles suficientes para {ativo}.")
            buffer_local = self._buffers[ativo].copy()

        if stream_atrasado:
            chave_fallback = (ativo, tf)
            agora_monotonico = time.monotonic()
            fallbacks = getattr(self, "_ultimo_fallback_stream", {})
            self._ultimo_fallback_stream = fallbacks
            ult_fallback = fallbacks.get(chave_fallback, float("-inf"))
            if agora_monotonico - ult_fallback >= 5.0:
                # O stream não avançou; get_candles costuma continuar
                # respondendo. Atualiza no máximo uma vez a cada 5s por ativo.
                fallbacks[chave_fallback] = agora_monotonico
                try:
                    direto = self._candles_para_df(
                        self._buscar_com_timeout(ativo, tf, self.config.limite_candles)
                    )
                    with self._lock_buffers:
                        combinado = pd.concat([self._buffers[ativo], direto])
                        combinado = combinado[~combinado.index.duplicated(keep="last")]
                        self._buffers[ativo] = combinado.sort_index().tail(self.config.limite_candles)
                        buffer_local = self._buffers[ativo].copy()
                except Exception as erro:
                    # A mensagem de atraso abaixo preserva a causa operacional
                    # sem encerrar todo o laboratório por uma leitura ruim.
                    print(f" [mercado] {ativo}: stream atrasado; fallback falhou ({erro})")

        if self._mercado_aberto.get(ativo, False):
            ultimo = pd.Timestamp(buffer_local.index[-1])
            ultimo_epoch = int(ultimo.tz_localize("UTC").timestamp()) if ultimo.tzinfo is None else int(ultimo.timestamp())
            if timestamp_servidor - ultimo_epoch > tf * 2:
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

    def snapshot_timeframe(self, ativo: str, timeframe_segundos: int) -> SnapshotMercado:
        """Snapshot ao vivo de um stream extra, na mesma conexão IQ.

        O chamador deve ativar o timeframe antes por ``iniciar_timeframes_extras``.
        """
        tf = int(timeframe_segundos)
        if tf == self.config.timeframe_segundos:
            return self.snapshot(ativo)
        if ativo not in self.config.ativos:
            raise ValueError(f"Ativo fora da configuração: {ativo}")
        limite = self._streams_extras.get(tf)
        if limite is None:
            raise MercadoIndisponivel(f"Stream tf={tf}s não foi iniciado.")
        self._atualizar_cache_se_preciso()
        try:
            with self._lock_api:
                bruto = self._buscar_realtime_com_timeout(ativo, tf)
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
            chave = (ativo, tf)
            with self._lock_buffers:
                if linhas:
                    recentes = self._candles_para_df(linhas)
                    anterior = self._buffers_extras.get(chave)
                    combinado = pd.concat([anterior, recentes]) if anterior is not None else recentes
                    combinado = combinado[~combinado.index.duplicated(keep="last")]
                    self._buffers_extras[chave] = combinado.sort_index().tail(limite)
                buffer = self._buffers_extras.get(chave)
                if buffer is None or len(buffer) < 3:
                    raise MercadoIndisponivel(f"Sem candles suficientes tf={tf}s para {ativo}.")
                buffer_local = buffer.copy()
            if self._mercado_aberto.get(ativo, False):
                ultimo = pd.Timestamp(buffer_local.index[-1])
                ultimo_epoch = int(ultimo.tz_localize("UTC").timestamp()) if ultimo.tzinfo is None else int(ultimo.timestamp())
                if timestamp_servidor - ultimo_epoch > tf * 2:
                    raise MercadoIndisponivel(f"Stream tf={tf}s de {ativo} está atrasado.")
            return SnapshotMercado(
                ativo=ativo,
                candles=buffer_local,
                payout=self._payouts.get(ativo),
                mercado_aberto=self._mercado_aberto.get(ativo, False),
                timestamp_servidor=timestamp_servidor,
            )
        except Exception as erro:
            raise MercadoIndisponivel(
                f"Falha ao ler snapshot tf={tf}s de {ativo}: {erro}"
            ) from erro

    def timestamp_servidor(self) -> float:
        """Lê o relógio atual da IQ no último instante antes de uma ordem."""
        with self._lock_api:
            return float(self._api.get_server_timestamp())

    # Tolerância de atraso do timestamp do servidor antes de considerar o
    # socket morto. O timesync chega a cada poucos segundos; 90s é folgado.
    _MAX_ATRASO_TIMESTAMP = 90.0

    def conexao_viva(self) -> bool:  # NÃO pega _lock_api: ver reconectar_se_necessario
        """Liveness real do WebSocket.

        NÃO usar get_server_timestamp() sozinho como sonda: é uma property que
        devolve o último valor recebido em cache e NUNCA levanta exceção com o
        socket morto (iqoptionapi/stable_api.py: `return
        self.api.timesync.server_timestamp`). Foi exatamente isso que deixou o
        bot 25min travado em 2026-08-27 — a sonda respondia "vivo" a cada 60s
        enquanto o socket estava fechado desde as 10:31, então _reconectar()
        nunca era chamado e o watchdog só imprimia alerta.

        Duas checagens complementares:
        - check_connect(): lê o flag que o handler de on_close zera. Pega
          desconexão limpa.
        - frescura do timestamp: pega o caso do socket tecnicamente aberto mas
          sem tráfego (half-open), em que check_connect ainda diz True.
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

    # Quanto esperar pelo _lock_api antes de concluir que alguém travou com ele.
    _TIMEOUT_LOCK_RECONEXAO = 5.0

    def reconectar_se_necessario(self, forcar: bool = False) -> bool:
        """Reconecta se a conexão estiver morta. Thread-safe.

        forcar=True pula a sonda — usado pelo watchdog, que já provou que o
        loop parou de progredir.

        POR QUE NÃO USA `with self._lock_api`: quando o WebSocket morre, a
        thread do loop principal fica pendurada dentro de uma chamada da
        iqoptionapi SEGURANDO o _lock_api (ex.: get_realtime_candles em
        _snapshot_uma_vez). Se a reconexão também esperasse esse lock, ela
        nunca rodaria — deadlock. Foi o que aconteceu em 2026-08-27 11:18:
        o socket caiu, a thread de reconexão de 60s ficou bloqueada no lock e
        o bot congelou de vez.

        Estratégia: tenta pegar o lock por alguns segundos. Se conseguir,
        reconecta pelo caminho normal. Se NÃO conseguir, isso por si só prova
        que alguém travou com ele — então reconecta assim mesmo, protegido por
        um lock próprio. É seguro porque _reconectar() constrói um objeto de
        API novo e só troca a referência: a thread pendurada continua com o
        objeto velho (já inútil) e morre sozinha quando/se destravar.
        """
        if not forcar and self.conexao_viva():
            return True

        motivo = "forçada pelo watchdog" if forcar else "sonda detectou socket morto"
        tem_lock = self._lock_api.acquire(timeout=self._TIMEOUT_LOCK_RECONEXAO)
        if not tem_lock:
            motivo += "; _lock_api preso (thread travada) — reconectando por fora"
        try:
            with self._lock_reconexao:
                # Outra thread pode ter reconectado enquanto esperávamos.
                if not forcar and self.conexao_viva():
                    return True
                print(f"[CONEXAO] Reconectando ({motivo})...")
                try:
                    self._reconectar()
                    print("[CONEXAO] Reconexão concluída.")
                    return True
                except Exception as e:
                    print(f"[CONEXAO] Reconexão FALHOU: {e!r}")
                    return False
        finally:
            if tem_lock:
                self._lock_api.release()

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
        print(f" [DIAG] {ativo}: retry após 0.2s...")
        time.sleep(0.2)
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
        # Espera ATE DEPOIS da expiracao real (nao antes). Consultar a IQ antes do
        # fechamento pode retornar pnl_net provisorio de uma opcao ainda aberta se
        # o filtro de status em _extrair_lucro_historico nao reconhecer o campo
        # exato que a IQ usa pra "ainda aberta" — resultado: grava win/loss que
        # ainda pode reverter nos segundos finais do candle.
        espera_inicial = minutos * 60 + 5
        time.sleep(espera_inicial)
        limite = time.monotonic() + 600
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
            # A IQ devolve `id` como lista em closed_options (mesmo quando há
            # uma única operação). Tratar a lista como IDs compostos; caso
            # contrário resultados reais ficam para sempre como "aberta".
            ids_listados = [
                item
                for valor in identificadores
                if isinstance(valor, (list, tuple, set))
                for item in valor
            ]
            bate_id = any(
                str(valor) == id_ordem
                for valor in identificadores
                if valor is not None and not isinstance(valor, (list, tuple, set))
            ) or any(str(valor) == id_ordem for valor in ids_listados) or (
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
                for tf in self._streams_extras:
                    try:
                        self._api.stop_candles_stream(ativo, tf)
                    except Exception:
                        pass
