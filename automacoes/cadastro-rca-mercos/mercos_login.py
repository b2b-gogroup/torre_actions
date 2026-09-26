#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Login no Mercos + resolucao de 2FA por e-mail (IMAP). Modulo compartilhado
por escritor.py (e qualquer script futuro que precise logar no Mercos a
partir do GitHub Actions).

Consolida os bugs reais encontrados testando ao vivo 23/jul/2026 (relato
completo em CadastroRCA-Mercos/decisoes-e-plano.md e arquitetura-producao.md
no repo torre_de_performance_b2b):

1. CI SEMPRE pede 2FA -- runner e maquina nova, sem sessao/cookie confiavel
2. Tela real de 2FA e 6 <input maxlength=1> separados
   (input[class*="inputCodigoAutenticacao"]), NAO um campo unico
   [name="codigo"]
3. Flag IMAP \\Seen nao persiste de forma confiavel entre conexoes
   separadas -- filtrar por DATA (e-mail posterior ao momento do login),
   nunca por lido/nao-lido
4. Gmail exige App Password pra IMAP -- senha normal da conta e recusada.
   ⚠️ App Password EXPIRA sozinho (senha da conta trocada, 2SV reconfigurado,
   politica do Workspace): `[AUTHENTICATIONFAILED] Invalid credentials`. Nesse
   caso o erro e CredencialGmailInvalida, com o passo a passo na mensagem --
   e `conferir_credencial_gmail()` roda ANTES do navegador pra falhar em
   segundos
5. wait_for_url com glob da falso positivo (URL de login ja contem
   "guia_inicial" no parametro next=...) -- confirmar sucesso por elemento
   real do dashboard (texto "INDICADORES" da sidebar), nunca por URL
6. E-mail do Mercos e so HTML (sem text/plain) -- corpo tem "color:#424242"
   (cor de texto) repetido varias vezes ANTES do codigo real -- regex
   ingenuo pegava a cor. Fix: excluir match precedido por "#"
7. O CODIGO VAI PARA A CAIXA DA CONTA QUE LOGA (MERCOS_EMAIL), nao para uma
   caixa fixa -- entao GMAIL_USER tem que ser essa mesma conta, ou uma caixa
   que receba o encaminhamento dela. Em 18/set/2026 o secret GMAIL_USER foi
   trocado para outra conta: a credencial era valida, o IMAP logava,
   conferir_credencial_gmail imprimia OK -- e os TRES robos de Mercos
   (credito, fotos, cadastro) passaram a morrer em "Nenhum codigo 2FA
   encontrado" por 3 dias, lendo a caixa errada. Guarda que testa "consigo
   logar" nao e a mesma coisa que testar "esta caixa recebe o codigo", e foi
   a diferenca entre as duas que ficou invisivel. Hoje a conferencia exige
   e-mail do Mercos NA CAIXA, e as pastas varridas incluem "Todos os
   e-mails" e o spam (filtro que arquiva da exatamente o mesmo sintoma)
"""

import email as email_lib
import imaplib
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    from playwright.sync_api import Page
except ModuleNotFoundError:  # pragma: no cover
    # Ferramenta que so LE e-mail (ferramentas/diagnostico_2fa.py) usa os
    # helpers de IMAP daqui e nao instala Playwright -- fazer o import do
    # navegador derrubar essa leitura obrigaria o workflow de diagnostico a
    # instalar o Chromium inteiro para nao abrir navegador nenhum.
    # `Page` aqui e so anotacao; onde o Playwright existe, nada muda.
    from typing import Any as Page  # type: ignore[assignment]

SELETOR_INPUT_CODIGO = 'input[class*="inputCodigoAutenticacao"]'


class CredencialGmailInvalida(RuntimeError):
    """IMAP recusou GMAIL_USER/GMAIL_SENHA -- nao e erro de codigo nem do Mercos.

    Aconteceu em 18/ago/2026: o pipeline rodou OK em 17/ago 13:33 no mesmo repo
    com os mesmos secrets (setados em 24/jul, iguais nos 3 espelhos -- conferido
    por `gh secret list`) e no dia seguinte o Gmail passou a devolver
    `[AUTHENTICATIONFAILED] Invalid credentials (Failure)`. App Password do Google
    morre sozinho quando a senha da conta muda, quando o 2SV e reconfigurado ou
    quando o admin do Workspace bloqueia app password -- nada disso da aviso.
    """


class CaixaNaoRecebe2FA(RuntimeError):
    """O IMAP logou, mas nesta caixa NAO existe e-mail nenhum do Mercos.

    Quase sempre significa GMAIL_USER apontando para outra conta que nao a do
    MERCOS_EMAIL -- ver bug 7. O robo aborta aqui, em segundos, porque seguir
    daria "Nenhum codigo 2FA encontrado" dois minutos depois, mensagem que
    manda procurar no lugar errado (Mercos fora do ar, rate limit, regex).
    """


def _pastas_de_busca(mail):
    """Onde procurar o e-mail do Mercos, em ordem.

    "Todos os e-mails" vem primeiro e e detectado pela FLAG \\All, nunca pelo
    nome: ele muda com o idioma da conta ("[Gmail]/All Mail" x
    "[Gmail]/Todos os e-mails"). Procurar so no INBOX faz um filtro que
    arquiva a mensagem parecer "o Mercos nao mandou".
    """
    todos, spam = None, None
    try:
        _, linhas = mail.list()
        for bruto in linhas or []:
            texto = bruto.decode("utf-8", errors="ignore")
            achado = re.search(r'"([^"]*)"\s*$', texto)
            nome = achado.group(1) if achado else texto.split()[-1]
            if "\\All" in texto:
                todos = nome
            elif "\\Junk" in texto:
                spam = nome
    except Exception:
        pass
    vistas, saida = set(), []
    for nome in [x for x in (todos, "INBOX", spam) if x]:
        if nome not in vistas:
            vistas.add(nome)
            saida.append(nome)
    return saida


def _uids_do_mercos(mail, pasta):
    """UIDs de e-mail vindo do Mercos naquela pasta; [] se a pasta nao abrir.

    Busca por "mercos" e nao pelo remetente exato: se o Mercos trocar o
    `nao-responder@`, o sintoma seria identico ao da caixa errada -- e o
    filtro de tempo (so e-mail posterior ao login) ja e o que garante que o
    codigo lido e o desta tentativa.
    """
    try:
        estado, _ = mail.select('"%s"' % pasta, readonly=True)
        if estado != "OK":
            return []
        _, dados = mail.search(None, '(FROM "mercos")')
        return dados[0].split()
    except Exception:
        return []


def _estado_da_caixa(mail):
    """(quantos e-mails do Mercos, data do mais recente, pasta) na 1a pasta que tiver."""
    for pasta in _pastas_de_busca(mail):
        uids = _uids_do_mercos(mail, pasta)
        if not uids:
            continue
        quando = None
        try:
            _, bruto = mail.fetch(uids[-1], "(BODY.PEEK[HEADER.FIELDS (DATE)])")
            quando = parsedate_to_datetime(
                email_lib.message_from_bytes(bruto[0][1]).get("Date"))
        except Exception:
            pass
        return len(uids), quando, pasta
    return 0, None, None


def _msg_caixa_errada(gmail_user: str, mercos_email: str) -> str:
    """A mensagem nao pode depender de MOSTRAR os e-mails: o Actions mascara
    valor de secret como ***. O que ela entrega e a COMPARACAO entre as duas
    contas, calculada aqui -- e comparacao nao e mascarada."""
    if mercos_email:
        iguais = gmail_user.strip().lower() == mercos_email.strip().lower()
        relacao = (
            "GMAIL_USER e MERCOS_EMAIL sao a MESMA conta, entao o e-mail deveria "
            "estar aqui: ou o Mercos parou de mandar o codigo por e-mail, ou uma "
            "regra da propria conta esta apagando a mensagem."
            if iguais else
            "GMAIL_USER e MERCOS_EMAIL sao contas DIFERENTES e nao ha "
            "encaminhamento chegando: essa e a causa mais provavel."
        )
    else:
        relacao = ("MERCOS_EMAIL nao foi passado para esta conferencia, entao nao "
                   "da para comparar as duas contas aqui.")
    return (
        "O IMAP de %s logou, mas nao ha NENHUM e-mail do Mercos nessa caixa "
        "(procurado em todas as pastas, inclusive arquivadas e spam). %s "
        "O Mercos manda o codigo de 2FA para o e-mail da conta que LOGA "
        "(MERCOS_EMAIL) -- nao para uma caixa fixa. Conserto: gerar um App "
        "Password em myaccount.google.com/apppasswords COM A CONTA DO "
        "MERCOS_EMAIL e atualizar GMAIL_USER/GMAIL_SENHA nos espelhos do "
        "rodizio (b2b-gogroup/torre_b2b, leandroperruci-web/"
        "torre_de_performance_b2b, italo-mgl/torre_etl)." % (gmail_user, relacao)
    )


def conferir_credencial_gmail(gmail_user: str, gmail_senha: str,
                              mercos_email: str = "") -> None:
    """Loga no IMAP E confere que esta caixa e a que recebe o codigo do Mercos.

    Sao DUAS perguntas, e a segunda so existe porque a primeira respondeu "OK"
    durante os 3 dias de falha de 18-21/set/2026 (bug 7 no topo do modulo).
    As duas rodam antes do Chromium para o run MORRER EM SEGUNDOS com a causa
    escrita, em vez de estourar 2 minutos depois (18/ago: 44s de run e a causa
    real na ultima linha)."""
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            mail.login(gmail_user, gmail_senha)
            quantos, ultimo, pasta = _estado_da_caixa(mail)
        finally:
            try:
                mail.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as exc:
        raise CredencialGmailInvalida(_msg_credencial(gmail_user, exc)) from exc

    if quantos == 0:
        raise CaixaNaoRecebe2FA(_msg_caixa_errada(gmail_user, mercos_email))

    # A DATA do ultimo e-mail separa "caixa certa" de "o Mercos parou de mandar":
    # se ela for de dias atras, o proximo erro nao e de credencial nem de caixa.
    quando = ultimo.isoformat() if ultimo else "data ilegivel"
    print("  [2fa] credencial IMAP de %s OK -- %d e-mail(s) do Mercos em %s, o "
          "mais recente de %s" % (gmail_user, quantos, pasta, quando))


def _msg_credencial(gmail_user: str, exc: Exception) -> str:
    return (
        f"IMAP do Gmail recusou a credencial de {gmail_user}: {exc}. "
        "Nao e bug do pipeline -- GMAIL_SENHA tem que ser um App Password do "
        "Google (16 caracteres) e ele e revogado sozinho se a senha da conta "
        "mudar, se o 2SV for reconfigurado ou se o admin do Workspace bloquear "
        "app password. Gere outro em myaccount.google.com/apppasswords com a "
        "conta que recebe o 2FA do Mercos e atualize o secret GMAIL_SENHA nos 3 "
        "espelhos (b2b-gogroup/torre_b2b, leandroperruci-web/"
        "torre_de_performance_b2b, italo-mgl/torre_etl)."
    )


def _pegar_codigo_2fa(gmail_user: str, gmail_senha: str, momento_login: datetime,
                       aguardar: int = 20, tentativas: int = 4,
                       mercos_email: str = "") -> str:
    """Pega o codigo do e-mail do Mercos posterior a 'momento_login'.
    Ver bugs 3, 6 e 7 no docstring do modulo."""
    ultimo_estado = (0, None, None)
    for tentativa in range(1, tentativas + 1):
        print(f"  [2fa] aguardando {aguardar}s (tentativa {tentativa}/{tentativas})...")
        time.sleep(aguardar)
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        try:
            mail.login(gmail_user, gmail_senha)
        except imaplib.IMAP4.error as exc:
            # Credencial recusada nao melhora esperando -- aborta na hora em vez
            # de queimar as 3 tentativas restantes (60s) escondendo a causa.
            raise CredencialGmailInvalida(_msg_credencial(gmail_user, exc)) from exc
        candidatos = []
        for pasta in _pastas_de_busca(mail):
            uids = _uids_do_mercos(mail, pasta)[-10:]
            if not uids:
                continue
            for uid in uids:
                _, msg_data = mail.fetch(uid, "(RFC822)")
                msg = email_lib.message_from_bytes(msg_data[0][1])
                try:
                    data_email = parsedate_to_datetime(msg.get("Date"))
                except Exception:
                    continue
                if data_email <= momento_login:
                    continue
                candidatos.append((data_email, msg))
            if candidatos:
                break  # "Todos os e-mails" ja contem o INBOX -- nao varrer de novo
        ultimo_estado = _estado_da_caixa(mail)
        mail.logout()

        if candidatos:
            candidatos.sort(key=lambda par: par[0])
            _, msg_mais_novo = candidatos[-1]
            if msg_mais_novo.is_multipart():
                body = ""
                for part in msg_mais_novo.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
                    if part.get_content_type() == "text/html" and not body:
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
            else:
                body = msg_mais_novo.get_payload(decode=True).decode("utf-8", errors="ignore")

            match = re.search(r"(?<!#)\b(\d{6})\b", body)
            if match:
                print(f"  [2fa] codigo encontrado: {match.group(1)}")
                return match.group(1)

        print(f"  [2fa] nenhum e-mail novo ainda (posterior a {momento_login.isoformat()})")

    # "Nenhum codigo encontrado" sozinho e ambiguo entre TRES causas com
    # consertos opostos: caixa errada, Mercos que nao mandou, e corpo/regex.
    # O estado da caixa e o que separa as duas primeiras -- entao ele vai junto.
    quantos, ultimo, pasta = ultimo_estado
    if quantos == 0:
        raise CaixaNaoRecebe2FA(_msg_caixa_errada(gmail_user, mercos_email))
    quando = ultimo.isoformat() if ultimo else "data ilegivel"
    raise RuntimeError(
        "Nenhum codigo 2FA encontrado apos %d tentativas. A caixa e a certa "
        "(%d e-mail(s) do Mercos em %s), mas o mais recente e de %s -- anterior "
        "ao login desta rodada (%s). Ou seja: o Mercos NAO mandou o codigo "
        "agora. Conferir se a conta esta bloqueada por tentativa repetida, se o "
        "canal do 2FA foi trocado no Mercos, ou se o envio dele esta fora do ar."
        % (tentativas, quantos, pasta, quando, momento_login.isoformat())
    )


# As 3 empresas da mesma conta. A sigla e o que o seletor do topo mostra entre
# parenteses ("BEAUTY HUB ATACADO (ES)"). SP esta congelada, mas fica aqui: sem
# ela o robo que um dia apontasse para 424523 falharia sem dizer por que.
EMPRESAS_MERCOS = {"424525": "ES", "424524": "RJ", "424523": "SP"}


def garantir_empresa(page: Page, empresa_id: str) -> None:
    """Confere em QUAL empresa a sessao abriu e troca se nao for a pedida.

    🔴 SEM ISTO, O ROBO CULPA A SESSAO POR UM PROBLEMA QUE E DE EMPRESA.

    O Mercos **nao honra URL direta entre empresas**: logado na sessao da ES,
    pedir `/424524/pedidos/` devolve `login?next=/424524/pedidos/`. E a conta
    **entra numa empresa que varia por sessao** -- o `next=` do login costuma
    ser respeitado, mas nao e garantido.

    O estrago e o diagnostico, nao o redirect: a tela de login tem
    `[name="senha"]` no DOM, entao toda checagem de "a sessao caiu?" responde
    SIM -- com a sessao viva. Foi o que derrubou o `mercos-fotos` em
    21/set/2026 as 15h38, com a mensagem *"a sessao do Mercos caiu"* logo
    depois de o 2FA ter funcionado (`codigo encontrado: 758290`). Quem lesse
    aquele log iria consertar o 2FA, que estava certo.

    Medido ao vivo em 21/set/2026, uma sessao aberta na ES:

        /industria/424525/produtos/  -> ok     (o prefixo /industria/ e valido)
        /424525/produtos|clientes|pedidos/ -> ok
        /424524/pedidos/   (a outra empresa) -> LOGIN, recusou

    ⚠️ A medicao tambem DESCARTOU a outra hipotese: o `/industria/` do
    `mercos-fotos` -- que so ele usa -- nao tem nada de errado. Trocar a URL
    teria sido consertar a coisa errada.

    A troca e pelo **seletor do topo**, o unico caminho que o Mercos honra;
    outro `goto` volta para o login. Portado de `orcamento-mercos`
    (`garantir_empresa`), onde ja rodava em producao -- aqui, em vez de copiar,
    fica no modulo que os **15 chamadores** de `login_mercos` compartilham.
    """
    # ⚠️ `/login?next=/424525/guia_inicial/` CONTEM `/424525/`, entao o teste
    # ingenuo dava no-op estando na tela de login -- foi assim que este guard
    # passou calado em 21/set enquanto a sessao nem existia ainda.
    if f"/{empresa_id}/" in page.url and "/login" not in page.url:
        return

    sigla = EMPRESAS_MERCOS.get(empresa_id)
    if not sigla:
        raise RuntimeError(
            f"A sessao abriu em {page.url}, que nao e a empresa {empresa_id}, e "
            f"{empresa_id} nao esta em EMPRESAS_MERCOS -- sem a sigla nao da para "
            f"achar o item no seletor do topo. Conhecidas: {sorted(EMPRESAS_MERCOS)}."
        )

    print(f"  [login] a sessao abriu em outra empresa ({page.url}) -- trocando para {sigla}")
    for tentativa in (1, 2, 3, 4):
        try:
            page.click("text=/BEAUTY HUB/i", timeout=8_000)
            page.wait_for_timeout(700)
            page.click(f"text=/[(]{sigla}[)]/", timeout=8_000)
            page.wait_for_url(f"**/{empresa_id}/**", timeout=20_000)
            print(f"  [login] empresa trocada para {sigla} ({empresa_id})")
            return
        except Exception as e:  # noqa: BLE001
            print(f"  [login] troca de empresa {tentativa}/4 nao pegou "
                  f"({str(e).splitlines()[0][:60]})")
            page.wait_for_timeout(1_500)

    raise RuntimeError(
        f"A sessao abriu numa empresa diferente de {empresa_id} ({sigla}) e o seletor do "
        f"topo nao trocou em 4 tentativas. URL atual: {page.url}. ⚠️ Isto NAO e sessao "
        f"caida: qualquer URL de /{empresa_id}/ vai devolver a tela de login enquanto a "
        f"sessao estiver na outra empresa, e a tela de login PARECE sessao morta."
    )


def login_mercos(page: Page, empresa_id: str, mercos_email: str, mercos_senha: str,
                  gmail_user: str, gmail_senha: str) -> None:
    """Loga no Mercos e resolve 2FA se pedido. Levanta RuntimeError se nao
    conseguir confirmar chegada no dashboard."""
    login_url = f"https://app.mercos.com/login?next=/{empresa_id}/guia_inicial/"
    page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
    page.fill('[name="usuario"]', mercos_email)
    page.fill('[name="senha"]', mercos_senha)
    momento_login = datetime.now(timezone.utc)
    page.click('[type="submit"]')
    try:
        page.wait_for_url(lambda url: "login" not in url, timeout=15_000)
    except Exception:
        pass
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    time.sleep(2)

    pediu_2fa = page.locator(SELETOR_INPUT_CODIGO).count() > 0
    print(f"  [login] pediu 2FA: {pediu_2fa}")

    if pediu_2fa:
        if not (gmail_user and gmail_senha):
            # Falha NOMEANDO a causa: sem a caixa que recebe o codigo nao ha o
            # que tentar, e "login nao confirmado" mandaria procurar bug de
            # seletor. Acontece quando alguem roda pela maquina propria com uma
            # conta que ele achava que nao pedia 2FA (01/set/2026).
            raise RuntimeError(
                f"O Mercos pediu codigo 2FA para {mercos_email} e nao ha "
                "GMAIL_USER/GMAIL_SENHA configurados pra ler o e-mail. "
                "Ou use uma conta que nao pede 2FA, ou configure a caixa que "
                "recebe o codigo (App Password do Google)."
            )
        codigo = _pegar_codigo_2fa(gmail_user, gmail_senha, momento_login,
                                   mercos_email=mercos_email)
        inputs = page.locator(SELETOR_INPUT_CODIGO)
        if inputs.count() != 6:
            raise RuntimeError(f"Esperava 6 inputs de codigo 2FA, achei {inputs.count()}")
        for i, digito in enumerate(codigo):
            inputs.nth(i).fill(digito)
        page.click('button:has-text("continuar")', timeout=10_000)
        # Apos o codigo, o Mercos passa por uma pagina intermediaria de
        # redirect (ex "/redirecionar_pagina_inicial/<id>") antes de chegar
        # no dashboard de verdade -- confirmado ao vivo 23/jul. Um networkidle
        # + sleep curto nao e suficiente; espera ativamente pelo elemento.
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass

    try:
        page.wait_for_selector("text=INDICADORES", timeout=30_000)
        logado = True
    except Exception:
        logado = False

    if not logado:
        raise RuntimeError(f"Login nao confirmado -- 'INDICADORES' ausente. URL: {page.url}")

    # ⚠️ Depois do 2FA o Mercos encadeia redirects
    # (`/login?next=...` -> `/redirecionar_pagina_inicial/<id>` -> `/<empresa>/guia_inicial/`)
    # e o texto "INDICADORES" ja aparece ANTES de a cadeia terminar. Sem esperar
    # aqui, o robo declarava sessao pronta na PROPRIA tela de login e disparava o
    # primeiro `goto` por cima do redirect em curso -- o Playwright entao aborta a
    # navegacao. Medido em 21/set: os tres sintomas que pareciam defeitos distintos
    # (`net::ERR_ABORTED` em 18/09, "seletor nao apareceu e depois caiu pra login"
    # no modo test, e "Navigation ... is interrupted by another navigation" no dry)
    # sao a MESMA corrida, variando so com o timing. Quem passava era quem tinha
    # trabalho entre o login e o 1o goto (ler o banco), nao quem estava "certo".
    # O caminho SEM 2FA (maquina local, IP ja confiado) nunca exercita isto.
    try:
        page.wait_for_url(lambda u: "/login" not in u, timeout=30_000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    if "/login" in page.url:
        raise RuntimeError(
            "O login nao assentou: depois de 30s a sessao ainda esta em "
            f"{page.url}. Isto NAO e 'sessao caiu no meio da rodada' -- ela "
            "nunca chegou a existir. Conferir credencial/2FA antes de olhar "
            "as telas seguintes."
        )

    # ⚠️ `INDICADORES` NAO prova que estamos na empresa certa: e o painel de
    # QUALQUER empresa da conta. Sem a linha abaixo, a mensagem seguinte
    # AFIRMA `empresa {empresa_id}` sem ter conferido -- e o robo so descobre
    # o contrario no primeiro goto, ja travestido de "sessao caiu".
    garantir_empresa(page, empresa_id)

    # A URL vai junto porque o caminho COM 2FA passa por uma pagina
    # intermediaria de redirect que o caminho sem 2FA nunca exercita -- e o
    # ambiente que pede 2FA (Actions, IP novo a cada run) e justamente o que
    # falha no primeiro goto depois daqui. Sem ela, a unica coisa que se sabe
    # e que "INDICADORES" apareceu, que e verdade nas duas.
    print(f"  [login] sessao pronta ({mercos_email}, empresa {empresa_id}) "
          f"| url={page.url}")
