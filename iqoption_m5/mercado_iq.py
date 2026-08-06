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
        """Conexão crua para ferramentas que apenas leem histórico (backtest).

        Não abre streams nem monta o cache de payout: nada aqui habilita o
        envio de ordens, só a leitura de candles antigos.
        """
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
        """Lê somente turbo; get_all_open_time também consulta digital e pode travar."""
        abertos = {ativo: False for ativo in self.config.ativos}
        try:
            dados = self._api.get_all_init_v2()
            turbo = dados.get("turbo", {}).get("actives", {}) if isinstance(dados, dict) else {}
            if not turbo:
                print(f"    [mercado] get_all_init_v2 voltou sem 'turbo.actives'. Chaves recebidas: {list(dados.keys()) if isinstance(dados, dict) else type(dados)}")
            nomes_vistos = []
            resolvidos = set()
            for chave, detalhe in turbo.items():
                if not isinstance(detalhe, dict):
                    continue
                nome = str(detalhe.get("name", "")).split(".", 1)[-1]
                nomes_vistos.append(nome)
                # A IQ retorna nomes como "EURUSD-op" pra pares reais e
                # as vezes "USDJPY-op" no lugar de "USDJPY-OTC". Geramos
                # todos os candidatos possiveis pra bater com o config.
                candidatos = {nome}
                if nome.endswith("-op"):
                    base = nome[: -len("-op")]
                    candidatos.add(base)
                    candidatos.add(f"{base}-OTC")
                aberto = bool(detalhe.get("enabled", False)) and not bool(detalhe.get("is_suspended", False))
                for candidato in candidatos:
                    if candidato in abertos:
                        abertos[candidato] = aberto
                        resolvidos.add(candidato)
                        # A biblioteca usa uma tabela fixa e desatualizada de
                        # IDs internos (iqoptionapi/constants.py). A chave
                        # deste dicionario e o ID de verdade, atualizado pela
                        # propria IQ — sobrescrevemos a tabela da lib com ele
                        # pra "comprar" nao ser recusado por ID obsoleto.
                        try:
                            self._ids_ativos[candidato] = int(chave)
                        except (TypeError, ValueError):
                            pass
            faltando = [a for a in self.config.ativos if a not in resolvidos]
            if faltando:
                print(f"    [mercado] ativos configurados mas NAO encontrados na resposta da IQ: {faltando}")
                # Busca direta por cada faltante (em vez de so mostrar os
                # primeiros 40 em ordem alfabetica, que escondia nomes com
                # letra inicial tardia tipo USDCHF/USDJPY-OTC).
                for ativo_faltante in faltando:
                    parecidos = [n for n in nomes_vistos if ativo_faltante.split("-")[0] in n]
                    print(f"    [mercado] nomes parecidos com '{ativo_faltante}' na resposta: {parecidos or 'NENHUM'}")
        except Exception as erro:
            # Falha fechada: sem confirmação da IQ, nenhuma ordem é autorizada.
            print(f"    [mercado] falha ao consultar abertura turbo, marcando tudo como fechado: {erro!r}")
        return abertos

    def _atualizar_cache_forcado(self) -> None:
        novos_abertos = self._obter_abertura_turbo()
        try:
            lucros = self._api.get_all_profit()
        except Exception as erro:
            print(f"    [mercado] falha ao consultar payout (get_all_profit): {erro!r}")
            lucros = {}
        novos_payouts = {}
        faltando_payout = []
        for ativo in self.config.ativos:
            # Tenta várias variações de chave:
            # 1. chave exata ("USDJPY-OTC")
            # 2. chave com "-op" ("EURUSD-op")
            # 3. para OTC, base sem sufixo + "-op" ("USDJPY-op")
            candidatos = [ativo, f"{ativo}-op"]
            if ativo.upper().endswith("-OTC"):
                base = ativo[:-4]
                candidatos += [base, f"{base}-op"]
            entrada = None
            for chave in candidatos:
                entrada = lucros.get(chave)
                if entrada:
                    break
            # Ativos OTC usam chave "binary"; pares normais em turbo usam "turbo".
            valor = None
            if isinstance(entrada, dict):
                valor = entrada.get("turbo") or entrada.get("binary")
            novos_payouts[ativo] = float(valor) if isinstance(valor, (int, float)) else None
            if novos_payouts[ativo] is None:
                faltando_payout.append(ativo)
        if faltando_payout and lucros:
            print(f"    [mercado] payout indisponivel pra: {faltando_payout}")
            for ativo_faltante in faltando_payout:
                parecidos = [k for k in lucros.keys() if ativo_faltante.split("-")[0] in k]
                print(f"    [mercado] chaves de payout parecidas com '{ativo_faltante}': {parecidos or 'NENHUM'}")
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
        bruto = self._api.get_realtime_candles(ativo, self.config.timeframe_segundos)
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
        if linhas:
            recentes = self._candles_para_df(linhas)
            combinado = pd.concat([self._buffers[ativo], recentes])
            combinado = combinado[~combinado.index.duplicated(keep="last")]
            self._buffers[ativo] = combinado.sort_index().tail(self.config.limite_candles)
        if ativo not in self._buffers or len(self._buffers[ativo]) < 3:
            raise MercadoIndisponivel(f"Sem candles suficientes para {ativo}.")
        timestamp_servidor = int(self._api.get_server_timestamp())
        if self._mercado_aberto.get(ativo, False):
            ultimo = pd.Timestamp(self._buffers[ativo].index[-1])
            ultimo_epoch = int(ultimo.tz_localize("UTC").timestamp()) if ultimo.tzinfo is None else int(ultimo.timestamp())
            if timestamp_servidor - ultimo_epoch > self.config.timeframe_segundos * 2:
                raise MercadoIndisponivel(f"Stream de {ativo} está atrasado.")
        return SnapshotMercado(
            ativo=ativo,
            candles=self._buffers[ativo].copy(),
            payout=self._payouts.get(ativo),
            mercado_aberto=self._mercado_aberto.get(ativo, False),
            timestamp_servidor=timestamp_servidor,
        )

    def snapshot(self, ativo: str) -> SnapshotMercado:
        with self._lock_api:
            try:
                return self._snapshot_uma_vez(ativo)
            except Exception as primeiro_erro:
                try:
                    self._reconectar()
                    return self._snapshot_uma_vez(ativo)
                except Exception as segundo_erro:
                    raise MercadoIndisponivel(
                        f"Falha após reconexão ({primeiro_erro}; {segundo_erro})"
                    ) from segundo_erro

    def comprar(self, valor: float, ativo: str, direcao: str, expiracao_minutos: int) -> tuple[bool, object]:
        with self._lock_api:
            id_fresco = self._ids_ativos.get(ativo)
            if id_fresco is None:
                return self._api.buy(valor, ativo, direcao, expiracao_minutos)
            # NUNCA sobrescrever OP_code.ACTIVES aqui: essa tabela e global e
            # compartilhada tambem pelo decodificador de mensagens do stream
            # (client.py faz o caminho inverso, id -> nome). Trocar o valor
            # quebra a leitura de qualquer mensagem que ainda chegue com o ID
            # antigo e derruba a thread de mensagens em loop de erro. Em vez
            # disso, chamamos buyv3 direto com o ID certo, sem tocar na tabela.
            return self._comprar_com_id(valor, id_fresco, direcao, expiracao_minutos)

    def _comprar_com_id(self, valor: float, id_ativo: int, direcao: str, expiracao_minutos: int) -> tuple[bool, object]:
        api_baixo_nivel = self._api.api
        req_id = "buy"
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
                return False, None
            time.sleep(0.02)
        return api_baixo_nivel.result, api_baixo_nivel.buy_multi_option[req_id]["id"]

    def aguardar_resultado(self, id_ordem: object) -> object:
        """Espera o resultado por consulta ao historico, nao pelo stream.

        check_win/check_win_v4 dependem de uma notificacao especifica do
        websocket que, se o ID interno do ativo mudou (o mesmo caso do
        '-op'), pode nunca chegar — e check_win() da biblioteca instalada
        e um `while True` SEM timeout, travando a ordem pra sempre e
        segurando o risco em "ordem_ja_aberta" indefinidamente.
        consultar_resultado() ja usa a mesma consulta de historico que
        recupera ordens pendentes no restart, e tem timeout embutido.
        """
        if not self.config.confiar_resultado_automatico:
            # O historico da IQ ja devolveu "win" pra ordens que na
            # realidade foram perda (2 vezes confirmadas). Ate existir uma
            # verificacao independente (comparar preco de entrada com o
            # candle no fechamento da expiracao), e mais seguro assumir
            # perda tecnica sempre do que confiar num "win" que pode ser
            # mentira — um falso positivo infla a banca e deixa o pyramid
            # arriscar mais em cima de informacao errada.
            return None
        limite = time.monotonic() + self.config.expiracao_minutos * 60 + 90
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
        """Calcula o resultado sozinho, comparando o preço de entrada com o
        fechamento do candle de entrada — exatamente como a IQ decide quem
        ganha (CALL ganha se fechar acima, PUT ganha se fechar abaixo).

        Usa os candles que o bot já recebe via stream (os mesmos que geram
        os sinais), sem depender do histórico da IQ — que já devolveu "win"
        pra ordens que na realidade foram perda (confirmado 2 vezes com
        prints reais da conta). Mais lento pra descobrir o resultado (só
        depois que o candle seguinte aparece no buffer), mas não mente.
        """
        alvo = pd.Timestamp(candle_hora)
        limite = time.monotonic() + timeout_segundos
        while time.monotonic() < limite:
            with self._lock_api:
                buffer = self._buffers.get(ativo)
            if buffer is not None and alvo in buffer.index:
                posicao = buffer.index.get_loc(alvo)
                # So confia se ja existe candle DEPOIS dele no buffer — prova
                # que o candle de entrada fechou de vez, nao e mais o que
                # esta se formando agora (que ainda pode mudar de preco).
                if isinstance(posicao, int) and posicao < len(buffer.index) - 1:
                    fechamento = float(buffer.iloc[posicao + 1]["Close"])
                    if fechamento == preco_entrada:
                        return "equal"
                    subiu = fechamento > preco_entrada
                    venceu = subiu if direcao == "call" else not subiu
                    return "win" if venceu else "loss"
            time.sleep(2.0)
        return None

    _CAMPOS_CONFIRMAM_OPCAO = ("active_id", "amount", "deposit", "win", "expired", "direction")

    @classmethod
    def _parece_registro_de_opcao(cls, dados: dict) -> bool:
        """Reduz falso-positivo: um dict que só por acaso tem uma chave 'id'
        batendo com o numero da ordem (ex: metadado de instrumento, id de
        outro objeto qualquer) nao e uma opcao de verdade. Exige que o dict
        tambem pareça uma opcao (tenha pelo menos um desses campos)."""
        return any(campo in dados for campo in cls._CAMPOS_CONFIRMAM_OPCAO)

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
        # So confiamos em campos de lucro documentados (pnl_net/net_profit)
        # ou no rotulo texto (win/loss/equal). Formulas de subtracao entre
        # campos como profit_amount-amount ja mostraram devolver numeros
        # absurdos (ex: -1999998 numa aposta de R$2) quando o dict
        # encontrado nao e exatamente o que a gente espera — melhor nao ter
        # resultado do que confiar numa conta especulativa com campos que a
        # gente nao sabe ao certo o que significam nessa conta/regiao.
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
        """Busca uma ordem antiga no histórico sem usar as rotinas infinitas da biblioteca."""
        api_baixo_nivel = self._api.api
        api_baixo_nivel.get_options_v2_data = None
        api_baixo_nivel.get_options_v2(100, "binary,turbo")
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
