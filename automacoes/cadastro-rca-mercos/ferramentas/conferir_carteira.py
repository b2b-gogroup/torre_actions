"""READ-ONLY: o cliente esta LIBERADO na carteira deste vendedor, no Mercos?

    python ferramentas/_env.py ../../.env.local ferramentas/conferir_carteira.py \
        424525 pares.txt

`pares.txt`: uma linha por cliente, `cnpj;Nome do Vendedor como o Mercos escreve`.

POR QUE NAO BASTA OLHAR O LEDGER
--------------------------------
`carteira_aplicada = true` significa "o robo CLICOU", nao "esta liberado". Medido em
14/set/2026 com a EME COSMETICOS (55.876.282/0001-87): o escritor registrou
`[ok] liberado` as 15:00:16, a flag ficou true, e o Mercos dizia **BLOQUEADO**.
Revinculada, passou a conferir. Toda conferencia que importa vem daqui, nao do log.

TRES ARMADILHAS QUE ESTE SCRIPT EVITA (todas medidas em 14/set/2026)
--------------------------------------------------------------------
1. **Sessao caindo falsifica a leitura.** Com a sessao morta, a busca filtrada devolve
   ZERO -- indistinguivel de "nao liberado". Os 4 clientes do Marcelo Chrispim vieram
   "NAO liberado" numa passada e LIBERADO na seguinte. Por isso: religa e repete antes
   de afirmar qualquer negativo.
2. **Paginacao por clique mente em silencio.** Clicar "Proxima »" parou em 10 de 34 sem
   erro (o texto do link vem com acento quebrado no headless). Aqui navega-se por URL.
3. **Os botoes NAO dizem o estado.** A linha traz `habilitar_<id>` E `desabilitar_<id>`
   ao mesmo tempo (par toggle). Quem sabe o estado e o filtro `filtro_bloqueio=l`.

Distingue tres desfechos, e a diferenca importa:
  LIBERADO             -- esta na carteira
  BLOQUEADO p/ ele     -- existe na empresa, mas fora da carteira dele
  nao existe na empresa -- nem cadastrado esta
"""

import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

from mercos_ui import abrir_todos_usuarios, abrir_clientes_do_vendedor
from mercos_login import login_mercos


def _logar(pg, empresa: str) -> None:
    login_mercos(pg, empresa, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                 os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))


def _total(pg, base: str, cnpj: str, filtro: str) -> tuple[int | None, str]:
    """Quantos resultados a busca devolveu. None = NAO CONSEGUI LER (nao e zero).

    🔴 A DISTINCAO ENTRE None E 0 E O CORACAO DESTE SCRIPT. A versao anterior devolvia
    0 sempre que o texto "Resultados X - Y de N" nao aparecia -- e a TELA DE LOGIN
    tambem nao tem esse texto. Com a sessao oscilando, "nao consegui ler" virava
    "nenhum resultado", que o chamador traduz em "nao existe na empresa".

    Custou caro em 16/set/2026: a conferencia em lote de 1.844 CNPJs acusou a Juliana
    Esmeria de ter 121 clientes inexistentes; reconferida isolada, eram 275 liberados
    e ZERO inexistentes. O erro e silencioso, massivo e PLAUSIVEL (121 ausentes num
    vendedor Apice parece normal) -- so apareceu porque o numero do robo discordava.

    ⚠️ O sinal de que a leitura vale e a PAGINA SER A TELA CERTA. Se o campo de busca
    `#id_nome` esta la, a pagina carregou e um zero e zero de verdade. Sem ele, nao
    sabemos nada -- e dizer "nao existe" seria inventar.
    """
    pg.goto(f"{base}?nome={cnpj}&filtro_bloqueio={filtro}", wait_until="domcontentloaded")
    time.sleep(1.5)
    corpo = pg.locator("body").inner_text()
    m = re.search(r"Resultados\s+\d+\s*-\s*\d+\s+de\s+(\d+)", corpo)
    nome = ""
    links = pg.locator('a[href*="/clientes/"]')
    if links.count():
        nome = " ".join((links.first.inner_text() or "").split())
    if m:
        return int(m.group(1)), nome
    # Sem o texto de resultados: so afirmamos ZERO se a tela de busca esta viva.
    try:
        pagina_valida = pg.locator("#id_nome").count() > 0
    except Exception:
        pagina_valida = False
    return (0 if pagina_valida else None), nome


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    empresa, caminho = sys.argv[1], sys.argv[2]

    pares: list[tuple[str, str]] = []
    for linha in open(caminho, encoding="utf-8"):
        t = linha.strip()
        if t and not t.startswith("#") and ";" in t:
            c, v = t.split(";", 1)
            pares.append((re.sub(r"\D", "", c).zfill(14), v.strip()))

    por_vendedor: dict[str, list[str]] = {}
    for c, v in pares:
        por_vendedor.setdefault(v, []).append(c)

    resultado: dict[str, str] = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context().new_page()
        _logar(pg, empresa)
        for vendedor, cnpjs in por_vendedor.items():
            abriu = False
            for tentativa in (1, 2):
                try:
                    abrir_todos_usuarios(pg)
                    abrir_clientes_do_vendedor(pg, vendedor)
                    abriu = True
                    break
                except Exception as e:
                    print(f"  [{vendedor}] tentativa {tentativa} falhou ({type(e).__name__}) -- religando", flush=True)
                    try:
                        _logar(pg, empresa)
                    except Exception as e2:
                        print(f"  [{vendedor}] religar falhou: {e2}", flush=True)
            if not abriu:
                # NAO marcar como "nao liberado": nao foi verificado, e dizer o
                # contrario manda gente consertar o que nao esta quebrado.
                print(f"  [{vendedor}] NAO VERIFICADO (nao consegui abrir a carteira)", flush=True)
                for c in cnpjs:
                    resultado[c] = "NAO VERIFICADO"
                continue

            base = pg.url.split("?")[0]
            for c in cnpjs:
                liberados, nome = _total(pg, base, c, "l")
                if liberados is None:
                    # Nao consegui ler. Religa UMA vez e repete; se ainda assim nao
                    # der, declara NAO VERIFICADO em vez de inventar um estado.
                    print(f"   {c}  leitura sem resposta -- religando", flush=True)
                    try:
                        _logar(pg, empresa)
                        abrir_todos_usuarios(pg)
                        abrir_clientes_do_vendedor(pg, vendedor)
                        base = pg.url.split("?")[0]
                        liberados, nome = _total(pg, base, c, "l")
                    except Exception as e:
                        print(f"   {c}  nao consegui religar: {type(e).__name__}", flush=True)
                        liberados = None
                if liberados is None:
                    est = "NAO VERIFICADO"
                elif liberados:
                    est = "LIBERADO"
                else:
                    ambos, nome2 = _total(pg, base, c, "")
                    if ambos is None:
                        est = "NAO VERIFICADO"
                    else:
                        est = "BLOQUEADO p/ ele" if ambos else "nao existe na empresa"
                    nome = nome or nome2
                resultado[c] = est
                print(f"   {c}  {est:<22} ({vendedor})", flush=True)
        b.close()

    # ⚠️ "nao verificado" NAO entra em "precisa de acao": misturar os dois manda
    # alguem consertar o que talvez nao esteja quebrado (licao de 16/set/2026).
    nverif = [c for c, e in resultado.items() if e == "NAO VERIFICADO"]
    ruins = [c for c, e in resultado.items() if e not in ("LIBERADO", "NAO VERIFICADO")]
    print(f"\nliberados: {len(resultado)-len(ruins)-len(nverif)}/{len(resultado)}")
    if ruins:
        print("precisam de acao:", ", ".join(ruins))
    if nverif:
        print(f"NAO VERIFICADOS ({len(nverif)}) -- rodar de novo, NAO tratar como ausente:",
              ", ".join(nverif))
    return 0


if __name__ == "__main__":
    sys.exit(main())
