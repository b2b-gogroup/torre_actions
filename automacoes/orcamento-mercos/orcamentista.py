#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gira - Orcamentista do Mercos
=============================

Cria um ORCAMENTO no Mercos (ES) a partir de um pedido confirmado no link do
Pedido Pronto, gera o LINK DE PAGAMENTO PIX e captura o QR Code para envio.

🔴 REGRA ABSOLUTA, E ELA E O CORACAO DESTE ROBO:
   NUNCA, EM HIPOTESE NENHUMA, CLICAR EM "GERAR PEDIDO".
   O artefato final e um ORCAMENTO com link de pagamento. Gerar o pedido e
   decisao humana, tomada DEPOIS que o cliente paga. Existe uma guarda explicita
   (`_guarda_gerar_pedido`) que aborta a rodada se o robo chegar perto do botao.

Quarto robo que escreve no Mercos. Herda de `automacoes/cadastro-rca-mercos`:
  - `mercos_login.py`  -> login + 2FA por IMAP (6 inputs, filtro por DATA, App Password)
  - `mercos_ui.py`     -> SessaoCaiu, sessao_caiu(), motivo_sessao()

Modos:
  --mapear    abre as telas e confere os seletores. NUNCA escreve.
  --dry       percorre tudo e diz o que faria. NUNCA escreve.
  --aplicar   cria o orcamento e o link de pagamento de verdade.

Uso:
  python orcamentista.py --mapear
  python orcamentista.py --dry     --cnpj 12345678000190 --itens BB02009:12,BB02001:6
  python orcamentista.py --aplicar --cnpj 12345678000190 --itens BB02009:12 --desconto 29
  python orcamentista.py --aplicar --fila --limite 5
"""

import argparse
import json
import os
import re
import urllib.request
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

# A fila do botao "Aprovar e pagar no PIX". Vizinho deste arquivo, nao
# vendorizado: e codigo deste robo, nao do Mercos.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fila  # noqa: E402

# ── Modulos compartilhados do Mercos ─────────────────────────────────────────
# ⚠️ Corpo unico (convencao #14 da Torre). NAO COPIAR estes arquivos para ca:
# o Mercos muda o HTML sem avisar, e com duas copias o primeiro robo e
# consertado e o segundo continua quebrado EM SILENCIO -- porque "nao achei o
# cliente" e uma saida valida da funcao.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CANDIDATOS = [
    os.path.join(BASE_DIR, "..", "..", "automacoes", "cadastro-rca-mercos"),
    os.path.join(BASE_DIR, "..", "cadastro-rca-mercos"),
    os.environ.get("MERCOS_MODULOS_DIR", ""),
]
for _c in _CANDIDATOS:
    if _c and os.path.isdir(_c):
        sys.path.insert(0, _c)
        break
try:
    from mercos_login import conferir_credencial_gmail, login_mercos  # noqa: E402
    import mercos_ui  # noqa: E402
except ImportError as e:
    raise SystemExit(
        f"Nao achei os modulos compartilhados do Mercos ({e}).\n"
        "Eles vivem em `automacoes/cadastro-rca-mercos/` do repo torre_de_performance_b2b.\n"
        "Se este robo estiver em outro repositorio, aponte MERCOS_MODULOS_DIR para a pasta\n"
        "-- NAO copie os arquivos: duas copias divergem em silencio."
    ) from None

# ── Ambiente ─────────────────────────────────────────────────────────────────
MERCOS_EMAIL = os.environ.get("MERCOS_EMAIL", "")
MERCOS_SENHA = os.environ.get("MERCOS_SENHA", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_SENHA = os.environ.get("GMAIL_SENHA", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Empresa ES no Mercos. Sai do mapeamento: https://app.mercos.com/424525/pedidos/
# ⚠️ `or` e nao o default do get(): o workflow passa a variavel SEMPRE, e vazia
# quando ninguem a definiu. Com `get(chave, default)` o default so vale se a
# chave nao existir -- e ai a empresa virava "" e a URL de login apontava para
# https://app.mercos.com/login?next=//guia_inicial/, que nao existe.
EMPRESA_ID = os.environ.get("MERCOS_EMPRESA_ID") or "424525"
BASE_URL = f"https://app.mercos.com/{EMPRESA_ID}"
# ES 424525 · RJ 424524 · SP 424523 (congelada). O Gira e ES: 95% dos clientes.
NOME_REGIAO = os.environ.get("MERCOS_REGIAO", "ES")

# Quanto esperar antes de reentrar quando a sessao cai. O padrao do robo de
# credito e 900s (15 min) porque roda 19h30 e ninguem esta esperando. AQUI o
# padrao e 45s: o cliente acabou de confirmar e a promessa na tela e de 5 min.
# ⚠️ O risco de 45s e real e esta declarado no README -- reentrar rapido rouba a
# sessao de quem acabou de logar. E escolha, nao descuido.
ESPERA_VOLTA_S = int(os.environ.get("ORCAMENTO_ESPERA_VOLTA_S", "45"))
VOLTAS_POR_RODADA = int(os.environ.get("ORCAMENTO_VOLTAS", "2"))

COND_PAGAMENTO = os.environ.get("ORCAMENTO_COND_PAGAMENTO", "PIX")
TRANSPORTADORA = os.environ.get("ORCAMENTO_TRANSPORTADORA", "CIF")

LOG_DIR = os.path.join(BASE_DIR, "logs")

# 🔴 O texto do botao proibido. Existe como constante para a guarda poder
# procura-lo em qualquer coisa que o robo va clicar.
BOTAO_PROIBIDO = "Gerar pedido"

# ── Os dois campos da tela de orcamento ──────────────────────────────────────
# Id primeiro, placeholder como rede. O id e o seletor bom (vem do form do
# backend), mas em 19/set/2026 01:26 a tela desenhou o campo e o
# `#id_codigo_cliente` nao ficou visivel em 30s -- o print mostra o orcamento
# #9315 aberto, com o campo CLIENTE na tela. Com so o id, o robo morre olhando
# para a tela certa.
CAMPO_CLIENTE = "#id_codigo_cliente, input[placeholder*='CNPJ/CPF do cliente']"
CAMPO_PRODUTO = "#produto_autocomplete, input[placeholder*='nome do produto']"

# ── Marcas que NAO entram no orcamento do Mercos ─────────────────────────────
# A Apice e negociada em outro canal, e boa parte dos clientes dela nem tem
# cadastro no Mercos. Item de Apice no orcamento seria linha que ninguem vai
# faturar por aqui.
#
# ⚠️ A MARCA VEM DO CADASTRO, NAO DO CODIGO DO SKU. Medido em 18/set/2026: a
# Apice tem SKUs com prefixo 00, AP, ER e MC, e o prefixo ER tambem existe na
# Barbours. Filtrar por prefixo erraria nos dois sentidos -- deixaria passar 17
# itens Apice e cortaria 1 da Barbours.
MARCAS_IGNORADAS = {
    m.strip().upper()
    for m in os.environ.get("ORCAMENTO_MARCAS_IGNORADAS", "AP").split(",")
    if m.strip()
}

# ── Cliente de passagem ──────────────────────────────────────────────────────
# Quando o CNPJ nao existe no Mercos, o orcamento sai no cadastro deste cliente
# so para gerar o link de pagamento.
#
# 🔴 O ORCAMENTO FICA NO NOME DE OUTRA EMPRESA. Isso e deliberado e tem custo:
# quem abrir o Mercos vai ver um orcamento que nao e daquele cliente. Por isso
# cada uso e declarado na auditoria (`cliente_generico`), com o CNPJ original
# junto -- sem esse rastro, o orcamento vira um registro orfao que ninguem
# consegue explicar depois.
#
# 🔴 ESTE CLIENTE COMPRA DE VERDADE -- e a diferenca mais importante em relacao
# a escolha anterior (Vania, 46067790000123, zero pedido na base). Medido em
# 18/set/2026: a Bruna tem 5 pedidos e 53 itens faturados, o ultimo em
# 31/jul/2026, e tem vendedor atribuido. Consequencias, para quem for mexer:
#   - o orcamento de passagem aparece no historico de uma cliente ativa, e o
#     vendedor dela vai ver;
#   - se alguem clicar "Gerar pedido" nele por engano, o valor entra na receita
#     DELA e contamina curva ABC, slow moving e comissao.
# Trocar por um cadastro sem historico elimina os dois riscos de uma vez.
CNPJ_GENERICO = os.environ.get("ORCAMENTO_CNPJ_GENERICO", "56128147000116")
NOME_GENERICO = "56 128 147 Bruna Jessica Pilati Me"

# O que DIGITAR no autocomplete -- que nao e o nome inteiro. O campo casa por
# pedaco do nome, e o cadastro comeca com o CNPJ por extenso ("56 128 147
# Bruna..."), entao procurar pelo nome completo depende de acertar espaco por
# espaco. "BRUNA" e o que a tela responde (print de 19/set/2026).
TERMO_GENERICO = os.environ.get("ORCAMENTO_TERMO_GENERICO", "BRUNA")

# 🔴 NO CLIENTE DE PASSAGEM, O PRIMEIRO DA LISTA VALE. E a unica excecao a regra
# de ouro "nunca adivinhar", decidida pelo Italo em 19/set/2026 para destravar os
# testes: se "BRUNA" devolver mais de um cadastro, o robo pega o primeiro em vez
# de abortar.
#
# O que sustenta a excecao: o artefato e um ORCAMENTO, nao um pedido -- ninguem
# e cobrado, nada entra em receita, e "Gerar pedido" continua proibido pela
# `_guarda_gerar_pedido`. Escolher errado aqui custa um rascunho no cadastro
# errado, nao dinheiro.
#
# ⚠️ A excecao vale SO para o de passagem. Quando o operador pediu um CNPJ
# especifico e a busca volta ambigua, o robo continua abortando: ali escolher
# errado manda o orcamento (e o link de pagamento) para o cliente errado.
PRIMEIRO_VALE_NO_GENERICO = os.environ.get("ORCAMENTO_GENERICO_PRIMEIRO", "1") == "1"


class OrcamentoAbortado(RuntimeError):
    """Algo que exige decisao humana. Nao escreve, nao tenta de novo."""


class SessaoDerrubada(RuntimeError):
    """A conta foi usada em outro computador (Mercos = 1 sessao por usuario)."""


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def auditar(evento: str, **campos) -> None:
    """Uma linha JSON por evento. O workflow extrai isto para o resumo do
    Actions e o artifact guarda por 90 dias -- a tela do Actions expira."""
    print("AUDIT " + json.dumps({"evento": evento, "em": datetime.now(timezone.utc).isoformat(),
                                 **campos}, ensure_ascii=False), flush=True)


def shot(page: Page, nome: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    caminho = os.path.join(LOG_DIR, f"{int(time.time())}-{nome}.png")
    try:
        page.screenshot(path=caminho, full_page=False)
        log(f"   screenshot -> {os.path.basename(caminho)}")
    except Exception as e:
        log(f"   (screenshot falhou: {e})")
    return caminho


def _checa_sessao(page: Page) -> None:
    if mercos_ui.sessao_caiu(page):
        raise SessaoDerrubada(mercos_ui.motivo_sessao(page, MERCOS_EMAIL))


def marcas_dos_skus(skus: list[str]) -> dict[str, str]:
    """Descobre a marca de cada SKU no cadastro (dim_produto).

    Devolve {sku: marca_id}. SKU ausente do cadastro nao entra no mapa.

    ⚠️ Se a consulta falhar, devolve VAZIO -- e quem chama trata isso como
    "nao ignora nada". Ignorar por engano tiraria item legitimo do orcamento do
    cliente, que e pior do que deixar passar um item de Apice: o primeiro e um
    pedido menor sem ninguem entender por que, o segundo alguem ve e remove.
    """
    if not (SUPABASE_URL and SUPABASE_KEY and skus):
        return {}
    lista = ",".join(f'"{s}"' for s in skus)
    url = f"{SUPABASE_URL}/rest/v1/dim_produto?select=sku,marca_id&sku=in.({lista})"
    try:
        req = urllib.request.Request(
            url,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            linhas = json.loads(r.read().decode("utf-8"))
        return {l["sku"]: (l.get("marca_id") or "").strip().upper() for l in linhas}
    except Exception as e:  # noqa: BLE001
        log(f"   [aviso] nao consegui ler as marcas no cadastro ({e}) -- nada sera ignorado")
        return {}


def separar_ignorados(itens: list[tuple[str, int]]) -> tuple[list[tuple[str, int]], list[dict]]:
    """Tira do pedido os itens das marcas que nao entram no Mercos."""
    mapa = marcas_dos_skus([s for s, _ in itens])
    ficam, saem = [], []

    for sku, qtd in itens:
        marca = mapa.get(sku)
        if marca and marca in MARCAS_IGNORADAS:
            saem.append({"sku": sku, "quantidade": qtd, "marca": marca})
            auditar("item_ignorado", sku=sku, quantidade=qtd, marca=marca,
                    motivo="marca negociada fora do Mercos")
            log(f"   - {sku} ({marca}) fica de fora: marca negociada em outro canal")
        elif not marca and sku not in mapa:
            # SKU fora do cadastro: entra, e a tela do Mercos dira se existe la
            ficam.append((sku, qtd))
        else:
            ficam.append((sku, qtd))

    return ficam, saem


def trancar_gerar_pedido(page: Page) -> None:
    """🔴 A TRANCA. Bloqueia o clique em "Gerar pedido" no proprio navegador.

    Um ouvinte na fase de captura: qualquer clique que caia no botao proibido --
    do robo, de um Enter perdido, de um script da propria pagina -- e parado
    antes de virar requisicao.

    Vale mais que qualquer checagem do robo porque nao depende de o robo ter
    pensado em checar naquele ponto. Reinstalada a cada carregamento, porque
    navegacao apaga o que foi injetado.
    """
    try:
        page.evaluate(
            """(proibido) => {
                if (window.__giraTrancado) return;
                window.__giraTrancado = true;
                window.__giraBloqueios = 0;
                document.addEventListener('click', (ev) => {
                    const alvo = ev.target && ev.target.closest
                        ? ev.target.closest('a, button, input[type=submit]')
                        : null;
                    const texto = ((alvo && (alvo.innerText || alvo.value)) || '').trim();
                    if (texto.toLowerCase().includes(proibido.toLowerCase())) {
                        ev.preventDefault();
                        ev.stopImmediatePropagation();
                        window.__giraBloqueios++;
                    }
                }, true);
            }""",
            BOTAO_PROIBIDO,
        )
    except Exception as e:  # noqa: BLE001
        log(f"   [tranca] nao consegui instalar a tranca do '{BOTAO_PROIBIDO}' ({e})")


def _guarda_gerar_pedido(page: Page, onde: str) -> None:
    """🔴 A guarda mais importante do robo, junto com a tranca acima.

    ⚠️ FOCO NAO E INTENCAO, e confundir os dois custou a primeira rodada
    `--aplicar` que deu certo (19/set/2026 02:23): o orcamento #166379458 estava
    pronto -- itens, 29%, PIX, CIF -- e a guarda abortou porque, depois do
    reload, o navegador tinha focado o primeiro botao da tela, que e o proibido.
    Ninguem ia clicar nele. A rodada morreu com o trabalho feito e sem o link.

    Entao: foco no botao proibido vira BLUR (um Enter perdido ali geraria o
    pedido de verdade -- tirar o foco e a correcao, nao abortar), e a tranca do
    navegador segue impedindo o clique. Abortar fica para o que e intencao de
    verdade: o robo ir clicar num elemento que E o botao proibido.
    """
    trancar_gerar_pedido(page)
    try:
        focado = page.evaluate(
            """(proibido) => {
                const el = document.activeElement;
                const texto = ((el && (el.innerText || el.value)) || '').trim();
                if (texto.toLowerCase().includes(proibido.toLowerCase())) {
                    el.blur();          // tira o alvo do caminho de um Enter
                    return texto;
                }
                return '';
            }""",
            BOTAO_PROIBIDO,
        )
    except Exception:  # noqa: BLE001
        focado = ""
    if focado:
        log(f"   [guarda] '{BOTAO_PROIBIDO}' estava em foco em '{onde}' -- foco removido")
        auditar("gerar_pedido_desfocado", onde=onde)


def _clicar_apesar_do_overlay(page: Page, seletor: str, onde: str) -> None:
    """Clica num elemento que o overlay do modal esta cobrindo.

    O Mercos poe um `div.Overlay__overlay___*` por cima ao abrir o modal de
    links de pagamento, e o Playwright -- certo -- recusa clicar no que esta
    coberto: `<div class="Overlay__overlay____IoRb"> intercepts pointer events`,
    56 tentativas em 30s (19/set/2026 02:35). Esperar nao resolve: o overlay E o
    fundo do modal, ele fica.

    🔴 O CLIQUE POR JS NAO BURLA A TRANCA. `el.click()` dispara um evento de
    clique de verdade, que passa pelo ouvinte de captura instalado por
    `trancar_gerar_pedido` -- se o alvo for o botao proibido, continua sendo
    cancelado. O que ele pula e a checagem de "esta coberto?", nao a protecao.
    """
    _guarda_do_clique(page, seletor, onde)
    alvo = page.locator(seletor).last
    alvo.wait_for(state="attached", timeout=20_000)
    # `closest` porque o texto costuma estar num <span> dentro do <a>/<button>:
    # clicar no span funciona, mas clicar no elemento clicavel e mais fiel.
    alvo.evaluate("(el) => (el.closest('a, button, [role=button]') || el).click()")


def _guarda_do_clique(page: Page, seletor: str, onde: str) -> None:
    """Confere o que o robo esta prestes a clicar. AQUI sim, aborta.

    A diferenca para a guarda acima: isto nao e um botao por perto, e o alvo do
    clique. Um seletor generico que passe a casar com "Gerar pedido" depois de
    uma mudanca do Mercos transformaria orcamento em pedido faturavel -- e isso
    nao tem desfazer limpo.
    """
    _guarda_gerar_pedido(page, onde)
    try:
        texto = (page.locator(seletor).first.inner_text(timeout=3_000) or "").strip()
    except Exception:  # noqa: BLE001
        return
    if BOTAO_PROIBIDO.lower() in texto.lower():
        shot(page, "ABORT-clique-no-gerar-pedido")
        raise OrcamentoAbortado(
            f"O seletor '{seletor}' em '{onde}' aponta para '{texto}', que contem "
            f"'{BOTAO_PROIBIDO}'. O robo NUNCA gera pedido -- abortado antes do clique."
        )


# ═════════════════════════════════════════════════════════════════════════════
# Os passos, na ordem do mapeamento
# ═════════════════════════════════════════════════════════════════════════════

def _sessao_morreu(page: Page) -> bool:
    """`sessao_caiu` com rede de seguranca.

    Se a propria checagem estourar -- pagina em transicao, contexto fechando --
    respondemos "nao sei" como False: um falso negativo gasta mais uma tentativa,
    um falso positivo abortaria uma rodada saudavel.
    """
    try:
        return bool(mercos_ui.sessao_caiu(page))
    except Exception:  # noqa: BLE001
        return False


def garantir_empresa(page: Page) -> None:
    """Confere em QUAL empresa a sessao abriu, e troca se nao for a nossa.

    🔴 A DESCOBERTA QUE EXPLICA AS QUEDAS DE 19/set/2026. Do guia da automacao
    Mercos da Torre, secao 5:

        "O Mercos nao honra URL direta entre empresas -- navegar direto pra
         /424524/relatorios/... estando logado na sessao da ES redireciona pra
         /login. (...) A conta entra por padrao numa empresa que VARIA POR
         SESSAO (as vezes SP, as vezes ES) -- por isso _trocar_empresa e
         chamado SEMPRE."

    Ou seja: o robo logava, confirmava INDICADORES (painel de QUALQUER empresa
    serve), pedia /424525/pedidos/ e o Mercos devolvia a tela de login. O DOM
    tem `[name="senha"]`, entao `sessao_caiu()` respondia "sim" -- e o robo
    gastava dois relogins com 2FA atras de uma sessao que nunca esteve morta.

    Sao 3 empresas na mesma conta: ES 424525, RJ 424524, SP 424523 (congelada).
    Os pedidos do Gira sao ES -- 95% dos clientes, segundo o Italo.
    """
    if f"/{EMPRESA_ID}/" in page.url:
        return

    log(f"   a sessao abriu em outra empresa (url: {page.url}) -- trocando para {EMPRESA_ID}")
    auditar("empresa_diferente_na_sessao", url_apos_login=page.url, empresa_alvo=EMPRESA_ID)

    # O seletor de empresa fica no topo. O texto do trigger e o nome da conta
    # ("BEAUTY HUB ATACADO") e cada item traz a sigla entre parenteses.
    for tentativa in (1, 2, 3, 4):
        try:
            page.click('text=/BEAUTY HUB/i', timeout=8_000)
            page.wait_for_timeout(700)
            page.click(f'text=/[(]{NOME_REGIAO}[)]/', timeout=8_000)
            page.wait_for_url(f"**/{EMPRESA_ID}/**", timeout=20_000)
            log(f"   empresa trocada para {NOME_REGIAO} ({EMPRESA_ID})")
            auditar("empresa_trocada", empresa=EMPRESA_ID, regiao=NOME_REGIAO)
            return
        except Exception as e:  # noqa: BLE001
            log(f"   troca de empresa {tentativa}/4 nao pegou ({str(e).splitlines()[0][:60]})")
            page.wait_for_timeout(1_500)

    shot(page, "troca-de-empresa-falhou")
    raise OrcamentoAbortado(
        f"A sessao abriu numa empresa diferente de {EMPRESA_ID} ({NOME_REGIAO}) e o seletor "
        f"do topo nao trocou em 4 tentativas. URL atual: {page.url}. Sem isso, qualquer URL "
        f"de /{EMPRESA_ID}/ devolve a tela de login -- que PARECE sessao caida e nao e."
    )


def passo_1_abrir_pedidos(page: Page) -> None:
    """Abre a lista de pedidos.

    Dois modos de falha DIFERENTES chegam aqui parecidos, e confundi-los custou
    duas rodadas:

    ⚠️ `net::ERR_ABORTED` — o Mercos troca a navegacao no meio. Logo depois do
    login a pagina ainda esta assentando e o goto e cancelado por uma segunda
    navegacao. Some sozinho: a tentativa seguinte passa.

    ⚠️ Timeout esperando `#btn_criar_pedido` — pode ser pagina lenta OU sessao
    derrubada, porque com a sessao morta o Mercos serve a tela de login e o
    botao nunca existe.

    🔴 E AQUI ESTA A ARMADILHA: nao da para perguntar "e a tela de login?" logo
    apos o goto. `sessao_caiu()` procura o campo `[name="senha"]` no DOM, e
    enquanto a pagina nova nao pintou o DOM AINDA E O DA TELA ANTERIOR -- que,
    vindo do login, e exatamente a tela de login. A resposta vem "sim" com a
    sessao viva. Foi o que abortou as rodadas de 23:43 e 23:45, matando o retry
    que vinha salvando as anteriores.

    Por isso: espera o DOM de verdade (`domcontentloaded`), gasta as tres
    tentativas, e so DEPOIS -- com uma recarga limpa no meio, como o mercos_ui
    faz -- pergunta pela sessao.
    """
    log("1. abrindo a tela de pedidos")
    log(f"   empresa da sessao apos o login: {page.url}")
    garantir_empresa(page)
    alvo = f"{BASE_URL}/pedidos/"
    ultimo_erro = None

    for tentativa in (1, 2, 3):
        try:
            page.goto(alvo, wait_until="domcontentloaded", timeout=45_000)
            trancar_gerar_pedido(page)   # navegar apaga o ouvinte injetado
            page.wait_for_selector("#btn_criar_pedido", timeout=25_000)
            _checa_sessao(page)
            return
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
            log(f"   tentativa {tentativa}/3 nao abriu ({str(e).splitlines()[0][:70]})")
            log(f"      url agora: {page.url}")
            page.wait_for_timeout(3_000)

    # ── esgotou: agora sim, uma recarga limpa e o veredito ───────────────────
    try:
        page.goto(alvo, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:  # noqa: BLE001
        pass

    if _sessao_morreu(page):
        # 🔴 "PARECE LOGIN" NAO PROVA SESSAO MORTA. Em 19/set/2026 01:00 o robo
        # logou TRES vezes, confirmou `INDICADORES` nas tres, e nas tres o
        # /pedidos/ voltou login -- comportamento determinista demais para ser
        # alguem roubando a sessao no mesmo segundo, tres vezes seguidas.
        #
        # Entao antes do veredito ele VOLTA ao painel. Se `INDICADORES` ainda
        # estiver la, a sessao esta viva e o problema e esta URL -- e chamar
        # isso de "sessao caiu" manda consertar o lugar errado, alem de queimar
        # dois relogins (e dois 2FA) que nao tem como dar certo.
        painel_vivo = False
        try:
            page.goto(f"{BASE_URL}/guia_inicial/", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_selector("text=INDICADORES", timeout=15_000)
            painel_vivo = True
        except Exception:  # noqa: BLE001
            painel_vivo = False

        if painel_vivo:
            log("   painel responde -- a sessao esta VIVA; o problema e a URL /pedidos/")
            shot(page, "painel-vivo-pedidos-negado")
            # Caminho de gente: clicar PEDIDOS na barra lateral em vez de
            # digitar a URL. Se o Mercos trocou a rota (SPA, novo id de
            # empresa, permissao por modulo), o link da tela sabe o endereco
            # certo e o goto nao sabe.
            try:
                page.click("text=PEDIDOS", timeout=10_000)
                page.wait_for_selector("#btn_criar_pedido", timeout=25_000)
                log(f"   entrou pelo menu lateral (url: {page.url})")
                _checa_sessao(page)
                return
            except Exception as e:  # noqa: BLE001
                shot(page, "menu-pedidos-nao-abriu")
                auditar("pedidos_negado_com_sessao_viva", url_tentada=alvo,
                        url_atual=page.url, erro=str(e).splitlines()[0][:120])
                raise OrcamentoAbortado(
                    f"A sessao esta VIVA (o painel responde) mas {alvo} devolve a tela de "
                    f"login, e o menu lateral tambem nao abriu a lista de pedidos. "
                    f"Isso nao e queda de sessao: e permissao do usuario neste modulo, "
                    f"empresa errada na URL, ou a rota mudou. Ultimo erro: {e}"
                ) from e

        shot(page, "sessao-caiu-na-tela-de-pedidos")
        raise SessaoDerrubada(
            "a sessao do Mercos caiu: depois de tres tentativas e uma recarga, a "
            "pagina servida em /pedidos/ ainda e a de login (e o painel tambem nao responde)"
        )

    shot(page, "pedidos-sem-botao")
    raise RuntimeError(
        f"A tela de pedidos abriu mas `#btn_criar_pedido` nao apareceu em 3 tentativas, "
        f"e a sessao esta viva -- pode ser lentidao do Mercos ou a tela mudou. "
        f"Ultimo erro: {ultimo_erro}"
    )


def passo_2_criar_orcamento(page: Page) -> None:
    log("2. clicando em 'Criar pedido / orcamento'")
    _guarda_do_clique(page, "#btn_criar_pedido", "tela de pedidos")
    page.click("#btn_criar_pedido")
    # A tela nova e a de selecao de cliente.
    page.wait_for_selector(CAMPO_CLIENTE, timeout=60_000)
    _checa_sessao(page)


def nome_do_cnpj(cnpj: str) -> str | None:
    """Acha o nome do cliente no cadastro, a partir do CNPJ.

    🔴 POR QUE ISTO E NECESSARIO. O campo do pedido e `#id_codigo_cliente`: ele
    autocompleta por CODIGO ou NOME, e NAO por CNPJ. Quem digita 46067790000123
    ali nao recebe nada -- enquanto a mesma busca na tela de Clientes acha na
    hora. Custou a rodada de 18/09 23:22, que abortou com "nenhum cliente" tendo
    o cliente cadastrado e ativo.
    """
    digitos = re.sub(r"\D", "", cnpj or "")
    if len(digitos) != 14 or not (SUPABASE_URL and SUPABASE_KEY):
        return None
    url = f"{SUPABASE_URL}/rest/v1/dim_cliente?select=nome_cliente&cnpj=eq.{digitos}"
    try:
        req = urllib.request.Request(
            url,
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            linhas = json.loads(r.read().decode("utf-8"))
        return (linhas[0].get("nome_cliente") or "").strip() or None if linhas else None
    except Exception as e:  # noqa: BLE001
        log(f"   [aviso] nao consegui buscar o nome do CNPJ no cadastro ({e})")
        return None


def termos_de_busca(busca: str) -> list[str]:
    """Os textos a tentar no autocomplete, do mais exato ao mais frouxo.

    🔴 O CAMPO E `#id_codigo_cliente`: ele autocompleta por CODIGO ou NOME, e
    NAO por CNPJ. E o nome que ele mostra e o NOME FANTASIA -- a Vania aparece
    como "Vania Cosmeticos Perfumes", enquanto o nosso cadastro guarda a razao
    social, "46 067 790 ANTONIA VANIA ANDRADE ITO ME". Por isso tentar o CNPJ e
    depois a razao social inteira nao basta: nenhum dos dois e o que esta
    escrito na tela.

    A ultima tentativa e o prefixo significativo do nome ("CASA BELLA"), que e
    como uma pessoa procuraria. Se isso devolver mais de um cliente, o robo
    aborta e lista os candidatos -- frouxo na BUSCA, nunca na ESCOLHA.
    """
    termos = [busca]
    digitos = re.sub(r"\D", "", busca or "")

    if len(digitos) == 14:
        nome = nome_do_cnpj(busca)
        if nome:
            termos.append(nome)
            # sem o sufixo juridico e sem o CNPJ que o MEI carrega no nome
            limpo = re.sub(r"\b(LTDA|ME|EPP|EIRELI|MEI|S/?A|CIA)\b", " ", nome, flags=re.I)
            limpo = re.sub(r"[\d./-]+", " ", limpo)
            palavras = [p for p in limpo.split() if len(p) > 2]
            if len(palavras) >= 2:
                termos.append(" ".join(palavras[:2]))

    vistos, unicos = set(), []
    for t in termos:
        t = (t or "").strip()
        if t and t.lower() not in vistos:
            vistos.add(t.lower())
            unicos.append(t)
    return unicos


# Os seletores ja vistos para a lista de sugestoes do cliente. O primeiro que
# aparecer vence.
#
# ⚠️ `.ui-autocomplete li` sozinho NAO SERVE nesta tela -- o screenshot de
# 18/09 23:57 mostra a sugestao desenhada e o robo reportando "nenhum cliente".
# O Mercos nao usa a mesma lista em todas as telas, e o codigo tem que tolerar
# isso em vez de ser reescrito a cada versao.
SELETORES_SUGESTAO = [
    # 🔴 ESTE E O DO MERCOS. Bootstrap typeahead, descoberto pelo [mapa] da
    # rodada de 19/set/2026 00:11 -- que e exatamente para isso que o mapa
    # existe. Vem primeiro porque `.dropdown-menu li` sozinho tambem casaria com
    # o menu de navegacao (INDICADORES / FUNIS DE VENDAS / PEDIDOS), que esta
    # visivel o tempo todo e faria o robo "achar" cliente nenhum.
    "ul.typeahead.dropdown-menu li",
    ".typeahead li",
    ".ui-autocomplete li",
    ".ui-menu-item",
    "[role='listbox'] [role='option']",
    ".autocomplete-suggestion",
    ".autocomplete-items div",
    ".tt-suggestion",
    ".resultado-busca-cliente li",
    "ul.resultado li",
]


def _sugestoes_visiveis(page: Page):
    """Devolve (locator, quantidade) da lista que apareceu -- ou (None, 0).

    🔴 `>> visible=true` FILTRA, e nao e detalhe. A versao anterior pegava o
    seletor inteiro e perguntava se o PRIMEIRO estava visivel -- e depois de
    escolher o cliente, o <ul> do cliente CONTINUA no DOM, oculto, e vem antes
    do <ul> do produto. O primeiro era invisivel, o seletor era descartado
    inteiro, e o robo abortava com "produto nao apareceu" tendo o produto
    desenhado na tela (rodada 35408896283, 19/set/2026 00:20).
    """
    for seletor in SELETORES_SUGESTAO:
        try:
            loc = page.locator(f"{seletor} >> visible=true")
            n = loc.count()
            if n:
                return loc, n
        except Exception:  # noqa: BLE001
            continue
    return None, 0


def _mapear_lista(page: Page, campo_sel: str = CAMPO_CLIENTE) -> None:
    """Quando nenhum seletor casa, escreve no log o que ESTA na tela.

    Sem isto, "nenhum cliente" e indistinguivel de "a lista existe e eu nao sei
    olhar" -- e foi o que custou duas rodadas. O log passa a trazer as classes
    dos elementos vivos logo abaixo do campo, que e o suficiente para descobrir
    o seletor certo sem abrir o navegador.
    """
    try:
        achados = page.evaluate("""(campoSel) => {
            const saida = [];
            document.querySelectorAll('ul, [role="listbox"], div[class*="autocomplete"], div[class*="sugest"]')
                .forEach((el) => {
                    const r = el.getBoundingClientRect();
                    const texto = (el.innerText || '').trim();
                    if (r.height > 0 && texto) {
                        saida.push({ tag: el.tagName, classe: el.className, texto: texto.slice(0, 60) });
                    }
                });
            return saida.slice(0, 6);
        }""", campo_sel)
        if achados:
            log("   [mapa] listas visiveis na tela agora:")
            for a in achados:
                log(f"      {a['tag']}.{a['classe']} -> {a['texto']!r}")
        else:
            log("   [mapa] nenhuma lista visivel -- o autocomplete nao abriu")
    except Exception as e:  # noqa: BLE001
        log(f"   [mapa] nao consegui inspecionar a tela ({e})")


# O typeahead do Mercos oferece uma ACAO junto com os resultados: quando nada
# casa exatamente, a ultima linha e 'Cadastrar "BB02027" como novo produto'.
# Ela nao e resultado, e contava como um: o passo 4 abortou por "2 produtos"
# tendo achado exatamente um (rodada local de 19/set/2026 01:22).
#
# 🔴 CLICAR NELA SERIA MUITO PIOR QUE ABORTAR: abriria o cadastro de um produto
# novo no Mercos a partir de um SKU digitado errado. Por isso ela e removida da
# lista, nunca escolhida -- nem quando e a unica coisa na tela.
PADRAO_ACAO_CADASTRO = re.compile(
    r"^cadastrar\b.*\b(nov[oa])\b", re.IGNORECASE | re.DOTALL
)


def _e_acao_de_cadastro(texto: str) -> bool:
    return bool(PADRAO_ACAO_CADASTRO.match((texto or "").strip()))


def _tentar_autocomplete(
    page: Page, texto: str, campo_sel: str = CAMPO_CLIENTE
) -> tuple[list[str], object | None]:
    """Digita e devolve ([(indice, nome)], locator da lista).

    ⚠️ SERVE CLIENTE E PRODUTO. Os dois campos usam o mesmo typeahead, e o
    passo 4 tinha uma copia propria olhando so `.ui-autocomplete li` -- a mesma
    lista errada que travou o passo 3 por tres rodadas. Uma funcao so: quando o
    Mercos mudar de componente, muda em um lugar.
    """
    campo = page.locator(campo_sel).first
    campo.click()
    campo.fill("")
    campo.type(texto, delay=60)

    # o autocomplete e assincrono: espera a lista aparecer, sem prender num
    # seletor unico
    for _ in range(20):
        loc, n = _sugestoes_visiveis(page)
        if n:
            # O typeahead do Mercos abre cada linha com um icone de fonte
            # (caractere de uso privado, \ue85f). Ele nao e nome de cliente e
            # sujaria a auditoria e a mensagem de erro.
            achados = []
            for i in range(min(n, 10)):
                nome = re.sub("[\ue000-\uf8ff]", "", loc.nth(i).inner_text()).strip()
                if not nome:
                    continue
                if _e_acao_de_cadastro(nome):
                    continue
                achados.append((i, nome))
            if achados:
                return achados, loc
        page.wait_for_timeout(500)

    _mapear_lista(page)
    return [], None


def passo_3_escolher_cliente(
    page: Page,
    busca: str,
    permitir_generico: bool = True,
    aceita_primeiro: bool = False,
) -> str:
    """Escolhe o cliente no autocomplete do pedido.

    ⚠️ Regra de ouro herdada do `mercos_ui`: NUNCA ADIVINHAR. Buscar de varios
    jeitos e legitimo -- escolher entre varios resultados nao e.

    `aceita_primeiro` e a unica excecao, e existe so para o cliente de passagem
    (veja PRIMEIRO_VALE_NO_GENERICO). Nunca passe True para o cliente que o
    operador pediu.
    """
    log(f"3. procurando cliente: {busca}")

    for termo in termos_de_busca(busca):
        if termo != busca:
            log(f"   tentando por: {termo}")
        achados, lista = _tentar_autocomplete(page, termo)
        nomes = [nome for _, nome in achados]

        if len(achados) > 1 and not aceita_primeiro:
            shot(page, "cliente-ambiguo")
            raise OrcamentoAbortado(
                f"'{termo}' devolveu {len(achados)} clientes: {nomes}. "
                "O robo nao escolhe cliente no palpite -- refine a busca."
            )

        if len(achados) > 1:
            log(f"   {len(achados)} resultados; pegando o primeiro (cliente de passagem): {nomes}")
            auditar("generico_primeiro_da_lista", termo=termo,
                    escolhido=nomes[0], descartados=nomes[1:])

        if achados:
            # nth(indice), nao first(): o indice e o da lista NA TELA, e linhas
            # descartadas (a acao "Cadastrar ... como novo") ficaram no meio.
            lista.nth(achados[0][0]).click()
            # O typeahead poe telefone, e-mail e cidade em linhas extras da
            # mesma <li>. So a primeira linha e o nome.
            nomes[0] = nomes[0].splitlines()[0].strip()
            log(f"   cliente: {nomes[0]}")
            if termo != busca:
                auditar("cliente_achado_por", busca_original=busca, termo=termo, cliente=nomes[0])
            page.wait_for_selector(CAMPO_PRODUTO, timeout=60_000)
            _checa_sessao(page)
            return nomes[0]

    # ── nada, por nenhum dos caminhos ────────────────────────────────────────
    # 🔴 O de passagem e tentado UMA vez, e sem voltar para ca: a versao anterior
    # recursava e refazia a cadeia inteira -- CNPJ, nome, generico, CNPJ, nome --
    # gastando 50 segundos para terminar no mesmo lugar, e com a mensagem de erro
    # falando do generico em vez do cliente que o operador pediu.
    if permitir_generico and re.sub(r"\D", "", busca or "") != CNPJ_GENERICO:
        log(f"   '{busca}' nao existe no Mercos -- usando o cliente de passagem")
        auditar("cliente_generico", cnpj_original=busca,
                cnpj_usado=CNPJ_GENERICO, nome=NOME_GENERICO,
                motivo="cliente sem cadastro no Mercos")
        return passo_3_escolher_cliente(
            page, TERMO_GENERICO,
            permitir_generico=False,
            aceita_primeiro=PRIMEIRO_VALE_NO_GENERICO,
        )

    shot(page, "cliente-sem-resultado")
    raise OrcamentoAbortado(
        f"O Mercos nao devolveu nenhum cliente para '{busca}' "
        f"(tentei: {', '.join(termos_de_busca(busca))})."
    )


def _estoque_da_sugestao(texto: str) -> int | None:
    """Tira o estoque da linha do typeahead: 'Estq.: 343.409'.

    E a UNICA tela onde o numero aparece neste fluxo. Devolve None quando nao
    achar -- None e "nao sei", nunca zero: tratar como zero recusaria item
    disponivel e mataria o pedido por engano.
    """
    m = re.search(r"Est(?:oque|q\.):\s*([\d.,]+)", texto or "", re.IGNORECASE)
    if not m:
        return None
    bruto = m.group(1).replace(".", "").replace(",", ".")
    try:
        return int(float(bruto))
    except ValueError:
        return None


def _mapear_modal(page: Page, porque: str) -> None:
    """Escreve no log o que o modal de produto realmente mostra.

    Mesma ideia do `_mapear_lista`: "nao consegui ler o estoque" sozinho nao
    diz se o rotulo mudou, se o numero esta em outro elemento ou se o modal nem
    abriu -- e a guarda de estoque fica morta em silencio, que e o pior estado
    possivel para uma guarda.
    """
    try:
        texto = page.evaluate("""() => {
            // A area que interessa e a do campo de quantidade -- e ali que o
            // Mercos desenha o produto recem-escolhido, com preco e estoque.
            const q = document.querySelector('#id_quantidade');
            let alvo = q;
            for (let i = 0; i < 5 && alvo && alvo.parentElement; i++) alvo = alvo.parentElement;
            if (!alvo) {
                alvo = document.querySelector('.modal.in, [role="dialog"], .modal-content')
                    || document.body;
            }
            return (alvo.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 400);
        }""")
        log(f"   [modal] {porque}")
        log(f"   [modal] texto: {texto!r}")
    except Exception as e:  # noqa: BLE001
        log(f"   [modal] nao consegui inspecionar ({e})")


def _estoque_do_modal(page: Page) -> int | None:
    """Le o 'Estoque: 4.785' do modal de adicionar produto.

    Devolve None quando nao consegue ler -- e None NAO e zero: significa "nao
    sei", e quem chama decide. Tratar como zero recusaria item disponivel."""
    # 🔴 O NUMERO NAO ESTA NO ELEMENTO DO ROTULO. Em 19/set/2026 01:36 o
    # `inner_text()` do elemento que casa com "Estq.:" devolveu exatamente
    # 'Estq.: ' -- o valor mora num irmao. Lendo so o proprio elemento, a guarda
    # de estoque respondia "nao sei" SEMPRE, em toda rodada, sem ninguem notar:
    # uma guarda morta em silencio, que e pior que guarda nenhuma.
    #
    # Por isso sobe pelos ancestrais ate achar um cujo texto tenha o numero.
    try:
        alvo = page.locator(r"text=/Est(oque|q\.):/i").first
        alvo.wait_for(timeout=5_000)
        txt = alvo.evaluate("""(el) => {
            const tem = (t) => /Est(oque|q\\.):\\s*[\\d.,]+/i.test(t || '');
            let no = el;
            for (let i = 0; i < 4 && no; i++) {
                if (tem(no.innerText)) return no.innerText;
                no = no.parentElement;
            }
            return el.innerText || '';
        }""")
    except Exception:
        _mapear_modal(page, "nao achei nenhum texto com 'Estoque:'/'Estq.:'")
        return None
    m = re.search(r"Est(?:oque|q\.):\s*([\d.,]+)", txt)
    if not m:
        _mapear_modal(page, f"achei o rotulo mas nao o numero, em {txt[:120]!r}")
        return None
    bruto = m.group(1).replace(".", "").replace(",", ".")
    try:
        return int(float(bruto))
    except ValueError:
        return None


def passo_4_a_9_adicionar_itens(page: Page, itens: list[tuple[str, int]], aplicar: bool) -> list[dict]:
    """Adiciona cada SKU com a quantidade pedida.

    Passo 6 do mapeamento: a tabela de preco fica no PADRAO (preco de tabela
    cheio). O desconto e do PEDIDO INTEIRO e entra depois, no passo 10/11 --
    e isso importa: desconto por item nao seria pego pelo modal de desconto
    do pedido (o Mercos avisa que 'nao sera aplicado em itens que tiveram o
    preco liquido alterado manualmente').
    """
    resultado = []
    for sku, qtd in itens:
        log(f"4. adicionando {sku} x{qtd}")
        # ⚠️ MESMO typeahead do cliente -- por isso a mesma funcao. A versao
        # anterior tinha copia propria olhando `.ui-autocomplete li`, que nao e
        # a lista desta tela: o passo 4 abortava com "nao apareceu" tendo o
        # produto desenhado na tela, repetindo o bug que travou o passo 3.
        achados, lista = _tentar_autocomplete(page, sku, campo_sel=CAMPO_PRODUTO)
        nomes = [nome for _, nome in achados]

        if not achados:
            shot(page, f"produto-nao-achado-{sku}")
            raise OrcamentoAbortado(f"Produto {sku} nao apareceu no autocomplete do Mercos.")

        # 🔴 Aqui NAO vale o primeiro da lista. A tolerancia do cliente de
        # passagem existe porque errar la custa um rascunho no cadastro errado;
        # errar o PRODUTO poe outro item no orcamento do cliente, com outro
        # preco, e o cliente paga por isso.
        if len(achados) > 1:
            shot(page, f"produto-ambiguo-{sku}")
            raise OrcamentoAbortado(f"O codigo {sku} devolveu {len(achados)} produtos: {nomes[:8]}.")

        log(f"   produto: {nomes[0].splitlines()[0]}")

        # 🔴 O ESTOQUE SO EXISTE AQUI, NA SUGESTAO -- e some no clique. Mapeado
        # em 19/set/2026 01:38: depois de escolher o produto, a area da
        # quantidade mostra 'Quantidade UN Peso bruto ... Volume ...' e nenhum
        # estoque. Quem le a tela depois do clique le NADA, e a guarda de
        # estoque vira enfeite -- foi o que aconteceu em todas as rodadas ate
        # agora, sempre com "nao consegui ler o estoque".
        estoque = _estoque_da_sugestao(nomes[0])

        lista.nth(achados[0][0]).click()

        # 5. O modal abre com quantidade e tabela de preco.
        page.wait_for_selector("#id_quantidade", timeout=20_000)

        # 9. Conferir estoque ANTES de adicionar. A tela e a segunda fonte: se
        # o Mercos voltar a mostrar o estoque aqui, ela vale mais que a
        # sugestao, porque e posterior.
        if estoque is None:
            estoque = _estoque_do_modal(page)
        if estoque is not None and estoque < qtd:
            shot(page, f"estoque-insuficiente-{sku}")
            raise OrcamentoAbortado(
                f"{sku}: estoque {estoque} < quantidade pedida {qtd}. "
                "O robo NAO reduz a quantidade sozinho -- pedido com quantidade "
                "diferente da confirmada pelo cliente e outro pedido."
            )
        if estoque is None:
            log(f"   ⚠️ nao consegui ler o estoque de {sku} -- seguindo, e declarado na auditoria")

        qcampo = page.locator("#id_quantidade")
        qcampo.click()
        qcampo.fill("")
        qcampo.type(str(qtd), delay=40)

        # 6. Tabela de preco fica no padrao. Nao tocar.
        if not aplicar:
            log(f"   [dry] fecharia o modal sem adicionar {sku}")
            page.keyboard.press("Escape")
            resultado.append({"sku": sku, "qtd": qtd, "estoque": estoque, "adicionado": False})
            continue

        # 7. Adicionar.
        _guarda_do_clique(page, 'a[type="submit"].botao.medio.primario', f"modal do produto {sku}")
        # 🔴 `hidden`, NAO `detached`. O Mercos nao remove o formulario do
        # produto: ele ESCONDE e reaproveita para o item seguinte. Esperar
        # "sumir do DOM" esperava uma coisa que nunca acontece, e o
        # `--aplicar` morreu aqui na primeira vez que rodou de verdade
        # (19/set/2026 02:21) -- com o item ja adicionado na tela, o que e o
        # pior tipo de timeout: o robo falha DEPOIS de escrever.
        #
        # O log do Playwright entregou o diagnostico: "7 x resolved to visible
        # ... 37 x resolved to hidden".
        page.click('a[type="submit"].botao.medio.primario')
        page.wait_for_selector("#id_quantidade", state="hidden", timeout=20_000)
        _checa_sessao(page)
        log(f"   {sku} x{qtd} adicionado (estoque lido: {estoque})")
        auditar("item_adicionado", sku=sku, qtd=qtd, estoque=estoque)
        resultado.append({"sku": sku, "qtd": qtd, "estoque": estoque, "adicionado": True})
    return resultado


def passo_10_a_12_desconto(page: Page, desconto_pct: float, aplicar: bool) -> None:
    """Desconto do PEDIDO INTEIRO, na coluna 'Desc. Acres.'.

    ⚠️ O proprio Mercos avisa no modal: 'Os descontos e acrescimos nao serao
    aplicados em itens que tiveram o preco liquido alterado manualmente'. Por
    isso o passo 6 deixa a tabela no padrao -- mexer no preco do item aqui
    faria o desconto do pedido pular aquele item, em silencio.
    """
    if desconto_pct <= 0:
        log("10. sem desconto a aplicar")
        return
    log(f"10. abrindo 'Desc. Acres.' para {desconto_pct}%")

    # 🔴 EM `dry` O LINK NAO EXISTE, E ISSO NAO E FALHA. O passo 4 fecha o modal
    # sem adicionar nada, entao o orcamento fica sem item -- e sem item o Mercos
    # nao desenha a secao de desconto. O robo ficou 30s esperando
    # `#link_descontos` e abortou uma rodada que tinha ido bem ate ali
    # (19/set/2026 01:29; o print mostra o #9317 com PRODUTOS vazio).
    #
    # O `dry` prova o caminho ate o desconto. Provar o desconto EM SI exige
    # item na tela, e isso e `--aplicar`.
    if not aplicar:
        existe = page.locator("#link_descontos").count() > 0
        if not existe:
            log(f"   [dry] pararia aqui: {desconto_pct}% seria digitado no "
                "'Desc. Acres.', que so aparece com item no pedido -- e o dry "
                "nao adiciona item")
            auditar("dry_desconto_nao_verificavel", pct=desconto_pct,
                    motivo="sem itens no orcamento, o Mercos nao desenha a secao de desconto")
            return
        page.click("#link_descontos")
        page.wait_for_selector("#id_form-0-desconto", timeout=20_000)
        log(f"   [dry] digitaria {desconto_pct}% e salvaria")
        page.keyboard.press("Escape")
        return

    page.click("#link_descontos")
    page.wait_for_selector("#id_form-0-desconto", timeout=20_000)

    campo = page.locator("#id_form-0-desconto")
    campo.click()
    campo.fill("")
    campo.type(str(desconto_pct).replace(".", ","), delay=40)

    _guarda_gerar_pedido(page, "modal de desconto")
    page.click("#botao_salvar_descontos")
    # mesma razao do item: o modal e escondido, nao removido
    page.wait_for_selector("#id_form-0-desconto", state="hidden", timeout=20_000)
    _checa_sessao(page)
    log(f"   desconto de {desconto_pct}% aplicado ao pedido")
    auditar("desconto_aplicado", pct=desconto_pct)


def _select2(page: Page, container_id: str, texto: str, rotulo: str) -> None:
    """Abre um select2 do Mercos, digita e escolhe a primeira opcao que casa.

    ⚠️ Nunca escolhe cega: se nada casar com `texto`, aborta. Select2 sem match
    deixa a selecao anterior e o robo seguiria achando que trocou."""
    page.click(f"#{container_id}")
    page.wait_for_selector("input.select2-search__field", timeout=10_000)
    busca = page.locator("input.select2-search__field")
    busca.type(texto, delay=60)
    page.wait_for_timeout(800)
    opcoes = page.locator("li.select2-results__option")
    n = opcoes.count()
    if n == 0:
        shot(page, f"select2-vazio-{rotulo}")
        raise OrcamentoAbortado(f"{rotulo}: nenhuma opcao casou com '{texto}'.")
    escolhido = opcoes.first.inner_text().strip()
    opcoes.first.click()
    log(f"   {rotulo}: {escolhido}")


def passo_13_a_17_detalhes(page: Page, aplicar: bool) -> None:
    """Condicao de pagamento (PIX) e transportadora (CIF).

    Passo 14 do mapeamento: o VENDEDOR fica no padrao -- e o dono da conta da
    automacao. O robo nao toca nesse campo de proposito.
    """
    log("13. abrindo 'Alterar detalhes do pedido'")
    page.click("#alterar_informacoes")
    page.wait_for_selector("#select2-id_cond_pagamento-container", timeout=20_000)

    if not aplicar:
        log(f"   [dry] escolheria cond. pagamento '{COND_PAGAMENTO}' e transportadora '{TRANSPORTADORA}'")
        page.keyboard.press("Escape")
        return

    _select2(page, "select2-id_cond_pagamento-container", COND_PAGAMENTO, "cond. pagamento")
    _select2(page, "select2-id_transportadora-container", TRANSPORTADORA, "transportadora")

    _guarda_gerar_pedido(page, "modal de detalhes")
    page.click("#botao-submit")
    # mesma razao: escondido, nao removido
    page.wait_for_selector("#select2-id_cond_pagamento-container", state="hidden", timeout=20_000)
    _checa_sessao(page)
    auditar("detalhes_salvos", cond_pagamento=COND_PAGAMENTO, transportadora=TRANSPORTADORA)


def passo_18_recarregar(page: Page) -> str:
    """F5 na mesma pagina. Devolve o numero do orcamento lido da tela."""
    log("18. recarregando a pagina do orcamento")
    page.reload(wait_until="domcontentloaded", timeout=45_000)
    trancar_gerar_pedido(page)   # o reload apaga o ouvinte, e esta e a tela
    _checa_sessao(page)          # onde o botao proibido fica visivel
    page.wait_for_selector("text=/Mais op/i", timeout=30_000)
    numero = ""
    try:
        txt = page.locator("text=/N.? do pedido/i").first.inner_text(timeout=5_000)
        m = re.search(r"(\d{3,})", txt)
        if m:
            numero = m.group(1)
    except Exception:
        pass
    if not numero:
        m = re.search(r"/pedidos/(\d+)", page.url)
        numero = m.group(1) if m else "(desconhecido)"
    log(f"   orcamento: #{numero}")
    return numero


def passo_20_a_23_link_pagamento(page: Page, aplicar: bool) -> dict:
    """Mais opcoes -> Links de pagamento -> Pix -> Criar link -> copiar.

    🔴 PASSO 19 DO MAPEAMENTO: NAO CLICAR EM 'GERAR PEDIDO'. A guarda roda antes
    de cada clique desta secao, que e onde o botao proibido esta mais perto.
    """
    log("20. abrindo 'Mais opcoes'")
    _guarda_do_clique(page, "a.dropdown-toggle.btn_mais", "detalhes do pedido")
    page.click("a.dropdown-toggle.btn_mais")
    page.wait_for_selector("a.links-de-pagamento-do-pedido", timeout=15_000)

    log("21. abrindo 'Links de pagamento'")
    page.click("a.links-de-pagamento-do-pedido")
    page.wait_for_selector("text=/Criar link de pagamento/i", timeout=20_000)

    if not aplicar:
        log("   [dry] abriria o modal e criaria o link Pix")
        shot(page, "dry-links-de-pagamento")
        return {"criado": False}

    # O modal de pagamentos ja lista links existentes; o botao abre o de criacao.
    _clicar_apesar_do_overlay(page, "text=/Criar link de pagamento/i",
                              "modal de links de pagamento")
    page.wait_for_selector("text=/Forma de pagamento/i", timeout=20_000)

    # 22. Pix ja vem selecionado no mapeamento -- garantir mesmo assim.
    try:
        pix = page.locator('input[type="radio"]').filter(has_text="Pix")
        if pix.count() == 0:
            page.click("text=Pix")
    except Exception:
        pass
    shot(page, "antes-de-criar-link")

    _clicar_apesar_do_overlay(page, 'button:has-text("Criar link")',
                              "modal de criacao do link")
    page.wait_for_selector('[data-testid="copiar-link"]', timeout=45_000)
    log("22. link de pagamento criado")

    # 23. Pegar a URL. O icone de copiar poe no clipboard -- ler o href do
    # icone de link e mais confiavel que depender do clipboard num runner.
    url = ""
    try:
        url = page.locator('[data-testid="copiar-link"]').first.get_attribute("data-clipboard-text") or ""
    except Exception:
        pass
    if not url:
        try:
            url = page.locator("table a[href^='http']").first.get_attribute("href") or ""
        except Exception:
            pass
    shot(page, "links-de-pagamento")
    if not url:
        raise OrcamentoAbortado(
            "O link foi criado mas nao consegui LER a URL da tela. "
            "O orcamento existe e o link existe -- precisa de olho humano para copiar."
        )
    log(f"   link: {url}")
    auditar("link_criado", url=url)
    return {"criado": True, "url": url}


def passo_24_qr_code(contexto, url: str) -> str | None:
    """Abre o link publico numa aba nova e fotografa o QR Code.

    A pagina de pagamento e PUBLICA (o cliente abre sem login), entao nao
    precisa da sessao do Mercos -- e por isso nao arrisca a sessao do robo."""
    if not url:
        return None
    log("24. abrindo o link publico para capturar o QR Code")
    aba = contexto.new_page()
    try:
        aba.goto(url, wait_until="domcontentloaded", timeout=45_000)
        aba.wait_for_selector("img, canvas, svg", timeout=30_000)
        aba.wait_for_timeout(1500)
        os.makedirs(LOG_DIR, exist_ok=True)
        caminho = os.path.join(LOG_DIR, f"qrcode-{int(time.time())}.png")
        alvo = None
        for sel in ["img[alt*='QR' i]", "canvas", "img[src^='data:image']", "svg"]:
            loc = aba.locator(sel)
            if loc.count() > 0:
                alvo = loc.first
                break
        if alvo is not None:
            alvo.screenshot(path=caminho)
        else:
            aba.screenshot(path=caminho, full_page=True)
            log("   ⚠️ nao isolei o QR -- salvei a pagina inteira")
        log(f"   QR salvo em {os.path.basename(caminho)}")
        auditar("qrcode_capturado", arquivo=os.path.basename(caminho))
        return caminho
    except Exception as e:
        log(f"   ⚠️ nao consegui capturar o QR: {e}")
        return None
    finally:
        try:
            aba.close()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════

def rodar_um(page, contexto, cnpj: str, itens: list[tuple[str, int]],
             desconto: float, aplicar: bool) -> dict:
    passo_1_abrir_pedidos(page)
    passo_2_criar_orcamento(page)
    cliente = passo_3_escolher_cliente(page, cnpj)

    itens, ignorados = separar_ignorados(itens)
    if not itens:
        raise OrcamentoAbortado(
            "Todos os itens sao de marca que nao entra no Mercos "
            f"({', '.join(sorted(MARCAS_IGNORADAS))}) -- nao ha orcamento a criar."
        )

    add = passo_4_a_9_adicionar_itens(page, itens, aplicar)
    passo_10_a_12_desconto(page, desconto, aplicar)
    passo_13_a_17_detalhes(page, aplicar)
    if not aplicar:
        log("[dry] pararia aqui -- nada foi escrito no Mercos")
        return {"cliente": cliente, "itens": add, "ignorados": ignorados, "aplicado": False}
    numero = passo_18_recarregar(page)
    link = passo_20_a_23_link_pagamento(page, aplicar)
    qr = passo_24_qr_code(contexto, link.get("url", ""))
    return {"cliente": cliente, "orcamento": numero, "itens": add,
            "ignorados": ignorados,
            "link": link.get("url"), "qrcode": qr, "aplicado": True}


def rodar_fila(page, contexto, limite: int, aplicar: bool) -> int:
    """Processa `gira_orcamento_fila`: o que o lojista aprovou no PIX.

    🔴 UMA LINHA QUE FALHA NAO DERRUBA A RODADA. Cada pedido e de um cliente
    diferente, e o CNPJ de um sem cadastro no Mercos nao pode impedir que os
    outros recebam o link. O erro fica escrito NA LINHA, com o motivo, e a
    rodada segue.

    ⚠️ Excecao a isso: sessao derrubada. Ai nao adianta tentar a proxima -- a
    sessao esta morta para todas. Sobe, e o `main` cuida do relogin.
    """
    linhas = fila.pendentes(limite)
    if not linhas:
        log("fila vazia -- nenhum pedido esperando orcamento")
        auditar("fila_vazia")
        return 0

    log(f"fila: {len(linhas)} pedido(s) para orcar")
    run_url = os.environ.get("RUN_URL", "")
    ok = falhas = 0

    for linha in linhas:
        token = linha.get("token")
        log("=" * 60)
        log(f"fila: {token} | cnpj {linha.get('cnpj')} | {linha.get('cliente_nome') or ''}")
        try:
            itens = fila.itens_da_linha(linha)
        except ValueError as e:
            # Linha malformada nao melhora com retentativa -- gasta a cota de
            # tentativas de uma vez e para de voltar na varredura.
            log(f"   linha invalida: {e}")
            fila.marcar_erro({**linha, "tentativas": fila.MAX_TENTATIVAS}, f"linha invalida: {e}")
            auditar("fila_linha_invalida", token=token, motivo=str(e))
            falhas += 1
            continue

        # Em `dry` a linha NAO e marcada: marcar "rodando" e nunca concluir
        # deixaria o pedido preso fora da varredura -- um teste tirando da fila
        # um cliente que ninguem atendeu.
        if aplicar:
            fila.marcar_rodando(linha, run_url)
        try:
            r = rodar_um(page, contexto, linha["cnpj"], itens,
                         float(linha.get("desconto_pct") or 0), aplicar)
        except SessaoDerrubada:
            # Devolve a linha para a fila antes de subir: `rodando` orfa nunca
            # mais seria pega pela varredura, e o cliente ficaria sem link.
            fila.marcar_erro(linha, "sessao do Mercos caiu durante esta linha")
            raise
        except OrcamentoAbortado as e:
            log(f"   ABORTADO nesta linha: {e}")
            fila.marcar_erro(linha, str(e))
            auditar("fila_linha_abortada", token=token, motivo=str(e))
            falhas += 1
            continue
        except Exception as e:  # noqa: BLE001
            shot(page, f"fila-erro-{token}")
            log(f"   ERRO nesta linha: {type(e).__name__}: {e}")
            fila.marcar_erro(linha, f"{type(e).__name__}: {e}")
            auditar("fila_linha_erro", token=token, tipo=type(e).__name__, motivo=str(e))
            falhas += 1
            continue

        link = r.get("link") or ""
        if not aplicar:
            log(f"   [dry] pararia aqui; nao marco {token} como pronto nem aviso o cliente")
            auditar("fila_linha_dry", token=token)
            ok += 1
            continue

        fila.marcar_pronto(linha, r.get("orcamento") or "", link)
        auditar("fila_linha_pronta", token=token, orcamento=r.get("orcamento"), link=bool(link))
        # O QR so existe como arquivo no runner; para chegar no WhatsApp precisa
        # de uma URL publica.
        qr_url = fila.publicar_qrcode(r.get("qrcode") or "", log=log)
        fila.avisar_cliente({**linha, "orcamento_numero": r.get("orcamento")},
                            link, qr_url=qr_url, log=log)
        ok += 1

    log("=" * 60)
    log(f"fila: {ok} ok, {falhas} com erro")
    auditar("fila_rodada_ok", ok=ok, falhas=falhas)
    # Erro em linha nao pinta a rodada de vermelho: o resultado esta gravado
    # linha a linha, e um CNPJ sem cadastro nao e falha do robo.
    return 0


def parse_itens(txt: str) -> list[tuple[str, int]]:
    """'BB02009:12,BB02001:6' -> [('BB02009', 12), ('BB02001', 6)]"""
    saida = []
    for parte in (txt or "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        if ":" not in parte:
            raise SystemExit(f"Item mal formado: '{parte}'. Use SKU:QTD, ex. BB02009:12")
        sku, qtd = parte.split(":", 1)
        saida.append((sku.strip().upper(), int(qtd.strip())))
    return saida


def main() -> int:
    ap = argparse.ArgumentParser(description="Gira - cria orcamento no Mercos e gera link Pix")
    modo = ap.add_mutually_exclusive_group(required=True)
    modo.add_argument("--mapear", action="store_true", help="abre as telas e confere seletores. NUNCA escreve")
    modo.add_argument("--dry", action="store_true", help="percorre tudo sem escrever")
    modo.add_argument("--aplicar", action="store_true", help="cria o orcamento e o link de verdade")
    ap.add_argument("--cnpj", default="", help="CNPJ (so digitos) ou nome exato do cliente")
    ap.add_argument("--itens", default="", help="SKU:QTD separados por virgula")
    ap.add_argument("--desconto", type=float, default=0.0, help="desconto do PEDIDO inteiro, em %%")
    ap.add_argument("--headful", action="store_true", help="abre o navegador visivel (debug local)")
    ap.add_argument("--fila", action="store_true",
                    help="processa gira_orcamento_fila em vez de --cnpj/--itens")
    ap.add_argument("--limite", type=int, default=5, help="quantas linhas da fila nesta rodada")
    args = ap.parse_args()

    aplicar = bool(args.aplicar)
    if not args.mapear and not args.fila:
        if not args.cnpj or not args.itens:
            raise SystemExit("--cnpj e --itens sao obrigatorios fora do --mapear e do --fila")
    itens = parse_itens(args.itens)

    for nome, valor in [("MERCOS_EMAIL", MERCOS_EMAIL), ("MERCOS_SENHA", MERCOS_SENHA),
                        ("GMAIL_USER", GMAIL_USER), ("GMAIL_SENHA", GMAIL_SENHA)]:
        if not valor:
            raise SystemExit(f"Falta a variavel de ambiente {nome}.")

    # ⚠️ ANTES do navegador: App Password do Gmail expira sozinho, e sem isto o
    # run subiria Chromium, logaria, pediria 2FA e so entao estouraria um
    # traceback de imaplib na ultima linha do log.
    conferir_credencial_gmail(GMAIL_USER, GMAIL_SENHA)
    log("credencial do Gmail conferida")

    log(f"modo: {'aplicar' if aplicar else ('mapear' if args.mapear else 'dry')}")
    log(f"empresa Mercos: {EMPRESA_ID}")
    if itens:
        log(f"itens: {itens} | desconto do pedido: {args.desconto}%")

    os.makedirs(LOG_DIR, exist_ok=True)
    tentativas = 0
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(headless=not args.headful)
        contexto = navegador.new_context(viewport={"width": 1600, "height": 1000})
        page = contexto.new_page()
        try:
            while True:
                try:
                    login_mercos(page, EMPRESA_ID, MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
                    log("login no Mercos: ok")
                    # A tranca do "Gerar pedido", antes de qualquer clique.
                    trancar_gerar_pedido(page)
                    if args.mapear:
                        passo_1_abrir_pedidos(page)
                        shot(page, "mapear-tela-pedidos")
                        passo_2_criar_orcamento(page)
                        shot(page, "mapear-tela-novo-orcamento")
                        log("MAPEAR: as duas telas iniciais responderam. Nada foi escrito.")
                        auditar("mapeamento_ok")
                        return 0
                    if args.fila:
                        return rodar_fila(page, contexto, args.limite, aplicar)
                    r = rodar_um(page, contexto, args.cnpj, itens, args.desconto, aplicar)
                    auditar("rodada_ok", **{k: v for k, v in r.items() if k != "itens"})
                    log("=" * 60)
                    log(f"RESULTADO: {json.dumps(r, ensure_ascii=False)}")
                    return 0
                except SessaoDerrubada as e:
                    tentativas += 1
                    if tentativas > VOLTAS_POR_RODADA:
                        auditar("sessao_derrubada_desistiu", tentativas=tentativas, motivo=str(e))
                        log(f"ABORTADO: {e}")
                        return 2
                    log(f"sessao caiu ({e}). Esperando {ESPERA_VOLTA_S}s para reentrar "
                        f"(tentativa {tentativas}/{VOLTAS_POR_RODADA})")
                    auditar("sessao_derrubada_vai_voltar", tentativa=tentativas, espera_s=ESPERA_VOLTA_S)
                    time.sleep(ESPERA_VOLTA_S)
        except OrcamentoAbortado as e:
            auditar("abortado", motivo=str(e))
            log(f"ABORTADO (precisa de gente): {e}")
            return 3
        except Exception as e:
            shot(page, "erro-inesperado")
            auditar("erro", tipo=type(e).__name__, motivo=str(e))
            log(f"ERRO: {type(e).__name__}: {e}")
            return 1
        finally:
            try:
                contexto.close()
                navegador.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
