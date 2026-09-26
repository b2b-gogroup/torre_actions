# -*- coding: utf-8 -*-
"""A fila entre o botao "Aprovar e pagar no PIX" e o robo do Mercos.

O lojista clica, o formulario grava uma linha em `gira_orcamento_fila` e dispara
o workflow. Este modulo e o outro lado: le o que esta pendente, devolve o
resultado e enfileira a mensagem de volta para o WhatsApp do cliente.

🔴 POR QUE UMA TABELA, E NAO UMA CHAMADA DIRETA. O robo leva ~2 min (login, 2FA,
Playwright) e o clique nao pode esperar isso com a tela presa. E se o robo
falhar, a intencao do cliente nao pode sumir junto: ela fica aqui, com o erro
escrito, para alguem ver e reprocessar.

⚠️ ESTE MODULO NAO DECIDE NADA DE COMERCIAL. Ele nao escolhe item, nao calcula
desconto e nao inventa numero de telefone -- so transporta o que o formulario ja
registrou. Quem decide preco e a tela do pedido; quem decide o que vira
orcamento e o lojista, clicando.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

TABELA = "gira_orcamento_fila"
OUTBOX = "whatsapp_outbox"
INSTANCIA = os.environ.get("EVOLUTION_INSTANCIA", "RCA")
# Mesmo bucket que a central usa para encarte e PDF.
BUCKET = os.environ.get("SUPABASE_BUCKET_ARQUIVOS", "salesops-arquivos")

# A Evolution, para mandar a IMAGEM do QR direto. Sem estas duas a mensagem sai
# so texto pela fila -- e o link cru ja paga a conta.
EVOLUTION_URL = os.environ.get("EVOLUTION_URL", "")
EVOLUTION_APIKEY = os.environ.get("EVOLUTION_APIKEY", "")

# O texto que volta para o chat. Curto de proposito: a mensagem anterior ja
# explicou o pedido e o desconto -- esta so entrega o link.
TEXTO_LINK = "Segue o link de pagamento do seu pedido:"

# ── Fase de testes: o link NAO vai para o cliente ────────────────────────────
# 🔴 ENQUANTO ISTO ESTIVER LIGADO, NENHUM LOJISTA RECEBE NADA. O link de
# pagamento de um pedido real sai para o Italo e a Mariana, e so para eles.
#
# E o padrao de proposito: o caminho inteiro (botao -> fila -> Mercos -> chat)
# nunca rodou ponta a ponta com cliente de verdade, e a primeira coisa que ele
# manda e uma COBRANCA. Errar o numero aqui e mandar cobranca para a pessoa
# errada -- o tipo de erro que nao tem desfazer.
#
# Para soltar de verdade: ORCAMENTO_NUMEROS_TESTE="" (vazio). Ai cada linha vai
# para o numero do proprio cliente.
#
# ⚠️ SO O NUMERO DO ITALO desde 21/set/2026 (decisao dele). O da Mariana saiu:
# enquanto o caminho esta sendo testado, cada numero a mais na lista e uma
# cobranca a mais saindo para uma pessoa por rodada -- e a mensagem que sai
# daqui e um link de PAGAMENTO, nao um aviso.
NUMEROS_TESTE = [
    n.strip()
    for n in os.environ.get(
        "ORCAMENTO_NUMEROS_TESTE", "5585986160142"
    ).split(",")
    if n.strip()
]

# Quantas vezes uma linha pode falhar antes de parar de ser tentada. Sem teto,
# um CNPJ que o Mercos nunca vai achar seria retentado para sempre, gastando uma
# sessao do Mercos por rodada.
MAX_TENTATIVAS = int(os.environ.get("ORCAMENTO_FILA_MAX_TENTATIVAS", "3"))

# Quantos minutos no futuro agendar a linha COM IMAGEM.
#
# 🔴 É O QUE FAZ A IMAGEM CHEGAR COMO IMAGEM, e o número não é folga: é um
# combinado entre dois consumidores da mesma fila.
#
#   - o worker Python oficial, no servidor, só enxerga `agendado_para <= agora`
#     e ignora `media_url` -- ele manda TEXTO;
#   - `scripts/entregar-midia.ts`, que roda numa máquina dentro da VPN, pega
#     justamente as agendadas para o futuro e chama `sendMedia`.
#
# Agendando para daqui a alguns minutos, a linha fica invisível para o worker
# oficial durante essa janela e o entregador a alcança primeiro. Se o entregador
# estiver parado, nada se perde: passados os minutos, o oficial manda como texto.
#
# Sem isto o worker oficial pega a linha no mesmo segundo e a imagem vira link
# no corpo -- foi o que aconteceu no pedido da Casa Bella em 19/set/2026.
MINUTOS_PARA_A_MIDIA = int(os.environ.get("ORCAMENTO_MINUTOS_MIDIA", "3"))


class FilaIndisponivel(RuntimeError):
    """Sem credencial de Supabase nao ha fila -- e isso precisa ser dito, nao
    virar "nenhum pedido pendente"."""


def _cabecalhos(extra: dict | None = None) -> dict:
    if not (SUPABASE_URL and SUPABASE_KEY):
        raise FilaIndisponivel(
            "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY ausentes -- sem isso nao da "
            "para ler a fila nem devolver o link para o cliente."
        )
    base = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    base.update(extra or {})
    return base


def _pedir(metodo: str, caminho: str, corpo=None, cabecalhos: dict | None = None):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{caminho}",
        method=metodo,
        headers=_cabecalhos(cabecalhos),
        data=json.dumps(corpo).encode("utf-8") if corpo is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            texto = r.read().decode("utf-8")
            return json.loads(texto) if texto.strip() else None
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Supabase {e.code} em {metodo} {caminho}: {detalhe}") from e


def pendentes(limite: int = 5) -> list[dict]:
    """O que esta esperando orcamento, mais antigo primeiro.

    Inclui as linhas em `erro` que ainda tem tentativa sobrando: falha de
    sessao do Mercos e comum e temporaria, e desistir na primeira deixaria o
    cliente sem link por um motivo que se resolve sozinho na proxima rodada.
    """
    filtro = (
        f"{TABELA}?select=*"
        f"&or=(status.eq.pendente,and(status.eq.erro,tentativas.lt.{MAX_TENTATIVAS}))"
        f"&order=criado_em.asc&limit={int(limite)}"
    )
    return _pedir("GET", filtro) or []


def marcar_rodando(linha: dict, run_url: str = "") -> None:
    _pedir(
        "PATCH",
        f"{TABELA}?id=eq.{linha['id']}",
        {
            "status": "rodando",
            "tentativas": int(linha.get("tentativas") or 0) + 1,
            "atualizado_em": "now()",
            **({"run_url": run_url} if run_url else {}),
        },
    )


def marcar_pronto(linha: dict, orcamento: str, link: str) -> None:
    _pedir(
        "PATCH",
        f"{TABELA}?id=eq.{linha['id']}",
        {
            "status": "pronto",
            "orcamento_numero": orcamento or None,
            "link_pagamento": link or None,
            "erro": None,
            "atualizado_em": "now()",
        },
    )


def marcar_erro(linha: dict, motivo: str) -> None:
    _pedir(
        "PATCH",
        f"{TABELA}?id=eq.{linha['id']}",
        {"status": "erro", "erro": (motivo or "")[:500], "atualizado_em": "now()"},
    )


def publicar_qrcode(caminho: str, log=print) -> str:
    """Sobe o PNG do QR para o bucket e devolve a URL publica.

    O robo fotografa o QR a cada rodada, mas o arquivo nascia e morria no
    runner: a mensagem saia so com o link, e o WhatsApp montava uma previa
    generica da pagina do Mercos. Para o QR chegar como IMAGEM ele precisa de
    uma URL que o WhatsApp alcance.

    ⚠️ O QR e o link viram publicos ao subir aqui -- mas isso ja e verdade do
    link de pagamento, que e publico por natureza (o cliente abre sem login). O
    nome do arquivo carrega o numero do orcamento, nao o do cliente.
    """
    if not caminho or not os.path.exists(caminho):
        return ""
    try:
        with open(caminho, "rb") as f:
            dados = f.read()
        destino = f"gira/qrcode/{os.path.basename(caminho)}"
        req = urllib.request.Request(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{destino}",
            method="POST",
            data=dados,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "image/png",
                "x-upsert": "true",
            },
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
        url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{destino}"
        log(f"   [fila] QR publicado: {url}")
        return url
    except Exception as e:  # noqa: BLE001
        # Sem QR a mensagem ainda leva o link, que e o que paga a conta.
        log(f"   [fila] nao consegui publicar o QR ({e}) -- a mensagem vai so com o link")
        return ""


def _enviar_media_direto(numero: str, legenda: str, media_url: str, log=print) -> str:
    """Manda a imagem pela Evolution AGORA, sem passar pelo worker do servidor.

    🔴 POR QUE FURAR A FILA SO PARA A IMAGEM. A `whatsapp_outbox` e o caminho
    normal e continua sendo -- mas o worker Python que a consome ainda nao le
    `media_url` (migration de 18/set/2026: colunas primeiro, worker depois).
    Enquanto ele nao aprender, imagem enfileirada NAO VIRA IMAGEM: sai texto, sem
    erro nenhum. Foi o que aconteceu duas vezes hoje -- a URL do QR chegou como
    link cru, e a primeira tentativa (previa do WhatsApp) nem previa gerou.

    Entao aqui o robo chama `POST /message/sendMedia/{instancia}` direto, com o
    payload que o doc 08 registrou funcionando (mediatype/media/caption).

    ⚠️ ISSO PERDE A RETENTATIVA DA FILA. O worker tenta 3 vezes; esta chamada e
    uma so. Por isso o retorno e checado e, quando falha, quem chama volta para
    a fila em texto -- link cru chega, e link cru paga.

    Devolve o id da Evolution, ou "" se nao enviou.
    """
    if not (EVOLUTION_URL and EVOLUTION_APIKEY):
        return ""
    corpo = json.dumps({
        "number": numero,
        "mediatype": "image",
        "media": media_url,
        "caption": legenda,
        "fileName": "qrcode-pix.png",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{EVOLUTION_URL.rstrip('/')}/message/sendMedia/{INSTANCIA}",
        method="POST",
        data=corpo,
        headers={"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"},
    )
    try:
        # ⚠️ 8s, NÃO 60. A Evolution vive em 100.78.71.58 -- faixa CGNAT do
        # Tailscale -- e só quem está na VPN a alcança. Do runner do GitHub a
        # conexão não é recusada, ela PENDURA: dois destinos × 60s viraram dois
        # minutos de rodada esperando o impossível (19/set/2026 04:30). Curto o
        # bastante para funcionar na VPN, curto o bastante para desistir rápido
        # fora dela.
        with urllib.request.urlopen(req, timeout=8) as r:
            resposta = json.loads(r.read().decode("utf-8") or "{}")
        ident = str((resposta.get("key") or {}).get("id") or "")
        log(f"   [fila] QR enviado como imagem para {numero} (evolution {ident or 'sem id'})")
        return ident or "enviado"
    except Exception as e:  # noqa: BLE001
        detalhe = ""
        if isinstance(e, urllib.error.HTTPError):
            detalhe = e.read().decode("utf-8", errors="replace")[:200]
        # ⚠️ "RECUSOU" ERA A PALAVRA ERRADA e assustou quem leu o log: no runner
        # do GitHub isto NAO e falha, e o caminho normal. A Evolution vive numa
        # faixa CGNAT do Tailscale e so existe dentro da VPN; daqui a conexao
        # simplesmente estoura o tempo. A imagem sai pela fila, com o entregador
        # da VPN -- e sai como IMAGEM, conferido em 19/set/2026 04:46.
        fora_da_vpn = "timed out" in str(e).lower() or "timeout" in str(e).lower()
        if fora_da_vpn:
            log("   [fila] Evolution inalcancavel daqui (fora da VPN Tailscale) -- "
                "normal no Actions; a imagem vai pela fila, pelo entregador")
        else:
            log(f"   [fila] Evolution recusou a imagem ({e}) {detalhe} -- vai pela fila")
        return ""


def numero_do_cnpj(cnpj: str) -> str:
    """O WhatsApp do cliente a partir do CNPJ, em `gira_cliente_mv`.

    ⚠️ MESMA FONTE QUE A CAMPANHA USA PARA DISPARAR. Se fosse outra, o link de
    pagamento sairia por um numero diferente do que mandou a oferta -- e para o
    lojista seriam dois remetentes falando do mesmo pedido.

    Existe porque o formulario NAO recebe telefone junto com o pedido: a central
    manda cnpj, itens e valores, e o numero nunca entrou nesse caminho. Enquanto
    nao entrar, o cadastro e a resposta.
    """
    digitos = "".join(c for c in (cnpj or "") if c.isdigit())
    if len(digitos) != 14:
        return ""
    try:
        linhas = _pedir("GET", f"gira_cliente_mv?select=telefone&cnpj=eq.{digitos}&limit=1") or []
    except Exception:  # noqa: BLE001
        return ""
    if not linhas:
        return ""
    return "".join(c for c in str(linhas[0].get("telefone") or "") if c.isdigit())


def avisar_cliente(linha: dict, link: str, qr_url: str = "", log=print) -> bool:
    """Poe "Segue o link de pagamento do seu pedido: <link>" na fila do WhatsApp.

    🔴 SEM LINK NAO SAI MENSAGEM. Mandar o texto sem a URL seria pior que o
    silencio: o lojista abre o WhatsApp, le que o link chegou e nao tem link.

    ⚠️ Devolve se ENFILEIROU, nunca se entregou. Quem entrega e o worker Python
    do servidor, que le a `whatsapp_outbox` e chama a Evolution -- este robo nao
    tem como saber se o WhatsApp aceitou.
    """
    if not link:
        log("   [fila] sem link -- nao enfileiro mensagem sem a URL dentro")
        return False

    numero = "".join(c for c in str(linha.get("numero_whatsapp") or "") if c.isdigit())
    if not numero:
        numero = numero_do_cnpj(str(linha.get("cnpj") or ""))
        if numero:
            log(f"   [fila] numero veio do cadastro (gira_cliente_mv): {numero}")

    # 🔴 O NUMERO DA LINHA VENCE A LISTA DE TESTE, quando existe.
    #
    # A linha so tem numero quando a campanha disse para onde foi -- e em fase de
    # teste esse numero JA E um numero de teste, o que o operador digitou na
    # tela. Sobrepor com a lista fixa mandava a oferta para um lugar e a cobranca
    # para outro: em 19/set/2026 a amostra foi para 8592288605 e o link de
    # pagamento chegou no Italo.
    #
    # ⚠️ A lista fixa continua valendo quando a linha NAO tem numero -- pedido
    # que veio sem destino conhecido nao vira cobranca para o cliente por
    # descuido.
    if numero and linha.get("numero_whatsapp"):
        log(f"   [fila] o link volta para quem recebeu a campanha: {numero}")
        destinos = [numero]
    elif NUMEROS_TESTE:
        # O numero do cliente vai na REFERENCIA, nao no destino: fica o rastro
        # de para quem isto iria na vida real, sem sair para ele.
        log(f"   [fila] FASE DE TESTES: o link vai para {', '.join(NUMEROS_TESTE)} "
            f"-- o cliente ({numero or 'sem numero'}) NAO recebe")
        destinos = list(NUMEROS_TESTE)
    elif numero:
        destinos = [numero]
    else:
        log("   [fila] sem numero na linha nem no cadastro -- ninguem a ser avisado")
        return False

    texto = f"{TEXTO_LINK} {link}"

    # 1º A IMAGEM, DIRETO PELA EVOLUTION. Enfileirar `media_url` nao basta: o
    # worker do servidor ainda nao le esse campo, entao a imagem vira texto sem
    # erro nenhum. Hoje isso ja falhou duas vezes -- previa do WhatsApp (nao
    # gerou) e link cru (chegou como link). Veja `_enviar_media_direto`.
    enviados: list[str] = []
    if qr_url:
        for destino in destinos:
            if _enviar_media_direto(destino, texto, qr_url, log=log):
                enviados.append(destino)

    faltam = [d for d in destinos if d not in enviados]
    if not faltam:
        return True

    # 2º O QUE A IMAGEM NAO COBRIU vai pela fila, em texto. A URL do QR entra no
    # corpo como ultimo recurso: nao vira imagem, mas fica clicavel -- e o link
    # de pagamento, que e o que importa, chega de qualquer jeito.
    agendamento = (
        datetime.now(timezone.utc) + timedelta(minutes=MINUTOS_PARA_A_MIDIA)
    ).isoformat() if qr_url else None

    corpo = f"{qr_url}\n\n{texto}" if qr_url else texto

    _pedir(
        "POST",
        OUTBOX,
        [
            {
                "numero": destino,
                "mensagem": corpo,
                **({"media_url": qr_url, "media_tipo": "image",
                    "media_nome": "qrcode-pix.png",
                    "agendado_para": agendamento} if qr_url else {}),
                "origem": "gira-orcamento",
                "referencia": {
                    "tela": "formulario",
                    "acao": "pagar-no-pix",
                    "token": linha.get("token"),
                    "orcamento": linha.get("orcamento_numero"),
                    "cnpj": linha.get("cnpj"),
                    **({"teste": True, "numero_real": numero or None} if NUMEROS_TESTE else {}),
                },
                "instancia": INSTANCIA,
            }
            for destino in faltam
        ],
        {"Prefer": "return=minimal"},
    )
    log(f"   [fila] mensagem do link enfileirada para {', '.join(faltam)}")
    return True


def itens_da_linha(linha: dict) -> list[tuple[str, int]]:
    """`itens` chega como [{"sku": "BB02027", "quantidade": 12}, ...].

    ⚠️ Quantidade invalida NAO vira 1 nem e descartada: levanta. Um orcamento
    com quantidade diferente da que o cliente aprovou e outro pedido.
    """
    bruto = linha.get("itens")
    if isinstance(bruto, str):
        bruto = json.loads(bruto)
    saida: list[tuple[str, int]] = []
    for item in bruto or []:
        sku = str(item.get("sku") or item.get("codigo") or "").strip()
        qtd = item.get("quantidade", item.get("qtd"))
        if not sku:
            raise ValueError(f"item sem sku na fila: {item!r}")
        try:
            qtd = int(qtd)
        except (TypeError, ValueError):
            raise ValueError(f"quantidade invalida para {sku}: {qtd!r}") from None
        if qtd <= 0:
            raise ValueError(f"quantidade invalida para {sku}: {qtd}")
        saida.append((sku, qtd))
    if not saida:
        raise ValueError("a linha da fila nao tem item nenhum")
    return saida
