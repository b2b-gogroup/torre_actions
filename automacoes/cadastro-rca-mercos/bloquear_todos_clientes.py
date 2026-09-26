"""Esvazia a carteira de um vendedor no Mercos ("Bloquear todos os clientes").

    python -u ferramentas/_env.py ../../.env.local bloquear_todos_clientes.py \
        --filial es --vendedor "Edilberto Lobo" --dry-run
    python -u ferramentas/_env.py ../../.env.local bloquear_todos_clientes.py \
        --filial es --vendedor "Edilberto Lobo" --confirmar

🔴 ESTA E A UNICA ACAO DESTRUTIVA DESTE CONJUNTO. Ela TIRA acesso.
Depois de rodar, o vendedor NAO VE CLIENTE NENHUM ate o vinculo repor a carteira.

POR QUE EXISTE (17/set/2026)
-----------------------------
`habilitar_` so ADICIONA. Marcar "Limitar acesso aos clientes" restringe o vendedor a uma
LISTA -- mas nao esvazia a lista que ja estava la. Quem ja tinha vinculos antigos continua
vendo tudo aquilo MAIS o que o robo adicionou.

Medido no Edilberto Lobo (ES): **3.539 clientes liberados para uma carteira de 485**,
depois de limitado e vinculado. Limitar + vincular, sozinhos, NAO restringem ninguem que
ja tivesse carteira povoada.

A doc do projeto ja citava este passo -- "Bloquear todos os clientes", em
`relatorio_mercos/mercos_gestao_carteira.py` -- anotado como "so pra reforma de carteira
em massa, nao pra inclusao pontual de 1 cliente novo". A operacao de limitar os inside
sales E uma reforma em massa, e o passo foi lido como inaplicavel.

⚠️ A ORDEM CERTA E:  limitar -> BLOQUEAR TODOS -> vincular a carteira

🔴 O SELETOR E O `onclick`, NAO O TEXTO NEM A CLASSE
-----------------------------------------------------
A tela tem DOIS botoes com a MESMA classe (`botao medio perigo`), lado a lado no mesmo
`modal-footer`:

    <button onclick="trocarPermissaoCliente(this, 'TODOS', false)">Bloquear todos</button>
    <button onclick="trocarPermissaoCliente(this, 'TODOS', true)">Liberar todos</button>

A diferenca entre ESVAZIAR a carteira e DAR ACESSO A BASE INTEIRA e o ultimo parametro.
Casar por classe pegaria qualquer um dos dois; casar por texto depende de o Mercos nunca
mudar o rotulo. Casamos pelo `onclick`, que descreve o COMPORTAMENTO -- e conferimos que
existe exatamente 1, senao aborta.
"""

import argparse
import os
import sys
import time

from playwright.sync_api import sync_playwright

from mercos_ui import abrir_carteira_por_url, sessao_caiu
from mercos_login import login_mercos, conferir_credencial_gmail

EMPRESAS = {"es": "424525", "rj": "424524"}

MERCOS_EMAIL = os.environ["MERCOS_EMAIL"]
MERCOS_SENHA = os.environ["MERCOS_SENHA"]
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
GMAIL_SENHA = os.environ.get("GMAIL_SENHA") or ""

# ⚠️ 'TODOS', false = BLOQUEAR. 'TODOS', true = LIBERAR (o oposto -- nunca usar aqui).
SEL_BLOQUEAR_TODOS = "button[onclick*=\"'TODOS', false\"]"
SEL_LIBERAR_TODOS = "button[onclick*=\"'TODOS', true\"]"


def _ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def contar(pg, base: str, filtro: str) -> int | None:
    """Total que a tela informa. None = nao consegui ler (NAO e zero)."""
    import re
    pg.goto(f"{base}?filtro_bloqueio={filtro}", wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)
    m = re.search(r"Resultados\s+\d+\s*-\s*\d+\s+de\s+(\d+)",
                  pg.locator("body").inner_text())
    if m:
        return int(m.group(1))
    try:
        return 0 if pg.locator("#id_nome").count() > 0 else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filial", choices=sorted(EMPRESAS), required=True)
    ap.add_argument("--vendedor", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="conta e localiza o botao, mas NAO clica")
    ap.add_argument("--confirmar", action="store_true",
                    help="obrigatorio para executar de verdade")
    args = ap.parse_args()

    if not args.dry_run and not args.confirmar:
        # Acao destrutiva nao roda por engano de linha de comando.
        raise SystemExit("Esta acao TIRA o acesso do vendedor a todos os clientes.\n"
                         "Rode com --dry-run para conferir, ou --confirmar para executar.")

    empresa_id = EMPRESAS[args.filial]
    print(f"{_ts()} {args.filial.upper()} ({empresa_id}) | '{args.vendedor}'"
          f"{' | DRY-RUN' if args.dry_run else ' | APLICANDO'}")
    if GMAIL_USER and GMAIL_SENHA:
        conferir_credencial_gmail(GMAIL_USER, GMAIL_SENHA, MERCOS_EMAIL)

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")).new_page()
        try:
            login_mercos(pg, empresa_id, MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
            abrir_carteira_por_url(pg, empresa_id, args.vendedor)
            base = pg.url.split("?")[0]

            antes = contar(pg, base, "l")
            if antes is None:
                raise RuntimeError("nao consegui ler quantos estao liberados -- nao vou "
                                   "bloquear no escuro")
            print(f"{_ts()} liberados ANTES: {antes}")
            if antes == 0:
                print(f"{_ts()} [=] carteira ja vazia -- nada a fazer")
                return 0

            pg.goto(base, wait_until="domcontentloaded", timeout=60_000)
            time.sleep(2)

            # ⚠️ Conferir que ha exatamente UM botao de bloquear-todos, e que ele NAO e o
            # de liberar. Ambiguidade aqui pode dar acesso a base inteira.
            bloq = pg.locator(SEL_BLOQUEAR_TODOS)
            lib = pg.locator(SEL_LIBERAR_TODOS)
            print(f"{_ts()} botoes: bloquear-todos={bloq.count()} liberar-todos={lib.count()}")
            if bloq.count() != 1:
                raise RuntimeError(
                    f"esperava 1 botao 'Bloquear todos' ({SEL_BLOQUEAR_TODOS}), achei "
                    f"{bloq.count()}. A tela mudou -- NAO vou clicar em nada.")
            rotulo = " ".join((bloq.first.inner_text() or "").split())
            if "bloquear" not in rotulo.lower():
                # Cinto e suspensorio: o onclick diz bloquear, o texto tem que concordar.
                raise RuntimeError(f"o botao casado pelo onclick diz {rotulo!r}, que nao e "
                                   "'Bloquear todos' -- abortando por divergencia")
            print(f"{_ts()} botao conferido: {rotulo!r}")

            if args.dry_run:
                print(f"{_ts()} DRY-RUN: bloquearia os {antes} liberados. Nada foi alterado.")
                return 0

            bloq.first.click()
            time.sleep(4)

            depois = contar(pg, base, "l")
            if depois is None:
                if sessao_caiu(pg):
                    raise RuntimeError("a sessao caiu logo apos o clique -- NAO CONFIRMADO. "
                                       "Rodar --dry-run para ver como ficou.")
                raise RuntimeError("nao consegui reler o total -- NAO CONFIRMADO")
            print(f"{_ts()} liberados DEPOIS: {depois}")
            if depois >= antes:
                raise RuntimeError(f"cliquei e o total nao caiu ({antes} -> {depois}) -- "
                                   "conferir na tela antes de seguir")
            print(f"{_ts()} [ok] carteira esvaziada: {antes} -> {depois}. "
                  "⚠️ O VENDEDOR ESTA SEM CLIENTES ATE O VINCULO RODAR.")
            return 0
        finally:
            b.close()


if __name__ == "__main__":
    sys.exit(main())
