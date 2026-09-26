"""READ-ONLY: dumpa a tela de edicao do colaborador pra mapear a caixa de permissao.

    python ferramentas/_env.py ../../.env.local ferramentas/explorar_permissoes.py \
        424525 "Mirella Melo"

Nao clica em nada, nao salva nada. So descreve o que existe na tela.
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright
from mercos_login import login_mercos

ROTULO = "Limitar acesso aos clientes"


def main() -> int:
    empresa_id, vendedor = sys.argv[1], sys.argv[2]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context().new_page()
        try:
            login_mercos(pg, empresa_id, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                         os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))
            pg.goto(f"https://app.mercos.com/{empresa_id}/colaboradores/",
                    wait_until="domcontentloaded", timeout=60_000)
            time.sleep(2)
            card = pg.locator(f'div.well.colaborador:has-text("{vendedor}")')
            print(f"cards achados: {card.count()}")
            href = card.first.locator('a.js-alterar-colaborador').first.get_attribute("href")
            print(f"href alterar: {href}")
            pg.goto(f"https://app.mercos.com{href}", wait_until="domcontentloaded", timeout=60_000)

            # Espera GENEROSA: a tela e React e a secao PERMISSOES monta depois do HTML.
            for s in (3, 5, 8):
                time.sleep(s if s == 3 else s - 3)
                achou = pg.get_by_text(ROTULO, exact=False).count()
                print(f"  apos ~{s}s: ocorrencias do texto '{ROTULO}' = {achou}")
                if achou:
                    break

            print("\n=== TEXTO DA PAGINA (primeiros 3000 chars) ===")
            print((pg.locator("body").inner_text() or "")[:3000])

            print("\n=== ELEMENTOS COM 'checkbox' NA CLASSE ===")
            cb = pg.locator('[class*="heckbox"]')
            print(f"total: {cb.count()}")
            for i in range(min(cb.count(), 12)):
                el = cb.nth(i)
                print(f"  [{i}] tag={el.evaluate('e=>e.tagName')} "
                      f"class={(el.get_attribute('class') or '')[:80]!r} "
                      f"txt={' '.join((el.inner_text() or '').split())[:60]!r}")

            print("\n=== input[type=checkbox] ===")
            ip = pg.locator('input[type="checkbox"]')
            print(f"total: {ip.count()}")

            print("\n=== HTML em volta do rotulo ===")
            alvo = pg.get_by_text(ROTULO, exact=False)
            print(f"ocorrencias: {alvo.count()}")
            for i in range(min(alvo.count(), 3)):
                el = alvo.nth(i)
                print(f"\n--- ocorrencia {i} ---")
                print(f"tag={el.evaluate('e=>e.tagName')} class={(el.get_attribute('class') or '')[:90]!r}")
                pai = el.locator("xpath=ancestor::*[self::label or self::div][1]")
                if pai.count():
                    html = pai.first.evaluate("e=>e.outerHTML")
                    print(f"PAI outerHTML ({len(html)} chars):\n{html[:1500]}")

            print("\n=== IFRAMES ===")
            print(f"total: {len(pg.frames)}")
            for f in pg.frames:
                print(f"  {f.url[:100]}")
        finally:
            b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
