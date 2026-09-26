"""READ-ONLY: quais inside sales ja estao com "Limitar acesso aos clientes" marcado?

    python ferramentas/_env.py ../../.env.local ferramentas/varrer_permissoes.py 424525 \
        "Mirella Melo" "Flavia Lopes" ...

Nao clica em nada, nao salva nada. Loga UMA vez e visita a tela de edicao de cada
vendedor -- 22 execucoes de `limitar_acesso_clientes.py --dry-run` seriam 22 logins.

Distingue quatro desfechos, e a diferenca importa:
  LIMITADO        -- ja restrito a carteira (nada a fazer)
  aberto          -- ve TODOS os clientes (e o que queremos mudar)
  sem card        -- o vendedor nao existe NESTA empresa
  ILEGIVEL        -- a caixa nao foi encontrada; provavelmente perfil "Administrador",
                     onde a secao CLIENTES nem existe. NUNCA tratar como "aberto".
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright
from mercos_login import login_mercos
from mercos_ui import sessao_caiu

ROTULO = "Limitar acesso aos clientes"
SEL_LINHA = f'div[class*="Checkbox__checkbox"]:has-text("{ROTULO}")'
SEL_BOTAO = 'button[class*="Checkbox__root"]'


def _religar(pg, empresa_id: str) -> None:
    """Reloga. A varredura e longa e a conta e de uma PESSOA: o Mercos aceita 1 sessao
    por usuario, entao qualquer um abrindo o Mercos derruba a leitura no meio."""
    print("      [sessao] caiu -- relogando")
    login_mercos(pg, empresa_id, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                 os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))


def estado_do_vendedor(pg, empresa_id: str, vendedor: str) -> tuple[str, str]:
    """(estado, detalhe). Nunca levanta -- um vendedor ruim nao derruba a varredura."""
    try:
        pg.goto(f"https://app.mercos.com/{empresa_id}/colaboradores/",
                wait_until="domcontentloaded", timeout=60_000)
        time.sleep(2)
        card = pg.locator(f'div.well.colaborador:has-text("{vendedor}")')
        n = card.count()
        if n == 0:
            # 🔴 Sessao morta serve a TELA DE LOGIN, que nao tem card nenhum -- e o
            # falso negativo e indistinguivel de "nao existe nesta empresa". Foi o que
            # aconteceu na 1a varredura (16/set): os 4 primeiros acharam card e os 7
            # seguintes vieram "sem card", com a Mirella tendo sido lida OK minutos
            # antes. Reloga e pergunta de novo antes de afirmar a ausencia.
            if sessao_caiu(pg):
                _religar(pg, empresa_id)
                pg.goto(f"https://app.mercos.com/{empresa_id}/colaboradores/",
                        wait_until="domcontentloaded", timeout=60_000)
                time.sleep(2)
                card = pg.locator(f'div.well.colaborador:has-text("{vendedor}")')
                n = card.count()
            if n == 0:
                return "sem card", "nao existe nesta empresa (sessao conferida)"
        if n > 1:
            return "AMBIGUO", f"{n} cards com esse nome"

        link = card.first.locator('a.js-alterar-colaborador')
        if link.count() == 0:
            return "ILEGIVEL", "card sem botao Alterar"
        href = link.first.get_attribute("href") or ""
        pg.goto(f"https://app.mercos.com{href}", wait_until="domcontentloaded", timeout=60_000)
        time.sleep(2)

        linha = pg.locator(SEL_LINHA)
        if linha.count() != 1:
            # Perfil Administrador nao tem a secao CLIENTES. "Nao achei" nunca vira
            # "aberto": os dois pedem acoes opostas.
            return "ILEGIVEL", f"{linha.count()} linhas do rotulo (perfil Administrador?) {href}"
        botao = linha.first.locator(SEL_BOTAO)
        if botao.count() != 1:
            return "ILEGIVEL", f"{botao.count()} botoes na linha {href}"
        marcado = (botao.first.inner_text() or "").strip() == "check"
        return ("LIMITADO" if marcado else "aberto"), href
    except Exception as e:
        return "ERRO", f"{type(e).__name__}: {e}"


def main() -> int:
    empresa_id, vendedores = sys.argv[1], sys.argv[2:]
    if not vendedores:
        print(__doc__)
        return 2

    print(f"empresa {empresa_id} | {len(vendedores)} vendedor(es)")
    resultados: list[tuple[str, str, str]] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context().new_page()
        try:
            login_mercos(pg, empresa_id, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                         os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))
            for v in vendedores:
                est, det = estado_do_vendedor(pg, empresa_id, v)
                print(f"  {est:<10} {v:<24} {det}")
                resultados.append((est, v, det))
        finally:
            b.close()

    print(f"\nRESUMO empresa {empresa_id}")
    for est in sorted({r[0] for r in resultados}):
        nomes = [r[1] for r in resultados if r[0] == est]
        print(f"  {est:<10} {len(nomes):>2}  {', '.join(nomes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
