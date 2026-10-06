#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sobe fotos de produto pra tela nativa "Importar fotos" do Mercos
(Produtos -> Importar fotos), reaproveitando o mesmo login+2FA de
automacoes/cadastro-rca-mercos/mercos_login.py.

Fonte: dim_produto_foto (Torre, sincronizada 1x/dia do card Metabase 28639 "Stg Shopify
Products" por etl-produto-fotos.yml -- até 04/set/2026 era a planilha "Produtos_Consolidado",
ver histórico em etl/produto-fotos/index.ts), filtrada por DOIS lados antes de decidir o
que enviar:
  1. Só extensão que o Pillow consegue abrir e converter (JPG/JPEG/GIF/PNG/WEBP/AVIF
     -- 06/out/2026: achadas 6 fotos reais em dim_produto_foto só em .webp/.avif, que
     o filtro antigo rejeitava ANTES de baixar mesmo com baixar_e_redimensionar já
     convertendo qualquer formato que o Pillow abra, não só os 4 originais). Vídeo
     continua de fora.
  2. Só SKU que a LISTA AO VIVO daquela empresa mostra SEM foto (pedido do usuário,
     28/ago/2026) -- varre a lista de produtos (não usa cache nosso) e olha o `src` da
     imagem principal de cada linha: se aponta pro placeholder `sem_imagem.jpg`, o
     produto não tem foto de verdade; senão, já tem e é PULADO (todas as ordens daquele
     SKU, não só a 1ª -- ver docstring de varrer_skus_sem_foto_mercos). Essa varredura é
     também a PROVA de que o SKU existe naquela empresa.

⚠️ Havia um 3º filtro, `sku in mercos_cadastro_mestre`, REMOVIDO em 22/set/2026: aquela
tabela não é o Mercos, é uma planilha Google replicada por etl/mercos-sheets, e com ela
fresca foi medido 0 SKU `AP` e 0 `YE` (a Ápice entra lá com o código numérico do Tiny).
Resultado: dos 378 SKUs sem foto no Mercos ES, 213 já tinham foto pronta e NENHUM passava
o filtro -- o robô rodava verde e enviava zero. Ver buscar_fotos_validas.

Cada foto candidata é baixada, redimensionada pra no máximo 800x800 (recomendação
do próprio Mercos) e reconvertida pra JPEG (achata transparência em fundo branco)
-- resolve tamanho (fotos da Shopify passam de 2MB) e formato de uma vez só.

Nome do arquivo segue o padrão EXATO que o Mercos exige pra associar automaticamente:
  ordem 1        -> {SKU}.jpg
  ordem 2, 3...  -> {SKU}(2).jpg, {SKU}(3).jpg...

Modos (mesmo padrão test/dry/aplicar já usado nos outros robôs do Mercos deste repo):
  test    -- loga, confirma acesso à tela de Importar fotos. Não toca no banco,
             não varre a lista de produtos, não baixa nem envia nada.
  dry     -- varre o Mercos + lê o banco + baixa e redimensiona (pra medir tamanho
             real) + mostra o que enviaria. NÃO envia.
  aplicar -- envia de verdade.

--filiais es,rj (18/set/2026): CSV opcional -- omitido/vazio = SÓ ES (default,
comportamento original). ⚠️ Até 22/set/2026 RJ nem completava a varredura (coluna
de código/imagem em posição diferente de ES -- ver achado na mesma data logo
abaixo, em varrer_skus_sem_foto_mercos); corrigido, e `aplicar` validado ponta a
ponta com --limit=1 no mesmo dia (SKU AP01002, conferido por screenshot real do
Mercos). São DUAS empresas
Mercos com o MESMO login/senha (confirmado pelo usuário) -- mesmo padrão já usado
em automacoes/cadastro-rca-mercos/escritor.py (EMPRESAS + --filial, lá é singular
porque cada rodada daquele robô é 1 CNPJ por vez). O catálogo de SKU
(mercos_cadastro_mestre) é global, não por região -- o que muda por empresa é só
quais SKUs estão cadastrados lá e quais já têm foto, resolvido pela varredura ao
vivo.

⚠️ **NÃO rode `es,rj` (nem `es` e `rj` em disparos próximos) na prática** --
tentado no mesmo dia (18/set/2026) e falhou nas duas vezes: ES conseguia logar e
caía com `net::ERR_ABORTED` navegando pra produtos, RJ logava e a sessão "caía"
(voltava pro login) segundos depois. `motivo_sessao()` apontou a causa: o Mercos
só aceita **1 sessão por usuário**, e MERCOS_EMAIL é a MESMA credencial usada por
esta automação, pelo robô `cadastro-rca-mercos`, pelo robô `credito-limite-mercos`
(cron 19h30 BRT) e por qualquer pessoa conferindo manualmente no navegador --
dois logins próximos (nossos ou de outra origem) se derrubam. A saída adotada é
**workflow SEPARADO por horário**, não `--filiais` combinado: ver
`.github/workflows/mercos-fotos.yml` (ES, 20h22 BRT) e `mercos-fotos-rj.yml`
(RJ, horário diferente) -- o parâmetro `--filiais` continua existindo pra rodar
manualmente uma filial específica, só não pra combinar as duas numa execução só.

--forcar-marca / --forcar-skus (14/set/2026, pedido do usuário -- fotos da Kokeshi
presas em SKU antigo no Mercos): a guarda (3) acima só cobre SKU SEM foto nenhuma --
um SKU que já tem foto ERRADA (não é o placeholder) é PULADO pra sempre pelo fluxo
normal, porque pra ele o Mercos não devolve `sem_imagem.jpg`. Essas duas flags furam
essa guarda DE PROPÓSITO, só pros SKUs explicitamente listados (marca_id de
`dim_produto`, ex. "KS", e/ou lista exata de SKUs) -- o resto do comportamento continua
igual (extensão aceita; e desde 22/set/2026 o SKU forçado precisa APARECER na lista ao
vivo daquela empresa, senão é descartado nomeando o código: enviar arquivo de SKU que a
empresa não lista faz o Mercos ignorá-lo em silêncio e o log diria "enviei N fotos").
⚠️ Validado ponta a ponta em 18/set/2026 (empresa ES, 3 SKUs SEM foto nenhuma antes --
login+2FA, download, redimensão, upload, confirmado no Mercos). O que CONTINUA sem
validar é o caso de REESCREVER um SKU que já tem foto ERRADA via --forcar-* -- os 3
primeiros não passaram por essa guarda. Rodar SEMPRE test -> dry -> aplicar com
--limit=1 num único SKU conhecido -> conferir manualmente no Mercos antes de soltar
o lote inteiro, principalmente com --forcar-marca/--forcar-skus.

Uso (env vars já usadas pelos outros robôs do Mercos neste repo -- nenhum secret novo):
  MERCOS_EMAIL=... MERCOS_SENHA=... GMAIL_USER=... GMAIL_SENHA=... \\
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python subir_fotos.py --modo=test
    python subir_fotos.py --modo=dry --limit=10
    python subir_fotos.py --modo=aplicar --limit=10
    python subir_fotos.py --modo=aplicar
    python subir_fotos.py --modo=dry --forcar-marca=KS
    python subir_fotos.py --modo=aplicar --forcar-marca=KS --limit=1
    python subir_fotos.py --modo=aplicar --forcar-skus=KS03043,KS03042
    python subir_fotos.py --modo=dry                # ES (default -- NÃO combine com rj, ver aviso acima)
    python subir_fotos.py --modo=dry --filiais=rj    # RJ isolado (via mercos-fotos-rj.yml)
"""
import argparse
import io
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cadastro-rca-mercos"))
from mercos_login import login_mercos, conferir_credencial_gmail  # noqa: E402
from mercos_ui import sessao_caiu, motivo_sessao, SessaoCaiu  # noqa: E402

import requests
from PIL import Image
from playwright.sync_api import sync_playwright
from supabase import create_client

# Registra o decoder AVIF no Pillow (nao vem embutido no pacote base). Guardado em
# try/except porque e so mais um formato a mais -- se o plugin faltar/quebrar num
# ambiente, webp (nativo do Pillow 10+) continua funcionando sozinho; nao vale travar
# o robo inteiro por causa de um plugin de formato que o Shopify raramente entrega de
# verdade (testado 06/out/2026: as 6 URLs .webp/.avif reais vieram como JPEG/PNG na
# pratica -- o Shopify so serve AVIF/WEBP de fato pra quem pede via header Accept, que
# o requests.get() daqui nao manda -- mas o plugin fica de garantia pro dia em que vier).
try:
    import pillow_avif  # noqa: F401
except Exception as _e:  # pragma: no cover
    print(f"[mercos-fotos] aviso: plugin AVIF do Pillow nao carregou ({_e}) -- "
          f".avif real falharia na conversao, .webp continua OK (suporte nativo)")

MERCOS_EMAIL = os.environ["MERCOS_EMAIL"]
MERCOS_SENHA = os.environ["MERCOS_SENHA"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_SENHA = os.environ["GMAIL_SENHA"]
# Supabase só é necessário pra dry/aplicar (precisa ler dim_produto_foto) -- modo=test só
# confere login + acesso à tela, sem tocar no banco, então não exige a env var.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
EMPRESA_ID_ES = "424525"
EMPRESA_ID_RJ = "424524"
# Mesmo login/senha pras duas empresas (confirmado pelo usuário 18/set/2026) e mesmo
# padrão já usado em automacoes/cadastro-rca-mercos/escritor.py (EMPRESAS + --filial).
# mercos_cadastro_mestre NÃO tem coluna de região -- é o catálogo SKU↔EAN↔marca
# compartilhado, o mesmo produto físico independe de em qual empresa Mercos ele está
# cadastrado; o que muda por empresa é só QUAIS SKUs existem lá e quais já têm foto,
# e isso a varredura ao vivo (varrer_skus_sem_foto_mercos) já resolve por empresa_id.
EMPRESAS = {"es": EMPRESA_ID_ES, "rj": EMPRESA_ID_RJ}

EXT_IMAGEM_OK = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif")
TAMANHO_MAX = 800
BATCH = int(os.environ.get("MERCOS_FOTOS_BATCH", "30"))
ESPERA_POR_ARQ_MS = int(os.environ.get("MERCOS_FOTOS_ESPERA_ARQ_MS", "2000"))
# Quanto tempo a REDE precisa ficar sem nenhum upload em voo pra considerar o lote gravado.
# ⚠️ Medido em 23/set/2026 escutando `page.on(request/response)`: o Dropzone do Mercos
# envia UM POST POR ARQUIVO, em SÉRIE, ~0,5s cada -- 30 arquivos = 31 POSTs e o último
# RESPONSE a 11,3s do `set_input_files`. Não são os 180s que a graça por tempo supunha.
SILENCIO_REDE_MS = int(os.environ.get("MERCOS_FOTOS_SILENCIO_MS", "8000"))
URL_UPLOAD = "importacao_de_fotos"
# 🔴 A GRAÇA É O QUE FAZ A FOTO GRAVAR, e foi o último defeito da cadeia de 23/set/2026.
#
# A tela do Mercos resolve o lote em ~4s (Status vira `done` em todas as linhas) e o
# servidor segue GRAVANDO por minutos depois disso. Tanto o `set_input_files` do lote
# seguinte quanto recarregar a tela INTERROMPEM o que ainda está sendo gravado -- e o que
# foi interrompido não vira foto, sem erro nenhum, com a tela ainda dizendo `done`.
#
# É isso que explica os dois padrões que mediram lote a lote, e que pareciam "teto do
# Mercos" até a graça ser testada:
#   sem recarga entre lotes:  44/0/0/0  -- o lote 1 gravou tudo porque ganhou de presente
#                                          o tempo dos lotes seguintes; os outros foram
#                                          cortados pelo set_input_files de cima
#   com recarga entre lotes:  10/13/14/3 -- cada um cortado pela própria recarga, ~25%
#   1 lote de 30 + 180s de graça: 28/28 SKUs, 100% (556 -> 584 na varredura seguinte)
#
# 6s por arquivo é o que foi medido funcionando (180s para 30). BATCH caiu de 50 para 30
# pelo mesmo motivo: lote grande deixa muita gravação pendurada de uma vez.
PLACEHOLDER_SEM_FOTO = "sem_imagem.jpg"
MAX_PAGINAS_SEGURANCA = 500  # trava de segurança -- ~1.018 SKUs / 15 por página = ~68 páginas
# Convenção de SKU da Torre inteira: 2 letras maiúsculas (sigla de dim_marca.id -- AP, AU,
# BB, BS, DV, KS, LC, RT, YE) + dígitos (AP01138, KS03043...). Usado pra achar a coluna do
# CÓDIGO na tabela do Mercos por CONTEÚDO, não por posição -- ver achado 22/set/2026 logo
# abaixo, em varrer_skus_sem_foto_mercos.
SKU_RE = re.compile(r"^[A-Z]{2}\d{2,}")
# Status de cada linha na tela "Importar fotos", medido ao vivo em 23/set/2026: o Mercos
# usa ícones do Material (o texto da ligadura vaza no inner_text), e a linha sai como
#   '\tdone\t Yenzah Amino Whey Condicionador 240ml - YE01001\t |  | YE01001.jpg |  | \t'
#   '\twarning\t\t |  | AP01004.jpg |  | \t'          <- recebeu o arquivo e NÃO vinculou
# `done` é a ÚNICA prova de vínculo; qualquer outra coisa é foto que não entrou.
STATUS_VINCULADO = "done"
STATUS_RESOLVIDO = ("done", "warning", "error")
RE_NOME_ARQ = re.compile(r"[A-Za-z0-9()_.-]+\.(?:jpg|jpeg|png|gif)", re.I)


def status_da_linha(texto: str) -> str:
    """Primeiro token não vazio da linha = o status. Vazio = ainda processando."""
    for parte in texto.replace("|", "\t").split("\t"):
        parte = parte.strip()
        if parte:
            return parte.split()[0].lower()
    return ""

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--modo", choices=["test", "dry", "aplicar"], default="dry")
    p.add_argument(
        "--filiais",
        default=None,
        help="CSV de empresas Mercos (ex.: es,rj ou só rj) -- vazio/omitido = SÓ ES "
             "(default original). NÃO combine es+rj numa mesma execução -- mesma conta "
             "Mercos, 1 sessão só, os dois logins se derrubam (achado 18/set/2026).",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--forcar-marca",
        default=None,
        help="CSV de marca_id (ex.: KS) -- reenvia mesmo SKU que já tem foto no Mercos (fura a guarda 3).",
    )
    p.add_argument(
        "--forcar-skus",
        default=None,
        help="CSV de SKUs exatos -- mesma furada de guarda acima, só pros SKUs listados.",
    )
    return p.parse_args()


def extensao_da_url(url: str) -> str:
    caminho = url.split("?")[0]
    return Path(caminho).suffix.lower()


def buscar_skus_forcados(sb, forcar_marca: str | None, forcar_skus: str | None) -> set:
    """Une --forcar-marca (via dim_produto.marca_id) e --forcar-skus (lista exata) num só
    conjunto de SKUs que devem ser reenviados MESMO já tendo foto no Mercos. `dim_produto_foto`
    não guarda marca, por isso o lookup à parte."""
    skus = set()
    if forcar_skus:
        skus |= {s.strip() for s in forcar_skus.split(",") if s.strip()}
    if forcar_marca:
        marcas = {m.strip().upper() for m in forcar_marca.split(",") if m.strip()}
        frm = 0
        PAGE = 1000
        while True:
            res = (
                sb.table("dim_produto")
                .select("sku, marca_id")
                .in_("marca_id", list(marcas))
                .range(frm, frm + PAGE - 1)
                .execute()
            )
            rows = res.data or []
            skus |= {r["sku"] for r in rows}
            if len(rows) < PAGE:
                break
            frm += PAGE
    return skus


def buscar_fotos_validas(sb):
    """dim_produto_foto filtrado SÓ por extensão aceita. Devolve também o conjunto de SKUs
    de `mercos_cadastro_mestre`, que a partir de 22/set/2026 é **informativo**, não gate.

    ⚠️ Até 22/set/2026 havia aqui um segundo filtro, `sku in mercos_cadastro_mestre`
    (pedido de 28/ago/2026, "não enviar foto de produto que não existe no Mercos"). A
    intenção estava certa e a FONTE estava errada: `mercos_cadastro_mestre` NÃO é o
    Mercos -- é uma planilha Google replicada por `etl/mercos-sheets`. Medido em
    22/set/2026, com a planilha fresca (10 min): ela tem **0 SKU `AP` e 0 `YE`**, porque
    a Ápice entra ali com o código numérico do Tiny (`20588`, 205 linhas) e a Yenzah não
    entra. A lista AO VIVO do Mercos, essa sim, mostra `AP01002` e `YE01001`.

    Efeito medido do gate: dos 378 SKUs sem foto no Mercos ES, **213 já tinham foto
    pronta** em `dim_produto_foto` e **0** passavam pelo gate (só 21 dos 378 estavam na
    planilha, e nenhum desses 21 tinha foto). O robô rodava verde e enviava ZERO, todo
    dia, desde 28/ago/2026. 212 dos 213 eram justamente AP e YE.

    A prova de "existe no Mercos" que vale é `vistos`, devolvido por
    `varrer_skus_sem_foto_mercos` -- lido da própria tela de produtos daquela empresa.
    Prova ao vivo ganha de planilha de terceiro (mesma doutrina de
    "não achei evidência ≠ não existe" do resto do projeto).
    """
    fotos = []
    frm = 0
    PAGE = 1000
    while True:
        res = (
            sb.table("dim_produto_foto")
            .select("sku, ordem, url")
            .order("sku")
            .order("ordem")
            .range(frm, frm + PAGE - 1)
            .execute()
        )
        rows = res.data or []
        fotos.extend(rows)
        if len(rows) < PAGE:
            break
        frm += PAGE

    skus_mercos = set()
    frm = 0
    while True:
        res = sb.table("mercos_cadastro_mestre").select("sku").range(frm, frm + PAGE - 1).execute()
        rows = res.data or []
        for r in rows:
            skus_mercos.add(r["sku"])
        if len(rows) < PAGE:
            break
        frm += PAGE

    validas = [f for f in fotos if extensao_da_url(f["url"]) in EXT_IMAGEM_OK]
    return validas, len(fotos), skus_mercos


def nome_mercos(sku: str, ordem: int) -> str:
    return f"{sku}.jpg" if ordem == 1 else f"{sku}({ordem}).jpg"


TENTATIVAS_NAVEGACAO = 4          # 1 tentativa inicial + 3 retentativas
ESPERAS_ENTRE_TENTATIVAS_S = (3, 8, 15)  # backoff crescente -- só entre tentativas, sessão viva


TEXTO_SEM_PRODUTO = "Não foi encontrado nenhum produto"


def navegar_confirmando(page, url: str, seletor_espera, descricao: str, log=print):
    """`page.goto` + confere que a página REAL carregou (não a de login) antes de seguir.

    `seletor_espera` aceita uma string CSS simples OU um `Locator` já combinado (ex.:
    `a.or_(b)`) -- a lista de produtos precisa de dois estados válidos, não um.

    Mesmo padrão de automacoes/cadastro-rca-mercos/mercos_ui.py::buscar_cliente_id_por_cnpj:
    um timeout aqui quase sempre é SESSÃO DERRUBADA (o Mercos serve a tela de login, que
    carrega rápido e nunca tem o seletor esperado), não página lenta -- achado real 28/ago/2026,
    numa run em GitHub Actions onde o primeiro `page.goto` pós-login devolveu 0 linhas de
    produto porque a navegação nunca chegou na lista de verdade.

    ⚠️ 2º achado real, mesmo dia: o que parecia "lentidão/rate-limit" numa run seguinte
    (retentativa esgotada, 4 tentativas, sessão viva) era outra coisa -- a página SEM
    NENHUM produto (fim real da paginação) mostra só o texto "Não foi encontrado nenhum
    produto", SEM `<table>` nenhuma. Esperar `table thead th` nunca ia resolver ali por
    retry nenhum, porque a tabela genuinamente não existe nesse estado -- confirmado por
    screenshot real do usuário. Por isso quem chama pra lista de produtos passa um Locator
    que aceita OS DOIS estados (tabela OU esse texto).

    Sessão morta é fim de linha na hora (levanta `SessaoCaiu`, sem gastar retentativa).
    """
    # ⚠️ 3º achado real, 18/set/2026 (run rj): `page.goto()` vivia FORA deste try/except --
    # um `net::ERR_ABORTED` nele (sessão caindo NO MEIO da navegação, não só na espera do
    # seletor) matava a função na 1ª tentativa, sem retry, sem sessao_caiu() e sem o
    # diagnóstico de motivo_sessao() -- o traceback subia cru até processar_filial(). Agora
    # o goto entra no mesmo try: falha nele cai no mesmo tratamento (sessao_caiu -> SessaoCaiu
    # com motivo; senão, backoff e tenta de novo).
    alvo = page.locator(seletor_espera) if isinstance(seletor_espera, str) else seletor_espera
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS_NAVEGACAO + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            alvo.first.wait_for(state="visible", timeout=20_000)
            return
        except Exception as e:
            ultimo_erro = e
        if sessao_caiu(page):
            # motivo_sessao distingue "conta usada em outro dispositivo/aba" (Mercos só
            # aceita 1 sessão por usuário -- MERCOS_EMAIL é compartilhado entre filiais
            # E com quem conferir manualmente no navegador) de "caiu por outro motivo",
            # que pedem ação diferente. Achado 18/set/2026: o erro genérico daqui não
            # dizia qual dos dois era, e a URL onde REALMENTE paramos ajuda a diagnosticar
            # sem precisar baixar screenshot nenhum.
            raise SessaoCaiu(
                f"{motivo_sessao(page, MERCOS_EMAIL)} -- tentando {descricao} ({url}), "
                f"parei em {page.url}"
            )
        if tentativa < TENTATIVAS_NAVEGACAO:
            espera = ESPERAS_ENTRE_TENTATIVAS_S[min(tentativa - 1, len(ESPERAS_ENTRE_TENTATIVAS_S) - 1)]
            log(
                f"  [aviso] {descricao} falhou ({type(ultimo_erro).__name__}: sessão viva), "
                f"tentativa {tentativa}/{TENTATIVAS_NAVEGACAO} — esperando {espera}s e tentando de novo..."
            )
            # ⚠️ Sem isto, a ÚNICA foto que sobra é a do fim -- e o fim é sempre a tela de
            # login, que só diz "a sessão morreu em algum momento". O estado que explica é
            # ESTE: sessão viva, numa página que não é a esperada. Em 21/set/2026 os runs
            # do ES morreram duas vezes aqui e o diagnóstico virou adivinhação por falta
            # desta foto. Título/URL vão pro log porque artifact expira em 90 dias e log
            # também, mas o log é lido sem baixar nada.
            try:
                log(f"  [diag] url agora: {page.url}")
                log(f"  [diag] título: {page.title()[:120]!r}")
                corpo = page.locator("body").inner_text(timeout=5_000)
                primeiras = " | ".join(
                    l.strip() for l in corpo.splitlines() if l.strip()
                )[:300]
                log(f"  [diag] texto da tela: {primeiras!r}")
                page.screenshot(
                    path=str(LOGS_DIR / f"nao_carregou_{tentativa}.png"), full_page=True
                )
            except Exception as e:
                log(f"  [diag] não consegui fotografar a tela: {type(e).__name__}: {e}")
            time.sleep(espera)
    raise RuntimeError(
        f"{descricao} não carregou em {TENTATIVAS_NAVEGACAO} tentativas ({url}) e a sessão "
        f"está viva -- pode ser lentidão do Mercos ou a tela mudou ({ultimo_erro})"
    )


def varrer_skus_sem_foto_mercos(page, empresa_id: str) -> tuple[set, set]:
    """Varre TODA a lista de produtos do Mercos (paginação `?p=N&status=1`, 15/página,
    descoberta em 28/ago/2026) e devolve DOIS conjuntos:
      - `sem_foto`: SKUs cuja foto principal ainda é o placeholder
        (`.../imagens/sem_imagem.jpg`) -- ou seja, sem foto de verdade no Mercos;
      - `vistos`:   TODO SKU lido da lista, com foto ou sem.
    status=1 = só produtos ativos (mesmo default da tela).

    `vistos` é a PROVA AO VIVO de que o SKU existe no Mercos -- veio da própria tela de
    produtos daquela empresa. É ela que autoriza o envio, não `mercos_cadastro_mestre`
    (que é uma PLANILHA Google replicada por etl/mercos-sheets, não o Mercos -- ver
    `processar_filial`). Antes de 22/set/2026 o `vistos` era descartado e o robô caía de
    volta na planilha, o que zerou o envio por semanas.

    Granularidade é por SKU, não por ordem: a lista do Mercos só expõe a foto PRINCIPAL
    (a 1ª) por linha -- se ela já existe, o SKU inteiro é tratado como "já tem foto" e
    NENHUMA ordem daquele SKU é reenviada. Checar ordem 2/3 exigiria abrir cada produto
    individualmente (centenas de páginas a mais); o pedido do usuário foi no nível do
    SKU ("os SKU que não tem foto"), não do slot de foto.
    """
    sem_foto = set()
    vistos = set()
    ilegiveis_total = 0
    pagina = 1
    t0_varredura = time.monotonic()
    while pagina <= MAX_PAGINAS_SEGURANCA:
        t0_pagina = time.monotonic()
        url = f"https://app.mercos.com/industria/{empresa_id}/produtos/?p={pagina}&status=1&nome_codigo="
        # Espera o CABEÇALHO da tabela OU o texto de "nenhum produto" -- a página que
        # passa do fim real da paginação não tem tabela NENHUMA, só esse texto (achado
        # real 28/ago/2026, via screenshot). Os dois são fim de paginação válido.
        alvo_carregado = page.locator("table thead th").or_(page.get_by_text(TEXTO_SEM_PRODUTO))
        navegar_confirmando(page, url, alvo_carregado, f"lista de produtos (página {pagina})")

        if page.get_by_text(TEXTO_SEM_PRODUTO).count() > 0:
            break

        linhas = page.locator("table tbody tr")
        n = linhas.count()
        if n == 0:
            break

        vistos_na_pagina = 0
        ilegiveis_na_pagina = 0
        for i in range(n):
            row = linhas.nth(i)
            tds = row.locator("td")
            total_td = tds.count()
            if total_td <= 3:
                continue

            # ⚠️ 23/set/2026 -- ÍNDICE FIXO (td[3]=código, td[2]=imagem) é uma suposição de
            # LAYOUT, e as duas empresas Mercos NÃO têm o mesmo layout: RJ (424524) tem uma
            # coluna extra (ícone "visibility") na FRENTE que ES (424525) não tem -- achado
            # via [diag-colunas] abaixo, 22/set/2026 -- empurrando código de td[3]->td[2] e
            # imagem de td[2]->td[1]. Índice fixo lia td[3]="1000 Ml - Cond Cw Af Ba" (nome
            # do produto) como se fosse o SKU e td[2] (sem <img> nenhuma) como a coluna de
            # foto -- toda linha de toda página do RJ virava "ilegível" (nenhuma tinha
            # imagem na posição errada), e o robô nunca varria além da página 1.
            #
            # Fix: acha o CÓDIGO pelo FORMATO, não pela posição -- convenção de SKU da
            # Torre inteira é 2 letras maiúsculas + dígitos (SKU_RE, ver dim_marca.id), e
            # nenhuma outra coluna observada (ícone, nome, "---", "0 UN", "R$ 0,00"...)
            # bate nesse padrão. Acha a IMAGEM pelo CONTEÚDO da linha inteira
            # (`row.locator("img")`, não `tds.nth(N)`) -- não importa em qual td ela está.
            # Os dois sobrevivem à próxima coluna que o Mercos adicionar/remover, em
            # QUALQUER das duas empresas -- índice fixo é o que quebrou desta vez.
            #
            # ⚠️ Timeout curto (5s) continua aqui pelo motivo já registrado em 22/set/2026:
            # sem ele, uma célula genuinamente lenta pra estabilizar consome o default do
            # Playwright (30s) em silêncio; célula saudável responde em milissegundos (o
            # [diag-colunas] leu as 21 células de uma linha inteira em ~2ms).
            codigo = ""
            for j in range(total_td):
                try:
                    txt = tds.nth(j).inner_text(timeout=5_000).strip()
                except Exception:
                    continue
                if SKU_RE.match(txt):
                    codigo = txt
                    break
            if not codigo:
                continue
            vistos_na_pagina += 1
            vistos.add(codigo)

            # ⚠️ 22/set/2026 -- TRÊS estados, não dois. Até aqui um `src` ilegível virava
            # `""`, `""` não contém o placeholder, e o produto era classificado como "JÁ
            # TEM FOTO" e pulado para sempre. Ou seja: "não consegui ler" virava "tem
            # foto", que é a conclusão mais cara possível (o SKU nunca mais é candidato).
            # Foi exatamente isso no RJ: 76,3s por página = 15 linhas x UM timeout de 5s
            # cada, com `0 SKUs sem foto` em 4 páginas seguidas enquanto o ES achava 94 em
            # 10 -- a coluna de imagem estava sendo procurada na célula errada (ver acima)
            # e o robô concluiu que a empresa inteira já estava com foto. `count()` é
            # instantâneo e não espera elemento nenhum, então a ausência de <img> deixa de
            # custar 5s e passa a ser um estado declarado (`ilegiveis`) em vez de um falso
            # "tem foto".
            imgs = row.locator("img")
            src = None
            try:
                if imgs.count() > 0:
                    src = imgs.first.get_attribute("src", timeout=2_000)
            except Exception:
                src = None

            if src is None:
                ilegiveis_na_pagina += 1
            elif PLACEHOLDER_SEM_FOTO in src:
                sem_foto.add(codigo)

        if vistos_na_pagina == 0:
            break

        # ⚠️ Página inteira ilegível não é "todo mundo tem foto" -- é leitura quebrada, e
        # seguir em frente produz uma varredura que diz "nada a fazer" com toda a
        # confiança. Aborta nomeando o estado em vez de devolver conjunto vazio silencioso.
        ilegiveis_total += ilegiveis_na_pagina
        if ilegiveis_na_pagina == vistos_na_pagina:
            # Antes de abortar, DIZ onde a imagem está de fato. A hipótese mais provável
            # pro RJ é que a coluna de foto não é o `td` índice 2 nesta empresa -- e sem
            # este mapa a próxima rodada seria gasta só pra descobrir isso. Custa uma
            # linha e fecha o caso em UMA execução.
            try:
                tds_diag = linhas.nth(0).locator("td")
                total_td = tds_diag.count()
                mapa = []
                for j in range(total_td):
                    cel = tds_diag.nth(j)
                    n_img = cel.locator("img").count()
                    txt = (cel.inner_text(timeout=2_000) or "").strip().replace("\n", " ")[:40]
                    src_j = ""
                    if n_img:
                        src_j = (cel.locator("img").first.get_attribute("src", timeout=2_000) or "")[-60:]
                    mapa.append(f"td[{j}] imgs={n_img} txt={txt!r} src=…{src_j}")
                print(f"  [diag-colunas] linha 1 tem {total_td} td(s):")
                for linha_mapa in mapa:
                    print(f"    {linha_mapa}")
                page.screenshot(path=str(LOGS_DIR / f"colunas_ilegiveis_p{pagina}.png"), full_page=True)
            except Exception as e:
                print(f"  [diag-colunas] não consegui mapear as colunas: {type(e).__name__}: {e}")

            raise RuntimeError(
                f"varredura: página {pagina} com {vistos_na_pagina}/{vistos_na_pagina} produtos "
                f"de foto ILEGÍVEL (coluna de imagem não respondeu) -- a tela do Mercos mudou ou "
                f"a sessão está degradada. Abortando: seguir daqui classificaria todos como "
                f"'já tem foto' e o robô não enviaria nada. Ver [diag-colunas] acima."
            )
        # ⚠️ Log de CADA página (não mais só a cada 10) -- é o que faltou pra diagnosticar
        # o travamento acima: sem isso, uma página lenta (ou 3-4 delas em sequência) fica
        # indistinguível de "nada está acontecendo" até o timeout do job inteiro (60 min).
        # Página saudável (ES) fica ~1-2s; qualquer coisa muito acima disso já é o sinal.
        dur_pagina = time.monotonic() - t0_pagina
        alerta = "  ⚠️ LENTA" if dur_pagina > 5 else ""
        aviso_ileg = f"  ⚠️ {ilegiveis_na_pagina} foto(s) ilegível(is)" if ilegiveis_na_pagina else ""
        print(
            f"  [varredura-mercos] página {pagina} — {dur_pagina:.1f}s{alerta}{aviso_ileg} — "
            f"{len(sem_foto)} SKUs sem foto até agora"
        )
        pagina += 1

    dur_total = time.monotonic() - t0_varredura
    print(
        f"[mercos-fotos] varredura completa: {pagina - 1} páginas em {dur_total:.0f}s "
        f"({dur_total / max(pagina - 1, 1):.1f}s/página em média), {len(vistos)} SKUs ativos vistos, "
        f"{len(sem_foto)} sem foto no Mercos"
    )
    if ilegiveis_total:
        # Declarado, nunca escondido: cada um destes é um SKU tratado como "já tem foto"
        # sem prova de que tem. Se o número for grande, o "nada a enviar" da rodada é
        # sintoma de leitura ruim, não de trabalho concluído.
        print(
            f"[mercos-fotos] ⚠️ {ilegiveis_total} produto(s) com a foto ILEGÍVEL na lista — "
            f"tratados como 'já tem foto' por falta de prova, então NÃO entram nesta rodada."
        )
    # Lista os CÓDIGOS de verdade, não só a contagem -- achado 18/set/2026: só 3 dos 583
    # candidatos batiam com o que o Mercos está pedindo (378), e sem a lista real não dá
    # pra saber SE os outros 375 sao produto sem foto em lugar nenhum (Shopify incluso)
    # ou outro caso de SKU divergente igual o da Apice (dim_produto_foto.md, 18/set).
    print(f"[mercos-fotos] SKUs sem foto no Mercos: {','.join(sorted(sem_foto))}")
    return sem_foto, vistos


def baixar_e_redimensionar(url: str, destino: Path) -> bool:
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        if img.mode in ("RGBA", "LA", "P"):
            fundo = Image.new("RGB", img.size, (255, 255, 255))
            img = img.convert("RGBA")
            fundo.paste(img, mask=img.split()[-1])
            img = fundo
        else:
            img = img.convert("RGB")
        img.thumbnail((TAMANHO_MAX, TAMANHO_MAX), Image.LANCZOS)
        img.save(destino, "JPEG", quality=85, optimize=True)
        return True
    except Exception as e:
        print(f"  [erro] {url}: {e}")
        return False


def processar_filial(filial: str, args) -> None:
    """Ciclo completo (login -> modo test/dry/aplicar) pra UMA empresa Mercos.
    Sessão de browser própria -- nunca compartilhada entre filiais na mesma rodada
    (escritor.py já documenta o motivo: misturar empresas na mesma sessão faz a
    operação sair na empresa errada sem erro nenhum)."""
    empresa_id = EMPRESAS[filial]
    pasta_fotos = Path(tempfile.mkdtemp(prefix=f"mercos_fotos_{filial}_"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        # Contador de uploads EM VOO -- é ele que diz quando o lote terminou de gravar.
        # O Dropzone manda um POST por arquivo, em série, pro endpoint de importação;
        # enquanto houver POST aberto, seguir em frente (próximo lote ou recarga) corta
        # a gravação no meio. Registrado uma vez por página, não por lote.
        em_voo = {"n": 0}

        def _conta_req(r):
            if r.method in ("POST", "PUT") and URL_UPLOAD in r.url:
                em_voo["n"] += 1

        def _conta_resp(r):
            if r.request.method in ("POST", "PUT") and URL_UPLOAD in r.url:
                em_voo["n"] -= 1

        page.on("request", _conta_req)
        page.on("response", _conta_resp)

        try:
            print(f"[mercos-fotos] login no Mercos (filial={filial}, empresa {empresa_id})...")
            login_mercos(page, empresa_id, MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
            # Folga best-effort após o login -- achado 18/set/2026: a 1ª navegação
            # NOSSA logo após login_mercos() confirmar sessão pronta falhou com
            # `net::ERR_ABORTED` (Chromium aborta um goto quando outro navega por
            # cima) -- login_mercos já espera "networkidle" uma vez, mas isso foi
            # ANTES da confirmação final por seletor, e pode sobrar redirect/XHR do
            # próprio dashboard do Mercos ainda em voo. Não afeta o caminho feliz
            # (timeout curto, silencioso se já estiver ocioso).
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
        except Exception:
            # Login vivia FORA do try/except de baixo -- falhar aqui não tirava
            # screenshot nenhum, só um traceback (que também sumiu nas duas rodadas
            # do achado 18/set/2026 sobre buffer). Cobrir também essa fase fecha o
            # gap: toda falha agora tenta um screenshot antes de propagar.
            try:
                print(f"  [erro] parei em: {page.url}")
            except Exception:
                pass
            try:
                page.screenshot(path=str(LOGS_DIR / f"erro_{filial}.png"), full_page=True)
            except Exception:
                pass
            browser.close()
            raise

        url_lista = f"https://app.mercos.com/industria/{empresa_id}/produtos/"

        def ir_pra_tela_importacao():
            # Deep-link direto (page.goto na URL de importação) devolve login -- precisa
            # navegar de dentro do app, clicando no link, como um usuário faria.
            navegar_confirmando(page, url_lista, "a[href*='importacao_de_fotos']", "lista de produtos")
            page.locator("a[href*='importacao_de_fotos']").first.click()
            try:
                # `state="attached"`, não "visible": o Mercos usa Dropzone.js, cujo
                # `<input type=file>` real fica sempre CSS-hidden (`dz-hidden-input`) por
                # design -- só a área de drop/botão estilizados são visíveis. Esperar
                # "visible" aqui timeoutava 100% das vezes mesmo na tela certa (achado
                # 28/ago/2026, `wait_for_selector` retornou "39x locator resolved to hidden").
                page.wait_for_selector("input[type='file']", timeout=20_000, state="attached")
            except Exception:
                if sessao_caiu(page):
                    raise SessaoCaiu("sessão do Mercos caiu ao abrir a tela de Importar fotos")
                raise RuntimeError(
                    "cliquei em 'Importar fotos' mas o input de arquivo não apareceu em 20s -- "
                    "a tela pode ter mudado"
                )

        pfx = f"[mercos-fotos:{filial}]"
        try:
            if args.modo == "test":
                ir_pra_tela_importacao()
                print(f"{pfx} na tela de importação: {page.url}")
                n_input = page.locator("input[type='file']").count()
                print(
                    f"{pfx} modo=test — tela OK, {n_input} input de arquivo encontrado. "
                    "Nada baixado/enviado (banco não foi consultado neste modo)."
                )
                browser.close()
                return

            if not SUPABASE_URL or not SUPABASE_KEY:
                raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY são obrigatórias pra modo=dry/aplicar.")

            print(f"{pfx} lendo dim_produto_foto + mercos_cadastro_mestre...")
            sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            validas, total_fotos, skus_planilha = buscar_fotos_validas(sb)
            print(
                f"{pfx} {total_fotos} fotos na base, {len(validas)} com formato aceito "
                f"(planilha mercos_cadastro_mestre tem {len(skus_planilha)} SKUs — informativa, "
                f"não filtra: quem autoriza o envio é a varredura ao vivo)"
            )

            skus_forcados = buscar_skus_forcados(sb, args.forcar_marca, args.forcar_skus)
            if skus_forcados:
                print(
                    f"{pfx} ⚠️ --forcar-marca/--forcar-skus ativo: {len(skus_forcados)} SKU(s) "
                    "serão reenviados MESMO se já tiverem foto no Mercos (fura a guarda de "
                    "'só SKU sem foto') -- validar SEMPRE com --limit=1 e conferir no Mercos antes do lote inteiro."
                )

            print(f"{pfx} varrendo lista de produtos do Mercos pra achar quem já tem foto...")
            skus_sem_foto_mercos, skus_no_mercos = varrer_skus_sem_foto_mercos(page, empresa_id)

            # ⚠️ SKU forçado precisa existir no Mercos, e a prova disso é `skus_no_mercos`
            # (lido da tela), não a planilha. Forçado que o Mercos não lista é quase sempre
            # erro de digitação -- o arquivo seria enviado e o Mercos o ignoraria em
            # silêncio, dando "enviei N fotos" sem nada ter sido associado.
            forcados_fantasma = {s for s in skus_forcados if s not in skus_no_mercos}
            if forcados_fantasma:
                print(
                    f"{pfx} ⚠️ {len(forcados_fantasma)} SKU(s) de --forcar-* NÃO aparecem na lista "
                    f"ativa do Mercos desta empresa e foram DESCARTADOS (o Mercos ignoraria o "
                    f"arquivo sem erro): {','.join(sorted(forcados_fantasma))}"
                )
                skus_forcados = skus_forcados - forcados_fantasma

            antes = len(validas)
            # A prova que autoriza o envio é a varredura AO VIVO desta empresa: ou o SKU
            # está sem foto aqui, ou foi forçado à mão e existe aqui. `mercos_cadastro_mestre`
            # saiu da conta em 22/set/2026 (ver buscar_fotos_validas).
            entra = lambda f: f["sku"] in skus_sem_foto_mercos or f["sku"] in skus_forcados
            fora = [f for f in validas if not entra(f)]
            validas = [f for f in validas if entra(f)]
            n_forcados_na_rodada = len({
                f["sku"] for f in validas
                if f["sku"] in skus_forcados and f["sku"] not in skus_sem_foto_mercos
            })

            # ⚠️ Decompõe o descarte em vez de somar tudo em "pulados por já ter foto".
            # "já tem foto aqui" é trabalho concluído; "o Mercos desta empresa não lista
            # esse SKU" é catálogo diferente (a Torre tem foto de produto que só existe na
            # outra filial, ou já descontinuado). Os dois sob um rótulo só foi o que
            # escondeu o gate da planilha por semanas -- a linha dizia "570 pulados por já
            # ter foto" quando 213 SKUs estavam esperando foto.
            n_ja_tem = len({f["sku"] for f in fora if f["sku"] in skus_no_mercos})
            n_fora_do_mercos = len({f["sku"] for f in fora if f["sku"] not in skus_no_mercos})
            print(
                f"{pfx} {antes} fotos com formato aceito -> {len(validas)} nesta rodada "
                f"({len(fora)} fora: {n_ja_tem} SKU(s) já com foto nesta empresa, "
                f"{n_fora_do_mercos} SKU(s) que esta empresa não lista"
                + (f"; {n_forcados_na_rodada} voltaram por --forcar-*" if skus_forcados else "")
                + ")"
            )

            alvo = validas[: args.limit] if args.limit else validas

            # ⚠️ --limit corta LINHAS (sku+ordem), não SKUs distintos -- um SKU com 3 fotos
            # cadastradas conta como 3 linhas, então --limit=2 pode subir 2 fotos do MESMO
            # produto em vez de 2 produtos diferentes. Log explícito pra nunca ter que
            # adivinhar "quais SKU subiram" olhando só a contagem (achado 28/ago/2026, a
            # pergunta do usuário depois de um --modo=aplicar --limit=2 real).
            skus_distintos = sorted({f["sku"] for f in alvo})
            print(f"{pfx} {len(alvo)} foto(s) / {len(skus_distintos)} SKU(s) distinto(s) nesta rodada:")
            for f in alvo:
                marca_tag = " [FORÇADO — já tinha foto]" if f["sku"] in skus_forcados and f["sku"] not in skus_sem_foto_mercos else ""
                print(f"  {f['sku']} (ordem {f['ordem']}) -> {nome_mercos(f['sku'], f['ordem'])}{marca_tag}")

            ir_pra_tela_importacao()
            print(f"{pfx} na tela de importação: {page.url}")

            print(f"{pfx} baixando e redimensionando {len(alvo)} fotos...")
            prontos = []
            for i, f in enumerate(alvo):
                nome = nome_mercos(f["sku"], f["ordem"])
                destino = pasta_fotos / nome
                if baixar_e_redimensionar(f["url"], destino):
                    prontos.append(destino)
                if (i + 1) % 100 == 0:
                    print(f"  {i + 1}/{len(alvo)} processadas")

            print(f"{pfx} {len(prontos)}/{len(alvo)} prontas em {pasta_fotos}")

            if args.modo == "dry":
                print(f"{pfx} modo=dry — amostra do que seria enviado:")
                for f in prontos[:20]:
                    tam = f.stat().st_size / 1024
                    print(f"  {f.name} ({tam:.0f} KB)")
                if len(prontos) > 20:
                    print(f"  ... e mais {len(prontos) - 20}")
                browser.close()
                return

            # aplicar: envia em lotes de BATCH arquivos por vez
            #
            # 🔴 23/set/2026 -- ANTES daqui o robô fazia `set_input_files` + um
            # `wait_for_timeout` FIXO e contava `enviados += len(lote)`. Isso conta ARQUIVO
            # ENTREGUE AO INPUT, não foto que o Mercos vinculou: numa rodada real ele
            # imprimiu "220/220 enviados", terminou `success`, e a varredura seguinte achou
            # os MESMOS 207 SKUs sem foto -- zero vinculadas. O screenshot pós-upload
            # mostrava as 220 linhas com Status = triângulo vermelho e "Vincular a produto"
            # VAZIO, e ao reabrir a tela ela estava vazia (a lista é da sessão, não é salva).
            #
            # Medido depois: 1 arquivo vincula; 10 numa chamada = 10/10 `done`; 50 numa
            # chamada COM espera proporcional = 48 `done` + 2 `warning`. Ou seja o defeito
            # não era o tamanho do lote -- era a espera de `3000 + 500*n` (28s pra 50) e a
            # chamada SEGUINTE de `set_input_files` entrando por cima dos uploads ainda em
            # voo e abortando todos. Espera fixa é um palpite sobre quanto o servidor
            # demora; o certo é esperar a TELA confirmar.
            # 🔴 23/set/2026, 2º achado no MESMO dia -- SÓ O PRIMEIRO `set_input_files` DA
            # PÁGINA VINCULA. Medido cruzando os 207 arquivos enviados com a varredura
            # seguinte, lote a lote:
            #     lote 1: 44 SKUs -> 44 com foto      lote 4: 50 -> 0
            #     lote 2: 48 SKUs ->  4 com foto      lote 5:  7 -> 0
            #     lote 3: 47 SKUs ->  0 com foto
            # Do 2º lote em diante o Mercos aceita o arquivo, desenha a linha e mostra
            # Status = `done` -- e NÃO grava. Ou seja a tela mente, e por isso a contagem
            # por status (que consertou o "conta arquivo entregue") ainda dava 207/207.
            # ⚠️ Prova de que não é tamanho nem pressa: o lote 1 vincula 44/44 e um envio
            # isolado de 50 numa página limpa deu 48/50.
            #
            # Fix: uma PÁGINA LIMPA por lote. Recarregar a tela de importação faz cada lote
            # ser o "primeiro" da sua página, que é o único caso que funciona. De quebra a
            # tabela volta vazia, então a contagem por lote não depende de offset nenhum.
            done_total = 0
            nao_vinculados = []
            for i in range(0, len(prontos), BATCH):
                lote = prontos[i : i + BATCH]
                n_lote = len(lote)
                print(f"{pfx} enviando lote {i // BATCH + 1} ({n_lote} arquivos)...")
                if i > 0:
                    ir_pra_tela_importacao()  # página limpa -- ver bloco acima
                input_el = page.locator("input[type='file']").first
                antes_linhas = page.locator("table tbody tr").count()
                em_voo["n"] = 0
                t_lote = time.monotonic()
                input_el.set_input_files([str(f) for f in lote])

                # Espera a TELA resolver cada arquivo (status vira `done` ou `warning`),
                # com teto generoso -- o que não pode acontecer é seguir pro próximo lote
                # com upload em voo. Teto por arquivo, não fixo: lote grande espera mais.
                limite_ms = 20_000 + ESPERA_POR_ARQ_MS * n_lote
                gasto = 0
                while gasto < limite_ms:
                    page.wait_for_timeout(2_000)
                    gasto += 2_000
                    linhas_now = page.locator("table tbody tr")
                    if linhas_now.count() < antes_linhas + n_lote:
                        continue  # ainda aparecendo na lista
                    pendentes = 0
                    for j in range(antes_linhas, linhas_now.count()):
                        try:
                            t = linhas_now.nth(j).inner_text(timeout=3_000)
                        except Exception:
                            pendentes += 1
                            continue
                        if status_da_linha(t) not in STATUS_RESOLVIDO:
                            pendentes += 1
                    if pendentes == 0:
                        break

                # 🔴 A TELA TERMINA MUITO ANTES DA GRAVAÇÃO -- espera a REDE, não o relógio.
                #
                # Medido em 23/set/2026 escutando request/response: o Dropzone manda UM
                # POST POR ARQUIVO, em SÉRIE, ~0,5s cada. Para 30 arquivos são 31 POSTs e o
                # último RESPONSE chega a 11,3s do `set_input_files` -- enquanto a TELA já
                # tinha desenhado tudo como `done` aos 2-4s. Sair nos 4s (o que o robô
                # fazia) cortava a gravação no meio, e foi isso que produziu os padrões
                # 44/0/0/0 e 10/13/14/3 por lote, que pareciam "teto do Mercos".
                #
                # ⚠️ A 1ª correção foi dormir 6s POR ARQUIVO (180s por lote de 30). Funcionou
                # -- 28/28 confirmados -- mas é 16x mais que o necessário, e transformava a
                # rodada do RJ em ~45 min. Numa conta de pessoa, compartilhada, rodada longa
                # é rodada derrubada: o run do Actions caiu aos 11 min com "a conta foi usada
                # em outro computador". Tempo de parede aqui não é só lentidão, é RISCO.
                #
                # Esperar a rede ficar quieta é exato E rápido: acaba quando o servidor
                # terminou de responder, seja qual for o tamanho do lote ou a velocidade
                # do dia.
                quieto_desde = None
                while time.monotonic() - t_lote < 300:
                    if em_voo["n"] > 0:
                        quieto_desde = None
                    elif quieto_desde is None:
                        quieto_desde = time.monotonic()
                    elif (time.monotonic() - quieto_desde) * 1000 >= SILENCIO_REDE_MS:
                        break
                    page.wait_for_timeout(500)
                print(f"{pfx}   lote gravado em {time.monotonic() - t_lote:.0f}s "
                      f"(rede quieta há {SILENCIO_REDE_MS // 1000}s)")

                # Conta o que a TELA diz, arquivo por arquivo -- é o único número honesto.
                linhas_now = page.locator("table tbody tr")
                for j in range(antes_linhas, linhas_now.count()):
                    try:
                        t = linhas_now.nth(j).inner_text(timeout=3_000)
                    except Exception:
                        t = ""
                    m = RE_NOME_ARQ.search(t)
                    nome = m.group(0) if m else f"<linha {j}>"
                    if status_da_linha(t) == STATUS_VINCULADO:
                        done_total += 1
                    else:
                        nao_vinculados.append(nome)
                print(f"{pfx} {done_total}/{len(prontos)} VINCULADAS até agora "
                      f"({len(nao_vinculados)} não vincularam)")

            page.screenshot(path=str(LOGS_DIR / f"apos-upload_{filial}.png"), full_page=True)
            print(f"{pfx} a tela declarou {done_total}/{len(prontos)} vinculadas"
                  + (f" ({len(nao_vinculados)} com aviso na tela)" if nao_vinculados else ""))

            # 🔴 CONFERÊNCIA OBRIGATÓRIA -- a tela do Mercos NÃO é prova de gravação.
            # Medido em 23/set/2026, três vezes seguidas: o robô declarou 220/220, depois
            # 207/207, depois 152/153, e a varredura seguinte mostrou 0, 47 e 40 SKUs
            # realmente com foto. `done` só diz que o navegador desenhou a linha.
            # A ÚNICA prova é reler a lista de produtos e ver quem saiu do "sem foto" --
            # custa ~2 min e é a diferença entre relatar trabalho e relatar intenção.
            print(f"{pfx} conferindo no Mercos o que REALMENTE gravou (revarrendo)...")
            sem_foto_depois, _ = varrer_skus_sem_foto_mercos(page, empresa_id)
            alvo_skus = {f["sku"] for f in alvo}
            gravados = sorted(s for s in alvo_skus if s not in sem_foto_depois)
            faltaram = sorted(s for s in alvo_skus if s in sem_foto_depois)
            print(f"{pfx} ✅ CONFIRMADO NO MERCOS: {len(gravados)}/{len(alvo_skus)} SKU(s) "
                  f"passaram a ter foto")
            if faltaram:
                # ⚠️ Não é erro do robô nem arquivo ruim: medido, cada rodada grava ~40-47
                # SKUs por mais que se mande (220 -> 0/47, 153 -> 40), e o restante continua
                # elegível. Rodar de novo converge; o cron diário faz isso sozinho.
                print(f"{pfx} ⚠️ {len(faltaram)} SKU(s) NÃO gravaram apesar de a tela dizer que sim "
                      f"-- o Mercos aceita um teto por rodada; rode de novo pra continuar. "
                      f"Faltaram: {','.join(faltaram[:30])}"
                      + (f" ... e mais {len(faltaram) - 30}" if len(faltaram) > 30 else ""))
            print(f"{pfx} screenshot: logs/apos-upload_{filial}.png")
            browser.close()
        except Exception:
            try:
                print(f"  [erro] parei em: {page.url}")
            except Exception:
                pass
            try:
                page.screenshot(path=str(LOGS_DIR / f"erro_{filial}.png"), full_page=True)
            except Exception:
                pass
            browser.close()
            raise


def main():
    # Saída SEM buffer, linha a linha — achado 18/set/2026: duas rodadas (test e dry)
    # terminaram em "0s"/"1s" no GitHub Actions SEM NENHUM print visível no log, nem
    # o 1º ("conferindo credencial do Gmail..."), mesmo com stdout sendo, em teoria,
    # flushado no encerramento normal do interpretador. Reconfigurar aqui é rede de
    # segurança: cada linha sai do buffer assim que é escrita, então mesmo se o
    # processo morrer de um jeito que não flushe no fim (kill do runner, segfault do
    # Chromium), o que já foi impresso ANTES do ponto da morte sobrevive no log.
    # ⚠️ `encoding="utf-8"` não é cosmético: no Windows o console é cp1252 e QUALQUER
    # print com emoji explode com UnicodeEncodeError. Achado rodando local em 23/set/2026 --
    # a sessão do Mercos caiu (normal, conta compartilhada), o robô foi imprimir
    # "⚠️ filial=es FALHOU: <motivo>" e MORREU NO PRÓPRIO PRINT: o traceback que apareceu
    # foi o do charmap, com o motivo real enterrado num "During handling of the above
    # exception". No GitHub Actions o stdout já é UTF-8, então isso nunca apareceu lá --
    # é exatamente a classe de defeito que só existe no caminho de execução menos usado.
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")

    args = parse_args()
    # Default = SÓ ES (não "todas") -- achado 18/set/2026: es+rj na MESMA execução
    # derruba sessão (1 login por conta Mercos, e a conta é compartilhada com outros
    # robôs/pessoas). ES e RJ agora são workflows SEPARADOS, em horários diferentes
    # (mercos-fotos.yml / mercos-fotos-rj.yml) -- nunca disparados juntos de propósito.
    filiais = [f.strip() for f in args.filiais.split(",")] if args.filiais else ["es"]
    for f in filiais:
        if f not in EMPRESAS:
            raise ValueError(f"filial desconhecida: '{f}' (válidas: {', '.join(EMPRESAS)})")

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    print("[mercos-fotos] conferindo credencial do Gmail...")
    conferir_credencial_gmail(GMAIL_USER, GMAIL_SENHA, MERCOS_EMAIL)  # 1x só -- mesmo Gmail resolve 2FA pras duas filiais

    print(f"[mercos-fotos] processando {len(filiais)} filial(is): {', '.join(filiais)}")
    erros: dict[str, Exception] = {}
    for filial in filiais:
        print(f"\n[mercos-fotos] ==== iniciando filial={filial} ====")
        try:
            processar_filial(filial, args)
        except Exception as e:
            # Uma filial falhar NÃO impede tentar a outra -- são sessões/empresas
            # independentes, e "ES quebrou" não diz nada sobre se RJ vai quebrar
            # também. O job só falha (exit != 0) no FIM, depois de tentar todas.
            print(f"[mercos-fotos] ⚠️ filial={filial} FALHOU: {e}")
            erros[filial] = e

    if erros:
        raise RuntimeError(
            f"{len(erros)}/{len(filiais)} filial(is) falharam: "
            + "; ".join(f"{f}: {e}" for f, e in erros.items())
        )
    print(f"\n[mercos-fotos] todas as {len(filiais)} filial(is) concluídas sem erro.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Redundante com o comportamento padrão do Python (traceback não tratado já vai
        # pro stderr) -- mas explícito e com flush manual dos dois streams, pra não
        # depender de nenhuma suposição sobre buffering/encerramento do interpretador
        # continuar valendo neste ambiente (achado 18/set/2026: duas rodadas seguidas
        # no GitHub Actions terminaram sem NENHUMA linha visível no log, nem sequer o
        # 1º print()). Se isto imprimir e o log seguir mudo, o problema não é Python.
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(1)
