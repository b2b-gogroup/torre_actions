#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CreditoLimite-Mercos -- REPOSITOR de credito disponivel. Peca 3 de
`docs/kaique/03-robo-do-mercos.md`.

O QUE FAZ, em uma frase: devolve ao "limite disponivel" do Mercos o valor de um
boleto que o cliente PAGOU e que o Mercos nunca somou de volta.

    fila = credito_evento_pagamento WHERE processado = false   (atividade 02)

⚠️ A FILA E POR BOLETO; A ESCRITA E POR CLIENTE (24/ago/2026). Os boletos
marcados na tela que sao do mesmo CNPJ viram UMA soma e UMA ida ao Mercos --
antes era um ciclo inteiro (buscar cliente, abrir modal, ler, salvar) por boleto,
~6 s cada. A selecao continua por boleto de proposito: e ela que impede repor o
que alguem ja repos na mao. Ver `processar_grupo`.

⚠️ ELE NAO MEXE NO "LIMITE TOTAL". Decisao do usuario, 20/ago/2026, literal:
"nao vamos mexer no limite total enquanto as regras do financeiro nao ficarem
prontas. ate la, apenas vamos repor aquilo que estava subtraido do disponivel e
nao voltou automatico apos o pagamento ser identificado."
O total e politica de credito (o teto que o cliente pode alcancar) e depende da
homologacao do financeiro. O disponivel e saldo -- e e ele que TRAVA A VENDA.

── POR QUE O DINHEIRO NAO VOLTA SOZINHO ────────────────────────────────────────
O Mercos SUBTRAI do disponivel quando o pedido entra, e nunca soma de volta
quando o boleto e pago -- porque ele nao sabe do pagamento em boleto (so de Pix
e cartao feitos pelo link dele; confirmado por quem opera o Mercos, 19/ago). Quem
ve o boleto e o banco, e o banco a Torre le. Dai o robo.

    devia 13k de um teto de 15k        -> disponivel 2k
    pagou 5k no boleto                 -> Mercos nao fica sabendo
    deveria ter 7k livres              -> continua com 2k = travado

── A TELA, MAPEADA AO VIVO (20/ago/2026, sessao logada do usuario) ─────────────
NAO e uma URL de formulario: e um modal da SPA, aberto por botao.

    ficha do cliente:  /{empresa}/clientes/{cliente_id}/
    abre o modal:      button[class*="LimiteDeCredito__iconeEdicao"]   (icone de lapis
                       dentro do card "Limite de credito")
    modal:             "Editar limite de credito"
      #limiteDisponivel   <- O UNICO campo que este robo escreve
      #limiteTotal        <- NAO TOCAR
    salvar:            button com texto "Salvar"   |   sair: "Cancelar"

⚠️ As classes sao CSS-modules com hash (`LimiteDeCredito__iconeEdicao___QX9Hg`).
Casamos pelo PREFIXO (`*=`), porque o hash muda a cada build deles. Se o prefixo
mudar, o robo levanta `TelaNaoMapeada` -- nunca clica em botao vizinho.

⚠️ O `id` dos inputs (`limiteDisponivel`/`limiteTotal`) e estavel e legivel, mas
o robo AINDA confere o rotulo do campo antes de digitar: id certo com rotulo
trocado (eles inverterem os campos num redesign) escreveria saldo no teto.

── TESTADO EM PRODUCAO, E REVERTIDO (20/ago/2026) ──────────────────────────────
Um cliente real (Della E Delle, id 64488634), com aprovacao passo a passo do
Kaique: 21.778,32 -> 22.129,68 -> de volta pra 21.778,32. Janela de 70 segundos,
rollback conferido nas duas fontes. O que ficou provado:
  · o Mercos ACEITA a escrita pelo modal, nas duas direcoes;
  · salvar so o disponivel NAO mexe no total (ficou 30.000,00 nas duas gravacoes);
  · o Mercos NAO recalcula o disponivel depois de salvar -- grava o que se digita;
  · a conta do robo bate ao centavo.
⚠️ Nao foi o CODIGO que rodou (foi a mao, pelo navegador, com as mesmas guardas).
Registro completo: `docs/kaique/teste-mercos-20082026-ROLLBACK.md`.

── COMO A CONTA E FEITA ────────────────────────────────────────────────────────
    novo_disponivel = min(disponivel_lido_na_tela + valor_pago, limite_total)

Le na tela imediatamente antes de escrever: somar sobre um numero de 10 minutos
atras ignora tudo que o Mercos subtraiu no meio.

── OS GUARDAS, E O QUE CADA UM EVITA ───────────────────────────────────────────
 1. `limite_total = 0` -> PULA. 63 de 75 clientes amostrados estao em 0/0, o que
    significa "sem limite controlado". Escrever disponivel neles CRIARIA um
    controle que nao existe -- o robo passaria a travar quem hoje compra livre.
 2. Clamp no total: disponivel nunca passa do teto. Se passar, grava o teto e
    declara.
 3. Ja esta no teto -> nada a repor, marca processado sem digitar.
 4. `eh_origem_integracao` -> nao mexe: o valor tem outro escritor.
 5. Cliente em 2 empresas do Mercos -> erro nomeado, nunca escolhe.
 6. Releitura obrigatoria depois de salvar (ver `escrever_disponivel`).

⚠️ IDEMPOTENCIA -- ESTE ROBO SOMA UM DELTA, e delta aplicado 2x devolve dinheiro
2x. A protecao e a fila: um boleto = uma linha = `processado` uma vez, para
sempre (`id_boleto` e PK do proprio Itau). Por isso:
  · marca `processado` SO depois de a releitura confirmar o valor novo;
  · se a releitura mostrar o valor ANTIGO, nao gravou -> fica na fila;
  · se mostrar um TERCEIRO valor (nem o antigo nem o esperado), marca processado
    com aviso de conferir na mao. Entre "repor de novo sobre estado desconhecido"
    e "nao repor e avisar", a segunda e a recuperavel.

── QUANDO FALHA, AVISA (P3 fechada em 21/ago/2026) ─────────────────────────────
Erro, "conferir na mao", rodada abortada ou crash mandam e-mail pra
`AVISO_PARA` (default: kaique.breno@gocase.com), pelo mesmo App Password do Gmail
que o robo ja usa pro 2FA -- nenhum secret novo. Rodada limpa nao gera e-mail.
O envio nunca derruba a rodada nem muda o exit code. Detalhe e o limite honesto
(se a credencial do Gmail for A falha, o aviso nao sai) em `aviso.py`.

⚠️ APROVACAO HUMANA = O DISPARO MANUAL. A doc fixou "nada sobe sozinho na
primeira versao". Nao existe tabela de aprovacao de reposicao, e inventar uma
seria outra atividade -- entao a aprovacao e uma pessoa rodando o workflow com
`--aplicar`, por lote, sabendo o que vai acontecer. E por isso que NAO HA CRON.

Config por variavel de ambiente:
    MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (a chave anon NAO serve: RLS)
    MERCOS_EMPRESAS (opcional, default 424525 = Beauty Hub Atacado ES)
    AVISO_PARA      (opcional, default kaique.breno@gocase.com -- aceita varios
                    separados por virgula; e o caminho pra trocar quem recebe o
                    aviso de falha SEM mexer em codigo. Ver `aviso.py`.)

Uso:
    python repositor.py --mapear              # abre a tela do 1o da fila e confere o mapeamento
    python repositor.py                       # dry-run: diz o que faria (padrao)
    python repositor.py --aplicar --limite 5  # repoe de verdade, no maximo 5
    python repositor.py --aplicar --cnpj 12345678000199
    python repositor.py --testar-aviso        # so testa o e-mail de falha, nao toca em nada
    python repositor.py --varredura --limite 50   # SO LE o limite de 50 clientes -> espelho
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import os
import re
import json
import time
import argparse
import unicodedata
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright, Page
from supabase import create_client

# Helpers de UI do Mercos: corpo unico, compartilhado com o robo de cadastro.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE_DIR, "..", "cadastro-rca-mercos"))
from mercos_login import conferir_credencial_gmail, login_mercos  # noqa: E402
import mercos_ui  # noqa: E402

from auditoria import (Auditoria, AuditoriaIndisponivel,  # noqa: E402
                       RodadaDeCronJaFeita, disparo_e_cron)
# Aviso de falha por e-mail (P3 fechada em 21/ago/2026) -- ver `aviso.py`.
from aviso import avisar_falha, destinatarios, enviar_teste  # noqa: E402

MERCOS_EMAIL = os.environ["MERCOS_EMAIL"]
MERCOS_SENHA = os.environ["MERCOS_SENHA"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_SENHA = os.environ["GMAIL_SENHA"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

LOGS_DIR = os.path.join(BASE_DIR, "logs")
# ⚠️ `os.environ.get(chave, default)` NAO cai no default quando a variavel EXISTE
# vazia -- e no Actions `${{ vars.X }}` de uma variavel nao cadastrada chega
# exatamente assim: string vazia. Sem este `or`, EMPRESAS virava [] e o robo
# morria em `EMPRESAS[0]` com IndexError, no primeiro passo, antes de qualquer
# log util. Medido em 20/ago: o repo nao tem NENHUMA variable cadastrada.
EMPRESAS = [e.strip() for e in (os.environ.get("MERCOS_EMPRESAS") or "424525").split(",") if e.strip()]
if not EMPRESAS:
    raise SystemExit("MERCOS_EMPRESAS veio sem nenhum id de empresa utilizavel.")

# Teto por rodada. Escrita em sistema de terceiro nao deve poder sair em massa
# por acidente; `--limite` maior sobrescreve, de propósito.
TETO_RODADA_PADRAO = 20

# ── Voltar depois de a sessao cair ─────────────────────────────────────────
# O Mercos aceita 1 sessao por usuario e a conta do robo e de uma PESSOA, entao
# qualquer login dela no meio da rodada derruba o robo. Nao ha usuario exclusivo
# pra dar ao robo (decisao de 26/ago/2026) -- logo, cair faz parte da vida dele e
# o que importa e VOLTAR.
#
# ⚠️ POR QUE A VOLTA E DENTRO DA MESMA EXECUCAO, e nao "roda de novo depois": o
# indice unico `ux_credito_reposicao_cron_dia` deixa UMA rodada de cron por dia
# BRT. Um segundo disparo -- outro cron, ou "Re-run" no Actions -- e barrado pela
# trava e sai com 0 sem tocar na fila. Ou seja: se a rodada de hoje morreu no
# meio, ou ela mesma volta, ou os boletos esperam ate amanha.
#
# ⚠️ POR QUE E SEGURO REFAZER O CLIENTE QUE CAIU: `SessaoDerrubada` so nasce em
# `resolver_cliente` e em `abrir_modal_limite`, os dois ANTES de qualquer clique
# em Salvar. Queda depois de escrever nao chega aqui -- vira `ConferirNaMao`, que
# carimba o boleto de propósito pra ninguem somar o delta duas vezes.
VOLTAS_POR_RODADA = int(os.environ.get("CREDITO_VOLTAS_SESSAO") or 2)
# ⚠️ 15 MINUTOS, E A DEMORA E O RECURSO -- nao um efeito colateral. Reentrar na
# hora ROUBA a sessao de volta de quem acabou de entrar: o robo derruba a pessoa,
# a pessoa loga de novo e derruba o robo, e os dois perdem a tarde. Quinze
# minutos e tempo de uma consulta no Mercos terminar; quem ficou mais que isso
# esta trabalhando de verdade, e ai o certo e o robo desistir e voltar amanha.
# Decisao do Kaique, 26/ago/2026.
ESPERA_VOLTA_S = int(os.environ.get("CREDITO_ESPERA_VOLTA_S") or 900)
# ⚠️ QUANTAS FALHAS SEGUIDAS ANTES DE DESISTIR DA VARREDURA. 15 = 1,5x o maior
# bloco de "nao existe no Mercos" ja MEDIDO em rodada sadia (10 seguidos no run
# 35073837491 de 16/set; 7 no 35200204803 de 17/set) -- e esses nem contam aqui,
# porque ausencia provada zera o contador. Serve pro caso em que a tela do Mercos
# mudou e todo cliente passa a dar erro: sem isso o robo varre a fila inteira
# errando, que foi o que gastou 52 min em 22/set.
FALHAS_SEGUIDAS_LIMITE = int(os.environ.get("CREDITO_FALHAS_SEGUIDAS") or 15)
# ⚠️ ESPERAR SO VALE SE COUBER NA JANELA DO ACTIONS. `timeout-minutes` mata o job
# no meio do sono: sem rodada fechada, sem e-mail, sem nada -- pior que abortar,
# porque a queda fica MUDA. Este numero tem que ser MENOR que o `timeout-minutes`
# do workflow (60 hoje), e o workflow passa o valor dele em CREDITO_JANELA_MIN.
JANELA_MIN = int(os.environ.get("CREDITO_JANELA_MIN") or 55)
# Sobra pra terminar o cliente da vez, fechar a rodada e mandar o e-mail depois
# de a espera acabar. Sem ela, a volta cabe e a prestacao de contas nao.
MARGEM_FIM_S = 240
_COMECOU_EM = time.time()

# ⚠️ NAO existe constante de "quantos erros ate emperrar" aqui, de proposito: o
# corte mora na view `v_credito_pagamento_emperrado` (migration 20260824e), que e
# lida tambem pela Gestao de Credito e pela conferencia manual. Repetir o numero
# neste arquivo criaria a terceira copia da mesma regra -- ver `emperrados_da_fila`.

# ── Seletores da tela (mapeados ao vivo 20/ago/2026) ───────────────────────
SEL_BOTAO_EDITAR = 'button[class*="LimiteDeCredito__iconeEdicao"]'
SEL_CARD_LIMITE = '[class*="LimiteDeCredito__header"]'
ID_DISPONIVEL = "limiteDisponivel"
ID_TOTAL = "limiteTotal"
ROTULO_DISPONIVEL = "limite disponivel"
ROTULO_TOTAL = "limite total"

_t0 = None


def _ts() -> str:
    global _t0
    agora = datetime.now()
    if _t0 is None:
        _t0 = agora
    return f"[{agora.strftime('%H:%M:%S')} +{int((agora - _t0).total_seconds()):>3}s]"


def log(msg: str) -> None:
    print(f"  {_ts()} {msg}", flush=True)


def _shot(page: Page, nome: str) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    caminho = os.path.join(LOGS_DIR, f"repositor_{nome}.png")
    try:
        page.screenshot(path=caminho, full_page=True)
        log(f"[debug] logs/{os.path.basename(caminho)}")
    except Exception as e:
        log(f"[debug] screenshot falhou: {e}")
    return caminho


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _para_numero(txt) -> float | None:
    """'12.895,74' / 'R$ 15000' / '' -> float. None quando nao da pra ler, e
    None NAO vira 0: 0 significaria 'sem limite' e mudaria a decisao do robo."""
    d = re.sub(r"[^\d,.-]", "", str(txt or "")).strip()
    if not d:
        return None
    if "," in d:                      # pt-BR: ponto e milhar, virgula e decimal
        d = d.replace(".", "").replace(",", ".")
    try:
        return float(d)
    except ValueError:
        return None


def _br(v: float) -> str:
    """15000 -> '15.000,00'. Usado em LOG e mensagem, nao para digitar no campo."""
    return f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _para_digitar(v: float) -> str:
    """15000 -> '15000,00'. SEM ponto de milhar, de propósito.

    ⚠️ MEDIDO no teste de 20/ago/2026 (docs/kaique/teste-mercos-20082026-ROLLBACK.md):
    o campo do Mercos DESCARTA o ponto de milhar na digitacao -- digitei
    '22.129,68' e o campo ficou '22129,68'. Salvou certo (22129.68 gravado), ou
    seja a mascara tolera as duas grafias hoje. Mandamos sem o ponto porque e a
    forma que SOBREVIVE no campo: depender da tolerancia da mascara e apostar que
    ela nao vai mudar num redesign deles.
    """
    return f"{v:,.2f}".replace(",", "").replace(".", ",")


class TelaNaoMapeada(RuntimeError):
    """A tela do Mercos nao esta como o mapeamento diz. NUNCA vira chute."""


class SessaoDerrubada(RuntimeError):
    """A conta do Mercos foi usada em outro lugar. Aborta a rodada inteira."""


class ClienteInexistente(RuntimeError):
    """O CNPJ foi procurado ATE O FIM, com sessao viva, e nao existe no Mercos.

    ⚠️ EXISTE PORQUE `except RuntimeError` E LARGO DEMAIS PRA ESTA DECISAO, e isso
    custou a varredura de 22/set/2026 (run 35704635785). `SessaoDerrubada` e
    subclasse de `RuntimeError`, entao o `except RuntimeError` de `varrer_um`
    engolia a QUEDA DE SESSAO, carimbava o cliente como ausente no espelho e a
    devolvia como `PulaSemErro` -- que o laco trata como "segue em frente". Efeito
    medido: a sessao caiu aos 5 min e o robo passou **52 minutos** marcando 149
    clientes como "nao existe no Mercos", terminando VERDE (`ok: true`), sem
    abortar e sem e-mail. O `except SessaoDerrubada` do laco, com as voltas de
    `reconquistar_sessao`, nunca chegou a rodar uma vez sequer.
    ⚠️ So ausencia PROVADA carimba o espelho. Qualquer outro `RuntimeError`
    (sessao caida, CNPJ em mais de uma empresa, tela fora do mapeamento) vira
    FALHA -- que nao escreve nada -- em vez de virar uma afirmacao falsa sobre o
    cliente. `nao_encontrado` bloqueia o botao Liberar e some com o teto do motor:
    marcar por engano nao e ruido de log, e cliente travado na tela de credito.
    """


class ConferirNaMao(RuntimeError):
    """A escrita pode ter acontecido e nao deu pra confirmar. Marca processado
    com aviso: repetir somaria o delta de novo."""


class PulaSemErro(RuntimeError):
    """Nao ha nada a repor (cliente sem limite, saldo ja cheio). Nao e falha:
    marca `processado` com o motivo, senao a linha volta em toda rodada."""


class PrecisaDeGente(RuntimeError):
    """Nao escreve e NAO carimba: o item CONTINUA na fila, esperando decisao
    humana.

    ⚠️ E o oposto de `PulaSemErro`, e confundir os dois e o defeito. `PulaSemErro`
    FECHA o assunto ("nao havia o que fazer aqui"); este diz "ha o que fazer e eu
    nao posso decidir". Carimbar um caso destes o faria sumir da fila sem nunca
    ter acontecido -- que e a forma de um robo perder trabalho em silencio."""


# ── Fila de recibos ────────────────────────────────────────────────────────

COLS = "id, id_boleto, cnpj, referencia, vencimento, valor_pago, data_pagamento, detectado_em"


def so_digitos(cnpjs: str | None) -> list[str]:
    """CSV de CNPJ -> lista de digitos puros, sem repetido e preservando a ordem.

    ⚠️ Dedup obrigatorio: o mesmo CNPJ duas vezes na lista faria a fila daquele
    cliente ser lida duas vezes (`.in_` nao duplica linha, mas a contagem que a
    tela mostra sairia errada) e, pior, esconderia um erro de quem montou a lista.
    """
    if not cnpjs:
        return []
    vistos: list[str] = []
    for parte in cnpjs.split(","):
        d = re.sub(r"\D", "", parte)
        if d and d not in vistos:
            vistos.append(d)
    return vistos


def carregar_fila(sb, cnpjs: list[str], teto: int, boletos: list[str] | None = None,
                  emperrados: dict[str, dict] | None = None) -> list[dict]:
    """A fila, opcionalmente restrita a BOLETOS ou a uma lista de clientes.

    ⚠️ `boletos` GANHA de `cnpjs`, e existe por um defeito real (achado pelo Kaique
    em 21/ago/2026, antes da 1a rodada): filtrar por CNPJ repoe TODOS os pagamentos
    pendentes daquele cliente. Se ele pagou 4 boletos e alguem ja repos 2 na mao, os
    4 continuam `processado=false` (nada baixa a fila quando o ajuste e manual --
    conferido: nao existe trigger nem vinculo com `credito_liberacao`), e a rodada
    somaria os 2 ja repostos DE NOVO. Com `--boletos`, o que a pessoa marcou na tela
    e exatamente o que e reposto.
    O caminho por CNPJ continua, mas e para uso consciente na linha de comando.

    ⚠️ O TETO NAO SE APLICA A UMA SELECAO EXPLICITA. Se a Torre mandou 30 CNPJs
    marcados por uma pessoa, cortar em 20 escreveria em 20 e deixaria 10 sem
    aviso -- a pessoa aprovou 30 e veria "rodada concluida". Teto existe pra
    proteger a rodada CEGA (a que pega "os N primeiros da fila"); selecao
    conferida por gente e o proprio limite.
    """
    q = (sb.table("credito_evento_pagamento").select(COLS)
         .eq("processado", False)
         .order("data_pagamento", desc=False))
    if boletos:
        q = q.in_("id_boleto", boletos)
    elif cnpjs:
        q = q.in_("cnpj", cnpjs)
    linhas = q.execute().data or []
    if boletos or cnpjs:
        # ⚠️ Selecao explicita NAO passa pelo filtro de emperrado: pedir um boleto
        # pelo id e justamente como se tenta de novo o que emperrou, depois de
        # consertar a causa no Mercos.
        return linhas
    if emperrados:
        antes = len(linhas)
        linhas = [e for e in linhas if e["id"] not in emperrados]
        if antes != len(linhas):
            log(f"[fila] {antes - len(linhas)} pagamento(s) EMPERRADO(S) fora desta rodada "
                "(erros repetidos no mesmo boleto) -- ver o resumo no fim")
    return linhas[:teto] if teto else linhas


def resumir_emperrados(emperrados: dict[str, dict]) -> None:
    """Escreve no log os pagamentos que o robo nao consegue repor.

    ⚠️ NAO manda e-mail, e isso e escolha. `aviso.py` avisa EVENTO (a rodada
    falhou); emperrado e ESTADO, e estado que persiste vira e-mail diario que
    ninguem le mais em uma semana. O canal do estado e a pilula da Gestao de
    Credito, que fica amarela enquanto o caso existir.
    """
    if not emperrados:
        return
    total = sum(float(d.get("valor_pago") or 0) for d in emperrados.values())
    print()
    print(f"── {len(emperrados)} PAGAMENTO(S) QUE O ROBO NAO CONSEGUE REPOR "
          f"(R$ {_br(total)}) ──────────────")
    for d in list(emperrados.values())[:20]:
        print(f"   boleto {d.get('id_boleto')} · cnpj {d.get('cnpj')} · "
              f"{d.get('tentativas')} tentativa(s) · ultimo erro {d.get('ultimo_erro_em')}")
        print(f"     {str(d.get('motivo') or '')[:200]}")
    if len(emperrados) > 20:
        print(f"   (+{len(emperrados) - 20} nao listados -- SELECT * FROM "
              "v_credito_pagamento_emperrado)")
    print("   O que fazer: consertar a causa no Mercos e rodar o workflow com")
    print("   modo=aplicar e boletos=<id_boleto> (selecao explicita ignora este filtro).")
    print("──────────────────────────────────────────────────────────────────────")


def emperrados_da_fila(sb) -> dict[str, dict]:
    """{evento_id: {...}} dos pagamentos que o robo tentou repor e nao consegue.

    ⚠️ A REGRA MORA NA VIEW `v_credito_pagamento_emperrado` (migration 20260824e),
    nao aqui. Tres lugares precisam da mesma resposta -- este robo, a pilula da
    Gestao de Credito e quem confere na mao -- e tres copias do "3+ erros"
    divergiriam em semanas, com a copia que ninguem conferiu decidindo o que o robo
    faz. O `HAVING`, o corte e o porque estao na migration.

    ⚠️ POR QUE FILTRAR: erro NAO carimba a fila de proposito (a constraint
    `credito_evento_pagamento_proc_coerente_chk` diz "pendente = sem resultado", e
    o boleto tem que poder ser tentado de novo). Mas a rodada CEGA le a fila pelo
    pagamento MAIS ANTIGO e corta no `--limite`: um boleto que falha sempre fica na
    CABECA da fila e come uma vaga todo dia, para sempre.
    """
    try:
        linhas = (sb.table("v_credito_pagamento_emperrado")
                  .select("evento_id, id_boleto, cnpj, valor_pago, tentativas, "
                          "ultimo_erro_em, motivo")
                  .order("ultimo_erro_em", desc=True)
                  .limit(5000).execute().data or [])
    except Exception as e:
        # ⚠️ Falhar aqui NAO pode parar a rodada: sem esta leitura o robo volta a
        # ser o de antes (tenta tudo), que e o lado certo de degradar. Mas TEM que
        # aparecer no log: filtro que sumiu em silencio e a vaga comida de volta.
        log(f"[!] nao consegui ler v_credito_pagamento_emperrado ({e}) -- "
            "esta rodada NAO vai filtrar emperrado")
        return {}
    return {l["evento_id"]: l for l in linhas if l.get("evento_id")}


def gravar_espelho(sb, *, cnpj: str, empresa_id: str | None, cliente_id: str | None,
                   nome: str | None, disponivel, total, origem: str,
                   rodada_id: str | None, ilegivel: bool = False) -> None:
    """Grava o retrato do limite lido na tela em `credito_limite_mercos`.

    A Torre nunca conheceu o **disponivel** do Mercos (so o teto, do Protheus) --
    e e o disponivel que TRAVA A VENDA. Como o robo ja le os dois antes de
    escrever, o espelho sai de graca: pedido do Kaique em 21/ago/2026.

    ⚠️ E RETRATO, NAO O VALOR DE AGORA. O Mercos subtrai a cada pedido novo, entao
    este numero comeca a envelhecer no segundo seguinte. Por isso `lido_em` e
    obrigatorio e toda tela mostra a data junto.

    ⚠️ ESTE ROBO NAO LE ESTA TABELA PARA CALCULAR NADA. A conta sai sempre da
    leitura ao vivo, imediatamente antes de escrever -- somar sobre um numero de
    10 minutos atras ignoraria tudo que o Mercos subtraiu no meio, e o resultado
    seria credito a mais para o cliente, do nosso bolso.

    ⚠️ NUNCA levanta: falhar em gravar o espelho nao pode derrubar a reposicao,
    que e o trabalho de verdade. Vira linha de log.
    """
    try:
        anterior = (sb.table("credito_limite_mercos").select("leituras")
                    .eq("cnpj", cnpj).limit(1).execute().data or [])
        leituras = int((anterior[0].get("leituras") if anterior else 0) or 0) + 1
        sb.table("credito_limite_mercos").upsert({
            "cnpj": cnpj,
            "empresa_id": empresa_id,
            "cliente_mercos_id": cliente_id,
            "cliente_nome": nome,
            "disponivel": disponivel,
            "total": total,
            "lido_em": datetime.now(timezone.utc).isoformat(),
            "origem": origem,
            "rodada_id": rodada_id,
            "leituras": leituras,
            # ⚠️ "abri e nao consegui ler" NAO e "nao tem limite". Sem esta marca,
            # disponivel NULL virava "R$ 0,00" na tela -- achado em 21/ago testando
            # a varredura, em 3 clientes (SENAC entre eles). Mesma familia de erro
            # do dia inteiro: ausencia de leitura aparecendo como valor zero.
            "ilegivel": bool(ilegivel),
            # ⚠️ DESMENTE A MARCA ANTIGA, e a falta disto e um bug separado do de
            # 22/set: o upsert so toca as chaves que manda, entao `nao_encontrado`
            # ficava `true` PARA SEMPRE depois de um carimbo errado -- mesmo com o
            # robo tendo achado o cliente e lido os limites dele agora. Medido em
            # 22/set/2026, antes do conserto: **299 linhas** diziam "nao existe no
            # Mercos" carregando `cliente_mercos_id` do proprio Mercos, e 199
            # delas com `disponivel` preenchido. A linha se contradizia sozinha, e
            # quem lia era o botao Liberar (que via teto nulo e travava).
            # Chegar aqui E a prova de que o cliente existe -- entao a marca cai.
            "nao_encontrado": False,
        }, on_conflict="cnpj").execute()
    except Exception as e:
        log(f"[!] espelho: nao gravei o limite de {cnpj}: {e}")


def fila_varredura(sb, teto: int, cnpjs_pedidos: list[str] | None = None,
                   universo: str = "credito", frescor_dias: int = 0) -> list[dict]:
    """Quem a varredura vai LER nesta rodada.

    ⚠️ QUEM VARRER MORA NO BANCO, nao aqui: `v_credito_varredura_fila`
    (migration 20260821g). Decisao do dono da Torre em 21/ago/2026 -- varrer os
    ~800 clientes que aparecem na tela de credito, nao os 6.269 de `dim_cliente`:
    dos 121 primeiros lidos, 41 nao existem no Mercos e so 14 tem teto, entao a
    base inteira gastaria ~10 horas para trazer quase nada.
    `--universo todos` ainda existe para o caso de alguem precisar da base toda.

    ⚠️ INCREMENTAL POR DESENHO. Cada leitura custa uma busca por CNPJ no Mercos (a
    parte cara), e o workflow morre em 30 minutos. Entao cada rodada leva `teto`
    clientes, na ordem "prioridade, depois nunca lido, depois retrato mais velho",
    e rodadas sucessivas cobrem a fila sozinhas.

    ⚠️ A ORDEM E O QUE FAZ A COBERTURA FECHAR. Sem ela, uma varredura que morre no
    meio recomecaria pelos mesmos clientes e os ultimos nunca seriam lidos.

    `frescor_dias` > 0 pula quem foi lido nos ultimos N dias -- e o que faz a
    varredura noturna PARAR de trabalhar sozinha quando tudo esta em dia, em vez
    de reler a mesma base para sempre.
    """
    if cnpjs_pedidos:
        return [{"cnpj": c, "nome": None, "lido_em": None, "prioridade": 0}
                for c in cnpjs_pedidos][:teto or None]

    if universo == "credito":
        linhas = (sb.table("v_credito_varredura_fila")
                  .select("cnpj, nome, lido_em, prioridade")
                  .limit(20000).execute().data or [])
    else:
        # Base inteira: `dim_cliente` + o que o espelho ja sabe.
        base = (sb.table("dim_cliente").select("cnpj, nome_cliente")
                .not_.is_("cnpj", "null").limit(20000).execute().data or [])
        espelho = {r["cnpj"]: r.get("lido_em")
                   for r in (sb.table("credito_limite_mercos").select("cnpj, lido_em")
                             .limit(20000).execute().data or [])}
        linhas = [{"cnpj": c.get("cnpj"), "nome": c.get("nome_cliente"),
                   "lido_em": espelho.get(c.get("cnpj")), "prioridade": 9}
                  for c in base]

    linhas = [{**l, "cnpj": re.sub(r"\D", "", l.get("cnpj") or "")} for l in linhas]
    linhas = [l for l in linhas if len(l["cnpj"]) == 14]

    if frescor_dias > 0:
        corte = (datetime.now(timezone.utc) - timedelta(days=frescor_dias)).isoformat()
        antes = len(linhas)
        linhas = [l for l in linhas if not l.get("lido_em") or l["lido_em"] < corte]
        log(f"[varredura] {antes - len(linhas)} cliente(s) pulado(s) por leitura "
            f"dos ultimos {frescor_dias} dia(s)")

    # `None` (nunca lido) tem que vir ANTES de qualquer data -- em Python, comparar
    # None com str levanta TypeError, entao a chave usa string vazia como sentinela
    # de "nunca", que ordena antes de qualquer ISO-8601.
    linhas.sort(key=lambda l: (l.get("prioridade") or 9, l.get("lido_em") or ""))
    return linhas[:teto] if teto else linhas


def varrer_um(page: Page, sb, cliente: dict, aud: Auditoria) -> str:
    """LE o limite de um cliente e grava no espelho. NAO escreve no Mercos.

    Existe para o espelho cobrir quem nunca entrou na fila de pagamento -- sem
    isto, a Torre so conheceria o disponivel de quem por acaso pagou um boleto.
    """
    cnpj = cliente["cnpj"]
    try:
        empresa_id, cliente_id = resolver_cliente(page, cnpj)
    except ClienteInexistente as e:
        # ⚠️ GRAVA MESMO ASSIM (`nao_encontrado`). Sem a linha, a ordenacao por
        # "mais antigo primeiro" traria estes mesmos CNPJs em TODA rodada e a
        # varredura nunca avancaria na base -- e ainda perderiamos a informacao
        # de que o cliente simplesmente nao existe no Mercos.
        try:
            sb.table("credito_limite_mercos").upsert({
                "cnpj": cnpj, "cliente_nome": cliente.get("nome"),
                "disponivel": None, "total": None,
                "lido_em": datetime.now(timezone.utc).isoformat(),
                "origem": "varredura", "rodada_id": aud.rodada_id,
                "nao_encontrado": True, "leituras": 1,
            }, on_conflict="cnpj").execute()
        except Exception as e2:
            log(f"[!] espelho: nao registrei o ausente {cnpj}: {e2}")
        raise PulaSemErro(f"{cnpj} nao encontrado no Mercos: {e}")

    abrir_modal_limite(page, empresa_id, cliente_id)
    disp, total = ler_limites(page)
    fechar_modal(page)

    # ⚠️ ABRIU E NAO LEU NADA: grava a linha (para a fila avancar) mas marcada como
    # ILEGIVEL. Nao pode virar "disponivel 0 de 0" -- na tela isso apareceria como
    # "disponivel ficou em R$ 0,00", que e uma afirmacao falsa sobre o cliente.
    ilegivel = disp is None and total is None
    gravar_espelho(sb, cnpj=cnpj, empresa_id=empresa_id, cliente_id=cliente_id,
                   nome=cliente.get("nome"), disponivel=disp, total=total,
                   origem="varredura", rodada_id=aud.rodada_id, ilegivel=ilegivel)
    if ilegivel:
        aud.evento_json(etapa="varredura_ilegivel", cnpj=cnpj, cliente=cliente.get("nome"))
        return (f"{cliente.get('nome') or cnpj}: ABRIU MAS NAO LI os limites "
                "(gravado como ilegivel, nao como zero)")
    return f"{cliente.get('nome') or cnpj}: disponivel {_br(disp or 0)} de {_br(total or 0)}"


def conferir_backfill(sb) -> None:
    """A fila so e confiavel depois do backfill inicial.

    Sem ele, `processado=false` inclui MESES de pagamento historico -- e o robo
    devolveria tudo de uma vez, inclusive credito que alguem ja repos na mao (que
    e como o problema e resolvido hoje). O controle e a coluna, nunca "a fila
    esta pequena"."""
    ctl = (sb.table("credito_evento_pagamento_ctl").select("backfill_em, backfill_linhas")
           .limit(1).execute().data or [{}])[0]
    if not ctl.get("backfill_em"):
        raise RuntimeError(
            "a fila de recibos ainda nao passou pelo backfill inicial "
            "(credito_evento_pagamento_ctl.backfill_em esta NULO). Rodar agora devolveria de uma vez "
            "o credito de meses de pagamento. Rodar o sync da atividade 02 primeiro.")
    log(f"[fila] backfill inicial ok ({ctl.get('backfill_linhas')} linhas, {ctl.get('backfill_em')})")


def marcar_processado(sb, ev: dict, resultado: str) -> None:
    """Consome o pagamento da fila. SO no sucesso (ou em "nada a repor").

    ⚠️ NAO EXISTE "marcar com erro" nesta tabela, e tentar isso era um BUG que
    explodiria na PRIMEIRA falha real de uma rodada. A constraint
    `credito_evento_pagamento_proc_coerente_chk` (atividade 02) diz:

        (processado AND processado_em NOT NULL)
        OR (NOT processado AND processado_em IS NULL AND resultado IS NULL)

    isto e: **pendente = sem resultado**, por desenho. Gravar o texto do erro
    mantendo `processado=false` viola o CHECK e levanta 23514. Achado em
    20/ago/2026 ao fazer o rollback do 1o teste de escrita -- por acidente, num
    UPDATE manual, nao pelo robo: uma rodada com 21 clientes onde todos deram
    certo nunca passa por esse caminho.

    O erro NAO se perde: vai pra `credito_reposicao_log` (decisao 'erro' ou
    'conferir', com o motivo inteiro). A fila e ESTADO DE CONTROLE ("ja
    processei?"); a auditoria e o REGISTRO. Misturar os dois foi o que gerou o bug.
    """
    sb.table("credito_evento_pagamento").update({
        "processado": True,
        "processado_em": datetime.now(timezone.utc).isoformat(),
        "resultado": resultado[:1000],
    }).eq("id", ev["id"]).execute()
    log(f"[carimbo] boleto {ev['id_boleto']}: processado -- {resultado[:110]}")


def marcar_processados(sb, evs: list[dict], resultado: str) -> None:
    """Carimba N pagamentos numa UNICA chamada.

    ⚠️ POR QUE UMA CHAMADA, e nao N (24/ago/2026). Agrupando a escrita por
    cliente, existe uma janela entre "escreveu no Mercos" e "carimbou a fila" em
    que o processo pode morrer -- e nela o valor em risco e a SOMA do cliente, nao
    um boleto. Carimbar as N linhas de uma vez faz a janela ser uma ida e volta ao
    banco em vez de N. Nao elimina o risco (nao existe transacao entre o Mercos e
    o nosso banco), e por isso a linha principal fica com `escreveu=true` na
    auditoria: `v_credito_reposicao_conferir` entrega o caso pro olho humano.

    Mesma regra de `marcar_processado`: NAO existe "marcar com erro" nesta tabela
    (a constraint `credito_evento_pagamento_proc_coerente_chk` diz "pendente = sem
    resultado"). O erro vive em `credito_reposicao_log`.
    """
    if not evs:
        return
    ids = [e["id"] for e in evs]
    sb.table("credito_evento_pagamento").update({
        "processado": True,
        "processado_em": datetime.now(timezone.utc).isoformat(),
        "resultado": resultado[:1000],
    }).in_("id", ids).execute()
    log(f"[carimbo] {len(ids)} boleto(s) processado(s) -- {resultado[:110]}")


def ainda_pendente(sb, ev: dict) -> bool:
    """Rele a linha imediatamente antes de escrever: duas execucoes sobrepostas
    somariam o mesmo pagamento duas vezes."""
    atual = (sb.table("credito_evento_pagamento").select("processado")
             .eq("id", ev["id"]).limit(1).execute().data or [None])[0]
    return bool(atual) and not atual.get("processado")


# ── Mercos ─────────────────────────────────────────────────────────────────

# ⚠️ A RODADA EM CURSO, pra o crash poder contar a verdade. O `except` la
# embaixo nao enxerga os locais de `main()`, e foi por isso que o e-mail de falha
# do cron de 25/ago/2026 disse "Nada foi escrito no Mercos" para uma rodada que
# tinha acabado de repor 20 boletos e R$ 140.945,17. Mentira no e-mail de falha e
# pior que e-mail nenhum: manda a pessoa procurar o dinheiro no lugar errado.
_RODADA_EM_CURSO = None


def _ja_feito_nesta_rodada(aud) -> tuple[int, float]:
    """(boletos escritos, soma dos deltas) lidos da AUDITORIA, nao de um contador
    em memoria -- o contador morre junto com o processo, a auditoria nao.

    Nunca levanta: e usado no caminho de crash, onde uma segunda excecao
    apagaria a primeira."""
    if not aud or not getattr(aud, "rodada_id", None):
        return (0, 0.0)
    try:
        linhas = (aud.sb.table("credito_reposicao_log")
                  .select("delta,escreveu")
                  .eq("rodada_id", aud.rodada_id).eq("escreveu", True)
                  .execute().data or [])
        return (len(linhas), round(sum(float(x.get("delta") or 0) for x in linhas), 2))
    except Exception:
        return (0, 0.0)


def _fechar_rodada_no_crash(aud, e: BaseException) -> tuple[str, int, float]:
    """Fecha a rodada que morreu e devolve a frase honesta pro e-mail.

    Rodada sem `finalizada_em` fica pra sempre em `v_credito_reposicao_travou` e
    contamina a leitura de "o que travou hoje" -- fechar dizendo COMO morreu e
    melhor que deixar em aberto sem explicacao."""
    escritas, valor = _ja_feito_nesta_rodada(aud)
    if escritas:
        frase = (f"{escritas} boleto(s) JA tinham sido repostos antes da queda "
                 f"(R$ {_br(valor)}), e estao carimbados. O resto da fila continua "
                 f"pendente e volta na proxima rodada.")
    else:
        frase = ("Nada foi escrito no Mercos por esta rodada: a falha foi antes "
                 "de qualquer escrita.")
    if aud and getattr(aud, "rodada_id", None):
        try:
            aud.sb.table("credito_reposicao_rodada").update({
                "finalizada_em": datetime.now(timezone.utc).isoformat(),
                "abortou_por": f"o robo morreu no meio: {type(e).__name__}: {e}"[:1000],
                "repostos": escritas, "valor_reposto": valor, "ok": False,
            }).eq("id", aud.rodada_id).execute()
        except Exception as erro_fechar:
            log(f"[!] nao consegui fechar a rodada no crash: {erro_fechar}")
    return (frase, escritas, valor)


def reconquistar_sessao(page: Page, aud, motivo: str, volta: int, de: int) -> bool:
    """Espera, loga de novo e diz se a rodada pode continuar.

    Devolve True se a sessao voltou. Nunca levanta: quem chama decide entre
    continuar e abortar, e um erro aqui NAO pode virar traceback -- morrer
    tentando voltar seria pior que a queda original.

    ⚠️ A volta e AUDITADA (`aud.evento_json`) mesmo quando da certo. Rodada que
    se recupera em silencio esconde exatamente o sinal que diz "a conta do robo
    esta sendo disputada com gente" -- que e o motivo pra um dia dar um usuario
    proprio a ele."""
    log(f"[sessao] caiu: {motivo}")
    sobra = (_COMECOU_EM + JANELA_MIN * 60) - time.time()
    if sobra < ESPERA_VOLTA_S + MARGEM_FIM_S:
        log(f"[sessao] volta {volta}/{de} CANCELADA -- faltam {int(sobra / 60)}min de janela e a "
            f"espera sozinha leva {ESPERA_VOLTA_S // 60}min. Abortando com o que ja foi feito.")
        aud.evento_json(etapa="sessao_sem_janela", motivo=motivo, volta=volta,
                        sobra_s=int(sobra), espera_s=ESPERA_VOLTA_S)
        return False
    log(f"[sessao] volta {volta}/{de} -- esperando {ESPERA_VOLTA_S // 60}min antes de entrar de novo "
        f"(entrar agora derrubaria quem acabou de logar, e ele nos derrubaria de volta)")
    aud.evento_json(etapa="sessao_caiu", motivo=motivo, volta=volta, de=de,
                    espera_s=ESPERA_VOLTA_S)
    try:
        time.sleep(ESPERA_VOLTA_S)
        login_mercos(page, EMPRESAS[0], MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
    except Exception as e:
        log(f"[sessao] nao consegui voltar: {e}")
        aud.evento_json(etapa="sessao_nao_voltou", motivo=str(e), volta=volta)
        return False
    log("[sessao] voltei -- retomando de onde parei")
    aud.evento_json(etapa="sessao_voltou", volta=volta)
    return True


def resolver_cliente(page: Page, cnpj: str) -> tuple[str, str]:
    """(empresa_id, cliente_id). Levanta se nao achar, ou se achar em mais de uma
    empresa -- em qual mexer e decisao de negocio que ninguem tomou."""
    achados: list[tuple[str, str]] = []
    for empresa in EMPRESAS:
        # ⚠️ DUAS PORTAS PRA MESMA QUEDA, e as duas precisam existir. A busca
        # levanta `SessaoCaiu` quando morre ANTES de devolver a pagina (o campo
        # de busca nunca aparece porque veio a tela de login); o `sessao_caiu`
        # logo abaixo pega o caso em que ela devolve normalmente e a queda so
        # aparece no conteudo. Tratar so a segunda foi o que deixou o timeout
        # escapar como erro generico no cron de 25/ago/2026.
        try:
            cid = mercos_ui.buscar_cliente_id_por_cnpj(page, empresa, cnpj, log=log, shot=lambda n: _shot(page, n))
        except mercos_ui.SessaoCaiu:
            raise SessaoDerrubada(mercos_ui.motivo_sessao(page, MERCOS_EMAIL)) from None
        if mercos_ui.sessao_caiu(page):
            raise SessaoDerrubada(mercos_ui.motivo_sessao(page, MERCOS_EMAIL))
        if cid:
            achados.append((empresa, cid))
    if not achados:
        # ⚠️ `ClienteInexistente`, nao `RuntimeError` cru: aqui a busca terminou com
        # a sessao VIVA (os dois testes de queda acima ja passaram), entao isto e
        # ausencia PROVADA -- a unica coisa que autoriza carimbar `nao_encontrado`
        # no espelho. Ver a classe.
        raise ClienteInexistente(
            f"CNPJ {cnpj} nao foi encontrado em nenhuma empresa do Mercos ({', '.join(EMPRESAS)})")
    if len(achados) > 1:
        onde = ", ".join(f"empresa {e} (cliente {c})" for e, c in achados)
        raise RuntimeError(f"CNPJ {cnpj} existe em mais de uma empresa do Mercos -- {onde}. "
                           "Nao da pra decidir sozinho onde repor; precisa de decisao humana.")
    return achados[0]


def nome_do_cliente(sb, cnpj: str) -> str | None:
    """Nome do cliente para a AUDITORIA -- CNPJ sozinho nao diz nada pra quem le o
    relatorio depois.

    ⚠️ Vem de `dim_cliente` da TORRE, nao da tela do Mercos. Duas razoes:
      · a 1a rodada (20/ago, run 32397094358) gravou `cliente: null` -- o nome da
        ficha do Mercos NAO esta num `h1` legivel: o `innerText` volta vazio mesmo
        com o nome grande na tela. Raspar isso seria adivinhar seletor de novo;
      · o nome da Torre e o que aparece em todo o resto do sistema, entao a
        auditoria fica comparavel com o Gerencial, a carteira e o financeiro.
    Nunca levanta: nome e dado de EXIBICAO, e falhar aqui nao pode impedir uma
    reposicao correta."""
    try:
        d = (sb.table("dim_cliente").select("nome_cliente")
             .eq("cnpj", cnpj).limit(1).execute().data or [None])[0]
        return ((d or {}).get("nome_cliente") or "").strip()[:120] or None
    except Exception as e:
        log(f"[!] nao achei o nome do cliente {cnpj} em dim_cliente: {e}")
        return None


def _reler_limites_silencioso(page: Page):
    """(disponivel, total) sem levantar. Serve pra AUDITORIA depois de salvar: e
    a releitura do total que PROVA que o teto ficou intacto. Falhar aqui nao pode
    derrubar uma escrita que ja deu certo."""
    try:
        if page.locator(f"#{ID_DISPONIVEL}").count() == 0:
            page.locator(SEL_BOTAO_EDITAR).first.click()
            page.wait_for_selector(f"#{ID_DISPONIVEL}", timeout=8_000, state="visible")
        return (_para_numero(page.locator(f"#{ID_DISPONIVEL}").input_value()),
                _para_numero(page.locator(f"#{ID_TOTAL}").input_value()))
    except Exception:
        return (None, None)


def abrir_modal_limite(page: Page, empresa_id: str, cliente_id: str) -> None:
    """Abre "Editar limite de credito" na ficha do cliente.

    Abrir NAO altera nada -- o modal so grava no clique em Salvar."""
    url = f"https://app.mercos.com/{empresa_id}/clientes/{cliente_id}/"
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    if mercos_ui.sessao_caiu(page):
        raise SessaoDerrubada(mercos_ui.motivo_sessao(page, MERCOS_EMAIL))

    # ⚠️ ESPERA PELO ELEMENTO, NAO PELA REDE. Antes era
    # `wait_for_load_state("networkidle", timeout=15_000)`, que significa "espera
    # a pagina ficar 500ms sem requisicao nenhuma" -- num sistema que conversa
    # com o servidor em segundo plano isso pode NUNCA acontecer, e o robo
    # esperava os 15s inteiros e desistia. Medido: ~33s por cliente.
    # O card do limite aparecer e a unica coisa que de fato importa aqui, e
    # esperar por ele e mais rapido E mais confiavel: se aparece em 2s, segue em
    # 2s; se nao aparece, falha dizendo o que faltou em vez de seguir com a tela
    # pela metade.
    # ⚠️ ESPERA PELO BOTAO, NAO PELO CARD. Regressao real de 20/ago: eu esperava
    # `SEL_CARD_LIMITE` e o cliente GRAYCIELLI falhou com "esperava 1 botao de
    # edicao, achei 0" -- o card fica no DOM antes do botao dentro dele. Espera
    # tem que apontar para O QUE VAI SER USADO, nao para o container dele.
    try:
        page.wait_for_selector(SEL_BOTAO_EDITAR, timeout=20_000, state="visible")
    except Exception:
        pass   # o diagnostico abaixo e quem nomeia o que faltou

    if page.locator(SEL_CARD_LIMITE).count() == 0:
        _shot(page, f"sem_card_{cliente_id}")
        raise TelaNaoMapeada(
            f"o card 'Limite de credito' nao existe na ficha de {url} "
            f"(procurei {SEL_CARD_LIMITE}). O Mercos mudou a tela -- remapear antes de seguir.")

    botao = page.locator(SEL_BOTAO_EDITAR)
    if botao.count() != 1:
        _shot(page, f"botao_editar_{cliente_id}")
        raise TelaNaoMapeada(
            f"esperava 1 botao de edicao do limite ({SEL_BOTAO_EDITAR}), achei {botao.count()} em {url}. "
            "Clicar em botao vizinho num cadastro de cliente pode disparar outra acao -- parei aqui.")
    botao.first.click()

    try:
        page.wait_for_selector(f"#{ID_DISPONIVEL}", timeout=15_000, state="visible")
    except Exception:
        _shot(page, f"modal_nao_abriu_{cliente_id}")
        raise TelaNaoMapeada(f"cliquei no lapis do limite mas o campo #{ID_DISPONIVEL} nao apareceu em {url}")


def ler_limites(page: Page) -> tuple[float | None, float | None]:
    """(disponivel, total) do modal aberto, conferindo o ROTULO de cada campo.

    ⚠️ O id certo com o rotulo trocado (um redesign deles invertendo os campos)
    faria o robo escrever saldo no teto -- exatamente o que o usuario proibiu.
    Por isso o rotulo e verificado, nao presumido."""
    for campo_id, esperado in ((ID_DISPONIVEL, ROTULO_DISPONIVEL), (ID_TOTAL, ROTULO_TOTAL)):
        lab = page.locator(f'label[for="{campo_id}"]')
        texto = _norm(lab.inner_text()) if lab.count() else ""
        if esperado not in texto:
            _shot(page, f"rotulo_divergente_{campo_id}")
            raise TelaNaoMapeada(
                f"o campo #{campo_id} deveria estar rotulado '{esperado}' e esta como '{texto or '(sem label)'}'. "
                "Os campos podem ter sido invertidos num redesign -- escrever agora poria saldo no teto.")
    return (_para_numero(page.locator(f"#{ID_DISPONIVEL}").input_value()),
            _para_numero(page.locator(f"#{ID_TOTAL}").input_value()))


def fechar_modal(page: Page) -> None:
    """Sai pelo Cancelar. Deixar modal aberto entre clientes faz o proximo
    `goto` acontecer com um formulario pendente na tela."""
    try:
        btn = page.locator('button:has-text("Cancelar")')
        if btn.count():
            btn.first.click()
            time.sleep(0.4)
    except Exception:
        pass


def escrever_disponivel(page: Page, novo: float, esperado_total: float) -> str:
    """Digita SO o disponivel, salva e RELE.

    A releitura nao e zelo: formulario que recusa o valor (mascara, validacao)
    devolve a tela sem erro visivel, e um RPA que confia no clique reporta
    sucesso sobre nada. E aqui ela tem um segundo papel -- e ela que autoriza
    marcar o pagamento como processado."""
    campo = page.locator(f"#{ID_DISPONIVEL}")
    antes = _para_numero(campo.input_value())

    campo.click()
    campo.fill("")
    campo.type(_para_digitar(novo), delay=25)   # sem ponto de milhar — ver _para_digitar

    # Confere que NAO tocamos no total antes de salvar (proibicao do usuario).
    total_agora = _para_numero(page.locator(f"#{ID_TOTAL}").input_value())
    if total_agora is None or abs(total_agora - esperado_total) > 0.01:
        raise TelaNaoMapeada(
            f"o campo 'limite total' saiu de {esperado_total} para {total_agora} enquanto eu digitava o "
            "disponivel. NAO salvei -- este robo nao pode mexer no teto.")

    salvar = page.locator('button:has-text("Salvar")')
    if salvar.count() == 0:
        raise TelaNaoMapeada("preenchi o disponivel mas nao achei o botao 'Salvar' -- nao cliquei em nada.")
    salvar.first.click()
    # Espera o modal FECHAR -- e o sinal de que o Mercos aceitou o formulario.
    # (`networkidle` aqui tinha o mesmo problema de nunca acontecer.)
    try:
        page.wait_for_selector(f"#{ID_DISPONIVEL}", timeout=15_000, state="detached")
    except Exception:
        # Modal que nao fecha nao e conclusao: pode ter salvo e ficado aberto.
        # A releitura logo abaixo e quem decide.
        pass
    time.sleep(0.8)

    # Releitura: reabre o modal e le o que ficou gravado.
    depois_disp, depois_total = None, None
    try:
        if page.locator(f"#{ID_DISPONIVEL}").count() == 0:
            page.locator(SEL_BOTAO_EDITAR).first.click()
            page.wait_for_selector(f"#{ID_DISPONIVEL}", timeout=10_000, state="visible")
        depois_disp = _para_numero(page.locator(f"#{ID_DISPONIVEL}").input_value())
        depois_total = _para_numero(page.locator(f"#{ID_TOTAL}").input_value())
    except Exception as e:
        log(f"[!] nao consegui reabrir o modal pra conferir: {e}")

    if depois_disp is not None and abs(depois_disp - novo) <= 0.01:
        aviso = ""
        if depois_total is not None and abs(depois_total - esperado_total) > 0.01:
            # Nao alteramos o total; se ele mudou, foi o Mercos recalculando.
            aviso = f" ⚠️ o total passou de {_br(esperado_total)} para {_br(depois_total)} sozinho"
        return (f"ok -- disponivel {_br(antes) if antes is not None else '?'} -> {_br(novo)}, "
                f"conferido na tela (total intocado em {_br(esperado_total)}){aviso}")

    if depois_disp is not None and antes is not None and abs(depois_disp - antes) <= 0.01:
        # Voltou ao valor de antes = nao gravou. Seguro tentar de novo depois.
        raise RuntimeError(
            f"o Mercos continua com {_br(depois_disp)} depois de eu digitar {_br(novo)} -- o formulario "
            "nao aceitou o valor. Nada foi alterado.")

    # Terceiro valor (ou ilegivel): pode ter gravado parcialmente. Nao repetir.
    raise ConferirNaMao(
        f"digitei {_br(novo)} e a releitura devolveu {_br(depois_disp) if depois_disp is not None else 'ilegivel'} "
        f"(antes era {_br(antes) if antes is not None else '?'}). Nao repito para nao somar duas vezes -- "
        "conferir este cliente no Mercos na mao.")


# ── Um cliente (N boletos, UMA escrita) ────────────────────────────────────

def processar_grupo(page: Page, sb, grupo: list[dict], aplicar: bool,
                    aud: Auditoria) -> tuple[str, list[dict], list[tuple[dict, str]]]:
    """UMA escrita por CLIENTE, somando os boletos SELECIONADOS.

    ⚠️ POR QUE AGRUPAR (pedido do Kaique, 24/ago/2026). Antes era uma escrita por
    BOLETO: cliente com 4 pagamentos custava 4 buscas de CNPJ no Mercos -- a parte
    cara da rodada, ~6 s cada -- e 4 ciclos de ler-somar-salvar. Agrupado e uma
    busca e uma escrita da SOMA.

    ⚠️ O QUE ISTO **NAO** MUDA, e e o ponto: a selecao continua vindo por BOLETO
    (`--boletos`). O motivo daquela decisao nunca foi a contagem de escritas --
    era que filtrar por CNPJ repoe TODOS os pagamentos pendentes do cliente,
    inclusive os que alguem ja repos na mao (nada baixa a fila quando o ajuste e
    manual). Somar so os marcados preserva a garantia inteira: o que a pessoa
    marcou na tela e exatamente o que entra na soma.

    ⚠️ A JANELA QUE AGRUPAR AUMENTA, declarada: entre a escrita no Mercos e o
    carimbo da fila existe um instante em que o processo pode morrer. Por boleto,
    o pior caso era duplicar UM pagamento numa nova tentativa; agrupado, e
    duplicar a soma do cliente. Mitigacao: o carimbo das N linhas e UMA chamada
    (`marcar_processados`), e a linha principal fica com `escreveu=true` e
    `confirmado_na_tela` -- `v_credito_reposicao_conferir` entrega o caso pro olho
    humano. Nao existe transacao entre o Mercos e o nosso banco, e nenhum desenho
    aqui faz existir.

    ── A AUDITORIA CONTINUA POR BOLETO, de proposito ──
    Cada boleto ganha a sua linha em `credito_reposicao_log`: a pergunta "o boleto
    X entrou?" tem que continuar respondivel, e `ix_credito_reposicao_log_boleto`
    indexa por ele. Os numeros de tela (`disponivel_antes/depois`, `total_*`,
    `delta`, `escreveu`) vao SO na linha PRINCIPAL do grupo.
    ⚠️ Repetir `delta` nas N linhas seria pior que nao ter: somar a coluna
    contaria a MESMA escrita N vezes. As companheiras dizem no `motivo` de qual
    escrita fizeram parte, e `valor_pago` continua correto em cada uma (essa soma
    fecha).

    Devolve (texto do resultado, eventos que entraram na soma, descartados).
    """
    t_ini = time.time()
    cnpj = re.sub(r"\D", "", grupo[0].get("cnpj") or "")
    nome = nome_do_cliente(sb, cnpj)

    def _ms():
        return int((time.time() - t_ini) * 1000)

    # ── Guardas POR BOLETO, antes de navegar ─────────────────────────────────
    # ⚠️ Boleto invalido nao envenena o grupo: sai da soma com a propria linha de
    # auditoria e o cliente segue sendo atendido pelos que sobraram. Somar
    # primeiro e conferir depois faria um valor zerado derrubar os 3 boletos bons
    # do mesmo cliente.
    validos: list[dict] = []
    descartados: list[tuple[dict, str]] = []
    for ev in grupo:
        if float(ev.get("valor_pago") or 0) <= 0:
            motivo = "valor pago zerado ou negativo na fila -- nada a repor"
            aud.abrir_linha(ev, decisao="pulado", fase="descartado", motivo=motivo,
                            cliente_nome=nome)
            descartados.append((ev, motivo))
            continue
        validos.append(ev)

    if not validos:
        raise PulaSemErro(
            f"{len(grupo)} boleto(s) deste cliente com valor zerado ou negativo -- nada a repor")

    pago = round(sum(float(e.get("valor_pago") or 0) for e in validos), 2)
    nfs = [str(e.get("referencia") or "?") for e in validos]
    log(f"── cliente {nome or cnpj} · CNPJ {cnpj} · {len(validos)} boleto(s) · "
        f"soma {_br(pago)} · NF(s) {', '.join(nfs[:6])}" + (" …" if len(nfs) > 6 else ""))

    # ⚠️ AS LINHAS NASCEM AGORA, antes de qualquer navegacao. `em_andamento` ate
    # concluir: se o processo morrer daqui pra frente (runner morto, timeout de 30
    # min, sessao derrubada, tela mudada), as linhas FICAM -- dizendo em QUEM o
    # robo estava, em que FASE, e QUANTO estava em jogo. Ver
    # `v_credito_reposicao_travou`.
    log_ids: list[str | None] = [
        aud.abrir_linha(ev, decisao="em_andamento", fase="iniciou", cliente_nome=nome)
        for ev in validos
    ]
    principal = log_ids[0]
    companheiras = [i for i in log_ids[1:] if i]

    def _fechar_todas(**kw) -> None:
        """Fecha as N linhas com o MESMO veredicto.
        ⚠️ Sem numero de tela aqui: quem passa por este caminho nao escreveu, e
        `delta` repetido em N linhas e o defeito que esta funcao evita."""
        for i in log_ids:
            aud.fechar_linha(i, **kw)

    aud.fase(principal, "iniciou", cliente=nome or cnpj, boletos=len(validos),
             valor_somado=pago, nfs=nfs[:20],
             ids_boleto=[str(e.get("id_boleto") or "") for e in validos][:20])

    try:
        empresa_id, cliente_id = resolver_cliente(page, cnpj)
    except Exception as e:
        _fechar_todas(decisao="erro", fase="resolver_cliente", motivo=str(e), duracao_ms=_ms())
        raise
    url = f"https://app.mercos.com/{empresa_id}/clientes/{cliente_id}/"
    aud.fase(principal, "cliente_resolvido", cliente=nome or cnpj, empresa=empresa_id,
             cliente_mercos=cliente_id, url=url)

    try:
        abrir_modal_limite(page, empresa_id, cliente_id)
    except Exception as e:
        _fechar_todas(decisao="erro", fase="abrir_modal", motivo=str(e),
                      screenshot=f"repositor_*_{cliente_id}.png", duracao_ms=_ms())
        raise
    aud.fase(principal, "modal_aberto", cliente=nome or cnpj)

    try:
        disp, total = ler_limites(page)
    except Exception as e:
        _fechar_todas(decisao="erro", fase="ler_limites", motivo=str(e), duracao_ms=_ms())
        raise
    log(f"[limite] {nome or cnpj}: disponivel {disp} · total {total}")
    aud.fase(principal, "limites_lidos", cliente=nome or cnpj, disponivel=disp, total=total)
    # Espelho na Torre (pedido do Kaique, 21/ago). Grava em TODO modo, inclusive
    # dry-run: dry-run tambem LE, e leitura descartada e leitura desperdicada.
    gravar_espelho(sb, cnpj=cnpj, empresa_id=empresa_id, cliente_id=cliente_id,
                   nome=nome, disponivel=disp, total=total,
                   origem="reposicao", rodada_id=aud.rodada_id)

    # Completa o que so se soube agora. ⚠️ Os numeros de limite vao SO na
    # principal; as companheiras recebem apenas ONDE o cliente esta no Mercos.
    try:
        if principal:
            aud.sb.table("credito_reposicao_log").update({
                "empresa_id": empresa_id, "cliente_mercos_id": cliente_id,
                "disponivel_antes": disp, "total_antes": total, "url": url,
            }).eq("id", principal).execute()
        if companheiras:
            aud.sb.table("credito_reposicao_log").update({
                "empresa_id": empresa_id, "cliente_mercos_id": cliente_id, "url": url,
            }).in_("id", companheiras).execute()
    except Exception as e:
        log(f"[!] auditoria: nao completei os limites da linha: {e}")

    if total is None or total <= 0:
        # 63 de 75 clientes amostrados estao assim: "sem limite controlado".
        # Escrever aqui CRIARIA um controle que nao existe e passaria a travar
        # quem hoje compra livre.
        motivo = (f"cliente sem limite configurado no Mercos (total {total}) -- nao ha o que repor, "
                  "e escrever aqui criaria um bloqueio que nao existe hoje")
        _fechar_todas(decisao="pulado", fase="sem_limite", motivo=motivo, duracao_ms=_ms())
        fechar_modal(page)
        raise PulaSemErro(motivo)
    if disp is None:
        motivo = "nao deu pra ler o disponivel atual -- nao somo sobre numero que nao li"
        _fechar_todas(decisao="erro", fase="disponivel_ilegivel", motivo=motivo, duracao_ms=_ms())
        fechar_modal(page)
        raise TelaNaoMapeada(motivo)
    if disp >= total - 0.01:
        motivo = f"disponivel ja esta no teto ({_br(disp)} de {_br(total)}) -- nada foi subtraido a repor"
        _fechar_todas(decisao="pulado", fase="ja_no_teto", motivo=motivo, duracao_ms=_ms())
        fechar_modal(page)
        raise PulaSemErro(motivo)

    # ⚠️ RECONFERE A PENDENCIA DE CADA BOLETO IMEDIATAMENTE ANTES DE SOMAR (so no
    # aplicar). Duas execucoes sobrepostas somariam o mesmo pagamento duas vezes,
    # e agrupado o estrago seria a soma inteira -- entao o boleto que outra rodada
    # consumiu no meio SAI da soma, em vez de derrubar o cliente todo.
    if aplicar:
        ainda: list[dict] = []
        for ev, lid in zip(validos, log_ids):
            if ainda_pendente(sb, ev):
                ainda.append(ev)
                continue
            motivo = "outra execucao processou este pagamento enquanto esta rodava"
            aud.fechar_linha(lid, decisao="pulado", fase="ja_processado", motivo=motivo,
                             duracao_ms=_ms())
            log(f"[pula] boleto {ev.get('id_boleto')}: {motivo}")
        if len(ainda) != len(validos):
            # O grupo encolheu: recalcula tudo que dependia dele.
            fora = len(validos) - len(ainda)
            if not ainda:
                fechar_modal(page)
                raise PulaSemErro(
                    "todos os pagamentos deste cliente foram consumidos por outra execucao")
            log_ids = [lid for ev, lid in zip(validos, log_ids) if ev in ainda]
            validos = ainda
            principal = log_ids[0]
            companheiras = [i for i in log_ids[1:] if i]
            pago = round(sum(float(e.get("valor_pago") or 0) for e in validos), 2)
            log(f"[grupo] {fora} boleto(s) sairam da soma (ja processados); "
                f"soma agora {_br(pago)} em {len(validos)} boleto(s)")

    novo = min(disp + pago, total)
    clamp = novo < disp + pago - 0.01
    detalhe_soma = (f"{len(validos)} boleto(s): " +
                    " + ".join(_br(float(e.get("valor_pago") or 0)) for e in validos[:8])
                    + (" + …" if len(validos) > 8 else ""))

    if not aplicar:
        # ⚠️ O `simulado` por CNPJ morreu aqui, e nao por descuido: ele existia
        # porque duas linhas do MESMO cliente partiam do mesmo `disp` lido na tela
        # (no dry-run nada muda no Mercos, entao a releitura devolve sempre o
        # original) e nenhuma das duas mostrava o estado final. Com uma escrita por
        # cliente ha UM calculo por cliente por rodada: o defeito deixa de existir
        # por construcao, em vez de ser corrigido por acumulador.
        txt = (f"[dry-run] disponivel {_br(disp)} + {_br(pago)} = {_br(novo)}"
               + (f" (limitado pelo teto de {_br(total)})" if clamp else "")
               + f" · {detalhe_soma} · empresa {empresa_id}, cliente {cliente_id}")
        # Dry-run TAMBEM e auditado: e o registro de que o ensaio aconteceu e com
        # que numeros. 'mapeamento' e 'dry-run' sao decisoes DIFERENTES: um so
        # confere a tela, o outro percorre a fila dizendo o que faria.
        decisao = "mapeamento" if aud.modo == "mapear" else "dry-run"
        aud.fechar_linha(principal, decisao=decisao, fase="ensaio", motivo=txt,
                         disponivel_depois=novo, total_depois=total,
                         disponivel_antes=disp, duracao_ms=_ms())
        for i in companheiras:
            aud.fechar_linha(i, decisao=decisao, fase="ensaio", duracao_ms=_ms(),
                             motivo=f"entraria na MESMA escrita do cliente ({detalhe_soma}) "
                                    f"-- os numeros de tela estao na linha principal")
        fechar_modal(page)
        return txt, validos, descartados

    # ⚠️ A principal e fechada com `escreveu=True` e `confirmado_na_tela` ainda
    # nulo ANTES do clique em Salvar. Se o processo morrer no meio, fica a prova de
    # que a tentativa existiu -- e `v_credito_reposicao_conferir` a entrega pro
    # olho humano. Sem registro, NAO escreve.
    if not principal:
        raise AuditoriaIndisponivel(
            "a linha de auditoria deste cliente nao existe — NAO vou escrever no Mercos sem registro")
    aud.fechar_linha(principal, escreveu=True, fase="vai_escrever",
                     motivo=f"vai escrever {_br(novo)} (= {_br(disp)} + {_br(pago)}; {detalhe_soma})")
    aud.fase(principal, "vai_escrever", cliente=nome or cnpj, de=disp, para=novo, total=total,
             boletos=len(validos), valor_somado=pago)
    try:
        resultado = escrever_disponivel(page, novo, total)
    except ConferirNaMao as e:
        _fechar_todas(decisao="conferir", fase="releitura_divergente", motivo=str(e),
                      duracao_ms=_ms())
        aud.fechar_linha(principal, confirmado=False)
        aud.evento_json(etapa="conferir_na_mao", cnpj=cnpj, cliente=nome, erro=str(e),
                        boletos=len(validos), valor_somado=pago)
        fechar_modal(page)
        raise
    except Exception as e:
        _fechar_todas(decisao="erro", fase="erro_na_escrita", motivo=str(e), duracao_ms=_ms())
        aud.evento_json(etapa="erro_na_escrita", cnpj=cnpj, cliente=nome, erro=str(e))
        fechar_modal(page)
        raise

    if clamp:
        resultado += f" ⚠️ a soma daria {_br(disp + pago)} e foi limitada ao teto de {_br(total)}"
    resultado += f" · {detalhe_soma}"
    # Rele o TOTAL depois de salvar: e o que prova, na auditoria, que o teto ficou
    # intacto. Auditoria que so guarda o campo alterado nao prova o que nao mudou.
    aud.fase(principal, "salvou", cliente=nome or cnpj)
    depois_disp, depois_total = _reler_limites_silencioso(page)
    aud.fechar_linha(principal,
                     disponivel_depois=depois_disp if depois_disp is not None else novo,
                     total_depois=depois_total if depois_total is not None else total,
                     disponivel_antes=disp, decisao="reposto", fase="releu", motivo=resultado,
                     confirmado=True, duracao_ms=_ms())
    # ⚠️ Companheiras: `decisao='reposto'` (o boleto FOI reposto) com
    # `escreveu=False` (nao foi a linha que clicou em Salvar) e SEM numero de tela.
    # As tres coisas juntas e que fazem a auditoria somavel: `valor_pago` fecha,
    # `delta` nao conta duas vezes, e `v_credito_reposicao_conferir` mostra UMA
    # linha por escrita incerta em vez de N.
    for i in companheiras:
        aud.fechar_linha(i, decisao="reposto", fase="releu", confirmado=True, duracao_ms=_ms(),
                         motivo=f"reposto na MESMA escrita do cliente ({detalhe_soma}); "
                                f"os numeros de tela estao na linha principal")

    # ⚠️ O ESPELHO TAMBEM E ATUALIZADO AQUI (pedido do Kaique, 21/ago/2026, depois do
    # 1o teste de escrita real). Antes so a varredura escrevia nele, entao logo depois
    # de repor o espelho ainda mostrava o valor ANTIGO -- retrato que a gente mesmo
    # acabou de invalidar. Grava a RELEITURA, nao o calculado: quem manda e o que a
    # tela do Mercos devolveu depois de salvar.
    gravar_espelho(sb, cnpj=cnpj, empresa_id=empresa_id, cliente_id=cliente_id, nome=nome,
                   disponivel=depois_disp if depois_disp is not None else novo,
                   total=depois_total if depois_total is not None else total,
                   origem="reposicao", rodada_id=aud.rodada_id)
    aud.evento_json(etapa="reposto", cnpj=cnpj, cliente=nome, de=disp,
                    para=depois_disp if depois_disp is not None else novo,
                    total_antes=total, total_depois=depois_total,
                    boletos=len(validos), valor_somado=pago,
                    ids_boleto=[str(e.get("id_boleto") or "") for e in validos][:20])
    fechar_modal(page)
    return resultado, validos, descartados


# ══════════════════════════════════════════════════════════════════════════
# LIBERACAO DE TETO -- o botao "Liberar" da Gestao de Credito chegando no Mercos
# ══════════════════════════════════════════════════════════════════════════
#
# Tudo daqui pra baixo ate "── Rodada ──" e o modo `--liberacoes`, que e OUTRO
# trabalho do mesmo robo: enquanto a reposicao devolve SALDO por aritmetica de
# recibo, isto escreve o TETO que uma pessoa assinou na tela da Torre.
#
# ⚠️ MEDIDO EM 25/ago/2026, NA TELA REAL, E E O QUE DEFINE O DESENHO:
# subir o `limiteTotal` NAO sobe o `limiteDisponivel`. Testado no cliente Kassio
# Perfumaria (424525/60382638): total 30.000 -> digitado 40.000 e o disponivel
# ficou parado em 22.554,18; fechado no Cancelar, nada gravado. Junte-se ao que o
# teste de escrita de 20/ago ja provava ("o Mercos NAO recalcula o disponivel
# depois de salvar -- grava o que se digita") e a conclusao e uma so:
#
#     ESCREVER SO O TETO ENTREGARIA UM BOTAO "LIBERAR" QUE NAO LIBERA NADA.
#
# Quem trava a venda e o disponivel. Entao a liberacao escreve OS DOIS CAMPOS,
# na mesma ida ao modal:
#
#     delta           = teto_novo - teto_lido_na_tela
#     disponivel_novo = min(max(disponivel_lido + delta, 0), teto_novo)
#
# ⚠️ NUNCA `disponivel = teto_novo`: o cliente do teste ja consumiu R$ 7.445,82 do
# teto dele, e igualar os dois devolveria isso de graca. O delta preserva o
# consumido. E o delta pode ser NEGATIVO (liberacao que reduz, e TODO desfazer):
# ai o disponivel desce junto, com piso em zero.
#
# ⚠️ ISTO NAO FURA `POLITICA_HOMOLOGADA`. Aquela trava proibe o MOTOR decidir o
# teto sozinho. Aqui o robo nao decide: ele datilografa `limite_para`, um numero
# que uma pessoa digitou e assinou, com motivo, na trilha de auditoria. Por isso
# ele le SO `credito_liberacao` -- `motor_limite_sugerido` esta gravado na mesma
# linha e e exatamente o campo que ele nao pode usar.


def fila_liberacoes(sb, teto: int, ids: list[str] | None = None) -> list[dict]:
    """Fila de APLICAR: `v_credito_liberacao_pendente_mercos`.

    ⚠️ A regra de "o que esta pendente" mora na VIEW, nao aqui. Duas copias (uma
    na view que a tela le, outra num `select` do Python) divergem em semanas e
    ninguem descobre ate um teto errado subir pro ERP -- convencao #14."""
    q = sb.table("v_credito_liberacao_pendente_mercos").select("*")
    if ids:
        q = q.in_("id", ids)
    else:
        q = q.limit(teto)
    linhas = (q.execute().data or [])
    if ids:
        # Selecao explicita ignora o teto de proposito, pela mesma razao do
        # `--boletos`: cortar em N uma lista aprovada por gente escreveria em
        # parte dela e calaria sobre o resto.
        achados = {str(l.get("id")) for l in linhas}
        for i in ids:
            if i not in achados:
                log(f"[selecao] liberacao {i} nao esta na fila de aplicar "
                    "(ja aplicada, revogada, ou substituida por outra)")
    return linhas


def fila_desfazer(sb, teto: int, ids: list[str] | None = None) -> list[dict]:
    """Fila de DESFAZER: `v_credito_liberacao_desfazer_mercos`.

    ⚠️ SEM ISTO, "LIBERACAO TEMPORARIA" VIRA PERMANENTE NO ERP. O cron
    `credito-liberacao-expira` (09h05 BRT) encerra a vencida como 'expirada' e
    deixa `revertido_no_mercos` nula justamente esperando este par. Hoje isso nao
    causa dano porque nada foi escrito no Mercos; no dia em que o aplicar entrar,
    causa -- a Torre mostraria "expirada" e o cliente seguiria com o teto alto
    para sempre. Aplicar e desfazer sobem juntos."""
    q = sb.table("v_credito_liberacao_desfazer_mercos").select("*")
    if ids:
        q = q.in_("id", ids)
    else:
        q = q.limit(teto)
    return (q.execute().data or [])


def escrever_limites(page: Page, novo_disp: float, novo_total: float,
                     esperado_disp: float, esperado_total: float) -> str:
    """Digita OS DOIS campos, salva e RELE.

    ⚠️ Por que os dois: ver o cabecalho desta secao. Subir so o teto nao destrava
    a venda -- medido na tela em 25/ago/2026.

    ⚠️ A ORDEM E TOTAL PRIMEIRO, DISPONIVEL DEPOIS. Medido que o formulario nao
    recalcula nada, entao a ordem nao muda o resultado hoje; ela e escolhida para
    o dia em que mudar: se um redesign deles passar a recalcular o disponivel ao
    mexer no teto, o nosso valor e o ultimo a ser digitado e sobrevive. Ordem
    inversa perderia para o recalculo em silencio.

    ⚠️ CONFERE O QUE FICOU NO CAMPO ANTES DE SALVAR. A mascara do Mercos ja
    demonstrou reescrever o que se digita (ela come o ponto de milhar). Campo que
    recusou o valor devolve a tela sem erro visivel, e um RPA que confia no clique
    reporta sucesso sobre nada."""
    campo_total = page.locator(f"#{ID_TOTAL}")
    campo_disp = page.locator(f"#{ID_DISPONIVEL}")

    antes_disp = _para_numero(campo_disp.input_value())
    antes_total = _para_numero(campo_total.input_value())
    # A tela mudou entre o `ler_limites` e agora? Nao deveria (sao milissegundos),
    # mas escrever sobre premissa nao conferida e como esta classe de bug comeca.
    if (antes_disp is None or abs(antes_disp - esperado_disp) > 0.01
            or antes_total is None or abs(antes_total - esperado_total) > 0.01):
        raise TelaNaoMapeada(
            f"o modal mudou entre a leitura e a digitacao (esperava {esperado_disp}/{esperado_total}, "
            f"achei {antes_disp}/{antes_total}). NAO salvei.")

    for campo, valor in ((campo_total, novo_total), (campo_disp, novo_disp)):
        campo.click()
        campo.fill("")
        campo.type(_para_digitar(valor), delay=25)   # sem ponto de milhar -- ver _para_digitar

    # Confere o formulario ANTES de salvar.
    form_total = _para_numero(campo_total.input_value())
    form_disp = _para_numero(campo_disp.input_value())
    if (form_total is None or abs(form_total - novo_total) > 0.01
            or form_disp is None or abs(form_disp - novo_disp) > 0.01):
        raise TelaNaoMapeada(
            f"digitei {_br(novo_disp)}/{_br(novo_total)} e o formulario ficou com "
            f"{form_disp}/{form_total} -- a mascara recusou o valor. NAO salvei.")

    salvar = page.locator('button:has-text("Salvar")')
    if salvar.count() == 0:
        raise TelaNaoMapeada("preenchi os dois campos mas nao achei o botao 'Salvar' -- nao cliquei em nada.")
    salvar.first.click()
    try:
        page.wait_for_selector(f"#{ID_DISPONIVEL}", timeout=15_000, state="detached")
    except Exception:
        # Modal que nao fecha nao e conclusao: a releitura abaixo e quem decide.
        pass
    time.sleep(0.8)

    depois_disp, depois_total = _reler_limites_silencioso(page)

    bateu_total = depois_total is not None and abs(depois_total - novo_total) <= 0.01
    bateu_disp = depois_disp is not None and abs(depois_disp - novo_disp) <= 0.01
    if bateu_total and bateu_disp:
        return (f"ok -- teto {_br(esperado_total)} -> {_br(novo_total)} e disponivel "
                f"{_br(esperado_disp)} -> {_br(novo_disp)}, conferidos na tela")

    # Nada mudou = nao gravou. Seguro tentar de novo depois (a liberacao continua
    # na fila, e escrever teto nao e delta -- repetir e inocuo no valor).
    if (depois_disp is not None and depois_total is not None
            and abs(depois_total - esperado_total) <= 0.01
            and abs(depois_disp - esperado_disp) <= 0.01):
        raise RuntimeError(
            f"o Mercos continua com {_br(depois_disp)}/{_br(depois_total)} depois de eu digitar "
            f"{_br(novo_disp)}/{_br(novo_total)} -- o formulario nao aceitou. Nada foi alterado.")

    # ⚠️ GRAVOU PELA METADE e a metade importa: teto novo com disponivel velho
    # deixa o cliente travado com um teto que diz o contrario; disponivel novo com
    # teto velho pode ter estourado o teto. Nos dois casos e olho humano, e o
    # texto tem que dizer QUAL das duas aconteceu.
    raise ConferirNaMao(
        f"digitei disponivel {_br(novo_disp)} / teto {_br(novo_total)} e a releitura devolveu "
        f"{_br(depois_disp) if depois_disp is not None else 'ilegivel'} / "
        f"{_br(depois_total) if depois_total is not None else 'ilegivel'} "
        f"(antes era {_br(esperado_disp)} / {_br(esperado_total)}). "
        + ("O TETO subiu e o DISPONIVEL nao: o cliente continua travado. "
           if bateu_total and not bateu_disp else
           "O DISPONIVEL mudou e o TETO nao: conferir se o disponivel passou do teto. "
           if bateu_disp and not bateu_total else "")
        + "Conferir este cliente no Mercos na mao.")


def _ev_liberacao(lib: dict) -> dict:
    """Adapta a liberacao ao formato que `Auditoria.abrir_linha` espera.

    ⚠️ `valor_pago`/`data_pagamento` ficam NULOS de proposito: nao houve pagamento
    nenhum aqui. Enfiar `limite_para` em `valor_pago` faria a soma da coluna
    (que responde "quanto de boleto pago voltou pro cliente?") passar a misturar
    teto de credito -- numero errado numa coluna que ja tem dono."""
    return {
        "id": None,
        "id_boleto": None,
        "cnpj": re.sub(r"\D", "", lib.get("cnpj") or ""),
        "referencia": lib.get("codigo"),      # LC-00148 -- e o que se cita num chamado
        "valor_pago": None,
        "data_pagamento": None,
    }


def processar_liberacao(page: Page, sb, lib: dict, aplicar: bool, aud: Auditoria) -> str:
    """APLICA no Mercos o teto que uma pessoa assinou. Uma liberacao = uma escrita."""
    t_ini = time.time()
    cnpj = re.sub(r"\D", "", lib.get("cnpj") or "")
    codigo = lib.get("codigo") or "?"
    nome = lib.get("nome_cliente") or nome_do_cliente(sb, cnpj)
    novo_total = float(lib.get("limite_para") or 0)

    def _ms():
        return int((time.time() - t_ini) * 1000)

    log(f"── {codigo} · {nome or cnpj} · CNPJ {cnpj} · teto assinado {_br(novo_total)} "
        f"· por {lib.get('criado_por')} em {str(lib.get('criado_em'))[:16]}")

    log_id = aud.abrir_linha(_ev_liberacao(lib), decisao="em_andamento", fase="iniciou",
                             cliente_nome=nome, liberacao_id=lib.get("id"),
                             exigir=aplicar)
    aud.fase(log_id, "iniciou", cliente=nome or cnpj, liberacao=codigo, teto_assinado=novo_total)

    # ⚠️ NAO EXISTE GUARDA DE "SEGUNDA APROVACAO" AQUI, e a ausencia e decisao
    # (Kaique, 25/ago/2026). O modal avisa desde 13/ago que acima do teto
    # automatico da politica (R$ 150 mil) a liberacao "exigira segunda aprovacao
    # QUANDO O FLUXO ESTIVER LIGADO" -- e ele nunca foi ligado. Eu tinha
    # transformado esse aviso em recusa do robo; ele mandou tirar, porque isso
    # inventaria uma trava que nunca existiu na pratica.
    # `credito_liberacao.exige_segunda_aprovacao` CONTINUA sendo gravada: e o
    # retrato do que a politica dizia no momento da decisao (mesma familia das
    # colunas `motor_*`), e e por ela que se acha o caso no dia em que o fluxo
    # existir. Gravar nao e o mesmo que barrar.

    try:
        empresa_id, cliente_id = resolver_cliente(page, cnpj)
    except Exception as e:
        aud.fechar_linha(log_id, decisao="erro", fase="resolver_cliente", motivo=str(e),
                         duracao_ms=_ms())
        raise
    url = f"https://app.mercos.com/{empresa_id}/clientes/{cliente_id}/"
    aud.fase(log_id, "cliente_resolvido", cliente=nome or cnpj, empresa=empresa_id,
             cliente_mercos=cliente_id, url=url)

    try:
        abrir_modal_limite(page, empresa_id, cliente_id)
        disp, total = ler_limites(page)
    except Exception as e:
        aud.fechar_linha(log_id, decisao="erro", fase="abrir_e_ler", motivo=str(e),
                         duracao_ms=_ms())
        raise
    log(f"[limite] {nome or cnpj}: disponivel {disp} · total {total}")
    aud.fase(log_id, "limites_lidos", cliente=nome or cnpj, disponivel=disp, total=total)
    gravar_espelho(sb, cnpj=cnpj, empresa_id=empresa_id, cliente_id=cliente_id, nome=nome,
                   disponivel=disp, total=total, origem="reposicao", rodada_id=aud.rodada_id)
    try:
        aud.sb.table("credito_reposicao_log").update({
            "empresa_id": empresa_id, "cliente_mercos_id": cliente_id,
            "disponivel_antes": disp, "total_antes": total, "url": url,
        }).eq("id", log_id).execute()
    except Exception as e:
        log(f"[!] auditoria: nao completei os limites da linha: {e}")

    # ── Guarda 1: nao inventa controle onde nao ha ───────────────────────────
    # 63 de 75 clientes amostrados estao em 0/0, que significa "sem limite
    # controlado" -- compram livre. Escrever um teto aqui CRIA um bloqueio que
    # hoje nao existe, e nao e o robo que decide comecar a controlar um cliente.
    if total is None:
        motivo = "nao deu pra ler o teto atual -- nao escrevo sobre numero que nao li"
        aud.fechar_linha(log_id, decisao="erro", fase="teto_ilegivel", motivo=motivo,
                         duracao_ms=_ms())
        fechar_modal(page)
        raise TelaNaoMapeada(motivo)
    # ⚠️ EXCECAO PARA O DESPACHO DA ANALISE DE CREDITO (decisao do usuario, 17/set/2026).
    #
    # A guarda acima existe porque "comecar a controlar um cliente e decisao de gente". Quando a
    # liberacao vem de `origem = 'analise-credito'`, ESSA DECISAO JA FOI TOMADA: um analista
    # assinou um limite especifico, com validade, parecer e trilha -- e o publico daquela aba e
    # justamente o cliente NOVO, que esta em 0/0 porque ninguem configurou, nao por politica.
    # A guarda continua valendo INTEIRA para o botao Liberar da Gestao de Credito, cujo publico e
    # quem ja compra ha anos sem trava.
    #
    # ⚠️ PERMITIR NAO E SILENCIAR. Passar a controlar quem comprava livre e uma RESTRICAO, e
    #    quem le o aviso precisa saber disso -- por isso a excecao carrega um marcador que a
    #    auditoria registra e o WhatsApp escreve. Sem ele, o cliente descobriria pelo pedido
    #    barrado.
    #
    # Medido em 17/set: dos 1.642 CNPJs com proposta aberta, 289 estao em 0/0 no espelho e 890
    # nunca foram lidos (o comentario original registra 63 de 75 amostrados em 0/0) -- ou seja,
    # sem esta excecao a maior parte do fluxo novo nao subiria sozinha.
    # ── MODO INCREMENTAL: o teto assinado e um ACRESCIMO, nao o valor final ──
    #
    # Decisao do usuario em 17/set/2026 para o despacho da Analise de Credito: "se a analise e de
    # 5 mil e o cliente ja tem 5 mil de limite, o robo ve quanto tem e adiciona -- fica 10 mil".
    #
    # ⚠️ O ALVO E CONGELADO NA PRIMEIRA LEITURA (`fn_credito_liberacao_fixa_alvo`), e e isso que
    #    devolve a IDEMPOTENCIA que o modo absoluto tinha de graca. O comentario do carimbo, mais
    #    abaixo, explica a janela: entre escrever no Mercos e carimbar `aplicado_no_mercos` o
    #    processo pode morrer; a liberacao volta na proxima rodada e o robo reescreve. Em valor
    #    absoluto isso e inocuo. Somando ao vivo, 5.000 viraria 10.000 e depois 15.000 -- a
    #    soma-em-dobro que obrigou a reposicao de recibos a ter trava de uma-rodada-por-dia.
    #    Com o alvo congelado, a 2a passada reescreve o MESMO numero.
    #    Provado em transacao desfeita: teto 5.000 + 5.000 -> alvo 10.000; repetindo a chamada com
    #    o Mercos ja em 10.000 e depois em 99.999, o alvo segue 10.000.
    if (lib.get("modo_aplicacao") or "absoluto") == "incremental":
        alvo = lib.get("teto_alvo")
        if alvo is None:
            r = sb.rpc("fn_credito_liberacao_fixa_alvo", {
                "p_id": lib.get("id"), "p_teto_lido": total,
            }).execute()
            alvo = r.data if not isinstance(r.data, list) else (r.data[0] if r.data else None)
        if alvo is None:
            motivo = f"{codigo}: modo incremental sem alvo -- nao escrevo sem saber o teto final"
            aud.fechar_linha(log_id, decisao="erro", fase="alvo_ausente", motivo=motivo, duracao_ms=_ms())
            fechar_modal(page)
            raise TelaNaoMapeada(motivo)
        acrescimo = novo_total
        novo_total = float(alvo)
        log(f"[limite] {nome or cnpj}: INCREMENTAL -- teto atual {_br(total)} + {_br(acrescimo)} "
            f"= {_br(novo_total)}")
        aud.fase(log_id, "alvo_incremental", cliente=nome or cnpj, teto_atual=total,
                 acrescimo=acrescimo, teto_alvo=novo_total)

    virou_controlado = False
    if total <= 0:
        if (lib.get("origem") or "") == "analise-credito":
            virou_controlado = True
            log(f"[limite] {nome or cnpj}: estava em 0/0 (compra livre) e PASSA A SER CONTROLADO "
                f"em {_br(novo_total)} -- despacho da Analise de Credito {codigo}")
            aud.fase(log_id, "passa_a_ser_controlado", cliente=nome or cnpj,
                     teto_novo=novo_total, origem="analise-credito")
        else:
            motivo = (f"{codigo}: cliente esta em 0/0 no Mercos (sem limite controlado, compra livre). "
                      f"Escrever o teto de {_br(novo_total)} aqui CRIARIA um bloqueio que hoje nao "
                      "existe -- comecar a controlar um cliente e decisao de gente.")
            aud.fechar_linha(log_id, decisao="pulado", fase="sem_limite_controlado", motivo=motivo,
                             duracao_ms=_ms())
            fechar_modal(page)
            raise PrecisaDeGente(motivo)
    if disp is None:
        motivo = "nao deu pra ler o disponivel atual -- o delta precisa dele"
        aud.fechar_linha(log_id, decisao="erro", fase="disponivel_ilegivel", motivo=motivo,
                         duracao_ms=_ms())
        fechar_modal(page)
        raise TelaNaoMapeada(motivo)

    # ── Guarda 2: alguem mexeu no Mercos entre a decisao e a escrita? ────────
    # O modal da Torre mostra o teto do Mercos (do espelho) no momento de decidir,
    # e grava esse retrato. Se o que esta na tela agora nao e o que a pessoa viu,
    # alguem digitou algo la no meio -- e sobrescrever seria apagar a decisao
    # dessa pessoa sem ela saber.
    # ⚠️ A recusa DIZ A IDADE DO RETRATO, porque a acao muda com ela: retrato de 2
    # horas divergindo = alguem mexeu mesmo; retrato de 3 semanas = o nosso
    # espelho e que estava velho, e basta reaprovar na tela ja atualizada.
    # ⚠️ EM MODO INCREMENTAL A GUARDA 2 NAO SE APLICA, e a razao e a propria semantica: ali o
    #    valor assinado e um ACRESCIMO ao que existir, entao o teto ter mudado desde a decisao
    #    nao invalida nada -- soma-se ao numero novo, que e o comportamento pedido. Recusar aqui
    #    barraria justamente o caso comum (cliente sem retrato ou com retrato velho). O que
    #    protege o modo incremental de escrever duas vezes e o alvo congelado, acima.
    # ⚠️ TAMBEM NAO SE APLICA AO DESPACHO DA ANALISE DE CREDITO (24/set/2026), agora que ele pode
    #    ser `absoluto` ("Novo limite"): ali o retrato vem do ESPELHO (`credito_limite_mercos`,
    #    renovado a cada ~7 dias) e diverge do Mercos por simples idade -- a guarda barraria o caso
    #    comum. E a decisao da analista ja e o teto FINAL: "novo limite = 10 mil" vale seja qual
    #    for o teto de hoje. O disponivel continua correto porque o delta sai do teto LIDO AGORA.
    #    A guarda segue inteira para o botao Liberar da Gestao de Credito (origem gestao-credito).
    retrato = (None if (lib.get("modo_aplicacao") or "absoluto") == "incremental"
               or (lib.get("origem") or "") == "analise-credito"
               else lib.get("mercos_teto_no_momento"))
    if retrato is not None and abs(float(retrato) - total) > 0.01:
        horas = lib.get("retrato_horas")
        motivo = (f"{codigo}: quando a decisao foi tomada o Mercos tinha teto {_br(float(retrato))} "
                  f"e agora tem {_br(total)}"
                  + (f" (o retrato era de {horas}h atras)" if horas is not None else "")
                  + ". Alguem mexeu no meio, ou o espelho estava velho. NAO sobrescrevo a "
                    "decisao de outra pessoa -- reaprovar na tela, que ja mostra o valor atual.")
        aud.fechar_linha(log_id, decisao="pulado", fase="teto_divergente", motivo=motivo,
                         duracao_ms=_ms())
        fechar_modal(page)
        raise PrecisaDeGente(motivo)
    sem_retrato = retrato is None

    # ── A conta ──────────────────────────────────────────────────────────────
    delta = round(novo_total - total, 2)
    novo_disp = round(min(max(disp + delta, 0.0), novo_total), 2)

    if abs(delta) <= 0.01:
        # O teto ja e o assinado. Nao ha o que escrever, e a liberacao esta
        # satisfeita -- carimbar aqui e o que a tira da fila para sempre. Nao
        # carimbar a deixaria voltando toda noite sem nunca ter o que fazer.
        motivo = f"{codigo}: o teto do Mercos ja esta em {_br(total)}, que e o valor assinado"
        if aplicar:
            _carimbar_aplicada(sb, lib, aud, motivo=motivo, teto_antes=total, teto_depois=total,
                               disp_antes=disp, disp_depois=disp)
        aud.fechar_linha(log_id, decisao="aplicado" if aplicar else "dry-run",
                         fase="ja_no_valor", motivo=motivo, duracao_ms=_ms(),
                         disponivel_antes=disp, disponivel_depois=disp, total_depois=total)
        fechar_modal(page)
        return f"[ja no valor] {motivo}"

    nota_retrato = (" ⚠️ sem retrato do Mercos no momento da decisao -- nao deu pra conferir se "
                    "alguem mexeu no meio" if sem_retrato else "")
    piso = novo_disp > disp + delta + 0.01     # o max(...,0) mordeu
    conta = (f"teto {_br(total)} -> {_br(novo_total)} (delta {_br(delta)}) e disponivel "
             f"{_br(disp)} -> {_br(novo_disp)}")

    if not aplicar:
        txt = f"[dry-run] {codigo} · {conta} · empresa {empresa_id}, cliente {cliente_id}{nota_retrato}"
        aud.fechar_linha(log_id, decisao="dry-run", fase="ensaio", motivo=txt,
                         disponivel_antes=disp, disponivel_depois=novo_disp,
                         total_depois=novo_total, duracao_ms=_ms())
        fechar_modal(page)
        return txt

    if not log_id:
        raise AuditoriaIndisponivel(
            "a linha de auditoria desta liberacao nao existe -- NAO vou escrever no Mercos sem registro")
    aud.fechar_linha(log_id, escreveu=True, fase="vai_escrever",
                     motivo=f"vai escrever {conta}{nota_retrato}")
    aud.fase(log_id, "vai_escrever", cliente=nome or cnpj, liberacao=codigo,
             teto_de=total, teto_para=novo_total, disp_de=disp, disp_para=novo_disp)

    try:
        resultado = escrever_limites(page, novo_disp, novo_total, disp, total)
    except ConferirNaMao as e:
        aud.fechar_linha(log_id, decisao="conferir", fase="releitura_divergente", motivo=str(e),
                         confirmado=False, duracao_ms=_ms())
        aud.evento_json(etapa="conferir_na_mao", liberacao=codigo, cnpj=cnpj, cliente=nome,
                        erro=str(e))
        fechar_modal(page)
        raise
    except Exception as e:
        aud.fechar_linha(log_id, decisao="erro", fase="erro_na_escrita", motivo=str(e),
                         duracao_ms=_ms())
        aud.evento_json(etapa="erro_na_escrita", liberacao=codigo, cnpj=cnpj, erro=str(e))
        fechar_modal(page)
        raise

    if piso:
        resultado += (f" ⚠️ o delta levaria o disponivel a {_br(disp + delta)} e foi limitado "
                      "ao piso de zero")
    resultado += nota_retrato
    aud.fase(log_id, "salvou", cliente=nome or cnpj)
    depois_disp, depois_total = _reler_limites_silencioso(page)

    # ⚠️ CARIMBA SO DEPOIS DA RELEITURA CONFIRMAR. Entre a escrita no Mercos e o
    # carimbo existe um instante em que o processo pode morrer: se morrer, a
    # liberacao volta na proxima rodada e o robo reescreve o MESMO teto -- inocuo
    # no valor (nao e delta, e valor absoluto), diferente da reposicao.
    _carimbar_aplicada(sb, lib, aud, motivo=resultado,
                       teto_antes=total,
                       teto_depois=depois_total if depois_total is not None else novo_total,
                       disp_antes=disp,
                       disp_depois=depois_disp if depois_disp is not None else novo_disp)

    aud.fechar_linha(log_id, decisao="aplicado", fase="releu", motivo=resultado, confirmado=True,
                     disponivel_antes=disp,
                     disponivel_depois=depois_disp if depois_disp is not None else novo_disp,
                     total_depois=depois_total if depois_total is not None else novo_total,
                     duracao_ms=_ms())
    gravar_espelho(sb, cnpj=cnpj, empresa_id=empresa_id, cliente_id=cliente_id, nome=nome,
                   disponivel=depois_disp if depois_disp is not None else novo_disp,
                   total=depois_total if depois_total is not None else novo_total,
                   origem="reposicao", rodada_id=aud.rodada_id)
    aud.evento_json(etapa="teto_aplicado", liberacao=codigo, cnpj=cnpj, cliente=nome,
                    teto_de=total, teto_para=depois_total, disp_de=disp, disp_para=depois_disp,
                    assinado_por=lib.get("criado_por"))
    # Avisa SO depois do carimbo e da releitura -- ver `avisar_credito_liberado`.
    avisar_credito_liberado(sb, lib, nome=nome, teto_depois=depois_total if depois_total is not None else novo_total,
                            virou_controlado=virou_controlado)
    fechar_modal(page)
    return f"{codigo} · {resultado}"


# ── Aviso de credito liberado (WhatsApp, canal interno) ──────────────────────
#
# Pedido do usuario em 17/set/2026: quando o analista despacha uma aprovacao na aba de Analise
# de Credito e o limite sobe no Mercos, avisar por WhatsApp -- "bem parecido" com o alerta que o
# robo de cadastro ja manda quando um cliente e vinculado.
#
# ⚠️ SO PARA LIBERACAO QUE VEIO DO DESPACHO (`origem = 'analise-credito'`). A liberacao feita
#    pelo botao "Liberar" da Gestao de Credito NAO avisa: aquele fluxo existe desde 25/ago sem
#    aviso nenhum, e passar a mandar WhatsApp por ele seria mudar um fluxo que ninguem pediu
#    para mudar -- e quem o usa veria mensagem nova sem explicacao.
#
# ⚠️ SO NO SUCESSO, e so DEPOIS do carimbo e da releitura da tela. Avisar antes diria "ja pode
#    fazer pedido" sobre um numero que ainda pode nao ter entrado. Falha tem canal proprio: o
#    e-mail de `aviso.py`, que e onde o erro precisa aparecer.
#
# ⚠️ MODO TESTE ligado, igual ao `escritor.py`: manda para o numero do Italo em vez do vendedor
#    real. Decisao do usuario em 17/set -- "por enquanto vamos testar apenas com o meu". O numero
#    do Rafael (+55 85 9643-4585) entra em `WHATSAPP_EXTRA_TESTE` quando os testes passarem.
#    Trocar `WHATSAPP_MODO_TESTE` para False manda para o vendedor do cliente.
#
# ⚠️ NUNCA derruba a rodada. O limite JA subiu no Mercos quando esta funcao roda; falhar em
#    avisar nao pode virar uma segunda falha nem mudar o exit code (mesma regra do `aviso.py`).
WHATSAPP_MODO_TESTE = True
WHATSAPP_NUMERO_TESTE = "5585986160142"          # Italo
WHATSAPP_EXTRA_TESTE: list[str] = []             # Rafael entra aqui depois dos testes


def _brl(v) -> str:
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "~").replace(".", ",").replace("~", ".")
    except Exception:
        return "—"


def _fmt_cnpj(c: str) -> str:
    d = re.sub(r"\D", "", str(c or ""))
    if len(d) != 14:
        return str(c or "—")
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


def _fmt_data_br(v) -> str:
    """AAAA-MM-DD (ou timestamp ISO) -> DD/MM/AAAA. Formato inesperado volta como veio, nunca some."""
    t = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else (t or "—")


def avisar_credito_liberado(sb, lib: dict, *, nome: str | None, teto_depois, virou_controlado: bool = False) -> None:
    if (lib.get("origem") or "") != "analise-credito":
        return

    vendedor = (lib.get("vendedor_nome") or "").strip()
    # ⚠️ O SENTINELA NAO E UM VENDEDOR. `dim_vendedor` tem a linha
    #    "VENDEDOR NAO MAPEADO (SISTEMA)" (id 00000000-...), que significa AUSENCIA de
    #    informacao -- escrever esse nome na mensagem faria o aviso parecer que o cliente tem um
    #    vendedor chamado assim. Caso real no 1o teste: a LORENA EVANGELISTA esta com o sentinela.
    if "nao mapeado" in _norm(vendedor):
        vendedor = ""
    celular = re.sub(r"[^0-9]", "", str(lib.get("vendedor_celular") or ""))

    if WHATSAPP_MODO_TESTE:
        destinos = [WHATSAPP_NUMERO_TESTE] + list(WHATSAPP_EXTRA_TESTE)
        prefixo = ("TESTE -- destino real seria "
                   + (vendedor or "vendedor sem cadastro")
                   + " (" + (celular or "sem celular") + ")@@")
        prefixo = "🧪 _[" + prefixo.replace("@@", "]_" + "\n")
    else:
        if not celular:
            log("  [whatsapp] " + str(lib.get("codigo")) + ": vendedor sem celular -- pulando aviso")
            return
        destinos = [celular if len(celular) > 11 else "55" + celular]
        prefixo = ""

    validade = lib.get("validade")
    linhas = [
        prefixo + "✅ *Análise de crédito aprovada*",
        "Cliente: " + str(nome or lib.get("nome_cliente") or "—"),
        "CNPJ: " + _fmt_cnpj(lib.get("cnpj")),
        "Vendedor: " + (vendedor or "nao mapeado na Torre"),
    ]
    # Os dois modos do despacho (24/set/2026): em "acrescentar" o valor aprovado e um ACRESCIMO, e
    # escrever so ele faria "Limite aprovado: R$ 5.000" num cliente que ficou com R$ 10.000.
    if (lib.get("modo_aplicacao") or "absoluto") == "incremental":
        linhas.append("Limite acrescentado: *" + _brl(lib.get("limite_para")) + "*")
    else:
        linhas.append("Novo limite aprovado: *" + _brl(lib.get("limite_para")) + "*")
    if teto_depois is not None:
        linhas.append("Limite total no Mercos: *" + _brl(teto_depois) + "*")
    # Data no formato DD/MM/AAAA e sem a linha "Analisado por" (pedido do usuario, 24/set/2026):
    # `validade` chega do banco como AAAA-MM-DD, que no WhatsApp lia como data americana.
    if validade:
        linhas.append("Válido até: " + _fmt_data_br(validade))
    linhas.append("")
    linhas.append("Limite já disponível no Mercos para fazer pedido.")
    # ⚠️ Declara a RESTRICAO. Este cliente comprava sem trava e passou a ter teto -- quem le o
    #    aviso precisa saber, senao descobre pelo pedido barrado.
    if virou_controlado:
        linhas.append("")
        linhas.append("⚠️ Este cliente não tinha limite controlado no Mercos (comprava sem trava) "
                      "e passou a ter, com o teto acima.")
    mensagem = "\n".join(linhas)

    for numero in destinos:
        try:
            sb.table("whatsapp_outbox").insert({
                "numero": numero,
                "mensagem": mensagem,
                "prioridade": 5,
                "origem": "torre/analise-credito",
                "referencia": json.dumps({"liberacao": lib.get("codigo"), "cnpj": lib.get("cnpj"),
                                          "proposta_id": lib.get("proposta_id")}),
            }).execute()
            log("  [whatsapp] aviso enfileirado (" + numero + ") -- " + str(lib.get("codigo")))
        except Exception as e:
            log("  [!] falha ao enfileirar whatsapp de " + str(lib.get("codigo")) + ": " + str(e))


def _carimbar_aplicada(sb, lib: dict, aud: Auditoria, *, motivo: str,
                       teto_antes, teto_depois, disp_antes, disp_depois) -> None:
    """Carimbo atomico no banco. Levanta se a liberacao foi encerrada no meio."""
    r = sb.rpc("fn_credito_liberacao_marcar_aplicada", {
        "p_id": lib.get("id"),
        "p_por": aud.quem,
        "p_resultado": motivo[:2000],
        "p_teto_antes": teto_antes,
        "p_teto_depois": teto_depois,
        "p_disponivel_antes": disp_antes,
        "p_disponivel_depois": disp_depois,
    }).execute()
    linha = (r.data or [{}])[0] if isinstance(r.data, list) else (r.data or {})
    if linha.get("ja_aplicada"):
        log(f"[!] {lib.get('codigo')} ja constava aplicada -- outra rodada chegou antes. "
            "O carimbo original foi preservado.")


def processar_desfazer(page: Page, sb, lib: dict, aplicar: bool, aud: Auditoria) -> str:
    """DESFAZ no Mercos uma liberacao que expirou ou foi revogada.

    ⚠️ Devolve `mercos_teto_antes` -- o valor que o ROBO leu na tela antes de
    escrever --, NUNCA `limite_de`, que e o limite do PROTHEUS e pode nao ter nada
    a ver com o que estava no Mercos.

    ⚠️ O RISCO QUE O DESFAZER TEM E O APLICAR NAO: entre uma coisa e outra o
    cliente pode ter comprado. Subtrair o delta de um disponivel ja consumido pode
    dar negativo -- dai o piso em zero, declarado no resultado."""
    t_ini = time.time()
    cnpj = re.sub(r"\D", "", lib.get("cnpj") or "")
    codigo = lib.get("codigo") or "?"
    nome = lib.get("nome_cliente") or nome_do_cliente(sb, cnpj)
    devolver = float(lib.get("teto_para_devolver"))
    deixamos = lib.get("teto_que_ficou")

    def _ms():
        return int((time.time() - t_ini) * 1000)

    log(f"── desfazer {codigo} · {nome or cnpj} · {lib.get('encerrado_tipo')} em "
        f"{str(lib.get('encerrado_em'))[:16]} · devolver teto {_br(devolver)}")

    log_id = aud.abrir_linha(_ev_liberacao(lib), decisao="em_andamento", fase="iniciou_desfazer",
                             cliente_nome=nome, liberacao_id=lib.get("id"), exigir=aplicar)

    try:
        empresa_id, cliente_id = resolver_cliente(page, cnpj)
        abrir_modal_limite(page, empresa_id, cliente_id)
        disp, total = ler_limites(page)
    except Exception as e:
        aud.fechar_linha(log_id, decisao="erro", fase="abrir_e_ler", motivo=str(e), duracao_ms=_ms())
        raise
    url = f"https://app.mercos.com/{empresa_id}/clientes/{cliente_id}/"
    aud.fase(log_id, "limites_lidos", cliente=nome or cnpj, disponivel=disp, total=total, url=url)
    try:
        aud.sb.table("credito_reposicao_log").update({
            "empresa_id": empresa_id, "cliente_mercos_id": cliente_id,
            "disponivel_antes": disp, "total_antes": total, "url": url,
        }).eq("id", log_id).execute()
    except Exception as e:
        log(f"[!] auditoria: nao completei os limites da linha: {e}")

    if total is None or disp is None:
        motivo = "nao deu pra ler os limites -- nao devolvo sobre numero que nao li"
        aud.fechar_linha(log_id, decisao="erro", fase="ilegivel", motivo=motivo, duracao_ms=_ms())
        fechar_modal(page)
        raise TelaNaoMapeada(motivo)

    # ⚠️ "JA NO VALOR" VEM ANTES DA GUARDA DE "MEXERAM DEPOIS" (24/set/2026). Quando alguem
    #    desfaz a mao, o teto ja e o valor a devolver -- o objetivo esta cumprido e nao ha nada a
    #    escrever, logo nada a sobrescrever. Com a ordem antiga a guarda disparava primeiro e o item
    #    ficava PRESO PARA SEMPRE (caso real: LC-00007, teste de 17/set desfeito a mao, voltando
    #    como 'com erro' em toda rodada).
    if abs(round(devolver - total, 2)) <= 0.01:
        motivo = f"{codigo}: o teto ja esta em {_br(total)}, que e o valor a devolver"
        if aplicar:
            _carimbar_revertida(sb, lib, aud, motivo)
        aud.fechar_linha(log_id, decisao="revertido" if aplicar else "dry-run",
                         fase="ja_no_valor", motivo=motivo, duracao_ms=_ms())
        fechar_modal(page)
        return f"[ja no valor] {motivo}"
    # ⚠️ O TETO AINDA E O QUE NOS DEIXAMOS? Se alguem mexeu depois da nossa
    # escrita, o valor de agora e decisao dessa pessoa -- devolver o antigo por
    # cima apagaria a decisao dela. Mesma regra da guarda 3 do aplicar.
    if deixamos is not None and abs(float(deixamos) - total) > 0.01:
        motivo = (f"{codigo}: escrevi {_br(float(deixamos))} e o Mercos esta em {_br(total)} -- "
                  "alguem mexeu no teto depois de mim. NAO devolvo por cima da decisao de outra "
                  "pessoa; conferir na mao se o valor de hoje e o desejado.")
        aud.fechar_linha(log_id, decisao="pulado", fase="teto_mexido_depois", motivo=motivo,
                         duracao_ms=_ms())
        fechar_modal(page)
        raise PrecisaDeGente(motivo)

    delta = round(devolver - total, 2)
    novo_disp = round(min(max(disp + delta, 0.0), devolver), 2)
    piso = novo_disp > disp + delta + 0.01
    conta = (f"teto {_br(total)} -> {_br(devolver)} (delta {_br(delta)}) e disponivel "
             f"{_br(disp)} -> {_br(novo_disp)}")


    if not aplicar:
        txt = f"[dry-run] desfazer {codigo} · {conta}"
        aud.fechar_linha(log_id, decisao="dry-run", fase="ensaio", motivo=txt,
                         disponivel_antes=disp, disponivel_depois=novo_disp,
                         total_depois=devolver, duracao_ms=_ms())
        fechar_modal(page)
        return txt

    if not log_id:
        raise AuditoriaIndisponivel(
            "a linha de auditoria deste desfazer nao existe -- NAO vou escrever no Mercos sem registro")
    aud.fechar_linha(log_id, escreveu=True, fase="vai_escrever", motivo=f"vai devolver {conta}")
    try:
        resultado = escrever_limites(page, novo_disp, devolver, disp, total)
    except ConferirNaMao as e:
        aud.fechar_linha(log_id, decisao="conferir", fase="releitura_divergente", motivo=str(e),
                         confirmado=False, duracao_ms=_ms())
        fechar_modal(page)
        raise
    except Exception as e:
        aud.fechar_linha(log_id, decisao="erro", fase="erro_na_escrita", motivo=str(e),
                         duracao_ms=_ms())
        fechar_modal(page)
        raise

    if piso:
        resultado += (f" ⚠️ o cliente comprou depois da liberacao: o delta levaria o disponivel a "
                      f"{_br(disp + delta)} e foi limitado ao piso de zero")
    depois_disp, depois_total = _reler_limites_silencioso(page)
    _carimbar_revertida(sb, lib, aud, resultado)
    aud.fechar_linha(log_id, decisao="revertido", fase="releu", motivo=resultado, confirmado=True,
                     disponivel_antes=disp,
                     disponivel_depois=depois_disp if depois_disp is not None else novo_disp,
                     total_depois=depois_total if depois_total is not None else devolver,
                     duracao_ms=_ms())
    gravar_espelho(sb, cnpj=cnpj, empresa_id=empresa_id, cliente_id=cliente_id, nome=nome,
                   disponivel=depois_disp if depois_disp is not None else novo_disp,
                   total=depois_total if depois_total is not None else devolver,
                   origem="reposicao", rodada_id=aud.rodada_id)
    aud.evento_json(etapa="teto_revertido", liberacao=codigo, cnpj=cnpj, cliente=nome,
                    teto_de=total, teto_para=depois_total, motivo_encerramento=lib.get("encerrado_tipo"))
    fechar_modal(page)
    return f"desfeita {codigo} · {resultado}"


def _carimbar_revertida(sb, lib: dict, aud: Auditoria, motivo: str) -> None:
    r = sb.rpc("fn_credito_liberacao_marcar_revertida", {
        "p_id": lib.get("id"), "p_por": aud.quem, "p_resultado": motivo[:2000],
    }).execute()
    linha = (r.data or [{}])[0] if isinstance(r.data, list) else (r.data or {})
    if linha.get("ja_revertida"):
        log(f"[!] {lib.get('codigo')} ja constava revertida -- outra rodada chegou antes.")


def rodar_liberacoes(sb, aud: Auditoria, aplicar: bool, teto: int,
                     ids: list[str] | None) -> tuple[int, int, int, int, int, str | None]:
    """A rodada do teto. Devolve (fila, feitas, pulados, erros, conferir, abortou).

    ⚠️ `fila` e o TAMANHO DA FILA, nao a soma dos processados: rodada abortada no
    meio tem que registrar quantos itens HAVIA, senao a auditoria diz que a fila
    era do tamanho do que deu tempo de fazer.

    ⚠️ DESFAZER VEM PRIMEIRO, e a ordem e escolhida. Se a rodada morrer no meio,
    o que ja aconteceu foi BAIXAR limite vencido, nao SUBIR limite novo -- entre
    as duas metades, a que reduz risco vai antes. A liberacao que nao subiu hoje
    mantem o estado de ontem e alguem pode digitar na mao; o teto que ficou alto
    depois de expirar e credito que a Torre disse que tinha acabado."""
    desfazer = fila_desfazer(sb, teto, ids)
    aplicar_fila = fila_liberacoes(sb, teto, ids)
    total_itens = len(desfazer) + len(aplicar_fila)
    if not total_itens:
        log("[liberacoes] nada na fila -- nenhuma liberacao pendente e nenhuma a desfazer")
        return (0, 0, 0, 0, 0, None)

    log(f"[liberacoes] {len(desfazer)} a desfazer · {len(aplicar_fila)} a aplicar")
    try:
        sb.table("credito_reposicao_rodada").update(
            {"fila_lida": total_itens}).eq("id", aud.rodada_id).execute()
    except Exception as e:
        log(f"[!] auditoria: nao gravei o tamanho da fila: {e}")

    feitas = pulados = erros = conferir = 0
    abortou = None
    with sync_playwright() as pw:
        nav = pw.chromium.launch(headless=True)
        page = nav.new_page()
        try:
            login_mercos(page, EMPRESAS[0], MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
            voltas_l = 0
            for etapa, fila, fn in (("desfazer", desfazer, processar_desfazer),
                                    ("aplicar", aplicar_fila, processar_liberacao)):
                # Mesma lista de trabalho dos outros dois lacos. Refazer a
                # liberacao que pegou a queda e seguro por DOIS motivos: a queda
                # so nasce antes do Salvar, e o teto e valor ABSOLUTO (aplicar
                # duas vezes nao soma nada -- diferente da reposicao, que soma
                # delta, e por isso la o carimbo da fila e sagrado).
                pendentes_l = list(fila)
                feitos_l = 0
                while pendentes_l:
                    lib = pendentes_l.pop(0)
                    feitos_l += 1
                    aud.fase(None, f"{etapa} {feitos_l}/{len(fila)}",
                             cliente=lib.get("nome_cliente") or lib.get("cnpj"))
                    try:
                        log(f"[{etapa}] {fn(page, sb, lib, aplicar, aud)}")
                        feitas += 1
                    except SessaoDerrubada as e:
                        if voltas_l < VOLTAS_POR_RODADA and reconquistar_sessao(
                                page, aud, str(e), voltas_l + 1, VOLTAS_POR_RODADA):
                            voltas_l += 1
                            pendentes_l.insert(0, lib)
                            feitos_l -= 1
                            continue
                        abortou = str(e)
                        if voltas_l:
                            abortou += f" (ja tinha voltado {voltas_l}x nesta rodada)"
                        break
                    except PrecisaDeGente as e:
                        pulados += 1
                        log(f"[{etapa}] [aguarda gente] {e}")
                    except PulaSemErro as e:
                        pulados += 1
                        log(f"[{etapa}] {e}")
                    except ConferirNaMao as e:
                        conferir += 1
                        log(f"[{etapa}] [!] CONFERIR NA MAO: {e}")
                    except Exception as e:
                        erros += 1
                        log(f"[{etapa}] [!] {lib.get('codigo')}: {e}")
                if abortou:
                    break
        finally:
            nav.close()
    return (total_itens, feitas, pulados, erros, conferir, abortou)


# ── Rodada ─────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Repoe no Mercos o credito disponivel de boletos pagos.")
    p.add_argument("--aplicar", action="store_true", help="ESCREVE de verdade. Sem isso, dry-run (padrao).")
    p.add_argument("--mapear", action="store_true", help="So abre a tela do 1o da fila e confere o mapeamento.")
    # Varredura de LEITURA (21/ago/2026, pedido do Kaique): alimenta o espelho
    # `credito_limite_mercos` com o disponivel de quem nunca entrou na fila.
    # Nunca escreve no Mercos, nem com --aplicar.
    p.add_argument("--varredura", action="store_true",
                   help="So LE o limite dos clientes e grava no espelho da Torre. Nunca escreve no Mercos.")
    p.add_argument("--universo", choices=["credito", "todos"], default="credito",
                   help="Quem varrer: 'credito' = os ~800 da tela de credito (padrao, decisao do "
                        "dono da Torre em 21/ago); 'todos' = a base inteira de dim_cliente.")
    p.add_argument("--frescor-dias", type=int, default=0,
                   help="Pula quem foi lido nos ultimos N dias. E o que faz a varredura noturna "
                        "parar de trabalhar sozinha quando tudo esta em dia.")
    p.add_argument("--limite", type=int, default=TETO_RODADA_PADRAO, help=f"Teto da rodada (padrao {TETO_RODADA_PADRAO}).")
    p.add_argument("--cnpj", default=None, help="Um CNPJ especifico.")
    # A Torre manda a lista que uma PESSOA marcou na tela (peca 1 de 5, 21/ago/2026).
    # Aceita 1 ou N -- `--cnpj` continua valendo, e os dois somam sem se anular.
    p.add_argument("--boletos", default=None,
                   help="Lista de id_boleto separados por virgula -- a selecao EXATA da tela. "
                        "Ganha de --cnpjs/--cnpj: ver carregar_fila().")
    p.add_argument("--cnpjs", default=None,
                   help="Lista de CNPJs separados por virgula (o que a tela da Torre manda). "
                        "Selecao explicita IGNORA o --limite: ver carregar_fila().")
    p.add_argument("--testar-aviso", action="store_true",
                   help="Manda um e-mail de teste pra AVISO_PARA e sai. Nao toca em nada.")
    # ── O TETO (25/ago/2026) -- outro trabalho, mesmo robo ────────────────────
    p.add_argument("--liberacoes", action="store_true",
                   help="Escreve no Mercos o TETO das liberacoes assinadas na Gestao de Credito, "
                        "e DESFAZ as que expiraram/foram revogadas. Sem --aplicar, e dry-run.")
    p.add_argument("--liberacao", default=None,
                   help="id(s) de credito_liberacao separados por virgula -- a selecao exata. "
                        "Ignora o --limite (mesma razao do --boletos).")
    args = p.parse_args()

    # ⚠️ ANTES de qualquer outra coisa: nao abre navegador, nao le fila, nao loga
    # no Mercos. A credencial do Gmail so existe como Secret do GitHub, entao este
    # e o unico jeito de provar que o aviso de falha chega -- e provar isso ANTES
    # do dia da falha e o ponto.
    if args.testar_aviso:
        print()
        print("=== CreditoLimite-Mercos · TESTE DO CANAL DE AVISO (nao escreve nada) ===")
        print(f"    destinatario(s): {', '.join(destinatarios())}")
        print()
        if enviar_teste(log):
            print("=== e-mail de teste enviado. Se nao chegar em 2 min, olhar spam. ===")
            return 0
        print("⛔ o e-mail de teste NAO saiu -- a causa esta na linha [aviso] acima.")
        return 1

    # ⚠️ `--varredura` derruba o `--aplicar`: quem pede varredura pediu LEITURA, e
    # um `--aplicar` esquecido na linha de comando nao pode virar escrita.
    aplicar = args.aplicar and not args.mapear and not args.varredura
    modo = ("VARREDURA -- so leitura" if args.varredura
            else "MAPEAMENTO (nao escreve)" if args.mapear
            else "APLICANDO DE VERDADE" if aplicar
            else "dry-run (nao escreve)")
    # ⚠️ O CABECALHO TEM QUE DIZER QUAL DOS DOIS CAMPOS ESTA EM JOGO. A frase "o
    # campo 'limite total' NAO e tocado" e verdadeira na reposicao e MENTIRA no
    # modo --liberacoes, que existe justamente para toca-lo. Log que declara a
    # garantia errada e pior que log sem garantia nenhuma.
    tarefa = "TETO por liberacao assinada" if args.liberacoes else "repositor de disponivel"
    print(f"\n=== CreditoLimite-Mercos · {tarefa} · {modo} ===")
    if args.liberacoes:
        print(f"    empresas: {', '.join(EMPRESAS)} · escreve 'limite total' E 'limite disponivel'")
        print("    o numero vem de credito_liberacao.limite_para -- assinado por uma PESSOA.")
        print("    NUNCA de motor_limite_sugerido (POLITICA_HOMOLOGADA segue false).")
    else:
        print(f"    empresas: {', '.join(EMPRESAS)} · o campo 'limite total' NAO e tocado")
    # Declarado no topo: quem le o log de uma rodada que escreveu no Mercos tem
    # que saber, na primeira tela, se uma PESSOA pediu isso ou se foi o relogio.
    if disparo_e_cron():
        print("    gatilho ......... CRON (schedule 19h30 BRT) -- ninguem pediu esta rodada;")
        print("                      trava de uma rodada de cron por dia ativa (20260824e)")
    print()

    conferir_credencial_gmail(GMAIL_USER, GMAIL_SENHA, MERCOS_EMAIL)
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    conferir_backfill(sb)

    # ⚠️ AUDITORIA ABRE ANTES DE TUDO. Rodada que nao consegue registrar nao roda:
    # escrever no Mercos sem registro e pior que nao escrever, porque o dinheiro
    # muda de lugar e ninguem sabe quem mandou. Ver `auditoria.py`, decisao 2.
    # ⚠️ 'liberacoes' e um modo PROPRIO, nao 'aplicar'. As duas rodadas escrevem
    # no Mercos, em campos diferentes e com autorizacoes de origem diferente --
    # colapsar as duas faria a auditoria nao conseguir responder "esta rodada
    # podia mexer no teto?" sem ler o log inteiro. Efeito colateral pretendido:
    # como `Auditoria` so preenche `cron_dia` quando modo == 'aplicar', a rodada
    # de liberacoes NAO consome a trava de uma-rodada-de-cron-por-dia, que existe
    # para a reposicao (que soma delta). Aplicar teto duas vezes e inocuo: e valor
    # absoluto, e `fn_credito_liberacao_marcar_aplicada` recusa o 2o carimbo.
    modo_registro = ("liberacoes" if (args.liberacoes and aplicar)
                     else "liberacoes-dry" if args.liberacoes
                     else "varredura" if args.varredura
                     else "mapear" if args.mapear
                     else "aplicar" if aplicar
                     else "dry-run")
    # `--cnpj` e `--cnpjs` SOMAM (um nao anula o outro): a tela manda a lista e
    # alguem pode acrescentar um caso na mao na mesma rodada.
    boletos = [b.strip() for b in (args.boletos or "").split(",") if b.strip()]
    boletos = list(dict.fromkeys(boletos))  # dedup preservando ordem
    selecao = so_digitos(args.cnpjs)
    if args.cnpj:
        um = so_digitos(args.cnpj)
        selecao = selecao + [c for c in um if c not in selecao]
    # A auditoria guarda a selecao INTEIRA, nao o primeiro: sem isso, uma rodada
    # de 30 clientes ficaria registrada como se tivesse sido pedida pra um.
    aud = Auditoria(sb, modo_registro, EMPRESAS,
                    1 if args.mapear else args.limite,
                    (f"boletos:{','.join(boletos)}" if boletos
                     else (",".join(selecao) if selecao else None)), log=log)
    # ⚠️ A TRAVA DE UMA RODADA DE CRON POR DIA MORA NO INSERT (migration 20260824e).
    # `schedule` dispara em TODO espelho do rodizio com o workflow ativo, e o
    # `concurrency` do Actions e por repositorio -- duas rodadas somariam o mesmo
    # delta. Sair 0: nao e falha, e a trava fazendo o trabalho dela.
    try:
        aud.abrir()
        # A partir daqui o crash la embaixo consegue dizer o que ja aconteceu.
        global _RODADA_EM_CURSO
        _RODADA_EM_CURSO = aud
    except RodadaDeCronJaFeita as e:
        print()
        print(f"=== nada a fazer: {e} ===")
        return 0
    print(f"    auditoria: rodada {aud.rodada_id} · quem: {aud.quem}", flush=True)
    # Declarado ANTES de agir: quem roda tem que saber pra quem vai o aviso se der
    # errado, em vez de descobrir quando o e-mail chega (ou nao chega).
    print(f"    aviso de falha: {', '.join(destinatarios())}", flush=True)
    if boletos:
        print(f"    selecao ......... {len(boletos)} BOLETO(S) marcado(s) na tela "
              f"(o --limite NAO se aplica)", flush=True)
    elif selecao:
        print(f"    selecao ......... {len(selecao)} cliente(s) marcado(s) -- TODOS os boletos "
              f"pendentes deles (o --limite NAO se aplica)", flush=True)

    # ── LIBERACOES DE TETO ────────────────────────────────────────────────────
    # Caminho proprio: nao le a fila de pagamento, nao carimba `processado`, e e o
    # UNICO caminho deste arquivo que pode tocar o campo 'limite total'.
    if args.liberacoes:
        ids = [i.strip() for i in (args.liberacao or "").split(",") if i.strip()]
        ids = list(dict.fromkeys(ids))
        if ids:
            print(f"    selecao ......... {len(ids)} liberacao(oes) (o --limite NAO se aplica)",
                  flush=True)
        fila_n, feitas, pulados, erros, conferir, abortou = rodar_liberacoes(
            sb, aud, aplicar, 1 if args.mapear else args.limite, ids or None)
        aud.fase(None, f"liberacoes concluidas: {feitas} feita(s), {pulados} aguardando gente")
        # ⚠️ `valor_reposto=0` e a resposta certa, nao um campo esquecido: aquela
        # coluna responde "quanto de boleto pago voltou pro cliente?" e teto de
        # credito nao e dinheiro que voltou. Somar `limite_para` ali envenenaria
        # o unico numero financeiro da auditoria. `repostos` e reusado como
        # "itens concluidos" -- a rodada tem `modo` para dizer de que se trata.
        aud.fechar(fila_lida=fila_n, repostos=feitas, pulados=pulados, erros=erros,
                   conferir=conferir, valor_reposto=0, abortou_por=abortou,
                   ok=not abortou and not erros)
        avisar_falha(log=log, aud=aud, modo=modo_registro, abortou=abortou, erros=erros,
                     conferir=conferir, fila_lida=fila_n, repostos=feitas)
        print()
        print(f"=== liberacoes: {feitas} concluida(s) · {pulados} aguardando gente · "
              f"{conferir} pra conferir na mao · {erros} com erro (fila: {fila_n}) ===")
        if not aplicar:
            print("    Nada foi escrito no Mercos (dry-run).")
        if pulados:
            print("    'aguardando gente' NAO some da fila: volta na proxima rodada ate alguem")
            print("    resolver a causa (segunda aprovacao, cliente 0/0, teto mexido no meio).")
        if abortou:
            print(f"⛔ ABORTOU: {abortou}")
            return 1
        return 1 if (erros or conferir) else 0

    # ── VARREDURA (so leitura) ────────────────────────────────────────────────
    # Caminho proprio, curto de proposito: nao le a fila de pagamento, nao marca
    # `processado` e nao escreve no Mercos. So enche o espelho.
    if args.varredura:
        alvos = fila_varredura(sb, args.limite, selecao or None,
                               universo=args.universo, frescor_dias=args.frescor_dias)
        if not alvos:
            # Fim natural da varredura noturna: tudo dentro do frescor pedido.
            # Sair 0 (sucesso) de proposito -- "nao havia trabalho" nao e falha.
            print(f"Nada para varrer (universo={args.universo}, "
                  f"frescor={args.frescor_dias}d). Tudo em dia.")
            aud.fechar(fila_lida=0, repostos=0, pulados=0, erros=0, conferir=0,
                       valor_reposto=0, abortou_por=None, ok=True)
            return 0
        nunca = sum(1 for a in alvos if not a.get("lido_em"))
        p1 = sum(1 for a in alvos if (a.get("prioridade") or 9) == 1)
        log(f"[varredura] universo={args.universo} · {len(alvos)} cliente(s) nesta rodada -- "
            f"{nunca} nunca lido(s), {p1} com pagamento esperando reposicao")
        # ⚠️ `fila_lida` ANTES de comecar, nao no fim. Achado pelo protocolo de
        # testes (21/ago/2026): a tela mostrava "subindo o ambiente" pelos 6
        # minutos inteiros da varredura, porque `fila_lida`/`fase_atual` so eram
        # gravados no `fechar()`. Sem denominador, "X de ?" nao existe.
        try:
            sb.table("credito_reposicao_rodada").update(
                {"fila_lida": len(alvos)}).eq("id", aud.rodada_id).execute()
        except Exception as e:
            log(f"[!] auditoria: nao gravei o tamanho da varredura: {e}")
        lidos = ausentes = falhas = 0
        abortou_v = None
        with sync_playwright() as pw:
            nav = pw.chromium.launch(headless=True)
            page = nav.new_page()
            try:
                login_mercos(page, EMPRESAS[0], MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)
                # Lista de trabalho pelo mesmo motivo do laco da reposicao: o
                # cliente que pegou a queda volta pra cabeca e e relido depois do
                # relogin. Aqui nada e escrito, entao refazer e sempre seguro.
                pendentes_v = list(alvos)
                voltas_v = 0
                feitos_v = 0
                seguidas_v = 0
                while pendentes_v:
                    alvo = pendentes_v.pop(0)
                    feitos_v += 1
                    # Progresso ao vivo. `fase(None, ...)` atualiza a RODADA sem
                    # criar linha de log por cliente -- varredura nao escreve, e
                    # 60 linhas de log de leitura seriam ruido na auditoria.
                    aud.fase(None, f"varrendo {feitos_v}/{len(alvos)}",
                             cliente=alvo.get("nome") or alvo["cnpj"])
                    try:
                        log(f"[varredura] {varrer_um(page, sb, alvo, aud)}")
                        lidos += 1
                    except SessaoDerrubada as e:
                        # A causa e UMA (a conta caiu): parar em vez de marcar N
                        # clientes como ausentes, que seria dado errado no espelho.
                        # Antes de parar, tenta voltar -- foi esta varredura que
                        # abortou de verdade em 26/ago/2026 (run 32928304722).
                        if voltas_v < VOLTAS_POR_RODADA and reconquistar_sessao(
                                page, aud, str(e), voltas_v + 1, VOLTAS_POR_RODADA):
                            voltas_v += 1
                            pendentes_v.insert(0, alvo)
                            feitos_v -= 1
                            continue
                        abortou_v = str(e)
                        if voltas_v:
                            abortou_v += f" (ja tinha voltado {voltas_v}x nesta rodada)"
                        break
                    except PulaSemErro as e:
                        ausentes += 1
                        seguidas_v = 0
                        log(f"[varredura] {e}")
                    except Exception as e:
                        falhas += 1
                        seguidas_v += 1
                        log(f"[varredura] [!] {alvo['cnpj']}: {e}")
                        # ⚠️ REDE DE SEGURANCA GENERICA, e ela existe por causa do
                        # custo do modo de falha, nao por causa de UMA causa. Em
                        # 22/set/2026 a varredura levou 58 min para nao fazer nada:
                        # 5 min de trabalho e 52 min errando cliente por cliente a
                        # ~21 s cada, porque nada media que o erro tinha virado
                        # regra. Falha isolada e normal (tela lenta, timeout); 15
                        # seguidas nao sao coincidencia -- e o robo trabalhando
                        # contra uma tela que nao existe mais. Sair aqui custa uma
                        # rodada; nao sair custou uma hora de runner e 149 clientes
                        # carimbados errado no espelho.
                        # ⚠️ O contador zera em QUALQUER desfecho bom (lido ou
                        # ausente provado): o alvo e a SEQUENCIA, nunca o total --
                        # erro espalhado ao longo da fila e outra coisa.
                        if seguidas_v >= FALHAS_SEGUIDAS_LIMITE:
                            abortou_v = (f"{seguidas_v} clientes seguidos falharam "
                                         f"(ultimo: {e}) -- parei em vez de varrer a fila inteira errando")
                            break
                    else:
                        seguidas_v = 0
            finally:
                nav.close()
        # ⚠️ Na varredura, `repostos` = clientes LIDOS e `pulados` = ausentes no
        # Mercos. Reusar as colunas evita uma tabela nova, mas quem ler a
        # auditoria precisa saber -- por isso a fase final diz o que sao.
        aud.fase(None, f"varredura concluida: {lidos} lido(s), {ausentes} ausente(s)")
        aud.fechar(fila_lida=len(alvos), repostos=lidos, pulados=ausentes, erros=falhas,
                   conferir=0, valor_reposto=0, abortou_por=abortou_v, ok=not abortou_v)
        avisar_falha(log=log, aud=aud, modo="varredura", abortou=abortou_v, erros=falhas,
                     fila_lida=len(alvos), repostos=lidos)
        print()
        print(f"=== varredura: {lidos} lido(s) · {ausentes} sem cliente no Mercos · "
              f"{falhas} com erro ===")
        print("    Nada foi escrito no Mercos. O espelho `credito_limite_mercos` foi atualizado.")
        if abortou_v:
            print(f"⛔ ABORTOU: {abortou_v}")
            return 1
        return 1 if falhas else 0

    # ⚠️ Emperrado sai da rodada CEGA e entra no resumo/e-mail -- nunca e apagado
    # nem carimbado. Ver `emperrados_da_fila`. Com selecao explicita nao se aplica:
    # `--boletos <id>` E o jeito de tentar de novo depois de consertar a causa.
    emperrados = {} if (boletos or selecao) else emperrados_da_fila(sb)
    fila = carregar_fila(sb, selecao, 1 if args.mapear else args.limite,
                         boletos=boletos, emperrados=emperrados)

    # ⚠️ CNPJ MARCADO QUE NAO TEM NADA NA FILA TEM QUE SER DITO. A pessoa aprovou
    # aquele cliente esperando que algo acontecesse; silencio faz ela achar que
    # aconteceu. Causa comum: outra rodada consumiu o pagamento no meio.
    if boletos:
        achados = {str(e.get("id_boleto") or "") for e in fila}
        sem_fila = [b for b in boletos if b not in achados]
        if sem_fila:
            log(f"[selecao] {len(sem_fila)} boleto(s) marcado(s) que JA NAO ESTAO na fila "
                "(outra rodada consumiu, ou alguem deu baixa manual): " + ", ".join(sem_fila))
            aud.evento_json(etapa="selecao_sem_fila", quantos=len(sem_fila), boletos=sem_fila)
    elif selecao:
        achados = {re.sub(r"\D", "", e.get("cnpj") or "") for e in fila}
        sem_fila = [c for c in selecao if c not in achados]
        if sem_fila:
            log(f"[selecao] {len(sem_fila)} cliente(s) marcado(s) SEM pagamento pendente: "
                + ", ".join(sem_fila))
            aud.evento_json(etapa="selecao_sem_fila", quantos=len(sem_fila), cnpjs=sem_fila)
    if not fila:
        # ⚠️ "Fila vazia" e "sobrou so o que o robo nao consegue" NAO sao a mesma
        # coisa, e as duas linhas levam a acoes opostas: uma e fim de trabalho, a
        # outra e dinheiro que ninguem devolveu ao cliente. Com cron, quem le e um
        # log -- se ele disser "nada a fazer" no segundo caso, ninguem descobre.
        if emperrados:
            print(f"Fila sem nada NOVO a repor, mas {len(emperrados)} pagamento(s) estao "
                  "EMPERRADOS (erros repetidos no mesmo boleto) e precisam "
                  "de olho humano -- veja o resumo abaixo.")
            resumir_emperrados(emperrados)
        else:
            print("Fila vazia: nenhum pagamento novo esperando reposicao. Nada a fazer.")
        aud.fechar(fila_lida=0, repostos=0, pulados=0, erros=0, conferir=0,
                   valor_reposto=0, abortou_por=None, ok=True)
        return 0
    log(f"[fila] {len(fila)} pagamento(s) a repor")
    aud.evento_json(etapa="fila_lida", quantos=len(fila), modo=modo_registro,
                    valor_total=round(sum(float(e.get("valor_pago") or 0) for e in fila), 2))

    # ── UMA ESCRITA POR CLIENTE (24/ago/2026) ────────────────────────────────
    # A fila e por BOLETO e continua sendo -- a selecao da tela e por boleto, e e
    # isso que impede repor o que alguem ja repos na mao. O que agrupa aqui e a
    # ESCRITA: os boletos marcados do mesmo CNPJ viram uma soma e uma ida ao
    # Mercos, em vez de N buscas de cliente (a parte cara da rodada).
    # ⚠️ A ordem da fila (pagamento mais antigo primeiro) e preservada pela ordem
    # de aparicao do CNPJ: dict em Python 3.7+ mantem ordem de insercao, e um
    # `sorted` por CNPJ trocaria a prioridade da fila por ordem alfabetica.
    grupos: dict[str, list[dict]] = {}
    for ev in fila:
        grupos.setdefault(re.sub(r"\D", "", ev.get("cnpj") or ""), []).append(ev)
    if len(grupos) != len(fila):
        log(f"[grupo] {len(fila)} boleto(s) em {len(grupos)} cliente(s) -- "
            f"{len(fila) - len(grupos)} escrita(s) a menos que no modo por boleto")
    aud.evento_json(etapa="agrupado", clientes=len(grupos), boletos=len(fila))

    ok = pulados = erros = conferir = 0
    valor_reposto = 0.0
    abortou = None
    # Motivos que vao no e-mail de falha. So erro e "conferir na mao" entram:
    # "nada a repor" e conclusao, nao problema (ver aviso.py).
    detalhes: list[str] = []
    with sync_playwright() as pw:
        nav = pw.chromium.launch(headless=True)
        page = nav.new_page()
        try:
            login_mercos(page, EMPRESAS[0], MERCOS_EMAIL, MERCOS_SENHA, GMAIL_USER, GMAIL_SENHA)

            # ⚠️ LISTA DE TRABALHO, nao `for` sobre o dict: quando a sessao cai o
            # cliente da vez volta pra CABECA da lista e e refeito depois do
            # relogin. Com `for` nao ha como devolver o item, e o unico caminho
            # seria abortar -- que foi o que custou 23 boletos em 25/ago/2026.
            # A ordem da fila (pagamento mais antigo primeiro) e preservada.
            pendentes = list(grupos.items())
            voltas = 0
            while pendentes:
                cnpj_grupo, grupo = pendentes.pop(0)
                try:
                    resultado, usados, descartados = processar_grupo(page, sb, grupo, aplicar, aud)
                except AuditoriaIndisponivel as e:
                    # Sem registro nao se escreve. E provavelmente sistemico
                    # (banco fora, permissao), entao aborta a rodada em vez de
                    # marcar N clientes como erro.
                    abortou = f"auditoria indisponivel: {e}"
                    break
                except SessaoDerrubada as e:
                    # A causa e UMA: nao marcar N pagamentos como falhos.
                    # ⚠️ Antes de desistir, tenta VOLTAR -- e refaz este mesmo
                    # cliente. Seguro porque `SessaoDerrubada` so nasce antes de
                    # qualquer Salvar (ver VOLTAS_POR_RODADA la em cima); o
                    # boleto continua pendente na fila, nada foi carimbado.
                    if voltas < VOLTAS_POR_RODADA and reconquistar_sessao(
                            page, aud, str(e), voltas + 1, VOLTAS_POR_RODADA):
                        voltas += 1
                        pendentes.insert(0, (cnpj_grupo, grupo))
                        continue
                    abortou = str(e)
                    if voltas:
                        abortou += f" (ja tinha voltado {voltas}x nesta rodada)"
                    break
                except PulaSemErro as e:
                    # ⚠️ Os contadores contam BOLETOS, nao clientes: sao as mesmas
                    # colunas da auditoria de antes do agrupamento, e trocar a
                    # unidade faria toda rodada nova parecer menor que as antigas.
                    pulados += len(grupo)
                    log(f"[pula] {cnpj_grupo}: {e}")
                    if aplicar:
                        # Consome de propósito: "nada a repor" e uma CONCLUSAO,
                        # nao uma falha. Sem isso o cliente sem limite volta em
                        # toda rodada, pra sempre.
                        marcar_processados(sb, grupo, f"nada a repor: {e}")
                    continue
                except ConferirNaMao as e:
                    conferir += len(grupo)
                    log(f"[!!] {e}")
                    detalhes.append(f"CONFERIR NA MAO - {cnpj_grupo} "
                                    f"({len(grupo)} boleto(s) na mesma escrita): {e}")
                    # Consome mesmo sem confirmar: repetir somaria o delta
                    # sobre um estado desconhecido. A view
                    # `v_credito_reposicao_conferir` entrega o caso pro humano.
                    # ⚠️ Agrupado, o grupo INTEIRO e consumido -- foi uma escrita
                    # so, e nao ha como saber qual parte dela entrou.
                    marcar_processados(sb, grupo, f"CONFERIR NA MAO: {e}")
                    continue
                except (TelaNaoMapeada, RuntimeError) as e:
                    erros += len(grupo)
                    log(f"[!] {e}")
                    detalhes.append(f"ERRO (fica na fila) - {cnpj_grupo}: {e}")
                    # ⚠️ NAO toca na fila: os pagamentos ficam pendentes pra nova
                    # tentativa e o erro ja esta na auditoria com o motivo
                    # inteiro. Gravar o erro aqui violaria a constraint
                    # "pendente = sem resultado" -- ver marcar_processado.
                    continue

                log(f"[ok] {resultado}")
                if aplicar:
                    marcar_processados(sb, usados, resultado)
                    # Descartado tem motivo PROPRIO (valor zerado, ja processado
                    # por outra rodada): carimbar com o resultado da escrita diria
                    # que ele entrou nela, e nao entrou.
                    for ev_d, motivo_d in descartados:
                        marcar_processado(sb, ev_d, f"nada a repor: {motivo_d}")
                    ok += len(usados)
                    pulados += len(descartados)
                    valor_reposto += sum(float(e.get("valor_pago") or 0) for e in usados)
        finally:
            nav.close()

    aud.fechar(fila_lida=len(fila), repostos=ok, pulados=pulados, erros=erros,
               conferir=conferir, valor_reposto=valor_reposto, abortou_por=abortou,
               ok=(not abortou and not erros and not conferir))

    print()
    print("── AUDITORIA DESTA RODADA ──────────────────────────────────────────")
    print(f"   rodada .......... {aud.rodada_id}")
    print(f"   disparada por ... {aud.quem} ({aud.origem})")
    if aud.run_url:
        print(f"   log da execucao . {aud.run_url}")
    print(f"   modo ............ {modo_registro}")
    print(f"   fila / repostos . {len(fila)} / {ok}")
    print()
    print("   Conferir depois (rodar no Supabase):")
    print(f"     SELECT * FROM v_credito_reposicao_auditoria WHERE rodada_id = '{aud.rodada_id}';")
    print("     SELECT * FROM v_credito_reposicao_violou_teto;   -- tem que voltar 0 linhas")
    print("     SELECT * FROM v_credito_reposicao_conferir;      -- 0 = nada pra olho humano")
    print("────────────────────────────────────────────────────────────────────")

    # ⚠️ Sempre, inclusive numa rodada que deu tudo certo: os emperrados ficaram
    # FORA desta rodada, e uma rodada verde que nao os menciona faz parecer que a
    # fila esta em dia. Ver `resumir_emperrados` (por que nao manda e-mail).
    resumir_emperrados(emperrados)

    # ⚠️ DEPOIS de `aud.fechar()`: o e-mail carrega os numeros finais da rodada.
    # Nunca derruba a rodada e nunca muda o exit code -- ver aviso.py.
    avisar_falha(log=log, aud=aud, modo=modo_registro, abortou=abortou,
                 erros=erros, conferir=conferir, repostos=ok,
                 fila_lida=len(fila), valor_reposto=valor_reposto,
                 detalhes=detalhes)

    print()
    if abortou:
        print(f"⛔ RODADA ABORTADA: {abortou}")
        print(f"   Reposto antes de abortar: {ok}. O resto segue na fila, intacto.")
        print("   O que fazer: rodar fora do horario em que a conta do Mercos e usada por gente, ou")
        print("   criar um usuario Mercos exclusivo do robo (o Mercos aceita 1 sessao por usuario).")
        return 1

    print(f"    {len(fila)} boleto(s) em {len(grupos)} cliente(s) -- uma escrita por cliente")
    print(f"=== fim · {ok} reposto(s) · {pulados} sem nada a repor · {erros} com erro"
          + (f" · {conferir} PARA CONFERIR NA MAO" if conferir else "") + f" · modo: {modo} ===")
    if not aplicar:
        print("    Nada foi escrito no Mercos. Para valer, rode com --aplicar.")
    if conferir:
        print("    ⚠️ Os marcados 'CONFERIR NA MAO' nao voltam na fila de propósito: repetir somaria o valor 2x.")
    return 1 if erros or conferir else 0


if __name__ == "__main__":
    # ⚠️ CRASH ANTES DA FILA TAMBEM E FALHA -- e era o unico caminho que ficava sem
    # aviso: credencial do Mercos recusada, Supabase fora, auditoria que nao abre.
    # Sem este bloco, o robo morria com traceback e o e-mail nunca saia (o
    # `avisar_falha` do fim do `main()` nunca era alcancado).
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        # ⚠️ O QUE JA FOI ESCRITO TEM QUE APARECER NO E-MAIL. Ver `_RODADA_EM_CURSO`.
        frase, escritas, valor = _fechar_rodada_no_crash(_RODADA_EM_CURSO, e)
        avisar_falha(log=log, aud=_RODADA_EM_CURSO, modo="crash", erros=1,
                     repostos=escritas, valor_reposto=valor,
                     abortou=f"o robo morreu antes de terminar: {type(e).__name__}: {e}",
                     detalhes=[frase])
        raise
