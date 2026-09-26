#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auditoria do RPA de reposicao de limite -- `credito_reposicao_rodada` + `_log`.

Pedido do Kaique (20/ago/2026): "cada rodagem atualize algum local do banco de
dados onde poderemos ver a data, a pessoa, o valor antes e o valor depois da
modificacao do RPA".

⚠️ ESTE E O UNICO REGISTRO DE UM EFEITO QUE ACONTECE FORA DA TORRE. O limite mora
no Mercos, que nao tem historico visivel pra nos. Se a linha nao for gravada aqui,
ninguem consegue reconstruir depois o que o robo fez -- nem desfazer.

Daí as tres decisoes deste modulo:

 1. A linha do log e ABERTA ANTES do clique em Salvar (com o valor ANTES) e
    FECHADA depois (com o valor DEPOIS). Se o processo morrer no meio da escrita,
    fica a prova de que a tentativa existiu -- `escreveu=true` +
    `confirmado_na_tela=null` e exatamente o caso "pode ter gravado e nao sei",
    que a view `v_credito_reposicao_conferir` entrega pra olho humano.

 2. SEM AUDITORIA, NAO ESCREVE. Se o INSERT do log falhar em modo `aplicar`, o
    robo aborta em vez de mexer no Mercos. Escrever sem registro e pior que nao
    escrever: o dinheiro muda de lugar e ninguem sabe quem mandou.

 3. `total_antes`/`total_depois` sao gravados mesmo o robo NAO podendo alterar o
    total. E o que PROVA que ele nao alterou -- auditoria que so guarda o campo
    mudado nao consegue provar o que ficou intacto.
"""

import os
import getpass
import json
from datetime import datetime, timedelta, timezone

# Brasil nao tem horario de verao desde 2019, entao UTC-3 fixo e correto e nao
# depende de tzdata no runner. Mesma razao de `fn_hoje_brt()` existir no banco:
# o servidor roda em UTC e "hoje" nele nao e "hoje" aqui (convencao critica #19).
BRT = timezone(timedelta(hours=-3))


def hoje_brt() -> str:
    """Data de hoje em Brasilia, `YYYY-MM-DD`. E a chave da trava do cron."""
    return datetime.now(BRT).date().isoformat()


class AuditoriaIndisponivel(RuntimeError):
    """Nao consegui registrar. Em modo `aplicar` isso ABORTA -- ver decisao 2."""


class RodadaDeCronJaFeita(RuntimeError):
    """O cron de hoje JA rodou -- e outra execucao tentou abrir a segunda.

    ⚠️ NAO E FALHA, e quem captura tem que sair com 0. E a trava do indice
    `ux_credito_reposicao_cron_dia` (migration 20260824e) funcionando: `schedule`
    dispara em TODO espelho do rodizio que tenha o workflow ativo, o `concurrency`
    do Actions e por repositorio, e o robo SOMA um delta -- duas rodadas no mesmo
    dia devolveriam o credito em dobro. A decisao e do banco, de proposito: uma
    consulta "ja rodou hoje?" nao resolve o caso simultaneo (as duas leem "nao" no
    mesmo segundo), uma UNIQUE resolve."""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def disparo_e_cron() -> bool:
    """A rodada nasceu do `schedule` do Actions (ninguem pediu)?

    ⚠️ `GITHUB_EVENT_NAME` e a UNICA fonte disso. Nao da pra deduzir do horario
    (uma pessoa pode rodar 19h31) nem do ator (ver `quem_disparou`)."""
    return (os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_EVENT_NAME") == "schedule")


def quem_disparou() -> tuple[str, str, str | None, str | None, str | None]:
    """(disparado_por, origem, run_url, run_id, git_sha).

    ⚠️ A PESSOA. No Actions, `GITHUB_ACTOR` e quem clicou em "Run workflow" — e o
    unico lugar onde essa informacao existe. Rodando local, o usuario da maquina.
    Nunca devolve vazio: rodada sem responsavel e o que a auditoria existe pra
    impedir.

    ⚠️ EXCECAO DO CRON (24/ago/2026): no gatilho `schedule` o `github.actor` e
    quem fez o ULTIMO PUSH na default branch -- uma pessoa que nao pediu esta
    rodada. Gravar o nome dela atribuiria a alguem uma escrita no Mercos que ela
    nao autorizou, e e justamente disso que esta coluna serve de prova. Entao o
    responsavel passa a ser o literal `cron`, e `origem` diz `github-cron`."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        ator = (os.environ.get("DISPARADO_POR") or os.environ.get("GITHUB_ACTOR") or "").strip()
        run_id = os.environ.get("GITHUB_RUN_ID")
        url = os.environ.get("RUN_URL") or (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{run_id}" if run_id else None)
        if disparo_e_cron():
            return ("cron", "github-cron", url, run_id, os.environ.get("GITHUB_SHA"))
        return (ator or "actions:desconhecido", "github-actions", url, run_id,
                os.environ.get("GITHUB_SHA"))
    try:
        usuario = getpass.getuser()
    except Exception:
        usuario = "desconhecido"
    return (f"local:{usuario}", "local", None, None, os.environ.get("GIT_SHA"))


class Auditoria:
    def __init__(self, sb, modo: str, empresas: list[str], teto: int | None,
                 filtro_cnpj: str | None, log=print):
        self.sb = sb
        self.log = log
        self.modo = modo
        self.rodada_id: str | None = None
        quem, origem, run_url, run_id, sha = quem_disparou()
        self.quem = quem
        self.origem = origem
        self.run_url = run_url
        self._campos_rodada = {
            "modo": modo, "disparado_por": quem, "origem": origem,
            "run_url": run_url, "run_id": run_id, "git_sha": sha,
            "empresas": empresas, "teto_rodada": teto, "filtro_cnpj": filtro_cnpj,
        }
        # ⚠️ `cron_dia` SO no cron QUE REPOE -- e o `and modo == "aplicar"` e a
        # correcao de um defeito real (25/ago/2026, migration 20260825).
        #
        # A trava de "uma rodada por dia" foi desenhada para a REPOSICAO, que roda
        # 1x/dia. Mas `credito-varredura-espelho.yml` tambem dispara por
        # `schedule` (a cada 30 min, 03h-07h30 UTC) e `disparo_e_cron()` so
        # pergunta "veio do schedule?" -- entao a varredura passou a disputar a
        # MESMA chave. Medido: as ~5 varreduras da noite cairam para 1, e a
        # varredura das 03h52 ocupou a chave do dia, de modo que a REPOSICAO das
        # 19h30 seria barrada. *A trava criada para impedir credito em dobro
        # impediria o credito de sair.*
        #
        # Preencher em rodada de PESSOA tambem esta fora, pela razao original:
        # trancaria o cron daquele dia. E nao preencher no cron que repoe furaria
        # a trava em silencio (indice parcial nao tranca NULL).
        if origem == "github-cron" and modo == "aplicar":
            self._campos_rodada["cron_dia"] = hoje_brt()

    # ── rodada ──────────────────────────────────────────────────────────────
    def abrir(self) -> str:
        """Abre a rodada. Falhar aqui aborta SEMPRE, inclusive em dry-run: se o
        banco nao aceita nem a linha de abertura, a rodada inteira sairia cega."""
        try:
            r = self.sb.table("credito_reposicao_rodada").insert(self._campos_rodada).execute()
            self.rodada_id = (r.data or [{}])[0].get("id")
            if not self.rodada_id:
                raise AuditoriaIndisponivel("o INSERT da rodada nao devolveu id")
        except AuditoriaIndisponivel:
            raise
        except Exception as e:
            # ⚠️ A trava do cron chega aqui como erro do banco, e NAO pode ser
            # lida como "auditoria indisponivel" -- sao coisas opostas: uma diz
            # "nao consigo registrar, nao rode", a outra diz "ja foi registrado
            # hoje, nao rode DE NOVO". Casar pelo nome do indice em vez de so
            # pelo 23505: outra UNIQUE desta tabela seria um problema de verdade.
            texto = str(e)
            if "ux_credito_reposicao_cron_dia" in texto or (
                    "23505" in texto and "cron_dia" in texto):
                raise RodadaDeCronJaFeita(
                    f"o cron de {self._campos_rodada.get('cron_dia')} (BRT) ja rodou -- "
                    "esta execucao nao vai repor nada. Isto e a trava do indice "
                    "ux_credito_reposicao_cron_dia funcionando, nao uma falha.")
            raise AuditoriaIndisponivel(
                f"nao consegui abrir a rodada de auditoria ({e}). Nao vou rodar sem registro — "
                "ver `auditoria.py`, decisao 2.")
        self.log(f"[auditoria] rodada {self.rodada_id} · disparada por {self.quem} ({self.origem})"
                 + (f" · log: {self.run_url}" if self.run_url else ""))
        return self.rodada_id

    def fechar(self, *, fila_lida: int, repostos: int, pulados: int, erros: int,
               conferir: int, valor_reposto: float, abortou_por: str | None, ok: bool) -> None:
        if not self.rodada_id:
            return
        try:
            self.sb.table("credito_reposicao_rodada").update({
                "finalizada_em": _agora(), "fila_lida": fila_lida, "repostos": repostos,
                "pulados": pulados, "erros": erros, "conferir": conferir,
                "valor_reposto": round(valor_reposto, 2),
                "abortou_por": abortou_por, "ok": ok,
            }).eq("id", self.rodada_id).execute()
            self.log(f"[auditoria] rodada {self.rodada_id} fechada")
        except Exception as e:
            # Nao aborta: a rodada JA aconteceu, e as linhas do log ja estao lá.
            # Uma rodada sem `finalizada_em` e legivel como "morreu sem fechar".
            self.log(f"[!] nao consegui fechar a rodada na auditoria: {e}")

    # ── linha por pagamento ─────────────────────────────────────────────────
    def abrir_linha(self, ev: dict, *, empresa_id=None, cliente_id=None, cliente_nome=None,
                    disponivel_antes=None, total_antes=None, decisao="erro", motivo=None,
                    escreveu=False, exigir=False, fase=None, url=None,
                    liberacao_id=None) -> str | None:
        """Grava a linha ANTES do clique em Salvar. `exigir=True` (modo aplicar)
        transforma falha em AuditoriaIndisponivel: sem registro, nao escreve.

        ⚠️ `liberacao_id` e o que AUTORIZA mexer no limite TOTAL (25/ago/2026).
        `v_credito_reposicao_violou_teto` acende quando o total muda com esta
        coluna NULA -- entao esquecer de passa-la numa escrita de teto legitima
        faz o alarme apitar, e passa-la numa rodada de reposicao desligaria o
        alarme que existe justamente para ela. Nunca preencher "por garantia"."""
        campos = {
            "rodada_id": self.rodada_id,
            "evento_id": ev.get("id"), "id_boleto": ev.get("id_boleto"),
            "cnpj": ev.get("cnpj"), "nf": ev.get("referencia"),
            "valor_pago": ev.get("valor_pago"), "data_pagamento": ev.get("data_pagamento"),
            "empresa_id": empresa_id, "cliente_mercos_id": cliente_id, "cliente_nome": cliente_nome,
            "disponivel_antes": disponivel_antes, "total_antes": total_antes,
            "decisao": decisao, "motivo": (motivo or "")[:2000] or None, "escreveu": escreveu,
            "fase": fase, "fase_em": _agora() if fase else None, "url": url,
            "tentativa": int(os.environ.get("GITHUB_RUN_ATTEMPT") or 0) or None,
            "liberacao_id": liberacao_id,
        }
        try:
            r = self.sb.table("credito_reposicao_log").insert(campos).execute()
            return (r.data or [{}])[0].get("id")
        except Exception as e:
            if exigir:
                raise AuditoriaIndisponivel(
                    f"nao consegui registrar a auditoria deste cliente ({e}). NAO escrevi no Mercos.")
            self.log(f"[!] auditoria da linha falhou (segue, pois nada sera escrito): {e}")
            return None

    def fechar_linha(self, log_id: str | None, *, disponivel_depois=None, total_depois=None,
                     decisao=None, motivo=None, escreveu=None, confirmado=None,
                     duracao_ms=None, disponivel_antes=None, fase=None, screenshot=None) -> None:
        if not log_id:
            return
        campos: dict = {}
        if disponivel_depois is not None:
            campos["disponivel_depois"] = disponivel_depois
            if disponivel_antes is not None:
                campos["delta"] = round(disponivel_depois - disponivel_antes, 2)
        if total_depois is not None:
            campos["total_depois"] = total_depois
        if decisao is not None:
            campos["decisao"] = decisao
        if motivo is not None:
            campos["motivo"] = motivo[:2000]
        if escreveu is not None:
            campos["escreveu"] = escreveu
        if confirmado is not None:
            campos["confirmado_na_tela"] = confirmado
        if duracao_ms is not None:
            campos["duracao_ms"] = int(duracao_ms)
        if fase is not None:
            campos["fase"] = fase
            campos["fase_em"] = _agora()
        if screenshot is not None:
            campos["screenshot"] = screenshot
        if not campos:
            return
        try:
            self.sb.table("credito_reposicao_log").update(campos).eq("id", log_id).execute()
        except Exception as e:
            self.log(f"[!] nao consegui completar a linha de auditoria {log_id}: {e}")

    def fase(self, log_id: str | None, nome: str, *, cliente=None, url=None, **extra) -> None:
        """Marca a fase alcancada -- na LINHA do cliente e no BATIMENTO da rodada.

        ⚠️ E isto que responde "onde parou?" quando o processo morre sem fechar.
        Antes, a linha do log so nascia imediatamente antes do clique em Salvar:
        se o runner morresse ao abrir o modal do cliente 7, esse cliente **nao
        tinha linha nenhuma** e ninguem sabia em quem o robo estava. Agora a linha
        nasce quando o cliente entra e vai sendo atualizada; linha que fica em
        `em_andamento` depois da rodada terminar aponta exatamente onde morreu
        (view `v_credito_reposicao_travou`).

        Nunca levanta: registro de progresso nao pode derrubar o trabalho."""
        agora = _agora()
        if log_id:
            try:
                self.sb.table("credito_reposicao_log").update(
                    {"fase": nome, "fase_em": agora, **({"url": url} if url else {})}
                ).eq("id", log_id).execute()
            except Exception as e:
                self.log(f"[!] auditoria: nao registrei a fase '{nome}': {e}")
        if self.rodada_id:
            try:
                self.sb.table("credito_reposicao_rodada").update({
                    "ultimo_sinal_em": agora, "fase_atual": nome,
                    **({"ultimo_cliente": cliente} if cliente else {}),
                }).eq("id", self.rodada_id).execute()
            except Exception:
                pass
        self.evento_json(etapa=nome, cliente=cliente, url=url, **extra)

    # ── log estruturado no stdout (o Actions guarda isso como artifact) ──────
    def evento_json(self, **kv) -> None:
        """Uma linha JSON por evento. Existe para a auditoria pos-execucao ser
        grepavel/parseavel, em vez de depender de ler prosa no log do Actions."""
        kv.setdefault("rodada_id", self.rodada_id)
        kv.setdefault("em", _agora())
        try:
            print("AUDIT " + json.dumps(kv, ensure_ascii=False, default=str), flush=True)
        except Exception:
            pass
