#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Teste isolado: login no Mercos a partir de um runner do GitHub Actions
(maquina nova, sem sessao/cookie previo) + resolucao do 2FA por e-mail via
IMAP. Confirmado ao vivo 23/jul/2026:
  - CI SEMPRE pede 2FA (maquina nova, sem sessao confiavel)
  - Tela real e 6 <input maxlength=1> separados (nao um campo unico
    [name=codigo] como os scripts antigos em relatorio_mercos assumiam)

Le MERCOS_EMAIL/MERCOS_SENHA/GMAIL_USER/GMAIL_SENHA de variavel de ambiente
(secrets do Actions). GMAIL_USER precisa ser a caixa que RECEBE o codigo do
Mercos (mesma conta de login, nao uma caixa de automacao separada).
"""

import email as email_lib
import imaplib
import os
import re
import sys
from datetime import datetime, timezone
import time

from playwright.sync_api import Page, sync_playwright

MERCOS_EMAIL = os.environ["MERCOS_EMAIL"]
MERCOS_SENHA = os.environ["MERCOS_SENHA"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_SENHA = os.environ["GMAIL_SENHA"]
EMPRESA_ID = "424525"

SELETOR_INPUT_CODIGO = 'input[class*="inputCodigoAutenticacao"]'


def pegar_codigo_2fa(momento_login, aguardar: int = 20, tentativas: int = 4) -> str:
    """Pega o codigo do e-mail mais recente de nao-responder@mercos.com que
    chegou DEPOIS de 'momento_login' (datetime UTC de quando o login foi
    submetido). NAO confia em UNSEEN/marcar-como-lido -- confirmado ao vivo
    23/jul que o flag \\Seen nao persiste de forma confiavel entre conexoes
    IMAP separadas (2 execucoes seguidas leram o MESMO e-mail antigo porque a
    marcacao da vez anterior nao "colou"). Filtrar por data e a fonte de
    verdade robusta aqui, independente do estado de leitura."""
    from email.utils import parsedate_to_datetime

    for tentativa in range(1, tentativas + 1):
        print(f"Aguardando {aguardar}s (tentativa {tentativa}/{tentativas}) pelo e-mail do codigo...")
        time.sleep(aguardar)
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_SENHA)
        mail.select("inbox")
        _, data = mail.search(None, '(FROM "nao-responder@mercos.com")')
        uids = data[0].split()[-10:]  # so os 10 mais recentes, nao precisa mais que isso

        candidatos = []
        for uid in uids:
            _, msg_data = mail.fetch(uid, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            data_email = parsedate_to_datetime(msg.get("Date"))
            if data_email <= momento_login:
                continue  # e-mail antigo, de antes desse login -- ignora
            candidatos.append((data_email, msg))
        mail.logout()

        if candidatos:
            candidatos.sort(key=lambda par: par[0])
            _, msg_mais_novo = candidatos[-1]
            body = ""
            if msg_mais_novo.is_multipart():
                for part in msg_mais_novo.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                        break
            else:
                body = msg_mais_novo.get_payload(decode=True).decode("utf-8", errors="ignore")
            # (?<!#) exclui cor CSS hex tipo "color:#424242" -- confirmado ao
            # vivo 23/jul que o e-mail do Mercos e so HTML (sem text/plain) e
            # tem varios "#424242" (cor de texto) ANTES do codigo real no
            # corpo -- regex sem essa exclusao pegava a cor, nao o codigo.
            match = re.search(r"(?<!#)\b(\d{6})\b", body)
            if match:
                print(f"Codigo encontrado (e-mail posterior ao login): {match.group(1)}")
                return match.group(1)

        print(f"  nenhum e-mail novo (posterior a {momento_login.isoformat()}) ainda -- tentando de novo")

    raise RuntimeError(f"Nenhum codigo encontrado apos {tentativas} tentativas")


def resolver_2fa(page: Page, momento_login) -> None:
    codigo = pegar_codigo_2fa(momento_login, aguardar=20)
    inputs = page.locator(SELETOR_INPUT_CODIGO)
    n = inputs.count()
    if n != 6:
        raise RuntimeError(f"Esperava 6 inputs de codigo, achei {n}")
    for i, digito in enumerate(codigo):
        inputs.nth(i).fill(digito)
    page.click('button:has-text("continuar")', timeout=10_000)
    # NAO usar wait_for_url aqui -- a URL de login tem "next=.../guia_inicial/"
    # na query string, entao qualquer glob/substring tipo "**/guia_inicial/**"
    # da falso positivo IMEDIATO sem esperar navegacao de verdade acontecer
    # (confirmado ao vivo 23/jul -- bug real, nao hipotetico).
    page.wait_for_load_state("networkidle", timeout=20_000)
    time.sleep(2)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        login_url = f"https://app.mercos.com/login?next=/{EMPRESA_ID}/guia_inicial/"
        print(f"Indo pra {login_url}")
        page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
        page.fill('[name="usuario"]', MERCOS_EMAIL)
        page.fill('[name="senha"]', MERCOS_SENHA)
        momento_login = datetime.now(timezone.utc)
        page.click('[type="submit"]')
        try:
            page.wait_for_url(lambda url: "login" not in url, timeout=15_000)
        except Exception:
            pass
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
        time.sleep(2)

        os.makedirs("logs", exist_ok=True)
        page.screenshot(path="logs/teste_2fa_01_apos_login.png", full_page=True)

        pediu_2fa = page.locator(SELETOR_INPUT_CODIGO).count() > 0
        print(f"Pediu 2FA: {pediu_2fa}")

        if pediu_2fa:
            resolver_2fa(page, momento_login)
            page.screenshot(path="logs/teste_2fa_02_apos_resolver.png", full_page=True)

        # Checa por elemento real do dashboard (sidebar), nao por URL --
        # a URL do login sempre contem "guia_inicial" na query string
        # (next=...), entao substring/glob nela sempre da falso positivo.
        logado = page.locator("text=INDICADORES").count() > 0
        print(f"URL final: {page.url}")
        print(f"Elemento 'INDICADORES' (sidebar dashboard) presente: {logado}")
        sucesso = logado
        print(f"RESULTADO: {'LOGIN COMPLETO COM SUCESSO' if sucesso else 'FALHOU -- nao chegou no dashboard'}")

        browser.close()
        return 0 if sucesso else 1


if __name__ == "__main__":
    sys.exit(main())
