"""Carrega o resultado da varredura em `mercos_cliente_confirmado` (2a fonte do portao).

    python ferramentas/_env.py ../../.env.local ferramentas/carregar_confirmados.py varredura.json [--dry]

Entrada: o JSON de `varredura_mercos.py` -- {cnpj: {"ES": id|None|"ERRO:..."|"SESSAO_CAIU"}}.

⚠️ SO PROVA POSITIVA, mesma regra de `fn_operacional_auto_baixa`: grava a linha
quando a varredura ACHOU o cliente. "Nao achei" nunca vira registro e nunca
desativa nada -- ausencia e ausencia de informacao, nao prova de que o cliente
saiu do Mercos, e `buscar_cliente_id_por_cnpj` devolve None tanto para 0
resultados quanto para 2+ (ambiguo).

⚠️ `SESSAO_CAIU`/`ERRO:*` sao DESCARTADOS em silencio proposital no upsert mas
CONTADOS no resumo: eles significam "nao consegui perguntar", e trata-los como
None faria o erro mais caro possivel -- mandar cadastrar quem ja esta cadastrado.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

from supabase import create_client

EMPRESAS = {"ES": "424525", "RJ": "424524"}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    caminho = sys.argv[1]
    dry = "--dry" in sys.argv[2:]

    with open(caminho, encoding="utf-8") as fh:
        varredura = json.load(fh)

    origem = "varredura_playwright_" + datetime.now(timezone.utc).strftime("%Y%m%d")
    achados, nao_achados, nao_perguntados = [], 0, 0

    for cnpj_raw, por_regiao in varredura.items():
        cnpj = re.sub(r"\D", "", cnpj_raw or "")
        if len(cnpj) != 14:
            print(f"  [!] '{cnpj_raw}' nao e CNPJ de 14 digitos -- pulando")
            continue
        for regiao, valor in (por_regiao or {}).items():
            empresa_id = EMPRESAS.get(regiao)
            if not empresa_id:
                print(f"  [!] regiao desconhecida '{regiao}' em {cnpj} -- pulando")
                continue
            texto = str(valor or "").strip()
            if not texto:
                nao_achados += 1
                continue
            if texto.startswith(("ERRO", "SESSAO")):
                nao_perguntados += 1
                continue
            achados.append({
                "cnpj": cnpj,
                "mercos_empresa_id": empresa_id,
                "mercos_cliente_id": texto,
                "origem": origem,
                "observacao": f"achado na varredura {regiao} ({empresa_id})",
                "ativo": True,
            })

    print(f"achados: {len(achados)} | nao achados: {nao_achados} | "
          f"nao perguntados (erro/sessao): {nao_perguntados}")
    for a in achados:
        print(f"  + {a['cnpj']} {a['mercos_empresa_id']} -> cliente {a['mercos_cliente_id']}")

    if not achados:
        print("nada a gravar.")
        return 0
    if dry:
        print("--dry: nada gravado.")
        return 0

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    sb.table("mercos_cliente_confirmado").upsert(
        achados, on_conflict="cnpj,mercos_empresa_id").execute()
    print(f"gravado: {len(achados)} linha(s), origem={origem}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
