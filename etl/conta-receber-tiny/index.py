"""
ETL — contas a receber do Tiny (Ápice) para `fato_conta_receber_tiny`.

O QUE RESOLVE
-------------
O trilho Protheus tira uma NF de "sem boleto" quando existe título no ERP — título é prova de
que a cobrança existe, ainda que o boleto não tenha sido emitido. O trilho Tiny/Ápice nunca teve
esse equivalente, e por isso R$ 16,08 mi de faturamento Ápice ES (fev–jul/2026) aparecem no
funil como "sem notícia do dinheiro".

Tem equivalente: `contas.receber.pesquisa.php` devolve a conta com a NF no histórico. Medido em
junho/2026 — 240 das 321 NFs órfãs têm conta a receber no Tiny (R$ 889.947).

⚠️ A API SÓ DEVOLVE CONTA EM ABERTO
-----------------------------------
Testados `pago`, `todas`, `recebido`, `paga`, `cancelada` e códigos numéricos: todos respondem
"a consulta não retornou registros", e as 368 contas de junho vêm 100% `aberto`.

Isso não é limitação — é a semântica, e o ETL é desenhado em cima dela: **sumir da lista é a
prova da baixa**. Cada varredura de um mês marca `visto_em` em quem apareceu; quem já estava na
base e não apareceu vira `ainda_aberta = false`.

É a lição que custou caro no Asaas em 04/ago: *sumir da consulta nunca apaga o que já foi
gravado.* Lá o conserto foi receber a linha morta; aqui a morte é deduzida da ausência — de
propósito, e só para mês cuja varredura foi COMPLETA (`conta_receber_tiny_varredura.completa`).
Se a varredura truncou, ausência não prova nada e nada é baixado.

⚠️ TRÊS COISAS QUE ESTE ETL **NÃO** FAZ
---------------------------------------
1. Não escreve em `fato_titulo_receber` nem em nada que a comissão leia. Fonte nova entra pelo
   funil primeiro; comissão é decisão à parte, com medição própria (ver `20260812c`).
2. Não afirma valor vencido. Conta aberta prova que a cobrança EXISTE; falta provar que o Tiny
   dá baixa (a liquidação acontece no Asaas). Até lá, é evidência de cobrança, não saldo.
3. Não cobre SP. `TINY_TOKEN_SP` aponta para a conta `bbvarejo-rp` — o mesmo número de conta lá
   é outro documento. Medido: a consulta de junho volta vazia. Incluir SP com esse token
   colaria conta de outra empresa na NF daqui.

USO
    python etl/conta-receber-tiny/index.py                 # mês atual + anterior (incremental)
    python etl/conta-receber-tiny/index.py --de 2026-01 --ate 2026-08     # carga histórica
    python etl/conta-receber-tiny/index.py --dry           # não grava, só mede
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Iterator

API = "https://api.tiny.com.br/api2"

# ⚠️ SP fora de propósito — token aponta para outra conta. Ver o cabeçalho.
ERPS = {"tiny_es": "TINY_TOKEN_ES", "tiny_rj": "TINY_TOKEN_RJ"}

# "Ref. a NF nº 26516, CLIENTE (parcela 1/4)" — a forma que o Tiny usa no histórico.
RE_NF_HIST = re.compile(r"\bNF\s*n?º?\s*0*(\d+)", re.I)
# "026516/01" — fallback quando o histórico não cita NF.
RE_NF_DOC = re.compile(r"^0*(\d+)\s*/\s*(\d+)")


def env(*nomes: str) -> str:
    """
    Primeiro nome que existir, no ambiente ou no `.env.local`.

    ⚠️ Aceita ALIASES de propósito: no GitHub Actions a URL do Supabase é o secret `SUPABASE_URL`
    e localmente é `NEXT_PUBLIC_SUPABASE_URL` (convenção do Next, que precisa do prefixo para
    chegar ao browser). Cadastrar um secret novo só para duplicar um que já existe é convite a
    eles divergirem depois — e o dia em que divergirem, o ETL aponta para outro banco sem avisar.
    """
    for nome in nomes:
        if v := os.environ.get(nome):
            return v
    caminho = os.path.join(os.path.dirname(__file__), "..", "..", ".env.local")
    if os.path.exists(caminho):
        for linha in io.open(caminho, encoding="utf-8", errors="ignore"):
            for nome in nomes:
                if linha.startswith(f"{nome}="):
                    return linha.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"nenhum de {' / '.join(nomes)} encontrado (nem no ambiente, nem em .env.local)")


def tiny(tk: str, ep: str, **params: Any) -> dict:
    params.update(token=tk, formato="json")
    url = f"{API}/{ep}?{urllib.parse.urlencode(params)}"
    for tentativa in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                corpo = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            if tentativa == 3:
                return {"_erro": f"rede: {e}"}
            time.sleep(4 * (tentativa + 1))
            continue

        ret = corpo.get("retorno", {})
        # ⚠️ O Tiny devolve HTTP 200 com `status: "Erro"` no corpo. Testar o código HTTP não basta
        #    — foi assim que uma conferência anterior nesta base concluiu errado.
        if str(ret.get("status", "")).lower() == "erro":
            msg = json.dumps(ret.get("erros", ""), ensure_ascii=False)
            if "não retornou registros" in msg:
                return {"contas": []}                       # vazio legítimo, não é falha
            if "excedido" in msg.lower() or "limite" in msg.lower():
                time.sleep(15)
                continue
            return {"_erro": msg[:200]}
        return ret
    return {"_erro": "4 tentativas sem sucesso"}


def meses(de: str, ate: str) -> Iterator[date]:
    a = datetime.strptime(de, "%Y-%m").date().replace(day=1)
    b = datetime.strptime(ate, "%Y-%m").date().replace(day=1)
    while a <= b:
        yield a
        a = (a.replace(day=28) + __import__("datetime").timedelta(days=8)).replace(day=1)


def fim_do_mes(d: date) -> date:
    prox = (d.replace(day=28) + __import__("datetime").timedelta(days=8)).replace(day=1)
    return prox - __import__("datetime").timedelta(days=1)


def br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def iso(br_data: str | None) -> str | None:
    if not br_data:
        return None
    try:
        return datetime.strptime(br_data, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def extrai_nf(conta: dict) -> tuple[str | None, str | None]:
    """(nf, parcela). Histórico é a fonte primária; numero_doc é o fallback."""
    if m := RE_NF_HIST.search(conta.get("historico") or ""):
        nf = m.group(1)
        parc = None
        if p := RE_NF_DOC.match(conta.get("numero_doc") or ""):
            parc = p.group(2)
        return nf, parc
    if m := RE_NF_DOC.match(conta.get("numero_doc") or ""):
        return m.group(1), m.group(2)
    return None, None


def _distintas(contas: list[dict]) -> int:
    return len({str(c["id"]) for c in contas})


def varre_mes(tk: str, mes: date) -> tuple[list[dict], int, bool]:
    """Devolve (contas sem repetição, paginas, completa). `completa=False` invalida a dedução de baixa.

    ⚠️ A ordenação da API do Tiny NÃO é estável entre páginas: contas com a mesma data trocam de
    lugar de uma página para a outra, então uma conta aparece duas vezes e OUTRA nunca aparece.
    Medido em 05/nov/2026: tiny_rj out/2026 trazia 527 linhas para 508 contas distintas — 19
    repetidas = 19 puladas — e o upsert do lote caía com "ON CONFLICT DO UPDATE command cannot
    affect row a second time" (11 runs seguidos em falha, de 31/out a 05/nov).
    Saída: janela menor. O mês é relido dia a dia e, no dia que ainda repete (30/10 tinha 267
    contas em 3 páginas), o dia é fatiado por VENCIMENTO até cada fatia caber sem repetir.
    """
    contas, pags, completa = varre_periodo(tk, mes, fim_do_mes(mes))
    if _distintas(contas) == len(contas):
        return contas, pags, completa

    print(f"    ↳ {mes:%Y-%m}: contas repetidas entre páginas — relendo dia a dia", file=sys.stderr)
    todas: dict[str, dict] = {}
    pags, completa = 0, True
    dia = mes
    while dia <= fim_do_mes(mes):
        c, p, ok = varre_dia(tk, dia)
        for x in c:
            todas[str(x["id"])] = x
        pags += p
        completa = completa and ok
        dia += timedelta(days=1)
        time.sleep(1.2)                                     # a API limita por minuto
    return list(todas.values()), pags, completa


def varre_dia(tk: str, dia: date) -> tuple[list[dict], int, bool]:
    c, p, ok = varre_periodo(tk, dia, dia)
    if _distintas(c) == len(c):
        return c, p, ok
    # Fatiar por vencimento. A faixa larga cobre qualquer prazo real; a conferência abaixo pega
    # o que ficar fora dela (conta lida na leitura simples e ausente das fatias = incompleto).
    fatias, pf, okf = varre_venc(tk, dia, dia - timedelta(days=400), dia + timedelta(days=800))
    ids_fatias = {str(x["id"]) for x in fatias}
    faltou = {str(x["id"]) for x in c} - ids_fatias
    if faltou:
        print(f"      ⚠️ {dia:%d/%m}: {len(faltou)} conta(s) fora das fatias de vencimento", file=sys.stderr)
    todas = {str(x["id"]): x for x in c}
    todas.update({str(x["id"]): x for x in fatias})
    return list(todas.values()), p + pf, ok and okf and not faltou


def varre_venc(tk: str, dia: date, vi: date, vf: date) -> tuple[list[dict], int, bool]:
    c, p, ok = varre_periodo(tk, dia, dia, vi, vf)
    if _distintas(c) == len(c):
        return c, p, ok
    if vi >= vf:
        print(f"      ⚠️ {dia:%d/%m}: ainda repete com vencimento {vi:%d/%m/%Y} — incompleto", file=sys.stderr)
        return c, p, False
    meio = vi + timedelta(days=(vf - vi).days // 2)
    time.sleep(1.2)
    a, pa, oka = varre_venc(tk, dia, vi, meio)
    time.sleep(1.2)
    b, pb, okb = varre_venc(tk, dia, meio + timedelta(days=1), vf)
    return a + b, p + pa + pb, ok and oka and okb


def varre_periodo(tk: str, ini: date, fim: date,
                  venc_ini: date | None = None, venc_fim: date | None = None) -> tuple[list[dict], int, bool]:
    """Leitura paginada crua (pode trazer repetidas — quem decide é o chamador)."""
    filtros: dict[str, str] = {"data_ini_emissao": br(ini), "data_fim_emissao": br(fim)}
    if venc_ini and venc_fim:
        filtros |= {"data_ini_vencimento": br(venc_ini), "data_fim_vencimento": br(venc_fim)}
    contas: list[dict] = []
    pagina, total_pag, completa = 1, 1, True
    while True:
        r = tiny(tk, "contas.receber.pesquisa.php", pagina=pagina, **filtros)
        if "_erro" in r:
            print(f"    ⚠️ página {pagina}: {r['_erro']}", file=sys.stderr)
            completa = False
            break
        contas += [c["conta"] for c in (r.get("contas") or [])]
        total_pag = int(r.get("numero_paginas") or 1)
        if pagina >= total_pag:
            break
        pagina += 1
        time.sleep(1.2)                                     # a API limita por minuto
    return contas, total_pag, completa


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    hoje = date.today()
    ap = argparse.ArgumentParser()
    ap.add_argument("--de", default=(hoje.replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m"))
    ap.add_argument("--ate", default=hoje.strftime("%Y-%m"))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    from supabase import create_client                       # só é necessário fora do --dry
    sb = None if args.dry else create_client(
        env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"), env("SUPABASE_SERVICE_ROLE_KEY"))

    total_lidas = total_grav = total_baixadas = 0

    for erp, var_token in ERPS.items():
        tk = env(var_token)
        for mes in meses(args.de, args.ate):
            contas, pags, completa = varre_mes(tk, mes)

            # ⚠️ A MESMA CONTA PODE VIR EM DUAS PÁGINAS (desde 31/out/2026, 11 runs seguidos em
            #    falha). A API pagina uma lista que muda enquanto a lemos: conta baixada entre uma
            #    página e outra desloca o resto, e a conta da borda reaparece na página seguinte.
            #    Duas linhas com o mesmo `id` no mesmo upsert derrubam o lote inteiro com
            #    "ON CONFLICT DO UPDATE command cannot affect row a second time".
            #    Remover a repetida não basta: se a lista deslocou, alguma conta pode ter sido
            #    PULADA, e a dedução de baixa abaixo a marcaria como paga sem ela ter sido. Então
            #    repetição = varredura incompleta — grava o que veio, mas não deduz baixa.
            unicas = {str(c["id"]): c for c in contas}
            repetidas = len(contas) - len(unicas)
            if repetidas:
                print(f"    ⚠️ {repetidas} conta(s) repetida(s) entre páginas — lista mudou "
                      f"durante a leitura; sem dedução de baixa neste mês", file=sys.stderr)
                contas = list(unicas.values())
                completa = False
            total_lidas += len(contas)

            linhas, sem_nf = [], 0
            for c in contas:
                nf, parcela = extrai_nf(c)
                if not nf:
                    sem_nf += 1
                linhas.append({
                    "id": str(c["id"]), "erp_origem": erp,
                    "nf_numero": nf, "parcela": parcela,
                    "numero_doc": c.get("numero_doc"), "serie_doc": c.get("serie_doc"),
                    "cliente_nome": c.get("nome_cliente"),
                    "valor": float(c["valor"]) if c.get("valor") else None,
                    "saldo": float(c["saldo"]) if c.get("saldo") else None,
                    "situacao": c.get("situacao"),
                    "data_emissao": iso(c.get("data_emissao")),
                    "data_vencimento": iso(c.get("data_vencimento")),
                    "historico": c.get("historico"),
                    "visto_em": "now()", "ainda_aberta": True,
                })

            marca = "seco" if args.dry else "grava"
            print(f"  {erp} {mes:%Y-%m}: {len(contas)} contas, {pags} pág, "
                  f"{sem_nf} sem NF{'' if completa else ', ⚠️ INCOMPLETA'} [{marca}]")

            if args.dry:
                continue

            # ⚠️ NÃO PULAR O MÊS VAZIO. A versão anterior fazia `if not linhas: continue`, o que
            # saltava a dedução de baixa E o registro da varredura junto com o upsert.
            #
            # Mês vazio é o caso MAIS informativo que existe aqui: se todas as contas daquele mês
            # baixaram, a consulta volta vazia — e é exatamente aí que `ainda_aberta` tem de virar
            # false. Com o `continue`, elas ficariam abertas para sempre, e o mecanismo falharia
            # justamente na situação para a qual foi criado.
            #
            # Achado ao conferir a primeira execução no Actions: 14 varreduras registradas onde
            # deviam ser 16 — faltavam `tiny_rj` 05 e 08, os dois meses com zero contas.
            for i in range(0, len(linhas), 500):
                lote = linhas[i:i + 500]
                for l in lote:
                    l["visto_em"] = datetime.now().astimezone().isoformat()
                sb.table("fato_conta_receber_tiny").upsert(lote, on_conflict="id").execute()
            total_grav += len(linhas)

            # ⚠️ A DEDUÇÃO DA BAIXA — só para varredura COMPLETA. Quem estava na base com emissão
            #    neste mês e não apareceu agora sumiu da lista de abertas, ou seja: baixou.
            #    Numa varredura truncada, ausência não prova nada e isto é pulado.
            if completa:
                vistos = [l["id"] for l in linhas]
                q = (sb.table("fato_conta_receber_tiny")
                       .update({"ainda_aberta": False})
                       .eq("erp_origem", erp)
                       .gte("data_emissao", mes.isoformat())
                       .lte("data_emissao", fim_do_mes(mes).isoformat())
                       .eq("ainda_aberta", True))
                if vistos:
                    # PostgREST: `not.in` com a lista do que veio agora.
                    q = q.not_.in_("id", vistos)
                baixadas = q.execute()
                n = len(baixadas.data or [])
                total_baixadas += n
                if n:
                    print(f"      ↳ {n} conta(s) sumiram da lista de abertas → baixadas")

            sb.table("conta_receber_tiny_varredura").upsert({
                "erp_origem": erp, "competencia": mes.isoformat(),
                "contas": len(contas), "paginas": pags, "completa": completa,
                "varrido_em": datetime.now().astimezone().isoformat(),
            }, on_conflict="erp_origem,competencia").execute()

    print(f"\nlidas {total_lidas} · gravadas {total_grav} · baixadas por ausência {total_baixadas}")


if __name__ == "__main__":
    main()
