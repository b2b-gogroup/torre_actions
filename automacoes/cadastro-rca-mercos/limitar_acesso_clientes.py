"""Marca "Limitar acesso aos clientes" no perfil de um usuario do Mercos.

Criado 16/set/2026, quando o pedido passou de "vincular cliente novo" para "cada
inside sales so enxerga a carteira dele".

⚠️ ESTE E O PASSO QUE RESTRINGE -- e ele e IRREVERSIVEL PELO ROBO
------------------------------------------------------------------
`vincular_carteira_lote.py` so ADICIONA cliente a carteira; sozinho ele nao limita
nada (o vendedor segue vendo todos). Este script e o que fecha a porta.

🔴 A ORDEM E FORCADA: ESTE SCRIPT RODA PRIMEIRO.
O botao "Clientes deste usuario", que e a porta do robo de vinculo, SO NASCE depois
desta caixa estar marcada -- antes o card so tem Alterar/Excluir, e
`vincular_carteira_lote.py` aborta (medido na Laura Pietz, 16/set/2026). Nao existe
caminho para popular a carteira antes de restringir.

⚠️ O PRECO E UMA JANELA CEGA e ela precisa ser declarada a quem opera: entre este
script e o fim do vinculo, o vendedor fica LIMITADO COM A CARTEIRA VAZIA -- sem
enxergar nem vender. Sao ~3s por cliente (~25 min numa carteira de 485). Para carteira
grande, rodar fora do expediente nao e conforto, e requisito.

⚠️ Ele NAO desmarca. Se a caixa ja estiver marcada, sai como `ja_limitado` sem clicar --
clicar de novo DESMARCARIA (e um toggle), devolvendo a base inteira ao vendedor. Por
isso o estado e lido ANTES, e o script se recusa a agir no escuro: sem conseguir ler o
estado, ele para em vez de chutar.

⚠️ So mexe NESSA caixa. O perfil "Comum" traz varias outras ja configuradas (cadastrar
clientes, relatorio de comissoes, links de pagamento, transportadoras...) e a tela salva
todas de uma vez no mesmo Salvar -- alterar outra seria mudanca de permissao nao pedida.

USO
---
  python -u ferramentas/_env.py ../../.env.local limitar_acesso_clientes.py \
      --filial es --vendedor "Mirella Melo" --dry-run
  python -u ferramentas/_env.py ../../.env.local limitar_acesso_clientes.py \
      --filial es --vendedor "Mirella Melo"

`--dry-run` navega, acha o card, abre a edicao e LE o estado da caixa -- sem clicar nem
salvar. E a forma de conferir que o nome casa com 1 card antes de mexer em permissao.
"""

import argparse
import os
import sys
import time

from playwright.sync_api import sync_playwright

from mercos_login import login_mercos, conferir_credencial_gmail
from mercos_ui import sessao_caiu, SessaoCaiu

EMPRESAS = {"es": "424525", "rj": "424524"}

MERCOS_EMAIL = os.environ["MERCOS_EMAIL"]
MERCOS_SENHA = os.environ["MERCOS_SENHA"]
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
GMAIL_SENHA = os.environ.get("GMAIL_SENHA") or ""

ROTULO = "Limitar acesso aos clientes"

# ⚠️ As classes do Mercos sao CSS Modules com hash no fim
# (`Checkbox__root___eFFN8`). O hash MUDA a cada build deles, entao casar pelo nome
# completo quebraria sozinho num dia qualquer. Casamos pelo PREFIXO estavel + o texto
# do rotulo, que e o que de fato identifica a caixa.
#
# Estrutura real, mapeada ao vivo em 16/set/2026:
#   <div class="Checkbox__checkbox___tx3h0">        <- a linha inteira
#     <button class="Checkbox__root___eFFN8">check</button>   <- O ESTADO MORA AQUI
#     <label><span>Limitar acesso aos clientes<i>info</i></span></label>
#   </div>
#
# ⚠️ `Checkbox__checkbox` (C maiusculo) NAO casa com o container externo
# `PerfilPermissoesUsuario__checkboxes`, que envolve a secao CLIENTES inteira -- e por
# isso a busca acerta a linha e nao o bloco.
SEL_LINHA = f'div[class*="Checkbox__checkbox"]:has-text("{ROTULO}")'
SEL_BOTAO = 'button[class*="Checkbox__root"]'


def _ts() -> str:
    from datetime import datetime
    return datetime.now().strftime("%H:%M:%S")


def abrir_edicao_do_colaborador(page, empresa_id: str, vendedor: str) -> str:
    """Navega ate a tela de edicao do colaborador. Devolve a URL aberta.

    ⚠️ Navega por URL, nao por menu: `/colaboradores/` e a mesma tela de "Todos os
    usuarios", e ir direto tira um clique que ja falhou em headless por rotulo com
    acento quebrado.

    ⚠️ O `colaborador_id` e POR EMPRESA -- o mesmo vendedor tem um id no ES e outro no
    RJ. Por isso o id sai do href do card, nunca de uma tabela nossa.
    """
    page.goto(f"https://app.mercos.com/{empresa_id}/colaboradores/",
              wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)

    # Mesmo seletor de card usado em `mercos_ui.abrir_clientes_do_vendedor` (corpo unico
    # de conhecimento sobre a tela). `:has-text()` e SUBSTRING case-insensitive: nome
    # mais CURTO que o do Mercos casa, mais LONGO nao.
    card = page.locator(f'div.well.colaborador:has-text("{vendedor}")')
    n = card.count()
    if n == 0:
        # 🔴 SESSAO MORTA SERVE A TELA DE LOGIN, QUE NAO TEM CARD NENHUM -- e o zero e
        # indistinguivel de "esse vendedor nao existe". Em 16/set/2026 isso derrubou a
        # Flavia Lopes com a mensagem abaixo, mandando procurar erro de NOME quando a
        # causa era outra rodada disputando a mesma conta. Perguntar ANTES de acusar.
        if sessao_caiu(page):
            raise SessaoCaiu(
                "a sessao do Mercos caiu ao abrir a lista de colaboradores "
                f"(procurando '{vendedor}') -- a conta so aceita 1 sessao por usuario")
        raise RuntimeError(
            f"Nenhum card para '{vendedor}' em /{empresa_id}/colaboradores/ "
            "(sessao conferida, esta viva). O nome tem que ser IGUAL OU MAIS CURTO que o "
            "do Mercos (ex.: 'Luciano Bicalho', nao 'Luciano Pitangueira Bicalho').")
    if n > 1:
        raise RuntimeError(f"{n} cards para '{vendedor}' -- nome ambiguo, nao da pra decidir sozinho")

    link = card.first.locator('a.js-alterar-colaborador')
    if link.count() == 0:
        raise RuntimeError(f"Card de '{vendedor}' nao tem botao 'Alterar'")
    href = link.first.get_attribute("href") or ""
    page.goto(f"https://app.mercos.com{href}", wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)
    return href


def ler_estado(page) -> bool | None:
    """A caixa esta marcada? None = nao consegui ler (e o script NAO chuta).

    Mapeado ao vivo em 16/set/2026 na Mirella (ES).

    O estado NAO esta num input nem numa classe "checked": esta no TEXTO do <button>,
    que renderiza o ligature `check` do material-icons quando marcado e fica VAZIO
    quando desmarcado. Conferido contra a tela: "Limitar acesso aos clientes" e
    "Permitir cadastrar novos clientes" vinham com `check`, "Permitir vincular tabelas
    de preco" vinha vazio.

    A pagina tem 40 input[type=checkbox] e 53 elementos com "checkbox" na classe -- ler
    um input generico pegaria outra permissao. Por isso partimos da LINHA que contem o
    rotulo e so dentro dela procuramos o botao.
    """
    linha = page.locator(SEL_LINHA)
    if linha.count() != 1:
        return None
    botao = linha.first.locator(SEL_BOTAO)
    if botao.count() != 1:
        return None
    return (botao.first.inner_text() or "").strip() == "check"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--filial", choices=sorted(EMPRESAS), required=True)
    ap.add_argument("--vendedor", required=True,
                    help='nome COMO O MERCOS ESCREVE (ex.: "Mirella Melo")')
    ap.add_argument("--dry-run", action="store_true",
                    help="navega e le o estado da caixa, mas nao clica nem salva")
    ap.add_argument("--visivel", action="store_true")
    args = ap.parse_args()

    empresa_id = EMPRESAS[args.filial]
    print(f"{_ts()} filial {args.filial.upper()} (empresa {empresa_id}) | "
          f"'{args.vendedor}'{' | DRY-RUN' if args.dry_run else ''}")

    if GMAIL_USER and GMAIL_SENHA:
        conferir_credencial_gmail(GMAIL_USER, GMAIL_SENHA, MERCOS_EMAIL)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.visivel)
        page = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )).new_page()
        try:
            print(f"{_ts()} Login Mercos ({MERCOS_EMAIL})...")
            login_mercos(page, empresa_id, MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)

            href = abrir_edicao_do_colaborador(page, empresa_id, args.vendedor)
            print(f"{_ts()} tela de edicao aberta: {href}")

            antes = ler_estado(page)
            if antes is None:
                # Sem estado legivel nao ha decisao segura: clicar poderia DESMARCAR.
                # A causa mais provavel e o perfil ser "Administrador", onde a secao
                # CLIENTES nem existe.
                raise RuntimeError(
                    f"Nao consegui ler a caixa '{ROTULO}' na tela de "
                    f"'{args.vendedor}'. Confira se o Perfil do usuario e 'Comum' -- "
                    "em 'Administrador' essa opcao nao existe. Nao vou clicar no escuro: "
                    "a caixa e um toggle e um clique errado DEVOLVE a base inteira a ele.")

            print(f"{_ts()} '{ROTULO}': {'MARCADA' if antes else 'desmarcada'}")

            if antes:
                print(f"{_ts()} [=] ja_limitado -- nada a fazer (clicar DESMARCARIA)")
                return 0
            if args.dry_run:
                print(f"{_ts()} DRY-RUN: marcaria a caixa e salvaria. Nada foi alterado.")
                return 0

            page.locator(SEL_LINHA).first.locator(SEL_BOTAO).first.click()
            time.sleep(0.5)

            # Conferir ANTES de salvar: clique que nao pegou salvaria a tela sem a
            # mudanca e o script anunciaria sucesso.
            if ler_estado(page) is not True:
                raise RuntimeError("cliquei e a caixa nao ficou marcada -- nao vou salvar")

            page.locator('a#btn-salvar').first.click()
            time.sleep(3)

            # Provar pela tela recarregada, nao pelo clique. "O robo clicou" nao e
            # "ficou salvo" -- a licao da EME COSMETICOS em 14/set/2026.
            abrir_edicao_do_colaborador(page, empresa_id, args.vendedor)
            depois = ler_estado(page)
            if depois is not True:
                raise RuntimeError(
                    f"salvei mas a releitura diz {depois!r} -- NAO confirmado. "
                    "Conferir na tela antes de rodar o vinculo da carteira.")
            print(f"{_ts()} [ok] limitado e CONFERIDO na releitura. "
                  "O card agora deve mostrar 'Clientes deste usuario'.")
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
