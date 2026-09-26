#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mercos -- helpers de UI compartilhados entre os robos que escrevem la.

✅ 20/ago/2026 -- EXTRAIDO de `escritor.py` (que agora importa daqui) quando o
segundo robo nasceu (`automacoes/credito-limite-mercos`, limite de credito).

O motivo e o de sempre: estas funcoes dependem de SELETOR de tela de terceiro.
O Mercos muda o HTML sem avisar, e com duas copias o primeiro robo e consertado
e o segundo continua quebrado -- em silencio, porque "nao achei o cliente" e uma
saida valida da funcao. Corpo unico (convencao #14 da Torre).

Regra de ouro herdada do decisor: NUNCA ADIVINHAR. Sem match unico, devolve
None/levanta -- nunca escolhe cliente ou campo no palpite.

Log/screenshot entram por parametro (`log`, `shot`) porque cada robo tem o
proprio formato de linha e a propria pasta de logs.
"""

import re
import time

from playwright.sync_api import Page

# Mensagem que o Mercos mostra quando a MESMA conta e usada em outro lugar --
# ele aceita 1 sessao por usuario, e derrubar a do robo no meio da rodada e o
# modo de falha mais comum em producao.
TEXTO_SESSAO_DERRUBADA = "acessou o sistema de outro computador"


class SessaoCaiu(RuntimeError):
    """A sessao do Mercos caiu no meio da navegacao (a conta foi usada em outro
    lugar -- o Mercos so aceita 1 sessao por usuario).

    ⚠️ Mora aqui, e nao no robo que chama, porque `mercos_ui` nao pode importar
    do chamador sem import circular. Quem chama converte na sua propria excecao
    (o repositor faz isso em `resolver_cliente`) para nao ter DUAS excecoes de
    sessao circulando no mesmo laco."""


def sessao_caiu(page: Page) -> bool:
    """Voltamos pra tela de login? Checa o form de login (a mensagem de
    desconexao nem sempre esta presente -- so aparece no 1o carregamento)."""
    try:
        if page.locator('[name="senha"]').count() > 0:
            return True
        return TEXTO_SESSAO_DERRUBADA in (page.content() or "")
    except Exception:
        return False


def motivo_sessao(page: Page, mercos_email: str) -> str:
    """Frase pronta pra explicar a queda. Nomear a causa importa: "a sessao
    caiu" manda procurar bug no robo; "a conta foi usada em outro computador"
    manda avisar quem estava logado."""
    try:
        if TEXTO_SESSAO_DERRUBADA in (page.content() or ""):
            return ("o Mercos derrubou a sessao do robo -- a conta "
                    f"{mercos_email} foi usada em outro computador/dispositivo "
                    "durante a rodada (o Mercos so aceita 1 sessao por usuario)")
    except Exception:
        pass
    return "a sessao do Mercos caiu (voltamos pra tela de login) no meio da rodada"


def buscar_cliente_id_por_cnpj(page: Page, empresa_id: str, cnpj: str,
                               log=print, shot=None) -> str | None:
    """Acha o cliente_id do Mercos pesquisando o CNPJ na tela de Clientes.

    Devolve None se nao achar ou se houver mais de 1 resultado -- nunca escolhe
    cliente no palpite. Seletores mapeados em
    `CadastroRCA-Mercos/mercos-ui-mapeamento.md` (passos 2-6).
    """
    page.goto(f"https://app.mercos.com/{empresa_id}/clientes/", wait_until="domcontentloaded", timeout=30_000)
    # ⚠️ Espera o CAMPO DE BUSCA, nao a rede parar. `networkidle` num SPA pode
    # nunca acontecer e queimava 15s por cliente (medido em 20/ago/2026).
    #
    # ⚠️ O TIMEOUT AQUI QUASE SEMPRE E SESSAO DERRUBADA, NAO PAGINA LENTA. Com a
    # sessao morta o Mercos serve a TELA DE LOGIN: o DOM carrega rapido e o campo
    # de busca simplesmente nunca existe -- ou seja, o sintoma de "outra pessoa
    # entrou na conta" e um timeout limpo de 20s. Custou a rodada do cron de
    # 25/ago/2026 (run 32908295684): 23 boletos nao chegaram a ser tentados
    # porque este `wait_for_selector` estourou e o erro subiu como
    # `playwright.TimeoutError`, que ninguem la em cima tratava.
    #
    # Por que a checagem mora AQUI e nao no chamador: `sessao_caiu()` so pode ser
    # perguntada com a pagina na mao, e quem estoura nunca chega a devolver a
    # pagina pro chamador. Levantar `SessaoCaiu` e o que da ao chamador a chance
    # de reconquistar a sessao e voltar em vez de morrer.
    try:
        page.wait_for_selector("#id_nome_rapido", timeout=20_000, state="visible")
    except Exception as e:
        if sessao_caiu(page):
            raise SessaoCaiu("a sessao do Mercos caiu durante a busca do cliente "
                             "(a tela de login voltou no lugar da lista de clientes)") from None
        # Sessao viva e campo ausente = pagina lenta ou tela remapeada. Uma
        # recarga resolve o 1o caso; o 2o vira erro DESTE cliente (RuntimeError),
        # nao da fila inteira -- a diferenca entre perder um cliente e perder a
        # rodada.
        log(f"[busca] campo de busca nao apareceu em 20s (sessao viva) -- recarregando uma vez")
        page.goto(f"https://app.mercos.com/{empresa_id}/clientes/", wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_selector("#id_nome_rapido", timeout=20_000, state="visible")
        except Exception:
            if sessao_caiu(page):
                raise SessaoCaiu("a sessao do Mercos caiu durante a busca do cliente "
                             "(a tela de login voltou no lugar da lista de clientes)") from None
            if shot:
                shot(f"sem_campo_busca_{cnpj}")
            raise RuntimeError(
                "a tela de Clientes do Mercos nao mostrou o campo de busca "
                f"(#id_nome_rapido) em 2 tentativas de 20s, empresa {empresa_id}, "
                f"e a sessao esta viva -- pode ser lentidao do Mercos ou a tela mudou") from e
    page.fill("#id_nome_rapido", cnpj)
    page.keyboard.press("Enter")

    # Espera o resultado RENDERIZAR, nao um sleep fixo. Com `sleep(2.5)` o
    # chamado 31404 foi contado como "0 resultados" em 18/ago -- e o screenshot,
    # tirado logo depois, mostrava o cliente na tela. Contar antes de a pagina
    # terminar vira "cliente nao existe no Mercos", que manda o humano procurar
    # o problema no lugar errado.
    seletor = f'a[href^="/{empresa_id}/clientes/"]'
    padrao = re.compile(r"^/" + re.escape(empresa_id) + r"/clientes/(\d+)/")
    ids: list[str] = []
    for espera in (12_000, 6_000):
        try:
            page.wait_for_selector(seletor, timeout=espera, state="attached")
        except Exception:
            continue
        time.sleep(1)  # deixa a lista terminar de montar antes de contar
        links = page.locator(seletor)
        for i in range(links.count()):
            href = links.nth(i).get_attribute("href") or ""
            m = padrao.match(href)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
        if ids:
            break
    if len(ids) == 1:
        log(f"[busca] cliente_id {ids[0]} achado por CNPJ {cnpj} (empresa {empresa_id})")
        return ids[0]
    log(f"[busca] CNPJ {cnpj}: {len(ids)} resultado(s) na empresa {empresa_id} -- nao da pra decidir sozinho")
    if shot:
        shot(f"busca_cnpj_{cnpj}")
    return None


# ── Carteira do vendedor ("Clientes deste usuario") ───────────────────────────
# ✅ 11/set/2026 -- EXTRAIDO de `escritor.py` quando o TERCEIRO robo nasceu
# (`vincular_carteira_lote.py`, vinculo em lote da rede Bel). Mesmo motivo do
# cabecalho deste arquivo: dependem de seletor de tela de terceiro, e duas copias
# divergem em silencio -- "nao encontrado" e uma saida VALIDA destas funcoes,
# entao a copia quebrada nao grita, so para de vincular.

def abrir_todos_usuarios(page: Page) -> None:
    """Menu "minha conta" -> "Todos os usuarios"."""
    try:
        page.locator('i[data-original-title="minha conta"]').click(timeout=10_000)
    except Exception:
        page.click('xpath=/html/body/nav/ul/li[10]/a/i', timeout=10_000)
    time.sleep(2)

    try:
        page.locator('span:has-text("Todos os usuários")').first.click(timeout=10_000)
    except Exception:
        page.click(
            'xpath=/html/body/div[5]/div[1]/div/div[1]/section/div[2]/div/div[1]/div/ul/li[2]/a/span',
            timeout=10_000,
        )
    time.sleep(2)


def abrir_clientes_do_vendedor(page: Page, vendedor_nome: str) -> None:
    """Abre "Clientes deste usuario" do card do vendedor.

    ⚠️ O card e achado por `:has-text()`, que e SUBSTRING case-insensitive: nome
    mais CURTO que o do Mercos casa, mais LONGO nao. Em 01/set/2026 "Luciano
    Pitangueira Bicalho" (como a planilha escreve) falhou porque o Mercos tinha
    "Luciano Bicalho". Levanta em 0 ou >1 card -- nunca escolhe no palpite.
    """
    card = page.locator(f'div.well.colaborador:has-text("{vendedor_nome}")')
    n = card.count()
    if n == 0:
        raise RuntimeError(f"Nenhum card encontrado para '{vendedor_nome}' em Todos os usuarios")
    if n > 1:
        raise RuntimeError(f"{n} cards encontrados para '{vendedor_nome}' -- nome ambiguo")
    card.first.scroll_into_view_if_needed()

    link = card.first.locator('a:has-text("Clientes deste usuário")')
    if link.count() == 0:
        raise RuntimeError(f"Card de '{vendedor_nome}' nao tem botao 'Clientes deste usuario'")
    link.first.click()
    time.sleep(2)


def abrir_carteira_por_url(page: Page, empresa_id: str, vendedor_nome: str) -> None:
    """Abre "Clientes deste usuario" indo direto pela URL da lista de colaboradores.

    ✅ 16/set/2026 -- nasceu porque `abrir_todos_usuarios` (menu "minha conta" ->
    "Todos os usuarios") falha LOGO APOS UM RELOGIN: o menu ainda nao montou e a
    funcao chega numa tela sem card, devolvendo "Nenhum card encontrado" para um
    vendedor que existe. Isso matou a retomada de uma rodada da Mirella no meio
    (23 de 45 vinculados) -- o robo voltou da queda e desistiu por card ausente.

    `/{empresa}/colaboradores/` E a mesma tela de "Todos os usuarios", entao ir por
    URL entrega o mesmo lugar sem depender do menu. Mesma preferencia por URL que a
    paginacao ja usa (clicar "Proxima" parava em 10 de 34 sem erro).
    """
    # ⚠️ Espera o CARD aparecer, nao um `sleep` fixo. Logo apos um relogin a lista
    # monta devagar, e com sleep fixo a funcao chegava numa pagina ainda vazia e
    # anunciava "Nenhum card encontrado" para um vendedor que existe -- foi assim que
    # a rodada da Rafaela Barros parou em 16 de 59 (16/set/2026). Duas tentativas: a
    # 1a cobre pagina lenta, a 2a cobre a sessao ter caido de novo no meio.
    seletor = f'div.well.colaborador:has-text("{vendedor_nome}")'
    for tentativa in (1, 2):
        page.goto(f"https://app.mercos.com/{empresa_id}/colaboradores/",
                  wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector(seletor, timeout=15_000, state="attached")
            break
        except Exception:
            if sessao_caiu(page):
                # Quem chama sabe reconquistar; aqui so nomeamos a causa, senao isso
                # sobe como "card nao existe" e manda procurar o problema no cadastro.
                raise SessaoCaiu(
                    "a sessao caiu ao abrir a lista de colaboradores "
                    f"(procurando o card de '{vendedor_nome}')") from None
            if tentativa == 2:
                raise RuntimeError(
                    f"a lista de colaboradores da empresa {empresa_id} nao mostrou o card "
                    f"de '{vendedor_nome}' em 2 tentativas de 15s, com a sessao viva")
            time.sleep(3)
    time.sleep(1)
    abrir_clientes_do_vendedor(page, vendedor_nome)


def liberar_cliente(page: Page, cnpj_digits: str, cliente_nome: str, log=print, ts=lambda: "") -> str:
    """Habilita UM cliente na carteira aberta. ADICIONA -- nao remove de ninguem.

    Devolve: liberado | nao_encontrado | ambiguo | erro.
    """
    try:
        page.fill('#id_nome', "")
        page.fill('#id_nome', cnpj_digits)
        page.click('button.botao.medio.primario:has-text("Pesquisar")')
        time.sleep(1.5)
    except Exception as e:
        log(f"      {ts()} erro na busca de {cnpj_digits}: {e}")
        return "erro"

    # ⚠️ NAO da pra inferir o estado ATUAL pelos botoes. Medido no DOM em
    # 11/set/2026: a linha traz `habilitar_<id>` E `desabilitar_<id>` ao MESMO
    # tempo (par toggle, um deles escondido). Tentei usar a ausencia de
    # `habilitar_` como "ja esta na carteira" e a checagem nunca disparava.
    # Quem sabe o estado e a coluna "Liberado" / o filtro "Mostrar: Liberados"
    # da propria tela -- e por ali que se CONFERE uma rodada.
    # Consequencia pratica: clicar em `habilitar_` num cliente ja liberado e
    # inofensivo (idempotente), e "liberado" aqui significa "cliquei", nao
    # "mudou de estado".
    botoes = page.locator('button[id^="habilitar_"]')
    n = botoes.count()
    if n == 0:
        # Sem linha nenhuma no resultado = o cliente nao existe NESTA empresa
        # Mercos (ES e RJ tem bases separadas). Saida valida, nao erro.
        log(f"      {ts()} [!] nao encontrado no Mercos: {cnpj_digits} ({cliente_nome})")
        return "nao_encontrado"
    if n > 1:
        log(f"      {ts()} [!] {n} resultados p/ {cnpj_digits} -- ambiguo, pulando")
        return "ambiguo"

    botoes.first.click()
    time.sleep(0.8)
    log(f"      {ts()} [ok] liberado: {cnpj_digits} ({cliente_nome})")
    return "liberado"
