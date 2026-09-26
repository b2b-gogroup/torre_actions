"""Acha linhas da planilha que estao PROCESSADAS no ledger mas sem o carimbo de OK.

    # so relata (padrao)
    python ferramentas/_env.py ../../.env.local ferramentas/conferir_planilha.py
    # relata E marca
    APLICAR=1 python ferramentas/_env.py ../../.env.local ferramentas/conferir_planilha.py

O PROBLEMA QUE ISTO RESOLVE (medido 14/set/2026)
------------------------------------------------
A planilha e um formulario: o mesmo cliente e reenviado e vira DUAS linhas com o mesmo
CNPJ. O ledger guarda UM `sheet_row` por (cnpj, filial) e `atualizar_status_planilha`
grava so nele -- a duplicata fica "Chamado aberto" para sempre, num cliente que esta
cadastrado e liberado ha semanas.

⚠️ E A VERIFICACAO INGENUA NAO PEGA. Cruzar "o `sheet_row` do ledger esta marcado?" da
100% de acerto e mesmo assim esconde o problema: em 14/set reportei "287 de 287
marcados" e o usuario continuava vendo 4 clientes como pendentes (linhas 386, 555, 572
e 612). A pergunta certa e **"TODA linha com esse CNPJ esta marcada?"**, cruzando pela
coluna E (CNPJ) -- que e o que este script faz.

⚠️ So escreve a COLUNA A, e so depois de conferir o CNPJ daquela linha. Se a planilha
for reordenada entre a leitura e a escrita, a linha certa muda e gravar as cegas
carimbaria OK no cliente errado.
"""

import json
import os
import re
import sys

import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

SHEET_ID = "1bX4GmpKoOITG6l1y-BeW9X8l3LZ6Xcm8COcaHk9Ys-I"   # "Cadastro de Clientes RCA"
OK = "Cliente OK no mercos"
COL_STATUS = 0   # A
COL_RAZAO = 3    # D
COL_CNPJ = 4     # E


def main() -> int:
    aplicar = os.environ.get("APLICAR") == "1"
    escopo = ["https://www.googleapis.com/auth/spreadsheets"] if aplicar else \
             ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    cred = Credentials.from_service_account_info(
        json.loads(os.environ["CADASTRO_RCA_SHEETS_SERVICE_ACCOUNT_JSON"]), scopes=escopo)
    ws = gspread.authorize(cred).open_by_key(SHEET_ID).worksheets()[0]
    todas = ws.get_all_values()

    linhas_por_cnpj: dict[str, list[int]] = {}
    for n, row in enumerate(todas[1:], start=2):
        if COL_CNPJ < len(row):
            d = re.sub(r"\D", "", row[COL_CNPJ]).zfill(14)
            if len(d) == 14 and d != "0" * 14:
                linhas_por_cnpj.setdefault(d, []).append(n)

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    led = sb.table("cadastro_rca_mercos_processado").select("cnpj,status").execute().data or []
    processado: dict[str, bool] = {}
    for r in led:
        c = re.sub(r"\D", "", r["cnpj"] or "").zfill(14)
        processado[c] = processado.get(c, False) or (r["status"] == "processado")

    faltam = []
    for cnpj, ok in processado.items():
        if not ok:
            continue
        for n in linhas_por_cnpj.get(cnpj, []):
            atual = (todas[n - 1][COL_STATUS] or "").strip()
            if atual != OK:
                faltam.append((n, cnpj, (todas[n - 1][COL_RAZAO] or "")[:38], atual or "(vazio)"))

    print(f"planilha: {len(todas)-1} linhas, {len(linhas_por_cnpj)} CNPJs distintos")
    print(f"processados no ledger: {sum(1 for v in processado.values() if v)}")
    print(f"linhas processadas SEM o carimbo: {len(faltam)}")
    for n, c, razao, atual in sorted(faltam):
        print(f"   linha {n:<5} {c}  {razao:<40} {atual!r}" + ("" if aplicar else "   [SIMULACAO]"))

    if aplicar:
        for n, c, _, _ in sorted(faltam):
            confere = re.sub(r"\D", "", todas[n - 1][COL_CNPJ]).zfill(14)
            if confere != c:
                print(f"   linha {n}: CNPJ mudou ({confere} != {c}) -- PULANDO")
                continue
            ws.update_acell(f"A{n}", OK)
        print(f"\n{len(faltam)} linha(s) marcadas. Rode de novo sem APLICAR para conferir.")
    elif faltam:
        print("\n(nada foi escrito -- rode com APLICAR=1 para marcar)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
