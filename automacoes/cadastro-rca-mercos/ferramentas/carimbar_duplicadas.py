"""Carimba na planilha a linha ABERTA cujo CNPJ ja esta aplicado no Mercos.

    python ferramentas/_env.py ../../.env.local ferramentas/carimbar_duplicadas.py [--aplicar]

Sem `--aplicar` so lista (dry).

POR QUE EXISTE
--------------
O formulario cria uma linha NOVA a cada submissao, e o decisor deduplica por
CNPJ: o ledger guarda UMA `sheet_row` por cliente. Quando o mesmo CNPJ e pedido
de novo, o robo aplica (ou ja aplicou) e carimba a linha ANTIGA -- a nova fica
"Chamado aberto" para sempre, e quem olha a planilha ve pendencia onde o
cliente ja esta configurado no Mercos.

⚠️ So carimba com PROVA de aplicacao: `vinculos_aplicado = true` no ledger.
`status='processado'` sozinho NAO basta -- linha encerrada a mao (decisao
humana, sem o robo tocar no Mercos) tem esse status e nao teve tabela nem
condicao aplicadas; carimbar ali afirmaria na planilha um trabalho que nunca
aconteceu.

⚠️ Nunca toca a linha que o proprio ledger aponta (`sheet_row`): essa e do
robo, e ele a carimba no fim da aplicacao.
"""
import os
import re
import sys

import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

SHEETS_CADASTRO_ID = "1bX4GmpKoOITG6l1y-BeW9X8l3LZ6Xcm8COcaHk9Ys-I"
STATUS_OK = "Cliente OK no mercos"


def _dig(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def main() -> int:
    aplicar = "--aplicar" in sys.argv[1:]

    creds = Credentials.from_service_account_info(
        json_loads(os.environ["CADASTRO_RCA_SHEETS_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    ws = gspread.authorize(creds).open_by_key(SHEETS_CADASTRO_ID).worksheets()[0]
    linhas = ws.get_all_values()
    header = linhas[0]
    i_status = 0
    i_cnpj = next(i for i, h in enumerate(header) if _norm(h).startswith("cnpj"))
    i_chamado = next(i for i, h in enumerate(header) if "chamado" in _norm(h))

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    led = (sb.table("cadastro_rca_mercos_processado")
           .select("cnpj,chamado_goservice,sheet_row,status,vinculos_aplicado,carteira_aplicada")
           .eq("status", "processado").limit(5000).execute().data or [])

    aplicado: dict[str, dict] = {}
    for r in led:
        if not r.get("vinculos_aplicado"):
            continue   # encerrada a mao: o robo nunca tocou no Mercos
        cnpj = _dig(r.get("cnpj"))
        atual = aplicado.get(cnpj)
        if atual is None:
            aplicado[cnpj] = r

    alvos = []
    for idx, linha in enumerate(linhas[1:], start=2):
        status = (linha[i_status] if i_status < len(linha) else "").strip()
        if status.lower().startswith(STATUS_OK.lower()):
            continue
        cnpj = _dig(linha[i_cnpj] if i_cnpj < len(linha) else "")
        if len(cnpj) != 14:
            continue
        r = aplicado.get(cnpj)
        if not r:
            continue
        if r.get("sheet_row") == idx:
            continue   # linha do proprio ledger -- o robo carimba
        chamado = (linha[i_chamado] if i_chamado < len(linha) else "").strip()
        alvos.append((idx, chamado, cnpj, r.get("chamado_goservice"), status))

    print(f"{len(alvos)} linha(s) aberta(s) com o CNPJ ja aplicado no Mercos:")
    for idx, chamado, cnpj, ch_led, status in alvos:
        print(f"  linha {idx:>4} | chamado {chamado:>7} | {cnpj} | aplicado no chamado {ch_led} | status atual: '{status}'")

    if not alvos:
        return 0
    if not aplicar:
        print("\n(dry) nada escrito. Rode com --aplicar para carimbar.")
        return 0

    for idx, chamado, cnpj, ch_led, _status in alvos:
        # ⚠️ Literal EXATO da lista de validacao da coluna A (ver escritor.py):
        # sufixo explicativo entra pela API e fica marcado como invalido na
        # planilha. O "duplicado, aplicado no chamado X" fica no log desta
        # ferramenta e no ledger, nao na celula.
        valor = STATUS_OK
        ws.update_acell(f"A{idx}", valor)
        print(f"  [ok] linha {idx} -> '{valor}'")
    print(f"\n{len(alvos)} linha(s) carimbada(s).")
    return 0


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", str(s or ""))
    return "".join(c for c in s if unicodedata.category(c) != "Mn").strip().lower()


def json_loads(s):
    import json
    return json.loads(s)


if __name__ == "__main__":
    sys.exit(main())
