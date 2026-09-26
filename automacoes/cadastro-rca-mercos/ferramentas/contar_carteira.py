"""READ-ONLY: quantos clientes estao LIBERADOS na carteira de cada vendedor?

    python ferramentas/_env.py ../../.env.local ferramentas/contar_carteira.py 424525 \
        "Edilberto Lobo" "Flavia Lopes" ...

Nao clica em nada. Le o total que a propria tela informa.

POR QUE ESTA FERRAMENTA EXISTE (17/set/2026)
--------------------------------------------
🔴 `habilitar_` so ADICIONA. Marcar "Limitar acesso aos clientes" restringe o vendedor a
uma LISTA -- mas nao esvazia a lista que ja estava la. Se o perfil dele ja tinha milhares
de clientes vinculados de antes, limitar + vincular a carteira NAO restringe nada: ele
continua vendo os antigos MAIS os novos.

Foi o que o usuario viu: Edilberto com +3 mil clientes liberados no ES depois da operacao,
quando a carteira dele tem 485.

A doc do projeto ja avisava, com o nome do passo que falta -- "Bloquear todos os clientes"
(em `relatorio_mercos/mercos_gestao_carteira.py`), anotado como "so pra reforma de
carteira em massa, nao pra inclusao pontual". Esta operacao E uma reforma em massa; o
passo foi lido como inaplicavel e nao foi portado.

⚠️ A ORDEM CERTA E:  limitar -> BLOQUEAR TODOS -> vincular a carteira.

Esta ferramenta mede o estrago antes de qualquer correcao: para cada vendedor, o total
LIBERADO na tela contra o tamanho da carteira que deveria ter.
"""

import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

from mercos_ui import abrir_carteira_por_url, sessao_caiu
from mercos_login import login_mercos


def _logar(pg, empresa: str) -> None:
    login_mercos(pg, empresa, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                 os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))


def _total(pg, base: str, filtro: str) -> int | None:
    """Total que a tela informa. None = nao consegui ler (NAO e zero).

    Mesma distincao de `conferir_carteira.py`: a tela de login tambem nao tem o texto
    "Resultados X - Y de N", e tratar isso como zero faria uma carteira cheia parecer
    vazia.
    """
    pg.goto(f"{base}?filtro_bloqueio={filtro}", wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)
    corpo = pg.locator("body").inner_text()
    m = re.search(r"Resultados\s+\d+\s*-\s*\d+\s+de\s+(\d+)", corpo)
    if m:
        return int(m.group(1))
    try:
        return 0 if pg.locator("#id_nome").count() > 0 else None
    except Exception:
        return None


def esperado_do_arquivo(vendedor: str) -> int | None:
    slug = re.sub(r"[^a-z0-9]+", "_", vendedor.lower()
                  .replace("á", "a").replace("ã", "a").replace("â", "a")
                  .replace("é", "e").replace("ê", "e").replace("í", "i")
                  .replace("ó", "o").replace("ô", "o").replace("ú", "u")
                  .replace("ç", "c")).strip("_")
    caminho = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "carteiras", f"cnpjs_{slug}.txt")
    if not os.path.exists(caminho):
        return None
    with open(caminho, encoding="utf-8") as fh:
        return sum(1 for l in fh if re.fullmatch(r"\d{14}", l.strip()))


def main() -> int:
    empresa, vendedores = sys.argv[1], sys.argv[2:]
    if not vendedores:
        print(__doc__)
        return 2

    print(f"empresa {empresa}")
    print(f"{'vendedor':<24} {'LIBERADO':>9} {'carteira':>9} {'excedente':>10}")
    linhas = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context().new_page()
        try:
            _logar(pg, empresa)
            for v in vendedores:
                try:
                    abrir_carteira_por_url(pg, empresa, v)
                    base = pg.url.split("?")[0]
                    lib = _total(pg, base, "l")
                    if lib is None and sessao_caiu(pg):
                        _logar(pg, empresa)
                        abrir_carteira_por_url(pg, empresa, v)
                        base = pg.url.split("?")[0]
                        lib = _total(pg, base, "l")
                except Exception as e:
                    print(f"{v:<24} ERRO {type(e).__name__}: {e}")
                    continue
                esp = esperado_do_arquivo(v)
                if lib is None:
                    print(f"{v:<24} {'NAO LI':>9} {str(esp or '-'):>9} {'?':>10}")
                    continue
                exc = (lib - esp) if esp is not None else None
                marca = ""
                if exc is not None and exc > 0:
                    # Excedente = clientes liberados que NAO sao da carteira dele. E o
                    # que "Bloquear todos" antes do vinculo teria eliminado.
                    marca = "  <-- EXCEDENTE"
                print(f"{v:<24} {lib:>9} {str(esp or '-'):>9} "
                      f"{str(exc if exc is not None else '?'):>10}{marca}")
                linhas.append((v, lib, esp, exc))
        finally:
            b.close()

    ruins = [l for l in linhas if l[3] is not None and l[3] > 0]
    if ruins:
        print(f"\n🔴 {len(ruins)} vendedor(es) com clientes liberados ALEM da carteira.")
        print("   Limitar + vincular NAO restringe: `habilitar_` so adiciona, e a lista")
        print("   antiga continua valendo. Falta o passo 'Bloquear todos os clientes'")
        print("   ANTES do vinculo. Ver docs/processos/mercos-limitar-carteira-*.md")
    else:
        print("\n✅ nenhum excedente: cada carteira tem so o que deveria")
    return 0


if __name__ == "__main__":
    sys.exit(main())
