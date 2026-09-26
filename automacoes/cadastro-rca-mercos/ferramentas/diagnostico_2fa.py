#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostico do canal de 2FA do Mercos -- SO LEITURA de e-mail, nao abre navegador.

Existe por causa de 21/set/2026: os tres robos de Mercos estavam parados ha 3
dias em "Nenhum codigo 2FA encontrado", e a primeira hipotese (secret
GMAIL_USER apontando para a caixa errada, trocado em 18/set 23:10) estava
ERRADA -- a guarda nova mediu 127 e-mails do Mercos na caixa. O que faltava
era enxergar O QUE tem nessa caixa e QUANDO o ultimo codigo chegou, e nao
havia ferramenta para isso: `subir_fotos --modo=test` sobe Chromium e pede
mais um codigo, que e justamente o que nao se quer fazer quando a suspeita e
bloqueio por tentativa repetida.

⚠️ NAO imprime o corpo do e-mail -- o codigo de 2FA esta nele, e log de
Actions fica 90 dias. Sai remetente, assunto e data, que e o que separa as
causas:

  - ultimo CODIGO recente        -> o envio funciona; o problema e a leitura
  - ultimo codigo velho, e depois
    so e-mail comercial          -> o Mercos parou de mandar codigo
  - e-mail de bloqueio/seguranca -> a conta foi travada (tentativa repetida)
  - zero e-mail                  -> caixa errada (ver bug 7 em mercos_login)

Uso:  python ferramentas/diagnostico_2fa.py [--quantos 20]
"""
import argparse
import email as email_lib
import imaplib
import os
import re
import sys
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mercos_login import _pastas_de_busca, _uids_do_mercos  # noqa: E402

# Assunto de e-mail de codigo x qualquer outra coisa que o Mercos manda. So
# para ROTULAR a linha -- a lista sai inteira de qualquer jeito, porque supor
# que "codigo" esta sempre no assunto e o tipo de premissa que criou este
# arquivo.
PADRAO_CODIGO = re.compile(r"c[oó]digo|autentica|verifica|acesso|token", re.I)
PADRAO_ALERTA = re.compile(r"bloque|suspens|seguran|tentativa|desativ|senha", re.I)


def _texto(valor: str) -> str:
    try:
        return str(make_header(decode_header(valor or "")))
    except Exception:
        return valor or ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quantos", type=int, default=20)
    args = ap.parse_args()

    user = os.environ.get("GMAIL_USER", "")
    senha = os.environ.get("GMAIL_SENHA", "")
    mercos_email = os.environ.get("MERCOS_EMAIL", "")
    if not (user and senha):
        raise SystemExit("faltam GMAIL_USER/GMAIL_SENHA no ambiente")

    # A comparacao vale mesmo com o Actions mascarando os dois valores.
    iguais = user.strip().lower() == (mercos_email or "").strip().lower()
    print(f"[diag] GMAIL_USER e MERCOS_EMAIL sao a mesma conta? "
          f"{'SIM' if iguais else 'NAO' if mercos_email else 'MERCOS_EMAIL ausente'}")

    # Qual conta e cada uma? O Actions mascara o VALOR do secret, entao a unica
    # forma de responder e comparar contra candidatos conhecidos e imprimir o
    # veredicto -- que nao e segredo.
    candidatos = [c.strip() for c in os.environ.get(
        "CANDIDATOS",
        "israel.xavier@gobeaute.com.br,italo.costa@gobeaute.com.br,"
        "b2b@gobeaute.com.br,salesops@gobeaute.com.br",
    ).split(",") if c.strip()]
    for i, c in enumerate(candidatos, 1):
        print(f"[diag] candidato #{i}: {c}")
    for rotulo, valor in (("GMAIL_USER", user), ("MERCOS_EMAIL", mercos_email)):
        if not valor:
            print(f"[diag] {rotulo}: ausente no ambiente")
            continue
        # ⚠️ Imprimir o e-mail que casou NAO funciona: ele E o valor do secret, e o
        # Actions o troca por *** -- a 1a versao disto saiu "GMAIL_USER = ***".
        # O INDICE na lista identifica sem repetir o segredo.
        idx = next((i for i, c in enumerate(candidatos, 1)
                    if c.lower() == valor.strip().lower()), None)
        print(f"[diag] {rotulo} = candidato #{idx} da lista" if idx
              else f"[diag] {rotulo}: NAO e nenhum dos {len(candidatos)} candidatos "
                   f"({len(valor)} chars, dominio @{valor.split('@')[-1]})")

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(user, senha)
    try:
        for pasta in _pastas_de_busca(mail):
            uids = _uids_do_mercos(mail, pasta)
            print(f"\n[diag] pasta {pasta}: {len(uids)} e-mail(s) do Mercos")
            if not uids:
                continue
            for uid in uids[-args.quantos:]:
                _, bruto = mail.fetch(
                    uid, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT TO)])")
                cab = email_lib.message_from_bytes(bruto[0][1])
                try:
                    quando = parsedate_to_datetime(cab.get("Date")).isoformat()
                except Exception:
                    quando = cab.get("Date") or "?"
                assunto = _texto(cab.get("Subject"))
                remetente = _texto(cab.get("From"))
                # ⚠️ O destinatario e a prova de PARA QUEM o Mercos mandou -- e o
                # que separa "existe usuario Mercos com esta caixa" de
                # "encaminhamento de outra conta". Nao da pra imprimir o valor
                # (o Actions mascara secret como ***), entao sai a COMPARACAO.
                destino = _texto(cab.get("To")).lower()
                if user.lower() and user.lower() in destino:
                    quem = "To=GMAIL_USER"
                elif mercos_email and mercos_email.lower() in destino:
                    quem = "To=MERCOS_EMAIL"
                elif destino:
                    quem = "To=OUTRA"
                else:
                    quem = "To=?"
                marca = "  "
                if PADRAO_ALERTA.search(assunto):
                    marca = "!!"
                elif PADRAO_CODIGO.search(assunto):
                    marca = "->"
                print(f"  {marca} {quando} | {quem} | {remetente} | {assunto}")
            # "Todos os e-mails" ja contem o INBOX; listar de novo so polui.
            break
    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
