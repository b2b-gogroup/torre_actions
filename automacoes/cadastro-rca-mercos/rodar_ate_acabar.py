"""Roda o escritor em VOLTAS ate a fila acabar, retomando sozinho a cada queda de sessao.

Criado 11/set/2026 (autorizacao do Italo: "toda vez que travar por queda, voce pode sair
e entrar novamente para continuar").

POR QUE ISTO EXISTE
-------------------
O Mercos aceita **1 sessao por usuario**. Como o robo loga com a conta de uma pessoa, ela
cai toda vez que alguem usa o Mercos -- e o escritor, ao perder a sessao, espera 15 min e
tenta voltar DENTRO da mesma janela (`CADASTRO_JANELA_MIN`, default 55). Se a janela
acabar, ele desiste com a fila pela metade e alguem precisa reparar e reinvocar.

Este driver fecha esse laco: relê o ledger a cada volta, roda o escritor so com o que
AINDA esta `pendente`, e repete enquanto houver progresso. Um processo novo por volta =
sessao nova e janela nova, que e exatamente o que o escritor sozinho nao consegue fazer.

CRITERIO DE PARADA -- por PROGRESSO, nao por tentativas
------------------------------------------------------
Para quando a fila zera OU quando uma volta inteira nao processa NINGUEM. A 2a condicao e
o que impede laco infinito: se o obstaculo nao e a sessao (representante fora da filial,
cliente que sumiu do Mercos, tela remapeada), insistir nao resolve e so gasta hora. Voltas
que processam 1 que seja continuam -- e assim que uma queda no meio da fila se parece.

⚠️ UMA FILIAL POR PROCESSO. O passo da carteira le os usuarios da empresa em que a SESSAO
esta logada; misturar ES e RJ poe a carteira na empresa errada SEM ERRO NENHUM.
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime

from supabase import create_client

EMPRESAS = {"es": "424525", "rj": "424524"}
MAX_VOLTAS = int(os.environ.get("MAX_VOLTAS") or 12)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ⚠️ `erro` entra na fila junto com `pendente` (11/set/2026). A 1a versao recolhia so
# `pendente` e teria abandonado em SILENCIO o CNPJ 48780872000227: quando a sessao do
# Mercos morre no meio de um cliente, o painel vem vazio e o escritor grava
# "Nenhuma linha com label exato '071 - BOLETO 40/50/60' encontrada" -- diagnostico
# ERRADO (essa condicao existe e foi aplicada em 6 outros clientes minutos antes), mas
# o status vira `erro` e sai do radar de quem so olha `pendente`. Resultado: o driver
# anunciaria "fila zerada" com um cliente perdido.
#
# Nao vira laco infinito porque a parada e por PROGRESSO: erro genuino (representante
# inexistente, cliente que sumiu do Mercos) nao conserta em volta nenhuma, a volta fecha
# com 0 processados e o driver para dizendo isso.
STATUS_REFAZER = ("pendente", "erro")


def pendentes(sb, empresa_id: str, alvo: list[str] | None) -> list[str]:
    q = (sb.table("cadastro_rca_mercos_processado")
           .select("cnpj")
           .eq("mercos_empresa_id", empresa_id)
           .in_("status", list(STATUS_REFAZER)))
    linhas = q.execute().data or []
    achados = []
    for l in linhas:
        c = re.sub(r"\D", "", l.get("cnpj") or "")
        if len(c) == 14 and (alvo is None or c in alvo):
            achados.append(c)
    return sorted(set(achados))


def main() -> int:
    filial = sys.argv[1].lower()
    if filial not in EMPRESAS:
        print(f"filial invalida: {filial} (use es|rj)")
        return 2
    # 2o argumento opcional: restringe a estes CNPJs. Sem ele, TODA a fila da filial.
    alvo = None
    if len(sys.argv) > 2 and sys.argv[2].strip():
        alvo = {re.sub(r"\D", "", c).zfill(14) for c in sys.argv[2].split(",") if c.strip()}

    empresa_id = EMPRESAS[filial]
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    for volta in range(1, MAX_VOLTAS + 1):
        fila = pendentes(sb, empresa_id, alvo)
        if not fila:
            print(f"{ts()} [{filial.upper()}] fila vazia -- nada a fazer. FIM.", flush=True)
            return 0
        print(f"{ts()} [{filial.upper()}] volta {volta}/{MAX_VOLTAS} -- {len(fila)} pendente(s)", flush=True)

        # `-u`: sem isto a saida do escritor fica no buffer do Python e so aparece no
        # fim -- durante a 1a rodada de 11/set o log ficou VAZIO por 20 min e a unica
        # forma de saber se andava era consultar o ledger.
        proc = subprocess.run(
            [sys.executable, "-u", "escritor.py", "--filial", filial, "--cnpjs", ",".join(fila)],
            env=os.environ.copy(),
        )

        restantes = pendentes(sb, empresa_id, alvo)
        feitos = len(fila) - len(restantes)
        print(f"{ts()} [{filial.upper()}] volta {volta}: {feitos} processado(s), "
              f"{len(restantes)} restante(s) (escritor saiu {proc.returncode})", flush=True)

        if not restantes:
            print(f"{ts()} [{filial.upper()}] FILA ZERADA em {volta} volta(s).", flush=True)
            return 0
        if feitos == 0:
            print(f"{ts()} [{filial.upper()}] volta sem NENHUM progresso -- parando. "
                  "O obstaculo nao e a sessao; ver o log desta volta.", flush=True)
            return 1
        time.sleep(5)  # respiro entre sessoes

    print(f"{ts()} [{filial.upper()}] MAX_VOLTAS ({MAX_VOLTAS}) atingido com fila aberta.", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
