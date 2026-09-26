"""Vincula uma LISTA de CNPJs a carteira de um vendedor no Mercos.

Criado 11/set/2026 para a rede Bel Cosmeticos (75 lojas -> Fernanda Vidal, ES e RJ).

POR QUE UM SCRIPT NOVO E NAO O `escritor.py`
--------------------------------------------
O escritor e movido pelo LEDGER (`cadastro_rca_mercos_processado`), alimentado pela
planilha de cadastro de cliente NOVO com gate de chamado GoService. `--cnpjs` la
FILTRA linhas que ja existem no ledger -- nao cria nenhuma. A Bel tem 1 linha no
ledger, nao 75, entao o escritor nao tem o que processar.

Este aqui recebe a lista direto e so faz a etapa de CARTEIRA. NAO toca tabela de
preco, condicao de pagamento, planilha, ledger nem WhatsApp -- de proposito: e uma
operacao pontual de carteira, e cada efeito colateral a mais e uma coisa a desfazer
se algo der errado.

O QUE ELE FAZ NO MERCOS
-----------------------
Reusa `mercos_ui.liberar_cliente`, que clica no botao `habilitar_` dentro de
"Clientes deste usuario". Isso ADICIONA o cliente a carteira do vendedor -- NAO
remove de ninguem. Vincular a Fernanda nao tira nada do Pedro Igor.

Diferente do escritor, abre "Todos os usuarios" -> "Clientes deste usuario" UMA vez
e faz a lista inteira ali dentro (o escritor reabre a cada cliente, o que para 75
CNPJs seriam 75 navegacoes desnecessarias).

⚠️ UMA FILIAL POR RODADA. O passo da carteira le os usuarios da empresa em que a
SESSAO esta logada -- misturar ES e RJ no mesmo processo poe a carteira na empresa
errada SEM ERRO NENHUM (mesma razao dos steps separados do escritor, 01/set/2026).

⚠️ MERCOS SO ACEITA 1 SESSAO POR USUARIO. Se alguem logar na mesma conta durante a
rodada, a sessao cai e o resto falha em cascata (incidente 18/ago/2026). Rodar fora
do expediente, ou com um usuario Mercos dedicado ao robo.

USO
---
  export MERCOS_EMAIL=... MERCOS_SENHA=... GMAIL_USER=... GMAIL_SENHA=...
  python vincular_carteira_lote.py --filial rj --vendedor "Fernanda Vidal" \
      --cnpjs-arquivo cnpjs_bel.txt
  python vincular_carteira_lote.py --filial es --vendedor "Fernanda Vidal" \
      --cnpjs 02125266000196,02125266000277 --dry-run

`--dry-run` loga e abre a carteira do vendedor, mas NAO clica em habilitar --
serve para conferir que o nome do vendedor casa com exatamente 1 card antes de
mexer em 75 clientes.
"""

import argparse
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

# ⚠️ NAO importar `escritor`: ele le CADASTRO_RCA_SHEETS_SERVICE_ACCOUNT_JSON e
# SUPABASE_* no nivel do MODULO, e este robo nao fala com planilha nem com o
# ledger -- importar exigiria segredos que ele nao usa. Os seletores vem do
# modulo compartilhado `mercos_ui` (corpo unico, convencao #14).
from mercos_ui import (abrir_carteira_por_url, liberar_cliente,
                       sessao_caiu, motivo_sessao)
from mercos_login import login_mercos, conferir_credencial_gmail

EMPRESA_ID_ES = "424525"
EMPRESA_ID_RJ = "424524"
EMPRESAS = {"es": EMPRESA_ID_ES, "rj": EMPRESA_ID_RJ}

MERCOS_EMAIL = os.environ["MERCOS_EMAIL"]
MERCOS_SENHA = os.environ["MERCOS_SENHA"]
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
GMAIL_SENHA = os.environ.get("GMAIL_SENHA") or ""

# Retomada apos queda de sessao. O Mercos aceita 1 sessao por usuario e a conta do
# robo e de uma PESSOA: quem abrir o Mercos derruba a rodada no meio.
#
# ESPERA_VOLTA_S = 60 por decisao do usuario (16/set/2026), para nao perder a tarde
# esperando. ⚠️ O robo de credito usa 900s de proposito -- entrar de novo em 1 min pode
# roubar a sessao de volta de quem acabou de logar, e os dois ficam se derrubando. Se
# aparecerem muitas voltas seguidas no resumo, esse e o sinal de subir este numero.
ESPERA_VOLTA_S = int(os.environ.get("VINCULO_ESPERA_VOLTA_S") or 60)

# Teto alto, mas NAO infinito: com um obstaculo permanente (senha trocada, 2FA sem
# caixa de e-mail) um laco sem teto tenta para sempre sem vincular nada.
VOLTAS_MAX = int(os.environ.get("VINCULO_VOLTAS_MAX") or 60)

# ⚠️ A parada de verdade e por PROGRESSO, nao por tentativas (mesmo criterio do
# rodar_ate_acabar.py): voltar 3 vezes seguidas sem vincular NENHUM cliente novo quer
# dizer que o obstaculo nao e a sessao, e insistir so gera log.
VOLTAS_SEM_PROGRESSO_MAX = 3


def _ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def carregar_cnpjs(args) -> list[str]:
    """Le a lista, ignorando comentario/linha vazia. Lixo ABORTA, nao e pulado.

    ⚠️ Nao usar `zfill(14)` em cima de `re.sub(r"\\D","")` cru como o escritor faz:
    la a entrada e o ledger (CNPJ ja validado), aqui e um arquivo escrito a mao.
    Na 1a rodada (11/set/2026) as duas linhas de cabecalho `#` do cnpjs_bel.txt
    viraram "00000000752026" e "00000000112026" e o script anunciou 77 CNPJs em
    vez de 75 -- silenciosamente, porque CNPJ inexistente so devolve
    "nao_encontrado", que e uma saida VALIDA. Ou seja: o modo de falha era um
    numero errado no log e nada mais. Por isso qualquer linha que nao seja
    exatamente 14 digitos para a rodada.
    """
    bruto: list[tuple[str, str]] = []   # (origem, texto)
    if args.cnpjs_arquivo:
        with open(args.cnpjs_arquivo, encoding="utf-8") as fh:
            for n, linha in enumerate(fh, 1):
                bruto.append((f"{args.cnpjs_arquivo}:{n}", linha))
    if args.cnpjs:
        for item in args.cnpjs.split(","):
            bruto.append(("--cnpjs", item))

    vistos: set[str] = set()
    saida: list[str] = []
    for origem, item in bruto:
        texto = item.strip()
        if not texto or texto.startswith("#"):
            continue
        digitos = re.sub(r"\D", "", texto)
        if len(digitos) != 14:
            raise SystemExit(
                f"{origem}: '{texto}' nao e um CNPJ de 14 digitos (achei {len(digitos)}). "
                "Corrija o arquivo -- seguir com isso so geraria 'nao_encontrado' mudo."
            )
        if digitos in vistos:          # planilha repete filial; clicar 2x nao ajuda
            continue
        vistos.add(digitos)
        saida.append(digitos)
    return saida


def reconquistar(page, empresa_id: str, vendedor: str, volta: int, motivo: str) -> bool:
    """Espera, loga de novo e REABRE a carteira. Diz se da pra continuar.

    Nunca levanta: quem chama decide entre seguir e abortar, e morrer tentando voltar
    seria pior que a queda original.

    ⚠️ Reabrir a carteira NAO e opcional. Depois do login o Mercos serve o dashboard, e
    `liberar_cliente` mexe em `#id_nome`, que so existe dentro de "Clientes deste
    usuario" -- sem reabrir, todo cliente restante viraria "erro" com a sessao viva.
    """
    print(f"{_ts()} [sessao] caiu: {motivo}")
    print(f"{_ts()} [sessao] volta {volta}/{VOLTAS_MAX} -- esperando {ESPERA_VOLTA_S}s")
    try:
        time.sleep(ESPERA_VOLTA_S)
        login_mercos(page, empresa_id, MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
        abrir_carteira_por_url(page, empresa_id, vendedor)
    except Exception as e:
        print(f"{_ts()} [sessao] nao consegui voltar: {type(e).__name__}: {e}")
        return False
    print(f"{_ts()} [sessao] voltei, carteira reaberta -- retomando de onde parei")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filial", choices=sorted(EMPRESAS), required=True,
                    help="empresa Mercos da rodada. UMA por vez -- ver aviso no topo.")
    ap.add_argument("--vendedor", required=True,
                    help='nome do vendedor COMO O MERCOS ESCREVE (ex.: "Fernanda Vidal"). '
                         'O card e achado por substring case-insensitive: nome mais CURTO '
                         'que o do Mercos casa, mais LONGO nao.')
    ap.add_argument("--cnpjs", default=None, help="lista separada por virgula")
    ap.add_argument("--cnpjs-arquivo", default=None, help="arquivo com 1 CNPJ por linha")
    ap.add_argument("--dry-run", action="store_true",
                    help="loga e abre a carteira, mas nao clica em habilitar")
    ap.add_argument("--visivel", action="store_true", help="roda com o browser visivel")
    args = ap.parse_args()

    cnpjs = carregar_cnpjs(args)
    if not cnpjs:
        print("nenhum CNPJ informado (--cnpjs ou --cnpjs-arquivo)")
        return 2

    empresa_id = EMPRESAS[args.filial]
    print(f"{_ts()} filial {args.filial.upper()} (empresa {empresa_id}) | "
          f"vendedor '{args.vendedor}' | {len(cnpjs)} CNPJ(s)"
          f"{' | DRY-RUN' if args.dry_run else ''}")

    # Mesma guarda do escritor: conferir a credencial do 2FA ANTES de gastar
    # Chromium + login e morrer com traceback de imaplib no meio (18/ago/2026).
    if GMAIL_USER and GMAIL_SENHA:
        conferir_credencial_gmail(GMAIL_USER, GMAIL_SENHA, MERCOS_EMAIL)
    else:
        print(f"{_ts()} [2fa] sem GMAIL_USER/GMAIL_SENHA -- se o Mercos pedir codigo, aborta.")

    resultados: dict[str, list[str]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.visivel)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()
        try:
            print(f"{_ts()} Login Mercos ({MERCOS_EMAIL})...")
            login_mercos(page, empresa_id, MERCOS_EMAIL, MERCOS_SENHA,
                         GMAIL_USER, GMAIL_SENHA)

            # Uma vez so para a lista inteira. `_abrir_clientes_do_vendedor` levanta
            # se achar 0 ou >1 card -- e a validacao que queremos ANTES de 75 cliques.
            abrir_carteira_por_url(page, empresa_id, args.vendedor)
            print(f"{_ts()} carteira de '{args.vendedor}' aberta (1 card, sem ambiguidade)")

            if args.dry_run:
                print(f"{_ts()} DRY-RUN: parando aqui, nada foi habilitado.")
                return 0

            # ⚠️ Laco por INDICE, nao `for ... in`: quando a sessao cai, o cliente da vez
            # PRECISA ser refeito, e um `for` ja teria avancado. `i` so anda quando o
            # resultado e confiavel.
            i = 0
            voltas = 0
            voltas_sem_progresso = 0
            i_da_ultima_queda = -1
            while i < len(cnpjs):
                cnpj = cnpjs[i]
                print(f"  [{i + 1}/{len(cnpjs)}] {cnpj}")
                try:
                    status = liberar_cliente(page, cnpj, "", log=print, ts=_ts)
                except Exception as e:                       # nao derruba o lote
                    status = f"erro: {type(e).__name__}"
                    print(f"      {_ts()} [x] {e}")

                # 🔴 SESSAO MORTA PRODUZ O MESMO "nao_encontrado" QUE UM CLIENTE QUE NAO
                # EXISTE (runbook §7: os 4 clientes do Marcelo Chrispim vieram "NAO
                # liberado" numa passada e LIBERADO na seguinte). Gravar sem conferir
                # encheria o resumo de clientes "inexistentes" que existem -- e ninguem
                # descobriria, porque `nao_encontrado` e uma saida VALIDA.
                #
                # So perguntamos quando o resultado NAO foi "liberado": clique que
                # funcionou prova que a sessao estava viva, e a checagem custa um
                # `page.content()` por cliente.
                if status != "liberado" and sessao_caiu(page):
                    voltas += 1
                    # Volta que nao vinculou ninguem desde a queda anterior = o obstaculo
                    # nao e a sessao.
                    voltas_sem_progresso = (voltas_sem_progresso + 1) if i == i_da_ultima_queda else 0
                    i_da_ultima_queda = i
                    if voltas_sem_progresso >= VOLTAS_SEM_PROGRESSO_MAX:
                        print(f"{_ts()} [sessao] {voltas_sem_progresso} voltas seguidas travadas no "
                              f"MESMO cliente ({cnpj}) -- nao e a sessao. Parando com o que ja foi feito.")
                        break
                    if voltas > VOLTAS_MAX:
                        print(f"{_ts()} [sessao] teto de {VOLTAS_MAX} voltas atingido. "
                              "Parando com o que ja foi feito.")
                        break
                    if not reconquistar(page, empresa_id, args.vendedor, voltas,
                                        motivo_sessao(page, MERCOS_EMAIL)):
                        break
                    continue     # REFAZ este cnpj -- o resultado acima era falso

                resultados.setdefault(status, []).append(cnpj)
                i += 1
                time.sleep(0.4)                              # respiro entre buscas

            nao_tentados = len(cnpjs) - i
            if nao_tentados:
                # Declarar o que ficou de fora. Resumo que so mostra o que foi tentado
                # faz uma rodada pela metade parecer uma rodada inteira.
                print()
                print(f"{_ts()} ⚠️ PARCIAL: {nao_tentados} de {len(cnpjs)} CNPJ(s) NAO foram "
                      f"tentados (a rodada parou antes do fim). Rodar de novo -- habilitar "
                      f"cliente ja liberado e inofensivo.")
                resultados.setdefault("nao_tentado", []).extend(cnpjs[i:])
            if voltas:
                print(f"{_ts()} [sessao] {voltas} queda(s) de sessao nesta rodada "
                      f"(espera de {ESPERA_VOLTA_S}s cada).")
        finally:
            browser.close()

    print(f"\n{_ts()} RESUMO ({args.filial.upper()}, '{args.vendedor}')")
    for status in sorted(resultados):
        cnpjs_st = resultados[status]
        print(f"  {status:<16} {len(cnpjs_st):>3}")
        # 'liberado' e 'ja_na_carteira' sao os dois desfechos BONS -- so lista o
        # que pede acao humana, senao o resumo vira 75 linhas de ruido.
        if status not in ("liberado", "ja_na_carteira"):
            for c in cnpjs_st:
                print(f"      {c}")
    # 'nao_encontrado' e esperado: nem toda loja da planilha existe no Mercos daquela
    # filial. Nao e falha do script -- por isso sai 0 e a lista fica no log.
    return 0


if __name__ == "__main__":
    sys.exit(main())
