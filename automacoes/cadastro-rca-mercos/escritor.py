#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CadastroRCA-Mercos -- Fase 5: ESCRITOR. Le o ledger `cadastro_rca_mercos_processado`
(status='pendente'), aplica de verdade no Mercos: vinculos e permissoes
(N tabelas de preco + N condicoes de pagamento, sempre via "Adicionar") + carteira
do vendedor (SEM bloquear todos -- isso e so pra reforma em massa, nao pra
inclusao pontual). Escreve o resultado de volta na planilha (coluna STATUS)
e notifica o vendedor por WhatsApp (Evolution/whatsapp_outbox) quando da certo.

Escopo desta primeira versao: SO empresa ES (424525) -- confirmado com o
usuario 23/jul. RJ fica pra depois de validar ES em producao.

Config via variavel de ambiente -- ver arquitetura-producao.md.

Uso:
  python escritor.py --cnpj 56383370000100   # 1 cliente especifico (retry manual)
  python escritor.py --limite 30              # ate 30 da fila 'pendente'
  python escritor.py                          # todos os 'pendente' de ES
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import os
import re
import csv
import json
import time
import argparse
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, Page
from supabase import create_client

from mercos_login import conferir_credencial_gmail, login_mercos
import mercos_ui  # helpers de UI compartilhados com o robo de limite de credito

MERCOS_EMAIL = os.environ["MERCOS_EMAIL"]
MERCOS_SENHA = os.environ["MERCOS_SENHA"]
# ⚠️ OPCIONAIS de proposito (01/set/2026): so servem pra ler o codigo 2FA no
# e-mail, e nem toda conta do Mercos pede 2FA (a de uma pessoa que ja usa o
# sistema no dia a dia costuma nao pedir). Eram `os.environ[...]`, o que fazia o
# script morrer com KeyError na IMPORTACAO -- antes de qualquer mensagem util --
# quando alguem rodava pela maquina propria com uma conta sem 2FA. Ausentes, o
# run segue e SO falha se o Mercos realmente pedir o codigo, com o motivo
# escrito (ver mercos_login.login_mercos). No Actions os secrets existem e nada
# muda.
GMAIL_USER = os.environ.get("GMAIL_USER") or ""
GMAIL_SENHA = os.environ.get("GMAIL_SENHA") or ""
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SHEETS_SERVICE_ACCOUNT_JSON = os.environ["CADASTRO_RCA_SHEETS_SERVICE_ACCOUNT_JSON"]
SHEETS_CADASTRO_ID = "1bX4GmpKoOITG6l1y-BeW9X8l3LZ6Xcm8COcaHk9Ys-I"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USUARIOS_MERCOS_CSV = os.path.join(BASE_DIR, "usuarios_mercos.csv")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

EMPRESA_ID_ES = "424525"
EMPRESA_ID_RJ = "424524"
# ⚠️ ES continua sendo o DEFAULT (o workflow nao passa --filial, entao o
# comportamento no Actions nao muda). RJ ficou de fora desde 23/jul ("depois de
# validar ES em producao") e a diferenca nao e so o filtro do ledger: o passo da
# carteira ("Todos os usuarios") le a empresa em que a SESSAO esta logada, entao
# rodar RJ exige logar na empresa RJ -- por isso a filial e escolhida uma vez,
# na entrada, e vale pro login E pra consulta. Nunca misture as duas numa
# rodada: a carteira sairia na empresa errada sem erro nenhum.
EMPRESAS = {"es": EMPRESA_ID_ES, "rj": EMPRESA_ID_RJ}
REGIAO_POR_EMPRESA = {EMPRESA_ID_ES: "ES", EMPRESA_ID_RJ: "RJ"}
STATUS_SHEET_OK = "Cliente OK no mercos"
# ⚠️ A coluna A da planilha tem VALIDACAO DE DADOS com lista fechada de dois
# valores ("Chamado aberto" / "Cliente OK no mercos", strict=true, medida nas
# linhas 735-834 em 24/set/2026) -- texto fora da lista entra pela API mas fica
# marcado como invalido para quem abre a planilha. Por isso o caso "sem
# representante" usa o MESMO literal: o motivo vive em
# `cadastro_rca_mercos_processado.erro_detalhe` e em `carteira_aplicada=false`,
# que e onde ele sobrevive e onde a conferencia procura.
STATUS_SHEET_OK_SEM_CARTEIRA = STATUS_SHEET_OK
STATUS_SHEET_ERRO = "Erro mercos"

CHAMADO_MINIMO = 30767  # mesmo corte do decisor.py e de lib/cadastro-rca-mercos/chamado.ts (18/ago)

# ── Sessao derrubada: esperar e VOLTAR (27/ago/2026) ──────────────────────
# Mesmo desenho do robo irmao `automacoes/credito-limite-mercos/repositor.py`
# (26/ago). O Mercos aceita 1 sessao por usuario e a conta do robo e de uma
# PESSOA -- em horario comercial alguem loga e derruba a rodada no meio. Foi o
# que aconteceu em 27/ago/2026 (run das 13h31): 13 clientes aplicados, o 14o
# caiu no passo da carteira e 3 nem chegaram a ser tentados.
#
# ⚠️ A ESPERA E O RECURSO, nao efeito colateral. Reentrar na hora rouba a sessao
# de volta de quem acabou de entrar; essa pessoa loga de novo e derruba o robo,
# e os dois perdem a tarde. 15 min e tempo de uma consulta terminar; quem ficou
# mais que isso esta trabalhando de verdade, e ai o certo e desistir.
VOLTAS_POR_RODADA = int(os.environ.get("CADASTRO_VOLTAS_SESSAO") or 2)
ESPERA_VOLTA_S = int(os.environ.get("CADASTRO_ESPERA_VOLTA_S") or 900)
# ⚠️ Esperar so vale se couber na janela do Actions: `timeout-minutes` mata o job
# no meio do sono e a queda fica MUDA -- pior que abortar com mensagem. Este
# numero tem que ser MENOR que o `timeout-minutes` do workflow (60), e o workflow
# passa o valor dele em CADASTRO_JANELA_MIN.
JANELA_MIN = int(os.environ.get("CADASTRO_JANELA_MIN") or 55)
MARGEM_FIM_S = 120  # sobra pra fechar o cliente da vez e imprimir o resumo
_COMECOU_EM = time.time()


def _chamado_acima_do_minimo(valor) -> bool:
    try:
        return int(str(valor).strip()) >= CHAMADO_MINIMO
    except (ValueError, TypeError):
        return False

_t0 = None


def _ts() -> str:
    global _t0
    agora = datetime.now()
    if _t0 is None:
        _t0 = agora
    delta = int((agora - _t0).total_seconds())
    return f"[{agora.strftime('%H:%M:%S')} +{delta:>3}s]"


def _shot(page: Page, nome: str) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    caminho = os.path.join(LOGS_DIR, f"escritor_{nome}.png")
    try:
        page.screenshot(path=caminho, full_page=True)
        print(f"  {_ts()} [debug] logs/{os.path.basename(caminho)}")
    except Exception as e:
        print(f"  {_ts()} [debug] screenshot falhou: {e}")
    return caminho


# ── Planilha (leitura de nome + escrita de status) ──────────────────────────

def _abrir_planilha_para_escrita():
    creds = Credentials.from_service_account_info(
        json.loads(SHEETS_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEETS_CADASTRO_ID).worksheets()[0]


def atualizar_status_planilha(ws, sheet_row, valor: str) -> None:
    if not sheet_row:
        print(f"  {_ts()} [!] sheet_row ausente -- nao deu pra atualizar STATUS na planilha")
        return
    try:
        ws.update_acell(f"A{sheet_row}", valor)
        print(f"  {_ts()} [planilha] linha {sheet_row} STATUS -> '{valor}'")
    except Exception as e:
        print(f"  {_ts()} [!] falha ao atualizar STATUS na planilha (linha {sheet_row}): {e}")


# ── De-para vendedor (email -> nome + telefone, pro login na carteira e pro WhatsApp) ──

class RepresentanteForaDaFilial(RuntimeError):
    """O representante existe, mas NAO tem cadastro Mercos nesta filial.

    Nao e erro do cliente nem do robo: e a carteira daquele representante nao
    existir naquela empresa. Quem levanta isto e PULADO -- sem "Erro mercos" na
    planilha, sem mexer no status do ledger -- porque carimbar erro mandaria
    alguem procurar defeito onde nao ha (pedido do usuario, 01/set/2026, ao
    ligar o RJ no cron: "rodar no RJ somente se o vendedor tiver cadastro no
    RJ")."""


def carregar_usuarios_mercos() -> dict:
    """(email lower, regiao) -> row (nome, telefone, categoria, regiao).

    ⚠️ A chave era so o e-mail, com `setdefault` -- ou seja, ficava com a
    PRIMEIRA linha do CSV, que e sempre a de ES. Pra cliente do RJ isso pegava
    o nome/telefone do cadastro de ES; funcionava por sorte (quem atua nas duas
    filiais tem o mesmo nome nas duas linhas) e nao respondia a pergunta que
    importa aqui: *esse representante tem cadastro NESTA filial?*"""
    mapa = {}
    with open(USUARIOS_MERCOS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            chave = (row["email"].strip().lower(), row["regiao"].strip().upper())
            mapa.setdefault(chave, row)
    return mapa


def _emails_do_representante(usuarios: dict, email: str) -> set[str]:
    """Regioes em que esse e-mail tem cadastro no CSV."""
    alvo = (email or "").strip().lower()
    return {regiao for (mail, regiao) in usuarios if mail == alvo}


# ── Carteira (portado de relatorio_mercos/mercos_gestao_carteira.py) ───────
# Mesmos seletores ja validados em producao (reforma de carteira jul/2026).
# NAO inclui o passo de "bloquear todos os clientes" -- isso e so pra reset
# em massa da reforma, nao faz sentido numa inclusao pontual de 1 cliente.

# Corpo unico em `mercos_ui` desde 11/set/2026, quando o 3o robo passou a usar
# os mesmos seletores (`vincular_carteira_lote.py`). Aqui ficam so os wrappers,
# porque este arquivo loga com `_ts()` proprio -- mesma divisao ja usada para
# `sessao_caiu`/`motivo_sessao` acima.


def _abrir_todos_usuarios(page: Page) -> None:
    mercos_ui.abrir_todos_usuarios(page)


def _abrir_clientes_do_vendedor(page: Page, vendedor_nome: str) -> None:
    mercos_ui.abrir_clientes_do_vendedor(page, vendedor_nome)


def _liberar_cliente(page: Page, cnpj_digits: str, cliente_nome: str) -> str:
    return mercos_ui.liberar_cliente(page, cnpj_digits, cliente_nome, log=print, ts=_ts)


def aplicar_carteira(page: Page, vendedor_nome: str, cnpj_digits: str, cliente_nome: str) -> str:
    _abrir_todos_usuarios(page)
    _abrir_clientes_do_vendedor(page, vendedor_nome)
    return _liberar_cliente(page, cnpj_digits, cliente_nome)


# ── Vinculos e Permissoes (tabela de preco + condicao de pagamento) ────────

class SessaoMercosCaiu(RuntimeError):
    """O Mercos derrubou a sessao do robo -- so aceita 1 sessao por usuario.

    Caso real 18/ago/2026: alguem logou na mesma conta as 17:13, no meio da
    rodada. A tela diz "Seu usuario acessou o sistema de outro computador ou
    dispositivo, por isso voce foi desconectado". O 1o cliente ficou pela
    metade (vinculos aplicados, carteira nao) e os 20 seguintes falharam em
    cascata com `Timeout` -- 20 erros cujo motivo real nao aparecia em
    nenhuma mensagem, e 21 linhas da planilha carimbadas "Erro mercos" por
    um problema que nao era de cliente nenhum.

    Por isso essa falha NAO marca cliente como erro e NAO escreve na planilha:
    aborta a rodada inteira nomeando a causa. Quem tem que decidir e o humano
    (esperar o outro sair do Mercos, rodar fora do expediente, ou criar um
    usuario Mercos so pro robo)."""


# Sessao derrubada: corpo unico em `mercos_ui` (o robo de limite de credito usa
# o mesmo). Aqui ficam so os wrappers, porque este arquivo passa `MERCOS_EMAIL`
# de global e o outro passa o dele.
_TEXTO_SESSAO_DERRUBADA = mercos_ui.TEXTO_SESSAO_DERRUBADA


def _sessao_caiu(page: Page) -> bool:
    return mercos_ui.sessao_caiu(page)


def _motivo_sessao(page: Page) -> str:
    return mercos_ui.motivo_sessao(page, MERCOS_EMAIL)


def _reconquistar_sessao(page: Page, motivo: str, volta: int, de: int, empresa_id: str = EMPRESA_ID_ES) -> bool:
    """Espera, loga de novo, e diz se a rodada pode continuar.

    Nunca levanta: quem chama decide entre seguir e abortar. Morrer tentando
    voltar seria pior que a queda original."""
    print(f"  {_ts()} [sessao] caiu: {motivo}")
    sobra = (_COMECOU_EM + JANELA_MIN * 60) - time.time()
    if sobra < ESPERA_VOLTA_S + MARGEM_FIM_S:
        print(f"  {_ts()} [sessao] volta {volta}/{de} CANCELADA -- faltam "
              f"{int(sobra / 60)}min de janela e a espera leva {ESPERA_VOLTA_S // 60}min")
        return False
    print(f"  {_ts()} [sessao] volta {volta}/{de} -- esperando {ESPERA_VOLTA_S // 60}min antes de "
          "entrar de novo (entrar agora derrubaria quem acabou de logar, e ele nos derrubaria de volta)")
    try:
        time.sleep(ESPERA_VOLTA_S)
        login_mercos(page, empresa_id, MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
    except Exception as e:
        print(f"  {_ts()} [sessao] nao consegui voltar: {e}")
        return False
    print(f"  {_ts()} [sessao] voltei -- retomando na fila")
    return True


def _estado_checkbox(tr) -> str:
    """"marcado" | "vazio" | "desconhecido" -- le a ligatura do icone Material
    (check_box / check_box_outline_blank). Valor JA vinculado ao cliente vem
    pre-marcado no painel (ver mercos-ui-mapeamento.md), e clicar nele
    DESMARCARIA -- o retry de um cliente ja aplicado tiraria o vinculo que
    existe. Quando nao da pra ler o estado, devolve "desconhecido" e o chamador
    clica (comportamento antigo)."""
    try:
        texto = (tr.locator("i.material-icons").first.inner_text() or "").strip().lower()
    except Exception:
        return "desconhecido"
    if "outline_blank" in texto:
        return "vazio"
    if "check_box" in texto:
        return "marcado"
    return "desconhecido"


_RE_ROTULO_MERCOS = re.compile(r"^(\d+)\s*-\s*(.+)$")


def _chave_rotulo(texto: str) -> str:
    """Rotulo comparavel: "005 - BOLETO 15" e "5   - BOLETO 15" sao O MESMO
    valor.

    ATENCAO -- bug real (chamado 33380, DISTRIBUIDORA LITORAL, 25/ago/2026): o
    catalogo em `decisor.py` foi capturado com o codigo zero-preenchido ("005")
    mas a tela do Mercos mostra "5   - BOLETO 15" -- so o BOLETO 15 fica sem os
    zeros, todos os outros codigos tem 3 digitos. Como a comparacao era string
    exata, o robo levantava "Nenhuma linha com label exato '005 - BOLETO 15'
    encontrada (48 linhas na tabela)" com a lista JA CARREGADA e o valor
    visivel ali. Espera curta se disfarca de valor inexistente; zero a
    esquerda se disfarca das duas coisas.

    Continua EXIGINDO codigo E descricao iguais -- so ignora zero a esquerda do
    codigo e espaco repetido. Nenhum par de codigos do catalogo colide depois de
    tirar o zero (005->5, 009->9, 056->56...), entao isto nao cria ambiguidade;
    e a guarda de ">1 match" segue valendo pra qualquer surpresa."""
    t = re.sub(r"\s+", " ", (texto or "").strip())
    m = _RE_ROTULO_MERCOS.match(t)
    if not m:
        return t.upper()
    return f"{m.group(1).lstrip('0') or '0'} - {m.group(2).strip().upper()}"


def _marcar_checkbox_por_label_exato(page: Page, label_exato: str) -> str:
    """O icone de checkbox (check_box_outline_blank/check_box) entra junto no
    inner_text() da linha ANTES do label -- por isso compara linha a linha
    (splitlines), nao o bloco inteiro. Levanta erro se 0 ou >1 match --
    nunca clica em algo ambiguo (ver mercos-ui-mapeamento.md).

    Retry em 0-match (nao em >1, esse e erro real de verdade): a lista de
    valores as vezes ainda nao terminou de renderizar quando o scan roda
    logo apos selecionar o atributo -- bug real 24/jul (label existia no
    catalogo ao vivo, confirmado, mas o escritor nao achou na hora certa).

    Devolve "marcado" (clicou) ou "ja_marcado" (valor ja vinculado, nao tocou)."""
    # Espera PROGRESSIVA (~16s): a lista de valores do painel vem por AJAX e o
    # tempo varia por cliente. Com 3 tentativas de 1,5s (versao de 30/jul) o
    # chamado 31406 falhou em 18/ago com "Nenhuma linha com label exato
    # '058 - BOLETO 30/45/60' encontrada (3 linhas na tabela)" -- as 3 linhas
    # eram a tabela de CLIENTES a esquerda, e o screenshot mostra o painel
    # ainda com o spinner. Espera curta se disfarca de "valor nao existe no
    # catalogo", que e um diagnostico completamente diferente.
    # 10/set/2026: era ~16s e nao bastou -- o chamado 141027 falhou DUAS vezes
    # com 'nao encontrada (4 linhas na tabela)' e o screenshot mostra o painel
    # ainda com o SPINNER (as 4 linhas eram a tabela de clientes a esquerda).
    # ~44s agora. So custa tempo no caminho de falha: quando a linha aparece,
    # a funcao retorna na hora e nenhum cliente sadio fica mais lento.
    esperas = [1.0, 1.5, 2.0, 3.0, 4.0, 4.5, 6.0, 7.0, 7.0, 8.0]
    ultimo_erro = ""
    for _tentativa in range(len(esperas)):
        alvo = _chave_rotulo(label_exato)
        linhas = page.locator("table tr")
        n = linhas.count()
        candidatos = []
        for i in range(n):
            tr = linhas.nth(i)
            texto = tr.inner_text()
            partes = [p.strip() for p in texto.splitlines() if p.strip()]
            if any(_chave_rotulo(p) == alvo for p in partes):
                candidatos.append(tr)
        if len(candidatos) == 1:
            tr = candidatos[0]
            if _estado_checkbox(tr) == "marcado":
                print(f"  {_ts()} [=] '{label_exato}' ja estava vinculado -- nao clicou (clicar desmarcaria)")
                return "ja_marcado"
            tr.locator("i.material-icons").first.click(force=True, timeout=10_000)
            return "marcado"
        if len(candidatos) > 1:
            raise RuntimeError(f"{len(candidatos)} linhas com label '{label_exato}' -- ambiguo")
        ultimo_erro = f"Nenhuma linha com label exato '{label_exato}' encontrada ({n} linhas na tabela)"
        time.sleep(esperas[_tentativa])
    raise RuntimeError(
        f"{ultimo_erro} -- apos {len(esperas)} tentativas em ~{sum(esperas):.0f}s "
        "(se o valor existe no catalogo do Mercos, e a lista do painel que nao "
        "carregou -- ver o screenshot no artifact)"
    )


def _selecionar_atributo_vinculo(page: Page, atributo: str) -> None:
    """Abre o dropdown de atributo do modal "Editar Vínculos e permissões" e
    escolhe "Condições de pagamento" / "Tabelas de preço".

    Retry no PAR abrir+escolher, reabrindo o dropdown do zero a cada tentativa
    (bug real 28/jul: 7 de 7 clientes da rodada morreram no mesmo
    `Timeout 10000ms` esperando o locator da opcao -- o clique no placeholder
    "Selecione..." nem sempre abre o menu, e quando abre a lista as vezes
    ainda nao renderizou). Mesma logica de retry que
    `_clicar_checkbox_por_label_exato` ja usava um passo depois.
    Screenshot por tentativa falha pra dar pra ver o estado real da tela no
    artifact do Actions."""
    slug = re.sub(r"\W+", "_", atributo).strip("_").lower()
    ultimo_erro = ""
    for tentativa in range(1, 4):
        try:
            page.locator('text=Selecione...').first.click(timeout=10_000, force=True)
            time.sleep(1.2)
            opcao = page.locator(f'div[class*="option"]:has-text("{atributo}")').first
            opcao.wait_for(state="visible", timeout=8_000)
            opcao.click(timeout=8_000, force=True)
            time.sleep(1)
            return
        except Exception as exc:
            ultimo_erro = str(exc).splitlines()[0]
            print(f"  {_ts()} [!] tentativa {tentativa}/3 de abrir o atributo '{atributo}' falhou: {ultimo_erro}")
            _shot(page, f"atributo_{slug}_tentativa{tentativa}")
            try:
                page.keyboard.press("Escape")  # fecha menu meio-aberto antes de tentar de novo
            except Exception:
                pass
            time.sleep(1.5)
    raise RuntimeError(f"Nao consegui selecionar o atributo '{atributo}' no dropdown -- {ultimo_erro} (apos 3 tentativas)")


def aplicar_vinculo(page: Page, empresa_id: str, mercos_cliente_id: str, atributo: str,
                    labels: list[str], tag: str) -> None:
    """Marca N valores do MESMO atributo e clica "Adicionar" UMA vez.

    Multi-valor (18/ago/2026, pedido do usuario): um cliente pode ter mais de
    uma tabela de preco (ex. "20% kokeshi / 27% barbours") e mais de uma
    condicao de pagamento (ex. 30d + 45d + 30/45). O ledger guarda a lista na
    mesma coluna, separada por SEPARADOR_MULTI. Marcar tudo antes de um unico
    "Adicionar" e melhor que N passadas: cada passada era um goto + abrir modal
    + dropdown, que e exatamente onde a UI do Mercos falha (bug de 28/jul)."""
    if not labels:
        raise RuntimeError(f"Nenhum valor pra aplicar em '{atributo}'")

    url = f"https://app.mercos.com/{empresa_id}/vinculos-e-permissoes-de-cliente/?cliente_id={mercos_cliente_id}"
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    # ⚠️ "networkidle" e ESPERANCA, nao garantia: a tela do Mercos e uma SPA que
    # segue fazendo request em segundo plano, entao a rede pode nunca ficar
    # ociosa e a espera estoura por tempo mesmo com a pagina inteira pronta.
    # Falhou assim em 10/set/2026 no 1o cliente da rodada (Timeout 15000ms com
    # o modal ja aberto no screenshot) e passou no retry seguinte, sem nada ter
    # mudado -- o sintoma se disfarca de "a pagina nao carregou". Damos o tempo,
    # mas quem decide se da pra seguir e o BOTAO existir na tela.
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        print(f"  {_ts()} [=] rede nao ficou ociosa em 15s -- seguindo pelo botao (SPA faz polling)")
    # ⚠️ Esperar o botao EXISTIR nao basta: ele nasce DESABILITADO e so habilita
    # quando a linha do cliente termina de renderizar e vem marcada. Foi assim
    # que o chamado 141027 morreu em 10/set -- "Timeout 10000ms" no click com o
    # locator resolvido para <button disabled aria-disabled="true">, que se
    # disfarca de "botao nao existe". O portao certo e ele estar HABILITADO.
    page.wait_for_selector(
        'button:has-text("Editar Vínculos e permissões"):not([disabled])',
        timeout=25_000,
    )
    time.sleep(1.5)

    page.click('button:has-text("Editar Vínculos e permissões")', timeout=10_000)
    time.sleep(1.5)

    _selecionar_atributo_vinculo(page, atributo)
    time.sleep(1)

    marcou_algum = False
    for label in labels:
        if _marcar_checkbox_por_label_exato(page, label) == "marcado":
            marcou_algum = True
            time.sleep(0.5)

    if not marcou_algum:
        # Todos os valores ja estavam vinculados (retry de cliente ja aplicado).
        # Clicar "Adicionar" sem nada novo nao acrescenta nada -- so sai.
        print(f"  {_ts()} [=] '{atributo}': todos os {len(labels)} valor(es) ja vinculados -- nada a adicionar")
        _shot(page, f"{tag}_ja_vinculado")
        return

    page.click('button:has-text("Adicionar")', timeout=10_000)
    time.sleep(1.5)
    _shot(page, f"{tag}_apos_adicionar")


def buscar_cliente_id_por_cnpj(page: Page, empresa_id: str, cnpj: str) -> str | None:
    """Corpo unico em `mercos_ui.buscar_cliente_id_por_cnpj` -- depende de seletor
    da tela do Mercos, e duas copias quebram em silencio quando eles mudam o HTML
    (a funcao tem "nao achei" como saida valida). Aqui so injetamos o log e o
    screenshot deste robo."""
    return mercos_ui.buscar_cliente_id_por_cnpj(
        page, empresa_id, cnpj,
        log=lambda msg: print(f"  {_ts()} {msg}"),
        shot=lambda nome: _shot(page, nome),
    )


# ── Notificacao WhatsApp (Evolution/whatsapp_outbox -- canal interno) ──────
# Regra do projeto (docs/modulos/alertas-whatsapp.md): vendedor/RCA SEMPRE
# via Evolution/outbox, nunca Tallos (isso e so pra cliente externo).
#
# MODO TESTE (pedido do usuario 23/jul): manda pro numero pessoal dele em vez
# do vendedor de verdade, ate confirmar que o fluxo completo funciona
# (insert -> worker -> Evolution -> WhatsApp). Trocar WHATSAPP_MODO_TESTE pra
# False quando for pra produção de verdade (manda pro RCA real).
WHATSAPP_MODO_TESTE = True
WHATSAPP_NUMERO_TESTE = "5585986160142"


def _rotulo(singular: str, plural: str, campo) -> str:
    """"Tabela de preço: X" ou "Tabelas de preço: X, Y" -- multi-valor (18/ago)."""
    valores = _valores_multi(campo)
    if len(valores) > 1:
        return f"{plural}: " + ", ".join(valores)
    return f"{singular}: " + (valores[0] if valores else "—")


def notificar_vendedor(sb, vendedor_row: dict, registro: dict) -> None:
    telefone = (vendedor_row.get("telefone") or "").strip()
    if not telefone and not WHATSAPP_MODO_TESTE:
        print(f"  {_ts()} [whatsapp] vendedor sem telefone cadastrado ({vendedor_row.get('nome')}) -- pulando notificacao")
        return

    if WHATSAPP_MODO_TESTE:
        numero = WHATSAPP_NUMERO_TESTE
        prefixo_teste = f"🧪 _[TESTE -- destino real seria {vendedor_row.get('nome')} ({telefone or 'sem telefone'})]_\n"
    else:
        numero = re.sub(r"\D", "", telefone)
        if len(numero) <= 11:  # sem DDI -- assume Brasil
            numero = "55" + numero
        prefixo_teste = ""

    mensagem = (
        f"{prefixo_teste}"
        f"✅ *Cliente vinculado no Mercos*\n"
        f"CNPJ: {registro['cnpj']}\n"
        f"Razão Social: {registro.get('razao_social_planilha') or '—'}\n"
        f"Representante: {registro.get('nome_representante_planilha') or '—'}\n"
        f"{_rotulo('Tabela de preço', 'Tabelas de preço', registro.get('tabela_preco_aplicada'))}\n"
        f"{_rotulo('Condição de pagamento', 'Condições de pagamento', registro.get('condicao_pagamento_aplicada'))}\n"
        f"Já liberado pra realizar pedidos."
    )
    try:
        sb.table("whatsapp_outbox").insert({
            "numero": numero,
            "mensagem": mensagem,
            "prioridade": 5,
            "origem": "torre/cadastro-rca-mercos",
            "referencia": json.dumps({"cnpj": registro["cnpj"], "chamado": registro.get("chamado_goservice")}),
        }).execute()
        print(f"  {_ts()} [whatsapp] enfileirado pra {vendedor_row.get('nome')} ({numero})")
    except Exception as e:
        print(f"  {_ts()} [!] falha ao enfileirar whatsapp: {e}")


# ── Orquestracao ─────────────────────────────────────────────────────────

SEPARADOR_MULTI = " | "  # mesma convencao do decisor.py -- 1 coluna, N valores


def _valores_multi(campo) -> list[str]:
    """Le a coluna multi-valor do ledger ("Tabela 20% | Tabela 27%")."""
    if not campo:
        return []
    return [p.strip() for p in str(campo).split(SEPARADOR_MULTI.strip()) if p.strip()]


def processar_um(page: Page, sb, ws_planilha, usuarios_por_email: dict, registro: dict) -> None:
    cnpj = registro["cnpj"]
    print(f"\n{_ts()} === Processando {cnpj} (chamado {registro.get('chamado_goservice')}) ===")

    empresa_id = registro.get("mercos_empresa_id") or EMPRESA_ID_ES
    regiao = REGIAO_POR_EMPRESA.get(empresa_id, "ES")
    email_vendedor = (registro["vendedor_mercos_email"] or "").lower()

    # ⚠️ SEM REPRESENTANTE nao e erro -- e um tipo de cliente (distribuidor
    # atendido direto, planilha traz "Distribuidor sem representante"/"nao tem").
    # Antes de 24/set/2026 isto virava `RuntimeError` e o robo nao aplicava NADA:
    # o cliente ficava sem tabela e sem condicao no Mercos, e alguem encerrava a
    # linha a mao (foi o que aconteceu com o PREMIER 142181, que ate hoje esta
    # com preco e condicao padrao). Agora aplica os dois e pula so a carteira.
    #
    # ⚠️ So vale para email AUSENTE. Email preenchido que nao casa no CSV segue
    # levantando erro: ali o de-para e que esta incompleto, e pular a carteira
    # calado deixaria o cliente sem dono sem ninguem perceber -- os dois casos
    # parecem iguais no ledger e pedem acoes opostas.
    sem_representante = not email_vendedor
    vendedor_row = None if sem_representante else usuarios_por_email.get((email_vendedor, regiao))
    if sem_representante:
        print(f"  {_ts()} [sem representante] aplicando tabela e condicao; carteira nao se aplica")
    elif not vendedor_row:
        # Dois casos com AÇÕES OPOSTAS, e o rotulo tem que dizer qual e:
        # sem cadastro em filial NENHUMA = de-para incompleto, alguem tem que
        # curar o CSV; sem cadastro NESTA filial = a carteira dele nao existe
        # aqui, e o certo e pular sem carimbar erro.
        outras = _emails_do_representante(usuarios_por_email, email_vendedor)
        if outras:
            raise RepresentanteForaDaFilial(
                f"{email_vendedor} nao tem cadastro Mercos em {regiao} "
                f"(so em {', '.join(sorted(outras))}) -- pulando esta filial"
            )
        raise RuntimeError(f"Nao achei usuario Mercos pro email {registro['vendedor_mercos_email']}")
    vendedor_nome = None if sem_representante else vendedor_row["nome"]

    mercos_cliente_id = registro.get("mercos_cliente_id")
    if not mercos_cliente_id:
        # `alerta_sem_cliente_id`: middleware confirmou o cadastro mas nao
        # devolveu o ID. Procura na tela de Clientes por CNPJ e grava no ledger
        # (o decisor preserva id conhecido, entao resolve de vez).
        mercos_cliente_id = buscar_cliente_id_por_cnpj(page, empresa_id, cnpj)
        if not mercos_cliente_id:
            raise RuntimeError("Sem mercos_cliente_id no ledger e a busca por CNPJ no Mercos nao achou 1 cliente unico")
        sb.table("cadastro_rca_mercos_processado").update(
            {"mercos_cliente_id": mercos_cliente_id}
        ).eq("cnpj", cnpj).eq("mercos_empresa_id", empresa_id).execute()

    condicoes = _valores_multi(registro.get("condicao_pagamento_aplicada"))
    tabelas = _valores_multi(registro.get("tabela_preco_aplicada"))
    if not condicoes or not tabelas:
        raise RuntimeError(
            f"Ledger sem valor normalizado (condicoes={condicoes}, tabelas={tabelas})"
            " -- o decisor marcou alerta de normalizacao, nao da pra aplicar"
        )

    print(f"  {_ts()} [1/3] Condicao(oes) de pagamento -> {', '.join(condicoes)}")
    aplicar_vinculo(page, empresa_id, mercos_cliente_id, "Condições de pagamento", condicoes, "condicao")

    print(f"  {_ts()} [2/3] Tabela(s) de preco -> {', '.join(tabelas)}")
    aplicar_vinculo(page, empresa_id, mercos_cliente_id, "Tabelas de preço", tabelas, "tabela")

    if sem_representante:
        print(f"  {_ts()} [3/3] Carteira -> PULADA (cliente sem representante)")
    else:
        print(f"  {_ts()} [3/3] Carteira -> {vendedor_nome}")
        status_carteira = aplicar_carteira(page, vendedor_nome, cnpj, cnpj)
        if status_carteira != "liberado":
            raise RuntimeError(f"Carteira nao liberou (status_carteira={status_carteira})")

    # ⚠️ `carteira_aplicada` continua FALSE quando nao houve carteira, e o motivo
    # fica escrito em `erro_detalhe` -- gravar True aqui afirmaria um vinculo que
    # nao existe no Mercos, e e por `carteira_aplicada` que a conferencia procura
    # cliente sem dono.
    sb.table("cadastro_rca_mercos_processado").update({
        "status": "processado",
        "vinculos_aplicado": True,
        "carteira_aplicada": not sem_representante,
        "erro_detalhe": (
            "Cliente sem representante na planilha: tabela de preco e condicao de "
            "pagamento aplicadas, carteira nao se aplica."
        ) if sem_representante else None,
        "processado_em": datetime.now(timezone.utc).isoformat(),
    }).eq("cnpj", cnpj).eq("mercos_empresa_id", empresa_id).execute()
    atualizar_status_planilha(
        ws_planilha, registro.get("sheet_row"),
        STATUS_SHEET_OK_SEM_CARTEIRA if sem_representante else STATUS_SHEET_OK)
    # Sem representante nao ha para quem notificar -- e o WhatsApp sai do cadastro
    # do vendedor, que aqui nao existe.
    if not sem_representante:
        notificar_vendedor(sb, vendedor_row, registro)
    print(f"  {_ts()} [OK] {cnpj} processado com sucesso"
          + (" (sem carteira -- sem representante)." if sem_representante else "."))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cnpj", default=None, help="processa so este CNPJ (retry manual, ignora status)")
    parser.add_argument("--cnpjs", default=None, help="processa esta lista de CNPJs, separados por virgula (retry manual, ignora status) -- selecao vinda da Torre")
    parser.add_argument("--limite", type=int, default=None)
    parser.add_argument("--visivel", action="store_true")
    parser.add_argument("--filial", choices=sorted(EMPRESAS), default="es",
                        help="empresa Mercos da rodada (default es). Vale pro login E pro filtro do ledger.")
    args = parser.parse_args()

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    usuarios_por_email = carregar_usuarios_mercos()

    empresa_alvo = EMPRESAS[args.filial]
    query = sb.table("cadastro_rca_mercos_processado").select("*").eq("mercos_empresa_id", empresa_alvo)
    if args.cnpjs:
        cnpjs_digits = [re.sub(r"\D", "", c).zfill(14) for c in args.cnpjs.split(",") if c.strip()]
        query = query.in_("cnpj", cnpjs_digits)
        registros = query.execute().data
    elif args.cnpj:
        cnpj_digits = re.sub(r"\D", "", args.cnpj).zfill(14)
        query = query.eq("cnpj", cnpj_digits)
        registros = query.execute().data
    else:
        # Lote automatico (sem --cnpj/--cnpjs): pula chamado antigo (pedido
        # do usuario 24/jul, mesmo corte do decisor.py). chamado_goservice
        # e TEXT -- comparar numero exige filtrar em Python, .gte() do
        # postgrest faria comparacao lexicografica errada ("9999" > "30767").
        query = query.eq("status", "pendente")
        todos = query.execute().data
        registros = [r for r in todos if _chamado_acima_do_minimo(r.get("chamado_goservice"))]
        if args.limite:
            registros = registros[: args.limite]

    print(f"{_ts()} {len(registros)} cliente(s) selecionado(s) ({args.filial.upper()}) a processar.")
    for r in registros:
        print(f"  - {r['cnpj']} | chamado {r.get('chamado_goservice')} | vendedor {r.get('vendedor_mercos_email')}"
              f" | tabela {r.get('tabela_preco_aplicada')} | condicao {r.get('condicao_pagamento_aplicada')}")
    if not registros:
        return

    ws_planilha = _abrir_planilha_para_escrita()

    # Antes de qualquer coisa caro (Chromium, login, 2FA): a credencial que le o
    # e-mail do 2FA ainda vale? Sem isso o run gasta ~45s e morre com traceback
    # de imaplib no meio do log (18/ago/2026).
    if GMAIL_USER and GMAIL_SENHA:
        conferir_credencial_gmail(GMAIL_USER, GMAIL_SENHA, MERCOS_EMAIL)
    else:
        print(f"{_ts()} [2fa] sem GMAIL_USER/GMAIL_SENHA -- seguindo sem checar IMAP. "
              "Se o Mercos pedir o codigo, o run aborta explicando (nao ha como ler o e-mail).")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.visivel)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        print(f"{_ts()} Login Mercos ({MERCOS_EMAIL})...")
        login_mercos(page, empresa_alvo, MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)

        voltas = 0
        caiu_no_meio: list[str] = []
        pulados: list[str] = []
        for indice, registro in enumerate(registros):
            try:
                processar_um(page, sb, ws_planilha, usuarios_por_email, registro)
            except RepresentanteForaDaFilial as pulo:
                # Nao toca no ledger nem na planilha: nao houve falha, so nao ha
                # o que fazer nesta filial pra este representante.
                print(f"  {_ts()} [pulado] {registro['cnpj']}: {pulo}")
                pulados.append(registro["cnpj"])
                continue
            except Exception as exc:
                if _sessao_caiu(page):
                    # Nao e erro deste cliente -- e do ambiente. Marcar erro aqui
                    # carimbaria "Erro mercos" na planilha de todo mundo que ainda
                    # nao rodou (foi o que aconteceu em 18/ago: 21 linhas).
                    restantes = len(registros) - indice - 1
                    _shot(page, "sessao_caiu")
                    motivo = _motivo_sessao(page)
                    if voltas < VOLTAS_POR_RODADA and _reconquistar_sessao(
                            page, motivo, voltas + 1, VOLTAS_POR_RODADA, empresa_alvo):
                        voltas += 1
                        caiu_no_meio.append(registro["cnpj"])
                        # ⚠️ SEGUE PARA O PROXIMO -- nao refaz o que caiu.
                        # `_liberar_cliente` procura o botao `habilitar_`, que
                        # some quando o cliente JA esta na carteira do vendedor:
                        # refazer um cliente cuja carteira ja passou devolveria
                        # `nao_encontrado` e o robo carimbaria "Erro mercos" num
                        # cliente que na verdade deu certo. Ele fica `pendente`,
                        # sem carimbo, e volta na proxima rodada -- que e
                        # exatamente o estado em que a queda ja o deixava.
                        continue
                    if voltas:
                        motivo += f" (ja tinha voltado {voltas}x nesta rodada)"
                    raise SessaoMercosCaiu(
                        f"{motivo}. Parei em {registro['cnpj']} "
                        f"(erro visivel: {str(exc).splitlines()[0]}); "
                        f"{restantes} cliente(s) nem chegaram a ser tentados e "
                        "continuam pendentes -- nada foi marcado como erro nem "
                        "escrito na planilha. Rode de novo com ninguem mais logado "
                        "nessa conta do Mercos."
                    ) from exc
                print(f"  {_ts()} [ERRO] {registro['cnpj']}: {exc}")
                sb.table("cadastro_rca_mercos_processado").update({
                    "status": "erro",
                    "erro_detalhe": str(exc)[:2000],
                }).eq("cnpj", registro["cnpj"]).eq("mercos_empresa_id", empresa_alvo).execute()
                atualizar_status_planilha(ws_planilha, registro.get("sheet_row"), STATUS_SHEET_ERRO)
                _shot(page, f"erro_{registro['cnpj']}")

        # Queda que o robo contornou nao pode sumir do log: e o sinal de que a
        # conta do robo esta sendo disputada com gente, e e o argumento pra um
        # dia dar um usuario proprio a ele.
        if pulados:
            print("")
            print(f"{_ts()} [pulado] {len(pulados)} cliente(s) desta filial tem representante "
                  f"sem cadastro Mercos aqui -- continuam pendentes, sem erro: "
                  + ", ".join(pulados))

        if caiu_no_meio:
            print("")
            print(f"{_ts()} [sessao] a sessao caiu {voltas}x nesta rodada e o robo voltou. "
                  f"{len(caiu_no_meio)} cliente(s) ficaram PENDENTES sem carimbo (rode de novo): "
                  + ", ".join(caiu_no_meio))

        browser.close()


if __name__ == "__main__":
    main()
