"""READ-ONLY: mapear o botao "Bloquear todos os clientes" na carteira do vendedor.

    python ferramentas/_env.py ../../.env.local ferramentas/explorar_bloquear_todos.py \
        424525 "Edilberto Lobo"

Nao clica em NADA. So descreve os controles da tela "Clientes deste usuario".

Existe porque `habilitar_` so ADICIONA: sem um reset, limitar o vendedor nao esvazia a
lista que ele ja tinha. O Edilberto ficou com 3.539 clientes liberados para uma carteira
de 485 (medido 17/set/2026).
"""

import os
import sys
import time

from playwright.sync_api import sync_playwright
from mercos_ui import abrir_carteira_por_url
from mercos_login import login_mercos


def main() -> int:
    empresa, vendedor = sys.argv[1], sys.argv[2]
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context().new_page()
        try:
            login_mercos(pg, empresa, os.environ["MERCOS_EMAIL"], os.environ["MERCOS_SENHA"],
                         os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_SENHA", ""))
            abrir_carteira_por_url(pg, empresa, vendedor)
            time.sleep(2)
            print(f"URL: {pg.url}")

            print("\n=== TEXTO DA PAGINA (2500 chars) ===")
            print((pg.locator("body").inner_text() or "")[:2500])

            print("\n=== BOTOES / LINKS DE ACAO ===")
            for sel in ("button", "a.botao", "input[type=submit]", "a[href*='bloque']",
                        "a[href*='libera']", "[class*='botao']"):
                loc = pg.locator(sel)
                n = loc.count()
                if not n:
                    continue
                print(f"-- {sel}: {n}")
                for i in range(min(n, 25)):
                    el = loc.nth(i)
                    try:
                        txt = " ".join((el.inner_text() or "").split())[:50]
                        idv = el.get_attribute("id") or ""
                        href = el.get_attribute("href") or ""
                        cls = (el.get_attribute("class") or "")[:45]
                    except Exception:
                        continue
                    if txt or idv or href:
                        print(f"   [{i}] txt={txt!r} id={idv!r} href={href!r} class={cls!r}")

            print("\n=== QUALQUER COISA COM 'bloque'/'todos' NO HTML ===")
            html = pg.content()
            import re as _re
            for m in _re.finditer(r"[^<>]*(?:bloque|Bloque|todos|Todos)[^<>]*", html):
                t = " ".join(m.group(0).split())
                if 3 < len(t) < 120:
                    print(f"   {t}")
        finally:
            b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
