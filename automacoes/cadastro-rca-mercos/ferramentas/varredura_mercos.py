"""READ-ONLY: este CNPJ existe no Mercos? Em qual empresa, com qual cliente_id?

    python ferramentas/_env.py ../../.env.local ferramentas/varredura_mercos.py \
        cnpjs.txt saida.json

Entrada: 1 CNPJ por linha (`#` = comentario). Saida: JSON {cnpj: {"ES": id|None, "RJ": id|None}}.

PARA QUE SERVE
--------------
O portao do decisor (`buscar_confirmacoes_metabase`) nao pergunta "existe no Mercos?",
e sim "o NOSSO middleware criou este cliente?" -- exige log com `mid_status='Sucesso'`
E `res_api_status=201`. Cliente cadastrado POR FORA (na mao, processo antigo, log
ausente) fica em `aguardando_mercos` para sempre.

Esta varredura responde a pergunta certa, olhando o Mercos. O resultado alimenta
`mercos_cliente_confirmado` (a 2a fonte do portao, migration 20260911g).

⚠️ TEM PRAZO DE VALIDADE. Medido em 11-14/set/2026: clientes aparecem no Mercos ao longo
do dia, criados por fora. Numa unica tarde, tres varreduras destravaram 7, 12 e 2
clientes. Nao trate um resultado como definitivo -- e candidato a cron diario.

⚠️ NAO confunde "nao achei" com erro: `buscar_cliente_id_por_cnpj` devolve None tanto
quando ha 0 resultados quanto quando ha 2+ (ambiguo). Ambos viram None aqui, de
proposito -- nunca escolher cliente no palpite.
"""

import json
import os
import sys

from playwright.sync_api import sync_playwright

# ⚠️ `python ferramentas/varredura_mercos.py` poe `ferramentas/` no sys.path, nao a
# pasta do robo -- entao `mercos_ui`/`mercos_login` (que vivem um nivel acima) somem.
# Rodando por `ferramentas/_env.py` isso nao aparecia, porque ele ja ajusta o path;
# o workflow chama o script direto e morreu com ModuleNotFoundError (run 36009136397).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mercos_ui import buscar_cliente_id_por_cnpj, SessaoCaiu  # noqa: E402
from mercos_login import login_mercos  # noqa: E402

EMPRESAS = {"ES": "424525", "RJ": "424524"}


def carregar(caminho: str) -> list[str]:
    import re
    fora = []
    for n, linha in enumerate(open(caminho, encoding="utf-8"), 1):
        t = linha.strip()
        if not t or t.startswith("#"):
            continue
        d = re.sub(r"\D", "", t)
        if len(d) != 14:
            raise SystemExit(f"{caminho}:{n}: '{t}' nao e CNPJ de 14 digitos (achei {len(d)})")
        fora.append(d)
    return list(dict.fromkeys(fora))   # dedup preservando ordem


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cnpjs = carregar(sys.argv[1])
    saida: dict[str, dict] = {c: {} for c in cnpjs}
    quieto = lambda *a, **k: None

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        for regiao, empresa in EMPRESAS.items():
            pg = b.new_context().new_page()
            login_mercos(pg, empresa, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                         os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))
            print(f"=== {regiao} ({empresa}) ===", flush=True)
            for i, c in enumerate(cnpjs, 1):
                try:
                    saida[c][regiao] = buscar_cliente_id_por_cnpj(pg, empresa, c, log=quieto)
                except SessaoCaiu:
                    # Abortar a filial inteira: com a sessao morta TODO resto viraria
                    # None, e None aqui significa "nao existe no Mercos" -- o erro mais
                    # caro possivel, porque manda cadastrar quem ja esta cadastrado.
                    print(f"  !! SESSAO CAIU em {c} -- abortando {regiao}", flush=True)
                    saida[c][regiao] = "SESSAO_CAIU"
                    break
                except Exception as e:
                    print(f"  !! erro {c}: {type(e).__name__}", flush=True)
                    saida[c][regiao] = f"ERRO:{type(e).__name__}"
                print(f"  [{i:>3}/{len(cnpjs)}] {c} -> {saida[c][regiao]}", flush=True)
            pg.context.close()
        b.close()

    json.dump(saida, open(sys.argv[2], "w", encoding="utf-8"), indent=1)
    achados = [c for c, v in saida.items()
               if any(x and not str(x).startswith(("ERRO", "SESSAO")) for x in v.values())]
    print(f"\nachados: {len(achados)} de {len(cnpjs)} -- JSON em {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
