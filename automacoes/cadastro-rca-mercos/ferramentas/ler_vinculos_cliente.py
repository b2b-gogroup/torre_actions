"""READ-ONLY: o que ESTA vinculado hoje num cliente do Mercos (tabela e condicao)?

    python ferramentas/_env.py ../../.env.local ferramentas/ler_vinculos_cliente.py \
        424525 64180829[,66929576,...]

Imprime, por cliente: razao social, cidade/UF, TABELAS de preco e CONDICOES de
pagamento atualmente vinculadas. Nao clica em nada, nao salva nada.

⚠️ POR QUE NAO DA PARA CONFERIR PELO PAINEL DE "EDITAR VINCULOS"
----------------------------------------------------------------
O painel de edicao abre com **zero checkbox marcado**: valor ja vinculado NAO vem
pre-marcado. Medido em 21/set/2026 no PK Pinheiro, que tinha 2 condicoes ativas e
exibia 41 linhas de BOLETO, todas desmarcadas. Quem tentar ler dali conclui "o
cliente nao tem nada", que e o oposto da verdade.

A tela de LEITURA (esta, antes de clicar em Editar) e a unica que mostra o estado
real -- as colunas "Tabelas de preco" e "Condicoes de pagamento" da linha do cliente.

⚠️ E o mesmo fato e o que torna o botao **Remover** do painel seguro: ele age so
sobre o que foi marcado naquele momento. Se um dia o painel passar a pre-marcar o
que ja existe, o MESMO clique passa a remover todas as condicoes do cliente.

PARA QUE SERVE
--------------
Conferir se um cadastro foi REALMENTE aplicado. `vinculos_aplicado = true` no ledger
significa "o robo clicou", nao "esta vinculado" -- a mesma distincao que
`conferir_carteira.py` faz para a carteira (passo 3 de 3). Sem esta ferramenta so a
carteira era verificavel, e os passos 1 (condicao) e 2 (tabela) ficavam no escuro.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright

from mercos_login import login_mercos


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    empresa = sys.argv[1]
    ids = [x.strip() for x in sys.argv[2].split(",") if x.strip()]

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context().new_page()
        login_mercos(pg, empresa, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                     os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))
        for cid in ids:
            pg.goto(f"https://app.mercos.com/{empresa}/vinculos-e-permissoes-de-cliente/"
                    f"?cliente_id={cid}", wait_until="domcontentloaded", timeout=60_000)
            time.sleep(4)
            # A linha do cliente e a que traz as colunas da grade separadas por TAB.
            # ⚠️ Contar tabs NAO basta: logo acima da grade existe uma linha feita
            # SO de tabs (o cabecalho vem quebrado, um rotulo por linha). Ela casa
            # com ">= 5 tabs" e, sendo a primeira, era a que o `next` pegava --
            # devolvendo tabela e condicao VAZIAS, que e exatamente o "sem vinculo"
            # falso que esta ferramenta existe para NAO produzir (achado ao conferir
            # o CDA em 22/set: a tela mostrava as duas tabelas e a ferramenta, nada).
            # Exigir celulas com conteudo e o que separa a grade do enfeite.
            def _celulas(linha_txt: str) -> list[str]:
                c = [x.strip() for x in linha_txt.split("\t")]
                return c[1:] if c and not c[0] else c   # a linha comeca com TAB

            c = next((cel for cel in (_celulas(l) for l in pg.inner_text("body").splitlines())
                      if len(cel) >= 6 and sum(1 for x in cel if x) >= 4), None)
            if not c:
                # NUNCA imprimir "sem vinculo" aqui: a causa mais provavel e sessao
                # caida ou cliente_id inexistente, e as tres coisas sao diferentes.
                print(f"  {cid}  ILEGIVEL -- a grade nao carregou (sessao caida? id errado?)")
                continue
            nome, cidade, uf = (c + ["", "", ""])[:3]
            tabelas = c[4] if len(c) > 4 else "?"
            condicoes = c[5] if len(c) > 5 else "?"
            print(f"\n  {cid}  {nome} ({cidade}/{uf})")
            print(f"      tabelas  : {tabelas}")
            print(f"      condicoes: {condicoes}")
        pg.context.close()
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
