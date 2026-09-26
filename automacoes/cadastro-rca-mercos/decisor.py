#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CadastroRCA-Mercos -- Fase 2+3+4: DECISOR (nao escreve nada no Mercos ainda).

Le a planilha de cadastro (Google Sheets), cruza cada CNPJ com o middleware
Protheus<->Mercos via Metabase (ES+RJ) pra confirmar que o cadastro ja
propagou, normaliza "Prazo do Cliente" / "Tabela de desconto do cliente" pro
catalogo real do Mercos, resolve o vendedor (Nome do representante -> usuario
Mercos), e grava o resultado no ledger `cadastro_rca_mercos_processado`
(Supabase) -- so leitura/decisao, nenhuma chamada ao Mercos acontece aqui.

Regra de ouro: nunca adivinhar. Sem match exato/confianca -> status de
alerta no ledger, nunca aplica um chute.

Config via variavel de ambiente (GitHub Actions secrets) -- ver
torre_de_performance_b2b/CadastroRCA-Mercos/arquitetura-producao.md:
  METABASE_URL, METABASE_API_KEY
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  CADASTRO_RCA_SHEETS_SERVICE_ACCOUNT_JSON (conteudo do JSON da service account)

Uso:
  python decisor.py            # roda e grava no ledger
  python decisor.py --dry-run  # so imprime, nao grava no ledger
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import os
import re
import csv
import json
import argparse
import unicodedata
import urllib.request
import urllib.error

import gspread
from google.oauth2.service_account import Credentials
from supabase import create_client

METABASE_URL = os.environ["METABASE_URL"]
METABASE_API_KEY = os.environ["METABASE_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
SHEETS_SERVICE_ACCOUNT_JSON = os.environ["CADASTRO_RCA_SHEETS_SERVICE_ACCOUNT_JSON"]
SHEETS_CADASTRO_ID = "1bX4GmpKoOITG6l1y-BeW9X8l3LZ6Xcm8COcaHk9Ys-I"  # planilha "Cadastro de Clientes RCA", nao e secreto

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USUARIOS_MERCOS_CSV = os.path.join(BASE_DIR, "usuarios_mercos.csv")

# ── Catalogo real do Mercos (capturado ao vivo 22/jul/2026) ────────────────

CONDICOES_PAGAMENTO = {
    "009": "BOLETO 20", "010": "BOLETO A VISTA", "011": "BONIFICAÇÃO",
    "017": "BOLETO 15/30/45", "019": "BOLETO 30/60/90", "056": "BOLETO 30",
    "057": "BOLETO 30/45", "058": "BOLETO 30/45/60", "059": "BOLETO 30/45/60/75",
    "060": "CRÉDITO 1X", "061": "CRÉDITO 2X", "062": "CRÉDITO 3X",
    "063": "CRÉDITO 4X", "064": "CRÉDITO 5X", "066": "CRÉDITO 6X", "067": "PIX",
    "068": "BOLETO 30/60/90/120", "069": "BOLETO 30/60", "071": "BOLETO 40/50/60",
    "072": "BOLETO 60/75/90", "073": "BOLETO 45/60/75/90", "074": "BOLETO 45/60",
    "075": "BOLETO 40/50/60/70", "076": "BOLETO 60/90", "077": "BOLETO 30/40/50",
    "078": "BOLETO 90", "079": "BOLETO 30/40/50/60", "080": "BOLETO 45/60/90",
    "081": "BOLETO 105", "083": "BOLETO 45/60/75", "084": "BOLETO 30/45/55/65",
    "086": "BOLETO 40/50", "087": "BOLETO 45", "089": "BOLETO 60/75/90/105",
    "090": "BOLETO 45/60/75/90/105/120/135/150", "091": "120/125/130/135",
    "092": "BOLETO 60", "093": "BOLETO 90/120", "094": "BOLETO 90/105/120",
    "095": "BOLETO 60/90/120", "096": "BOLETO 75", "005": "BOLETO 15",
    # ⚠️ Faltavam no catalogo capturado em 22/jul (achado 10/set/2026, conferido
    # contra a lista ao vivo do Mercos). Enquanto nao estavam aqui, TODO prazo
    # que casaria com uma delas derrubava a linha inteira pra
    # alerta_normalizacao_pagamento -- o valor existia no Mercos e o robo dizia
    # que nao existia. Catalogo incompleto se disfarca de "prazo invalido".
    "097": "BOLETO 45/55/65/75", "098": "BOLETO 120",
    "099": "BOLETO 45/55/65/75/85", "100": "BOLETO 45/55/65/75/85/95",
    "101": "BOLETO 60/75/90/105/120",
    "102": "BOLETO 50/60/70/80/90/100/120/130",
    "103": "BOLETO 14/21/28/35/42/49",
}
_BOLETO_POR_NUMEROS = {}
for _cod, _label in CONDICOES_PAGAMENTO.items():
    # "BOLETO " e OPCIONAL: o codigo 091 e o unico do catalogo cujo rotulo e so
    # o grupo de dias ("120/125/130/135"), sem o prefixo. Com o regex exigindo
    # "BOLETO ", ele ficava inalcancavel por prazo -- um texto "120/125/130/135"
    # na planilha caia em alerta apontando pra um valor que esta la na tela.
    _m = re.fullmatch(r"(?:BOLETO )?([\d/]+)", _label)
    if _m:
        _BOLETO_POR_NUMEROS[_m.group(1)] = f"{_cod} - {_label}"

_PERCENTUAIS_VALIDOS = {5, 10, 15, 20, 25, 27, 30, 35, 40, 42, 45, 50}

ALIASES_REPRESENTANTE = {
    "CEÇA TORRES": "Maria Conceição",
    "PEDRO - PIAB REPRESENTAÇÃO COMERCIAL": "Pedro Igor Alves Bernardes",
    "PEDRO IGOR - PIAB REPRESENTAÇÃO COMERCIAL": "Pedro Igor Alves Bernardes",
    "PEDRO IGOR - PIAB": "Pedro Igor Alves Bernardes",
    "PEDRO IGOR PIAB": "Pedro Igor Alves Bernardes",
    "JOSÉ TORRES": "José Robério da Silva Torres",
    "JOSE TORRES": "José Robério da Silva Torres",
    "MATHEUS TORRES POTENCIALIZ": "Matheus Torres Potencializ",
    "LUIZ GUSTAVO SANTANA SEVERO": "Matheus Torres Potencializ",
    "MATHEUS BATISTA": "Matheus Batista Bento",
    "MATHEUS BATISTA BENTO": "Matheus Batista Bento",
    "RODRIGO COSTA": "Rodrigo de Oliveira Costa",
    "MARCELO CHRISPIM": "Marcelo Chrispim Nunes",
    "ROSEVALDO": "Rosevaldo Santana Santos",
    "ROSEVALDO SANTANA": "Rosevaldo Santana Santos",
    # Erro de digitacao na planilha (14/set/2026, chamado 142067 TRX TARPON):
    # "ROSAVELDO" com A no lugar do E. Confirmado pelo Italo que e a mesma pessoa.
    # Nenhum fallback (prefixo/subsequencia) alcanca isso -- a troca e no MEIO da
    # palavra, entao so alias resolve.
    "ROSAVELDO SANTANA SANTOS": "Rosevaldo Santana Santos",
    "WANTUIR SILVEIRA": "Wantuir Silva Silveira",
    "PAULO TELES": "Paulo César Teles da Costa",
    "ROMEU": "Alberto Romeu Ferreira",
    "LUIS GUSTAVO SANTANA SEVERO": "Matheus Torres Potencializ",
    "LUIZ GUSTAVO SANTAN SEVERO": "Matheus Torres Potencializ",
    "LUIZ GUSTAVO SANTANA SEVRO": "Matheus Torres Potencializ",
    "RAFAELA GONÇALVES - SM REPRESENTAÇÕES": "Rafaela Gonçalves Costa",
    # 22/set/2026 -- decisao do usuario: "esse da BL e da rafaela goncalves".
    # A planilha as vezes escreve a DUPLA ("Rafaela/Socorro"), que e o nome
    # comercial da SM Representacoes; a pessoa cadastrada no Mercos e uma so.
    "RAFAELA/SOCORRO SM REPRESENTAÇÕES": "Rafaela Gonçalves Costa",
    "RAFAELA/SOCORRO - SM REP": "Rafaela Gonçalves Costa",
    "RAFAELA/SOCORRO SM REP": "Rafaela Gonçalves Costa",
    "RAFAELA/SOCORRO": "Rafaela Gonçalves Costa",
    # 22/set/2026 -- "DJA" e a abreviacao do Djanilson (chamado 143248, Vonny).
    # O 143247, MESMO cliente e MESMO dia, veio escrito "djanilson" e resolveu.
    "DJA": "Djanilson Amancio da Silva",
    # Curados 24/jul (pedido do usuário, lote de apelidos/variações reais da planilha)
    "VAL LEAL": "Aurenivaldo Santos Leal",
    "JENIFFER BRITTO": "Jeniffer Barboza de Britto",
    "JOSÉ TORRES": "José Robério da Silva Torres",
    "DJANILSON": "Djanilson Amancio da Silva",
    "FERNANDA GOULART": "Fernanda Somavilla",
    # Fernanda Vidal (11/set/2026) -- co-titular da rede Bel Cosmeticos com o Pedro Igor,
    # cadastrada no Mercos ES e RJ. No Mercos e no CSV o nome e so "Fernanda Vidal"; a
    # planilha tende a colar a razao social junto (mesmo caso do Luciano Bicalho, 01/set).
    # ⚠️ Nao criar alias so "FERNANDA" -- colide com a Fernanda Somavilla.
    "FERNANDA VIDAL CONSULTORIA LTDA": "Fernanda Vidal",
    "FERNANDA VIDAL CONSULTORIA": "Fernanda Vidal",
    "FERNANDA VIDAL - FERNANDA VIDAL CONSULTORIA LTDA": "Fernanda Vidal",
    "MATHEUS": "Matheus Batista Bento",
    "PEDRO - PIAB": "Pedro Igor Alves Bernardes",
    "LUIZ SILVA": "Luiz Claudio Romeiro da Silva",
    "VALTENCIR SANTOS": "Valtencir Santos Araújo",
    "LUIZ H LUCHEZI": "Luiz Henrique Luchezi",
    "RAFAELA GONÇALVES - SM REP": "Rafaela Gonçalves Costa",
    "RAFAELA GONÇALVES DOS SANTOS": "Rafaela Gonçalves Costa",
    "WILZA TEIXEIRA": "Wilza Argolo Teixeira",
    "RAQUEL CALLOVI": "Raquel de Brito Callovi do Amaral",
    "MARCELO CHRISPIMM2-TESTE": "Marcelo Chrispim Nunes",
    # 18/ago -- planilha some com o nome do meio ("Bruno Rodrigues Girao")
    "BRUNO GIRAO": "Bruno Rodrigues Girao",
    # 18/ago -- erro de digitacao real na planilha (falta o "L")
    "RAFAELA GONCAVES COSTA": "Rafaela Goncalves Costa",
    # 01/set -- representante novo (NEGA REPRESENTACOES, ES). A planilha cola a
    # razao social do escritorio no nome da pessoa, como ja faz com PIAB/SM --
    # nenhum dos dois fallbacks (prefixo/subsequencia) alcanca isso, entao o
    # de-para e obrigatorio.
    # ⚠️ O alvo e "Luciano Bicalho" -- nome EXATO do cadastro no Mercos, conferido
    # ao vivo em 01/set. Apostei em "Luciano Pitangueira Bicalho" (como vem na
    # planilha) e o escritor falhou: ele acha o card por `has-text`, que e
    # substring, entao nome mais CURTO que o do Mercos casa e mais LONGO nao.
    "LUCIANO PITANGUEIRA BICALHO - NEGA REPRESENTACOES E CONSULTORIA LTDA": "Luciano Bicalho",
    "LUCIANO PITANGUEIRA BICALHO - NEGA REPRESENTACOES": "Luciano Bicalho",
    "LUCIANO PITANGUEIRA BICALHO": "Luciano Bicalho",
    "LUCIANO BICALHO - NEGA REPRESENTACOES": "Luciano Bicalho",
    "NEGA REPRESENTACOES E CONSULTORIA LTDA": "Luciano Bicalho",
}


def _fold(s: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", sem_acento).strip().upper()


ALIASES_REPRESENTANTE_FOLDED = {_fold(k): v for k, v in ALIASES_REPRESENTANTE.items()}


def normalizar_cnpj(valor) -> str | None:
    digitos = re.sub(r"\D", "", str(valor) if valor is not None else "")
    if not digitos:
        return None
    digitos = digitos.zfill(14)
    if len(digitos) != 14:
        return None
    return digitos


# ── Normalizacao multi-valor (18/ago/2026) ────────────────────────────────
# Pedido do usuario: um cliente PODE ter mais de uma tabela de preco e mais de
# uma condicao de pagamento liberada. Casos reais que motivaram:
#   "20% kokeshi / 27% barbours"   -> Tabela 20% + Tabela 27%
#   "normal 30d (introducao 45d)"  -> BOLETO 30 + BOLETO 45 + BOLETO 30/45
#   "30/45/60 ou 30/45/60/75"      -> BOLETO 30/45/60 + BOLETO 30/45/60/75
# O ledger guarda a LISTA na mesma coluna, separada por SEPARADOR_MULTI; o
# escritor marca um checkbox por valor e clica "Adicionar" uma vez.
#
# O que MUDOU junto, e por que: o matching por aproximacao ("distancia de
# prazos") foi REMOVIDO. Contrariava a regra de ouro do projeto ("sem match
# exato, alerta -- nunca aproxima") e aplicava valor errado em silencio.
# Medido no ledger em 18/ago/2026: "21 DIAS 3% DESCONTO FINANCEIRO" (12 linhas)
# virava "081 - BOLETO 105"; "40.5" virava BOLETO 105 e FOI aplicado no Mercos;
# "30/45 ou 30/45/60 ou 30 dd" virava "068 - BOLETO 30/60/90/120", aplicado.
# Agora cada opcao exige match EXATO no catalogo -- numero que nao casa derruba
# a linha inteira pra alerta, com o texto cru visivel na Torre pra decisao
# humana (que e o unico lugar onde "21 dias" pode ser resolvido).
SEPARADOR_MULTI = " | "

# De-para CURADO de texto livre da planilha -> valor(es) do catalogo Mercos,
# para o que nenhuma regra pode deduzir sem decisao de negocio. Chave = texto
# normalizado por _fold (sem acento, maiusculo, espacos colapsados); valor =
# lista de rotulos exatos do catalogo.
#
# Existe porque a planilha tem prazos que NAO EXISTEM no Mercos ("21 dias",
# "35 dias", "42 dias") e "aproximar pro mais perto" foi justamente o que
# aplicava valor errado em silencio. Enquanto ninguem decidir o de-para, essas
# linhas ficam em alerta na Torre -- que e o comportamento certo (alguem tem que
# escolher entre criar a condicao no Mercos ou usar outra).
ALIASES_CONDICAO_PAGAMENTO: dict[str, list[str]] = {
    # 24/set/2026 -- ANGELA MARIA SOMMER 15.679.965/0001-20 (chamado 143034,
    # Luiz Henrique Luchezi): planilha diz "28/35/42 DIAS", escalonamento que o
    # catalogo do Mercos nao tem em prazo nenhum. Decisao do usuario: liberar as
    # TRES condicoes abaixo e deixar quem vende escolher no pedido -- mesmo
    # racional das tabelas do "22%" e do "43%".
    # ⚠️ Medido antes de gravar: "28/35/42 DIAS" e a chave de UM cliente so na
    # planilha inteira (247 acima do corte). O outro que cita 28/35/42 escreve
    # "14/21/28/35/42/49", texto diferente, entao nao e alcancado por esta chave.
    "28/35/42 DIAS": [
        "077 - BOLETO 30/40/50",
        "057 - BOLETO 30/45",
        "017 - BOLETO 15/30/45",
    ],
    # 10/set/2026 -- decisoes do usuario sobre prazos que NAO existem no
    # catalogo do Mercos. Chave = texto da planilha ja passado por _fold.
    #
    # CIRO BARBOSA (5 CNPJs, chamados 141679-141683): "BOLETO A VISTA 5 DIAS".
    # O menor prazo do Mercos e 15 dias. Instrucao literal: "deixe como boleto
    # 15, que e o mais proximo" -- SO o BOLETO 15, sem o "010 - BOLETO A VISTA"
    # que a palavra "a vista" acionaria, porque aqui "a vista 5 dias" e UM
    # prazo curto, nao duas condicoes.
    "BOLETO A VISTA 5 DIAS": ["005 - BOLETO 15"],
    # MHEL ATACADO (chamado 141441): "30/45/60/75/90 ou 30/60/90 ou 30/45/60".
    # As duas ultimas existem exatas (019 e 058); a primeira nao -- o catalogo
    # nao tem NENHUM parcelamento de 5 vezes comecando em 30, e o mais proximo
    # e o mesmo escalonamento sem a ultima parcela (059 - BOLETO 30/45/60/75).
    # Instrucao do usuario: "verifique as opcoes dentro do mercos e aplique as
    # mais proximas".
    "30/45/60/75/90 OU 30/60/90 OU 30/45/60": [
        "019 - BOLETO 30/60/90",
        "058 - BOLETO 30/45/60",
        "059 - BOLETO 30/45/60/75",
    ],
    # ── 11/set/2026 -- decisoes do usuario sobre a fila que destravou quando o
    #    portao ganhou a 2a fonte (`mercos_cliente_confirmado`). ────────────────
    #
    # COPACABANA COSMETICOS (11 filiais, chamados 141917-141927): "40/70". O
    # catalogo nao tem nenhuma condicao 40/70 nem nada comecando em 40 com 2
    # parcelas. Instrucao literal: "esses que estao com condicao de pagamento
    # 40/70 -- transformar em 074 - BOLETO 45/60".
    "40/70": ["074 - BOLETO 45/60"],
    # ── 21/set/2026 -- decisao do usuario sobre os dois H N Y que sobraram. ──────
    #
    # H N Y filiais 0006 e 0002 (chamados 142874 e 142881, rep ROMEU): "30/45/60/70".
    # O catalogo NAO tem essa escada -- tem a irma de um digito de distancia,
    # "059 - BOLETO 30/45/60/75", que e exatamente o que a filial 0001 (chamado 142884,
    # MESMO cliente, mesma planilha, mesmo dia) pediu e recebeu. Instrucao literal:
    # "vamos por esse ai tambem que faltou".
    #
    # ⚠️ A diferenca e 5 dias SO na ultima parcela (70 -> 75) e favorece o cliente; as
    # tres primeiras (30/45/60) sao identicas. Nao ha nada entre as duas no catalogo.
    "30/45/60/70": ["059 - BOLETO 30/45/60/75"],
    # 22/set/2026 -- decisao do usuario para MARCIA COSMETICOS (chamado 142953,
    # escrito "35.45.55" com ponto) e TAIS SALES (142959, "35/45/55"): aplicar
    # "058 - BOLETO 30/45/60". A escada pedida nao existe no catalogo e nao tem
    # vizinha de um digito -- 058 cobra 5 dias ANTES em cada parcela, e isso e
    # escolha comercial dele, nao aproximacao nossa.
    "35/45/55": ["058 - BOLETO 30/45/60"],
    "35.45.55": ["058 - BOLETO 30/45/60"],
    # RAV SHEFA / LOJA DA VIVI / EME COSMETICOS (chamados 142095, 142096, 142092 --
    # todos do Luiz Luchezi): "30/40/50/60/70 DIAS". O catalogo nao tem esta escada.
    # QUATRO opcoes gravadas, para o vendedor escolher no pedido (decisao do Italo,
    # 14/set/2026 -- ele mandou somar 075, 097 e 099 a que eu tinha proposto):
    #   079 (30/40/50/60)     -- bate EXATO nas 4 primeiras, so falta a 5a
    #   075 (40/50/60/70)     -- bate nas 4 ULTIMAS, so falta a 1a
    #   097 (45/55/65/75)     -- mesma quantidade de passos, escada deslocada
    #   099 (45/55/65/75/85)  -- unica de 5 parcelas viavel, mas comeca 15 dias depois
    # ⚠️ 075 nao aparecia na minha 1a busca: procurei "5 parcelas" e "4 comecando em
    # 30", e ele e 4 comecando em 40. Catalogo completo veio do proprio Mercos.
    "30/40/50/60/70 DIAS": [
        "075 - BOLETO 40/50/60/70",
        "079 - BOLETO 30/40/50/60",
        "097 - BOLETO 45/55/65/75",
        "099 - BOLETO 45/55/65/75/85",
    ],
    # DOTNEW 39.437.300/0001-78 (chamado 28872, Luiz Claudio): "7 DIAS". O menor prazo
    # de boleto do catalogo e 15 dias. Decisao do Italo (14/set/2026): gravar as tres
    # pontas curtas -- 005 (15), 009 (20) e 010 (a vista) -- e deixar quem vende escolher.
    "7 DIAS": ["005 - BOLETO 15", "009 - BOLETO 20", "010 - BOLETO A VISTA"],
    # KOSBEL 50.035.166/0001-11 (chamado 26938, Valtencir Santos): "PRAZO 50/60/70/80".
    # Nao existe essa escada. Decisao do Italo: 073 e 075, as duas de 4 parcelas que
    # cercam o pedido (075 comeca antes, 073 termina depois).
    "PRAZO 50/60/70/80": ["073 - BOLETO 45/60/75/90", "075 - BOLETO 40/50/60/70"],
    # SD SCARPANTE (chamado 142024, Jeniffer Britto): "15/30". O catalogo nao tem
    # NENHUMA condicao de 2 parcelas comecando em 15 -- as vizinhas erram por lados
    # opostos: 005 encurta (perde a 2a parcela), 017 mantem as duas e ACRESCENTA uma
    # 3a em 45, 057 mantem duas mas alonga ambas. Decisao do Italo (14/set/2026):
    # gravar as TRES e deixar quem vende escolher no pedido, em vez de a automacao
    # decidir por ele. Mesmo racional do MHEL ATACADO acima.
    "15/30": [
        "005 - BOLETO 15",
        "017 - BOLETO 15/30/45",
        "057 - BOLETO 30/45",
    ],
    # ROBERTO DA SILVA PEREIRA (chamado 141786): "30/45/60/75 E 30/45/60/75/90".
    # A 1a existe exata (059); a 2a nao (o catalogo nao tem 5 parcelas comecando
    # em 30). O usuario mandou gravar TRES condicoes, nao duas -- 073 e 099 sao
    # escolha dele, nao inferencia nossa.
    "30/45/60/75 E 30/45/60/75/90": [
        "059 - BOLETO 30/45/60/75",
        "073 - BOLETO 45/60/75/90",
        "099 - BOLETO 45/55/65/75/85",
    ],
}

# ⚠️ Estes aliases sao GLOBAIS por texto da planilha, como os de condicao acima:
# valem para QUALQUER cliente que escreva o mesmo percentual, nao so para o que
# originou a decisao. Foi assim que o arquivo sempre funcionou -- mas a decisao
# aqui e comercial (percentual e desconto), entao cada entrada nomeia quem pediu.
ALIASES_TABELA_PRECO: dict[str, list[str]] = {
    # 24/set/2026 -- ANGELA MARIA SOMMER (chamado 143034): planilha diz "13%",
    # que nao existe (validos vizinhos: 10% e 15%). Decisao do usuario: vincular
    # AS DUAS, como ja foi feito no "22%", "18%" e "43%".
    # ⚠️ Medido: "13%" aparece em UM cliente so na planilha inteira.
    "13%": ["Tabela 10%", "Tabela 15%"],
    # BC PIEDADE 02.125.266/0003-58 (chamado 141942, Fernanda Vidal): planilha diz
    # "46%", que nao existe -- os validos vao de 45% para 50%. Instrucao: "bote
    # tabela de preco de 45%, a mais proxima que tiver".
    "46%": ["Tabela 45%"],
    # D C DOS SANTOS FERNANDES 61.176.934/0001-73 (chamado 141853, Bruno Girao):
    # planilha diz "7%", que nao existe (validos: 5% e 10%). Instrucao: "coloque
    # tabela de 10%". ⚠️ E o percentual MAIOR dos dois vizinhos -- desconto maior
    # que o pedido na planilha, escolha explicita do usuario, nao arredondamento.
    "7%": ["Tabela 10%"],
    # C A GONCALVES JUNIOR 12.146.219/0001-82 (chamado 142118, Luiz Luchezi): "22%".
    # NB CALDONHO 36.825.363/0001-03 (chamado 142151, Luiz Luchezi): "18%".
    # Decisao do Italo (14/set/2026): em vez de escolher UM vizinho, gravar OS DOIS
    # e deixar quem vende escolher no pedido -- mesmo racional do "15/30" do SD
    # SCARPANTE. Evita que a automacao decida desconto sozinha quando o pedido da
    # planilha cai exatamente entre duas tabelas.
    "22%": ["Tabela 20%", "Tabela 25%"],
    # 22/set/2026 -- CDA COMERCIO (chamado 143253) pediu 43%, que nao existe.
    # Decisao do usuario: "vamos adicionar a tabela de 40% e 45%, vamos adicionar
    # as 2 tabelas para ele" -- o cliente fica com AS DUAS vinculadas, nao com a
    # mais proxima. Mesmo formato de lista do "22%" acima.
    "43%": ["Tabela 40%", "Tabela 45%"],
    "18%": ["Tabela 15%", "Tabela 20%"],
    # "NORMAL" na coluna de tabela nao e percentual nenhum -- e o jeito de dizer
    # "sem desconto, a tabela padrao do sistema". Decisao do Italo (14/set/2026):
    # "tabela normal, tabela zero, sem tabela" => `Preço de Tabela`, que e o mesmo
    # destino que "SEM DESCONTO" e "0%" ja tinham no normalizador.
    # ⚠️ Global por texto: alcanca tambem ANGATU, MARES & TRINDADE, SILVINO CAUSTA e
    # LIDER COSMETICOS (todos do Paulo Teles), que escrevem "NORMAL" do mesmo jeito.
    "NORMAL": ["Preço de Tabela"],
    # 22/set/2026 -- regra do usuario: "quando tem tabela padrao ou tabela zero,
    # e o preco de tabela". "0%"/"SEM DESCONTO" ja caiam nesse destino pelo
    # normalizador; faltavam as grafias de "padrao" (com e sem acento).
    "PADRÃO": ["Preço de Tabela"],
    "PADRAO": ["Preço de Tabela"],
}

# Separadores de ALTERNATIVA (uma opcao OU outra). Nao incluem "/" nem "." --
# esses ficam DENTRO do grupo, sao o parcelamento ("30/45" = 1 condicao de 2
# parcelas, nao duas condicoes). "-" entra como alternativa porque nesta
# planilha ele separa opcoes completas ("40/50-40/50/60-PIX").
_ALT_SEPARADORES_CONDICAO = re.compile(r"\bOU\b|\bE\b|[;,|+\-\n]")
# Na tabela de preco a virgula e DECIMAL ("18,3%"), entao nao separa nada.
_ALT_SEPARADORES_TABELA = re.compile(r"\bOU\b|\bE\b|[;/|+\-\n]")
_RE_GRUPO_DIAS = re.compile(r"\d+(?:\s*[./]\s*\d+)*")
_RE_PERCENTUAL = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_RE_CREDITO_PARCELAS = re.compile(r"\b(?:CREDITO|CARTAO)\s*(\d)\s*X?\b")

_CONDICAO_POR_CODIGO = {cod: f"{cod} - {label}" for cod, label in CONDICOES_PAGAMENTO.items()}
_ORDEM_CONDICAO = {v: k for k, v in _CONDICAO_POR_CODIGO.items()}


def _condicoes_por_palavra_chave(segmento: str) -> list[str]:
    """Condicao que nao e prazo em dias -- vem de palavra, nao de numero."""
    achados = []
    if re.search(r"\bA\s*VISTA\b", segmento):
        achados.append(_CONDICAO_POR_CODIGO["010"])
    if "PIX" in segmento:
        achados.append(_CONDICAO_POR_CODIGO["067"])
    if "BONIFICA" in segmento:
        achados.append(_CONDICAO_POR_CODIGO["011"])
    m = _RE_CREDITO_PARCELAS.search(segmento)
    if m:
        procurado = f"CREDITO {m.group(1)}X"
        for cod, label in CONDICOES_PAGAMENTO.items():
            if _fold(label) == procurado:
                achados.append(_CONDICAO_POR_CODIGO[cod])
                break
    return achados


def _condicao_por_dias(dias: list[int]) -> str | None:
    return _BOLETO_POR_NUMEROS.get("/".join(str(d) for d in dias))


# -- Substituicao NOMINAL de prazo que nao existe no Mercos (01/set/2026) --
#
# Decisao explicita do usuario, caso real: chamado 32410 (HELLO BIJOUX SP,
# "pix, 7/30/45") caiu em alerta_normalizacao_pagamento porque BOLETO 7/30/45
# nao existe no catalogo -- o menor prazo do Mercos e 15 dias. Regra dele:
# "quando nao encontrar boleto 7 dias, deixe a modalidade de 15 dias, que e a
# mais proxima" -- e marcar TAMBEM o boleto avulso desse prazo
# ("017 - BOLETO 15/30/45" + "005 - BOLETO 15").
#
# ATENCAO: isto NAO reabre o matching por aproximacao removido em 18/ago (o que
# aplicava "21 dias" -> BOLETO 105 em silencio). A diferenca e o que manda: la a
# regra era "pegue o mais perto de QUALQUER numero" -- aqui e uma tabela
# NOMINAL, fechada, de um dia so, escrita por decisao humana. Numero que nao
# esta nesta tabela continua derrubando a linha inteira pra alerta. Crescer esta
# tabela e decisao de negocio, nunca inferencia do codigo.
# 10/set/2026: 5 entrou pelo mesmo motivo do 7 -- decisao do usuario no
# caso CIRO BARBOSA ("boleto 15, que e o mais proximo"). O menor prazo
# que existe no Mercos e 15 dias.
_DIAS_SUBSTITUIDOS = {5: 15, 7: 15}


def _condicoes_por_dias_substituidos(dias: list[int]) -> list[str]:
    """Condicoes quando o grupo exato nao existe mas o dia fora do catalogo tem
    substituto nominal em `_DIAS_SUBSTITUIDOS`. Devolve o parcelado ja
    substituido + o boleto avulso de cada dia trocado. Lista vazia = nao ha
    substituto -> o chamador derruba pra alerta, como sempre."""
    if not any(d in _DIAS_SUBSTITUIDOS for d in dias):
        return []
    trocados = [_DIAS_SUBSTITUIDOS.get(d, d) for d in dias]
    label = _condicao_por_dias(trocados)
    if label is None:
        return []          # substituir nao resolveu -> alerta (nunca chuta)
    achados = [label]
    for original, novo in zip(dias, trocados):
        if original == novo:
            continue
        avulso = _condicao_por_dias([novo])
        if avulso:
            achados.append(avulso)
    return achados


def _dedup(valores: list[str]) -> list[str]:
    vistos, unicos = set(), []
    for v in valores:
        if v not in vistos:
            vistos.add(v)
            unicos.append(v)
    return unicos


def normalizar_condicoes_pagamento(texto) -> list[str]:
    """TODAS as condicoes de pagamento reconhecidas no texto livre da planilha.
    Lista vazia = nada reconhecido com certeza -> alerta (nunca chuta)."""
    if not texto:
        return []
    t = _fold(str(texto))
    if t in ALIASES_CONDICAO_PAGAMENTO:
        return list(ALIASES_CONDICAO_PAGAMENTO[t])
    t = _RE_PERCENTUAL.sub(" ", t)   # "3%" e desconto financeiro, nao prazo
    t = re.sub(r"\bDIAS?\b|\bDD\b|\bPRAZO\b", " ", t)

    achados: list[str] = []
    grupos: list[list[int]] = []
    for segmento in _ALT_SEPARADORES_CONDICAO.split(t):
        seg = segmento.strip()
        if not seg:
            continue
        achados.extend(_condicoes_por_palavra_chave(seg))
        for bruto in _RE_GRUPO_DIAS.findall(seg):
            dias = [int(n) for n in re.findall(r"\d+", bruto)]
            if not dias:
                continue
            label = _condicao_por_dias(dias)
            if label is None:
                substitutos = _condicoes_por_dias_substituidos(dias)
                if not substitutos:
                    return []   # numero fora do catalogo ("21 dias", "45/55/65") -> alerta
                achados.extend(substitutos)
                grupos.append([_DIAS_SUBSTITUIDOS.get(d, d) for d in dias])
                continue
            achados.append(label)
            grupos.append(dias)

    # "normal 30d (introducao 45d)": alem de BOLETO 30 e BOLETO 45, o usuario
    # quer tambem o parcelado que combina as duas datas (BOLETO 30/45), quando
    # existe no catalogo.
    if len(grupos) > 1:
        combinado = sorted({d for g in grupos for d in g})
        label = _condicao_por_dias(combinado)
        if label:
            achados.append(label)

    return sorted(_dedup(achados), key=lambda a: _ORDEM_CONDICAO.get(a, "999"))


def normalizar_tabelas_preco(texto) -> list[str]:
    """TODAS as tabelas de preco reconhecidas. Percentual sem match no catalogo
    junto da palavra "financeiro" e ignorado de proposito (3% de desconto
    financeiro nao e tabela de preco -- caso real "30%+3% financeiro", 17
    clientes ja processados assim). Qualquer outro percentual sem match derruba
    a linha pra alerta."""
    if not texto:
        return []
    t = _fold(str(texto))
    if t in ALIASES_TABELA_PRECO:
        return list(ALIASES_TABELA_PRECO[t])
    achados: list[str] = []
    for segmento in _ALT_SEPARADORES_TABELA.split(t):
        seg = segmento.strip()
        if not seg:
            continue
        if "SEM DESCONTO" in seg:
            achados.append("Preço de Tabela")
            continue
        for bruto in _RE_PERCENTUAL.findall(seg):
            if not bruto.isdigit():
                return []   # "18,3%" -- nao existe tabela fracionada
            pct = int(bruto)
            if pct == 0:
                achados.append("Preço de Tabela")
            elif pct in _PERCENTUAIS_VALIDOS:
                achados.append(f"Tabela {pct}%")
            elif pct % 100 == 0 and (pct // 100) in _PERCENTUAIS_VALIDOS:
                achados.append(f"Tabela {pct // 100}%")   # celula % no Sheets: 10 -> "1000%"
            elif "FINANCEIR" in seg:
                continue
            else:
                return []
    return _dedup(achados)


def normalizar_condicao_pagamento(texto) -> str | None:
    """Valor pronto pro ledger: 1 coluna, N opcoes separadas por SEPARADOR_MULTI."""
    valores = normalizar_condicoes_pagamento(texto)
    return SEPARADOR_MULTI.join(valores) if valores else None


def normalizar_tabela_preco(texto) -> str | None:
    valores = normalizar_tabelas_preco(texto)
    return SEPARADOR_MULTI.join(valores) if valores else None


BRANCH_PARA_EMPRESA_MERCOS = {"1301": "424525", "1303": "424524"}
EMPRESA_MERCOS_PARA_REGIAO = {"424525": "ES", "424524": "RJ"}


def carregar_usuarios_mercos() -> dict:
    exato, folded = {}, {}
    with open(USUARIOS_MERCOS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nome = row["nome"].strip()
            regiao = row["regiao"].strip().upper()
            exato.setdefault((nome.upper(), regiao), row)
            folded.setdefault((_fold(nome), regiao), row)
    return {"exato": exato, "folded": folded}


def _resolver_por_prefixo_unico(nome_representante: str, usuarios: dict, regiao: str) -> dict | None:
    """Fallback de segurança (pedido do usuário 24/jul): se o nome da
    planilha for só o primeiro nome (ou primeiras palavras) e isso bater
    com EXATAMENTE 1 pessoa cadastrada nessa região, resolve -- ex.
    "Fernanda" sozinha resolve pra "Fernanda Somavilla" porque só existe
    1 Fernanda em toda a região. Se batesse em 2+ pessoas, nunca chuta:
    devolve None (mesma regra de sempre -- sem match seguro, vira
    alerta_representante_nao_mapeado, não aplica um chute)."""
    query_fold = _fold(nome_representante)
    if not query_fold:
        return None
    candidatos = []
    vistos: set[str] = set()
    for (nome_fold, reg), row in usuarios["folded"].items():
        if reg != regiao:
            continue
        if nome_fold != query_fold and not nome_fold.startswith(query_fold + " "):
            continue
        chave_pessoa = (row.get("email") or "").strip().lower()
        if chave_pessoa in vistos:
            continue
        vistos.add(chave_pessoa)
        candidatos.append(row)
    if len(candidatos) == 1:
        return candidatos[0]
    return None


def _resolver_por_subsequencia_unica(nome_representante: str, usuarios: dict, regiao: str) -> dict | None:
    """Fallback (18/ago/2026): a planilha costuma cortar o nome do MEIO --
    "Bruno Girao" para "Bruno Rodrigues Girao", "Luiz Silva" para "Luiz Claudio
    Romeiro da Silva". Resolve quando TODAS as palavras do nome da planilha
    aparecem, NA MESMA ORDEM, no nome cadastrado, o primeiro nome bate exato, e
    isso acontece com EXATAMENTE 1 pessoa da regiao. 2+ candidatos ou 0 nunca
    chuta -- cai em alerta_representante_nao_mapeado como antes.

    Diferente de `_resolver_por_prefixo_unico`, que so cobre nome truncado no
    FIM ("Fernanda" -> "Fernanda Somavilla")."""
    tokens = _fold(nome_representante).split()
    if len(tokens) < 2:
        return None   # nome de 1 palavra e caso do prefixo, nao deste
    candidatos = []
    vistos: set[str] = set()
    for (nome_fold, reg), row in usuarios["folded"].items():
        if reg != regiao:
            continue
        cadastrado = nome_fold.split()
        if not cadastrado or cadastrado[0] != tokens[0]:
            continue
        i = 0
        for palavra in cadastrado:
            if i < len(tokens) and palavra == tokens[i]:
                i += 1
        if i != len(tokens):
            continue
        chave_pessoa = (row.get("email") or "").strip().lower()
        if chave_pessoa in vistos:
            continue
        vistos.add(chave_pessoa)
        candidatos.append(row)
    if len(candidatos) == 1:
        return candidatos[0]
    return None


def resolver_vendedor(nome_representante: str, usuarios: dict, regiao: str) -> dict | None:
    if not nome_representante:
        return None
    chave = nome_representante.strip().upper()

    if (chave, regiao) in usuarios["exato"]:
        return usuarios["exato"][(chave, regiao)]

    alias = ALIASES_REPRESENTANTE.get(chave)
    if alias:
        if (alias.upper(), regiao) in usuarios["exato"]:
            return usuarios["exato"][(alias.upper(), regiao)]
        if (_fold(alias), regiao) in usuarios["folded"]:
            return usuarios["folded"][(_fold(alias), regiao)]

    chave_fold = _fold(nome_representante)
    if (chave_fold, regiao) in usuarios["folded"]:
        return usuarios["folded"][(chave_fold, regiao)]

    alias_fold = ALIASES_REPRESENTANTE_FOLDED.get(chave_fold)
    if alias_fold and (_fold(alias_fold), regiao) in usuarios["folded"]:
        return usuarios["folded"][(_fold(alias_fold), regiao)]

    return (_resolver_por_prefixo_unico(nome_representante, usuarios, regiao)
            or _resolver_por_subsequencia_unica(nome_representante, usuarios, regiao))


def regioes_do_representante(nome_representante: str, usuarios: dict) -> set[str]:
    """Todas as regioes onde esse representante (nome exato, alias ou fold
    de acento) tem cadastro confirmado em usuarios_mercos.csv. Usado pra
    distinguir 2 casos que hoje viravam o MESMO alerta:
    (a) nome desconhecido em qualquer filial -- de-para incompleto,
        alerta de verdade, acionavel (curar ALIASES_REPRESENTANTE ou
        usuarios_mercos.csv);
    (b) representante confirmado só em OUTRA filial (ex.: so atua em ES) --
        nunca vai resolver nessa filial, nao e alerta, e so a filial nao
        se aplicar pra esse representante (fix 24/jul, pedido do usuario --
        caso real Fernanda Somavilla/Luiz Henrique Luchezi, ES-only)."""
    if not nome_representante:
        return set()
    chave = nome_representante.strip().upper()
    chave_fold = _fold(nome_representante)
    alias = ALIASES_REPRESENTANTE.get(chave)
    alias_fold = ALIASES_REPRESENTANTE_FOLDED.get(chave_fold)

    regioes: set[str] = set()
    for (nome_chave, regiao) in usuarios["exato"]:
        if nome_chave == chave or (alias and nome_chave == alias.upper()):
            regioes.add(regiao)
    for (nome_chave, regiao) in usuarios["folded"]:
        if nome_chave == chave_fold or (alias_fold and nome_chave == _fold(alias_fold)):
            regioes.add(regiao)
    # Mesmo fallback de prefixo único do resolver_vendedor -- sem isso,
    # nome curto ("FERNANDA") que só resolve via prefixo ficava mostrando
    # alerta de novo na região errada em vez de ser pulado como as demais
    # (mesma pessoa, mesmo caso "só atua em outra filial").
    for regiao_candidata in EMPRESA_MERCOS_PARA_REGIAO.values():
        if regiao_candidata not in regioes and (
            _resolver_por_prefixo_unico(nome_representante, usuarios, regiao_candidata)
            or _resolver_por_subsequencia_unica(nome_representante, usuarios, regiao_candidata)
        ):
            regioes.add(regiao_candidata)
    return regioes


CHAMADO_MINIMO = 30767  # 18/ago (era 26000 em 24/jul) -- ver nota do corte abaixo


# Corte de chamado. Sobe quando o usuario decide "daqui pra frente" -- em
# 18/ago/2026 foi de 26000 pra 30767, decisao explicita dele depois de ver que a
# normalizacao antiga tinha aplicado condicao errada em 14 clientes: "o passado
# ja foi feito, deixa la; corrige apenas os novos".
#
# ⚠️ Subir o corte NAO apaga nada -- as linhas de ledger abaixo dele deixam de
# ser lidas/atualizadas e viram fossil (invisiveis na Torre, congeladas no
# status em que estavam). Na virada de 18/ago isso incluiu **109 linhas
# `pendente` / 106 clientes** que nunca foram aplicados no Mercos: decisao do
# usuario, nao esquecimento. `escritor.py --cnpjs` continua ignorando o corte,
# entao dá pra rodar um desses pela linha de comando se algum precisar depois.
def _chamado_acima_do_minimo(valor) -> bool:
    try:
        return int(str(valor).strip()) >= CHAMADO_MINIMO
    except (ValueError, TypeError):
        return False


# ── Nomes de coluna da planilha (o formulario RENOMEIA colunas) ────────────
#
# 24/set/2026: a coluna do representante virou "RCA" (era "Nome do
# representante") e "STATUS" virou "Status". Como a leitura era por nome
# literal, `item.get(...)` passou a devolver None em 100% das linhas e TODO
# cliente caiu em `alerta_representante_nao_mapeado` -- o pipeline terminava
# verde cadastrando zero. O ledger prova a data: nome preenchido em 541/541
# linhas de julho ate 23/set, None a partir dai.
#
# Dois consertos, e o segundo e o que importa: aliases resolvem HOJE, a guarda
# de contrato resolve a PROXIMA vez -- coluna que sumir do header aborta a
# rodada nomeando o campo, em vez de virar alerta em massa sem erro nenhum.
COLUNAS = {
    "status":       ["Status", "STATUS"],
    "chamado":      ["Chamado Goservice"],
    "cnpj":         ["CNPJ ", "CNPJ"],
    "representante": ["RCA", "Nome do representante"],
    "razao":        ["Razão social", "Razao social"],
    "prazo":        ["Prazo do Cliente"],
    "tabela":       ["Tabela de desconto do cliente"],
    "financeiro":   ["Status Financeiro"],
    "justificativa": ["Justificativa"],
}

# Sem estas a decisao e impossivel ou sai errada -- abortar e o estado seguro.
COLUNAS_OBRIGATORIAS = ["status", "chamado", "cnpj", "representante", "prazo", "tabela"]


def col(linha: dict, campo: str):
    """Valor da coluna `campo` da planilha, testando os aliases conhecidos.

    Devolve o primeiro alias PRESENTE no dict (nao o primeiro truthy): celula
    vazia e uma resposta legitima da planilha, e cair pro alias seguinte por
    causa dela mascararia o header errado."""
    for nome in COLUNAS[campo]:
        if nome in linha:
            return linha[nome]
    return None


def _conferir_contrato_planilha(header: list[str]) -> None:
    faltando = []
    for campo in COLUNAS_OBRIGATORIAS:
        if not any(nome in header for nome in COLUNAS[campo]):
            faltando.append(f"{campo} (esperava um de {COLUNAS[campo]})")
    if not faltando:
        return
    detalhe = "; ".join(faltando)
    raise SystemExit(
        "[decisor] A planilha de cadastro mudou de formato. Colunas nao encontradas: "
        + detalhe
        + f" | Header atual: {header}"
        + " | Acrescente o nome novo em COLUNAS (decisor.py) antes de rodar."
        + " Abortando de proposito: sem estas colunas o decisor classificaria todo"
        + " mundo como alerta e cadastraria zero, sem erro nenhum."
    )


def carregar_planilha_cadastro() -> list[dict]:
    creds = Credentials.from_service_account_info(
        json.loads(SHEETS_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEETS_CADASTRO_ID)
    ws = sh.worksheets()[0]
    linhas = ws.get_all_records()
    _conferir_contrato_planilha(ws.row_values(1))

    por_cnpj: dict[str, dict] = {}
    for indice, linha in enumerate(linhas):
        sheet_row = indice + 2
        status = str(col(linha, "status") or "").strip().upper()
        if "DADOS DO RCA" in status:
            continue
        cnpj = normalizar_cnpj(col(linha, "cnpj") or "")
        if not cnpj:
            continue
        if not _chamado_acima_do_minimo(col(linha, "chamado")):
            continue
        existente = por_cnpj.get(cnpj)
        eh_duplicado = "DUPLICADO" in status
        if existente is None:
            por_cnpj[cnpj] = {"_sheet_row": sheet_row, **linha}
        elif eh_duplicado:
            continue
        else:
            por_cnpj[cnpj] = {"_sheet_row": sheet_row, **linha}
    return [{"_cnpj": cnpj, **linha} for cnpj, linha in por_cnpj.items()]


_SQL_CADASTRO_CLIENTE_MULTI_FILIAL = """
SELECT
  "middleware"."transaction"."mid_status" AS "mid_status",
  "middleware"."transaction"."tra_branch" AS "tra_branch",
  "middleware"."transaction"."res_api_status" AS "res_api_status",
  "middleware"."transaction"."res_api_request" AS "res_api_request",
  "middleware"."transaction"."res_api_doc_ref" AS "res_api_doc_ref",
  "middleware"."transaction"."mid_date_add" AS "mid_date_add"
FROM "middleware"."transaction"
WHERE ("middleware"."transaction"."tra_type" = 'Client')
  AND ("middleware"."transaction"."res_api_method" = 'POST')
  AND ("middleware"."transaction"."tra_branch" IN ('1301', '1303'))
ORDER BY "middleware"."transaction"."mid_date_add" DESC, "middleware"."transaction"."mid_id" DESC
LIMIT {limit} OFFSET {offset}
"""
# ORDER BY tem que ter desempate unico (mid_id) -- todo cliente replica pra
# ES+RJ no MESMO mid_date_add exato (ver "descoberta multi-filial" em
# fluxo-e-fontes-de-dados.md), entao SO por mid_date_add o Postgres nao
# garante ordem estavel entre paginas/execucoes diferentes quando ha
# empate -- paginacao por OFFSET perdia linhas silenciosamente de forma
# nao-deterministica. Bug real encontrado 24/jul: 4 linhas do ledger
# ficaram travadas em status='erro' desde 23/jul porque o cnpj/empresa
# delas sumia do resultado do Metabase em execucoes seguintes (empate mal
# desempatado), entao o decisor nunca mais as revisitava pra tentar de novo.

_PAGINA_TAMANHO = 2000  # /api/dataset (nativo) capa em 2000 linhas mesmo com LIMIT maior -- ver arquitetura-producao.md


def _rodar_query_nativa(sql: str) -> list[dict]:
    body = json.dumps({"database": 48, "type": "native", "native": {"query": sql}}).encode()
    req = urllib.request.Request(
        f"{METABASE_URL}/api/dataset",
        method="POST",
        headers={"Content-Type": "application/json", "x-api-key": METABASE_API_KEY},
        data=body,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read())
    cols = [c["name"] for c in payload["data"]["cols"]]
    return [dict(zip(cols, row)) for row in payload["data"]["rows"]]


def buscar_confirmacoes_metabase() -> dict:
    linhas: list[dict] = []
    offset = 0
    while True:
        sql = _SQL_CADASTRO_CLIENTE_MULTI_FILIAL.format(limit=_PAGINA_TAMANHO, offset=offset)
        pagina = _rodar_query_nativa(sql)
        linhas.extend(pagina)
        if len(pagina) < _PAGINA_TAMANHO:
            break
        offset += _PAGINA_TAMANHO

    confirmados: dict[str, dict] = {}
    for linha in linhas:
        if linha.get("mid_status") != "Sucesso" or linha.get("res_api_status") != 201:
            continue
        branch = linha.get("tra_branch")
        empresa_id = BRANCH_PARA_EMPRESA_MERCOS.get(branch)
        if not empresa_id:
            continue
        try:
            req_payload = json.loads(linha.get("res_api_request") or "{}")
        except json.JSONDecodeError:
            continue
        cnpj = re.sub(r"\D", "", req_payload.get("cnpj") or "")
        if len(cnpj) != 14:
            continue

        por_empresa = confirmados.setdefault(cnpj, {})
        anterior = por_empresa.get(empresa_id)
        data_atual = linha.get("mid_date_add") or ""
        if anterior is None or data_atual > anterior.get("_data", ""):
            por_empresa[empresa_id] = {
                "mercos_cliente_id": linha.get("res_api_doc_ref"),
                "_data": data_atual,
            }

    # ── 2a fonte: `mercos_cliente_confirmado` (11/set/2026) ──────────────────
    #
    # ⚠️ O filtro acima NAO responde "este cliente existe no Mercos?". Ele responde
    # "o NOSSO middleware criou este cliente no Mercos?" -- exige o log com
    # mid_status='Sucesso' E res_api_status=201. Cliente que existe no Mercos mas
    # nasceu por FORA (cadastrado na mao, processo antigo, log ausente do Metabase)
    # nunca satisfaz o portao e fica preso em `aguardando_mercos` PARA SEMPRE, com
    # a aba dizendo "sem cadastro no Mercos" sobre um cliente que tem cadastro.
    #
    # Medido em 11/set/2026, varrendo os 63 presos um a um nas duas empresas:
    # 53 EXISTEM no Mercos, 10 nao. Relato que originou: chamado 141666 (STARMIX).
    #
    # O Metabase continua GANHANDO quando os dois conhecem o mesmo (cnpj, empresa):
    # o log do middleware acompanha recadastro, a medicao pontual nao.
    # Cliente proprio: `main()` so cria o dele depois daqui, e fora de --dry-run.
    # Esta leitura e read-only, entao vale nos dois modos -- o dry-run tem que
    # enxergar exatamente o que a rodada real vai enxergar, senao nao serve de ensaio.
    try:
        conf = (
            create_client(SUPABASE_URL, SUPABASE_KEY)
            .table("mercos_cliente_confirmado")
            .select("cnpj, mercos_empresa_id, mercos_cliente_id")
            .eq("ativo", True)
            .execute()
            .data
            or []
        )
    except Exception as e:
        # Nao derruba a rodada: sem a 2a fonte o decisor volta ao comportamento
        # antigo (so Metabase), que e o estado seguro -- cliente fica pendente,
        # ninguem e processado errado.
        print(f"      [!] mercos_cliente_confirmado indisponivel ({type(e).__name__}) -- seguindo so com o Metabase")
        conf = []

    extras = 0
    for linha in conf:
        cnpj = re.sub(r"\D", "", linha.get("cnpj") or "")
        empresa_id = (linha.get("mercos_empresa_id") or "").strip()
        cid = linha.get("mercos_cliente_id")
        if len(cnpj) != 14 or not empresa_id or not cid:
            continue
        por_empresa = confirmados.setdefault(cnpj, {})
        atual = por_empresa.get(empresa_id)
        # ⚠️ O Metabase manda SO QUANDO TEM O ID. Achado em 14/set/2026 com a SOFIA
        # PERFUMARIA (42689734000170): o log tinha Sucesso+201 mas `res_api_doc_ref`
        # VAZIO, e a versao anterior desta regra (`if empresa_id in por_empresa:
        # continue`) deixava o Metabase ganhar assim mesmo -- a linha caia em
        # `alerta_sem_cliente_id` com o id disponivel aqui do lado, medido na varredura.
        # Empate so existe entre duas fontes que sabem a resposta; uma fonte vazia
        # nao e autoridade sobre nada.
        if atual and str(atual.get("mercos_cliente_id") or "").strip():
            continue
        por_empresa[empresa_id] = {"mercos_cliente_id": str(cid), "_data": ""}
        extras += 1
    if extras:
        print(f"      +{extras} confirmacao(oes) de mercos_cliente_confirmado (fora do log 201)")

    return confirmados


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("[1/4] Carregando planilha de cadastro...")
    planilha = carregar_planilha_cadastro()
    print(f"      {len(planilha)} clientes unicos (pos dedupe/filtro)")

    print("[2/4] Consultando Metabase (ES + RJ, SQL nativo ad-hoc)...")
    confirmados = buscar_confirmacoes_metabase()
    n_es = sum(1 for v in confirmados.values() if "424525" in v)
    n_rj = sum(1 for v in confirmados.values() if "424524" in v)
    print(f"      {len(confirmados)} CNPJs confirmados no middleware (ES={n_es}, RJ={n_rj})")

    print("[3/4] Carregando usuarios Mercos (de-para representante, por regiao)...")
    usuarios = carregar_usuarios_mercos()

    sb = None if args.dry_run else create_client(SUPABASE_URL, SUPABASE_KEY)

    ja_processados: set[tuple[str, str]] = set()
    cliente_id_conhecido: dict[tuple[str, str], str] = {}
    if sb is not None:
        existentes = sb.table("cadastro_rca_mercos_processado").select("cnpj,mercos_empresa_id").eq("status", "processado").execute().data
        ja_processados = {(r["cnpj"], r["mercos_empresa_id"]) for r in existentes}
        print(f"      {len(ja_processados)} (cnpj, filial) ja processados -- nao vao ser recalculados/sobrescritos")

        com_id = sb.table("cadastro_rca_mercos_processado").select("cnpj,mercos_empresa_id,mercos_cliente_id").not_.is_("mercos_cliente_id", "null").execute().data
        cliente_id_conhecido = {(r["cnpj"], r["mercos_empresa_id"]): r["mercos_cliente_id"] for r in com_id}
        print(f"      {len(cliente_id_conhecido)} (cnpj, filial) com mercos_cliente_id ja conhecido -- preservado mesmo se o Metabase vier vazio de novo")

    print("[4/4] Decidindo por cliente x filial...\n")
    contagem = {"pronto": 0, "aguardando_mercos": 0, "alerta_preco": 0, "alerta_pagamento": 0, "alerta_representante": 0, "alerta_sem_cliente_id": 0, "ja_processado": 0, "pulado_outra_filial": 0}
    linhas_ledger = 0

    for item in planilha:
        cnpj = item["_cnpj"]
        chamado = col(item, "chamado")
        nome_rep = col(item, "representante")
        razao_social = col(item, "razao")
        prazo_raw = col(item, "prazo")
        tabela_raw = col(item, "tabela")
        status_financeiro = col(item, "financeiro")
        justificativa = col(item, "justificativa")

        confirmacoes_por_empresa = confirmados.get(cnpj)
        base = {
            "cnpj": cnpj,
            "chamado_goservice": str(chamado) if chamado else None,
            "nome_representante_planilha": nome_rep,
            "razao_social_planilha": razao_social or None,
            "tabela_preco_planilha": tabela_raw,
            "condicao_pagamento_planilha": prazo_raw,
            "status_financeiro_planilha": status_financeiro or None,
            "justificativa_planilha": justificativa or None,
            "sheet_row": item["_sheet_row"],
        }

        if not confirmacoes_por_empresa:
            registro = {**base, "mercos_empresa_id": "", "status": "aguardando_mercos"}
            contagem["aguardando_mercos"] += 1
            print(f"  {cnpj} | chamado {chamado} | {nome_rep} | (todas filiais) status=aguardando_mercos")
            if sb is not None:
                sb.table("cadastro_rca_mercos_processado").upsert(registro, on_conflict="cnpj,mercos_empresa_id").execute()
            linhas_ledger += 1
            continue

        # Cliente confirmado em pelo menos 1 filial agora -- o placeholder
        # "aguardando_mercos" (mercos_empresa_id='') tem que sair, senao
        # fica pra sempre como uma "3a filial" fantasma na Torre, ao lado
        # das linhas reais de ES/RJ (bug real encontrado 24/jul).
        #
        # ⚠️ Mas SO DEPOIS do loop, e so se sobrar alguma linha real.
        # "Achou o cliente em alguma filial" NAO garante que alguma linha
        # va existir: se o representante so atua na OUTRA filial, essa e
        # pulada E APAGADA (`pulado_outra_filial`, logo abaixo). Apagando o
        # placeholder antes do loop, o cliente ficava sem linha NENHUMA --
        # sumia da Torre e da propria lista de alvos da varredura, que sai
        # do ledger, entao nem voltava a ser perguntado. Bug real
        # 25/set/2026: KIMAKE (33982295000106) confirmou no RJ, ROMEU so
        # atua no ES, e o cliente desapareceu com o job verde.
        linhas_reais_do_cliente = 0

        for empresa_id, confirmacao in confirmacoes_por_empresa.items():
            if (cnpj, empresa_id) in ja_processados:
                contagem["ja_processado"] += 1
                linhas_reais_do_cliente += 1
                print(f"  {cnpj} | chamado {chamado} | {nome_rep} | {EMPRESA_MERCOS_PARA_REGIAO[empresa_id]} | ja processado -- so atualizando campos informativos da planilha")
                # Nao recalcula status/tabela/condicao/vendedor (decisao ja
                # foi tomada e aplicada no Mercos) mas os campos abaixo sao
                # so informativos (exibicao na Torre) -- sempre refletem a
                # planilha, mesmo depois de processado.
                if sb is not None:
                    sb.table("cadastro_rca_mercos_processado").update({
                        "razao_social_planilha": razao_social or None,
                        "status_financeiro_planilha": status_financeiro or None,
                        "justificativa_planilha": justificativa or None,
                    }).eq("cnpj", cnpj).eq("mercos_empresa_id", empresa_id).execute()
                continue

            regiao = EMPRESA_MERCOS_PARA_REGIAO[empresa_id]
            # Preserva o mercos_cliente_id ja conhecido (achado manualmente
            # ou em execucao anterior) se o Metabase vier vazio de novo --
            # sem isso, um cliente corrigido na mao (Res Api Doc Ref antigo
            # sem log, resolvido buscando o cliente_id direto no Mercos)
            # voltava a "alerta_sem_cliente_id" no proximo Atualizar, porque
            # a transacao antiga no middleware nunca vai ganhar o doc_ref
            # retroativamente. Bug real encontrado 24/jul.
            cliente_id = confirmacao["mercos_cliente_id"] or cliente_id_conhecido.get((cnpj, empresa_id))
            registro = {
                **base,
                "mercos_empresa_id": empresa_id,
                "mercos_cliente_id": cliente_id,
            }

            vendedor = resolver_vendedor(nome_rep, usuarios, regiao)

            if vendedor is None:
                outras_regioes = regioes_do_representante(nome_rep, usuarios) - {regiao}
                if outras_regioes:
                    # Representante comprovadamente so tem cadastro Mercos
                    # em outra(s) filial(is) -- nunca vai resolver aqui, nao
                    # e alerta acionavel (diferente de nome desconhecido em
                    # QUALQUER filial). So pula essa filial pra esse
                    # cliente, sem criar linha/alerta nenhum -- e apaga
                    # qualquer linha antiga que tenha sobrado de antes desse
                    # fix (fix 24/jul, caso real Fernanda Somavilla/Luiz
                    # Henrique Luchezi).
                    print(f"  {cnpj} | chamado {chamado} | {nome_rep} | {regiao} | representante so atua em {sorted(outras_regioes)} -- pulando filial, nao e alerta")
                    contagem["pulado_outra_filial"] = contagem.get("pulado_outra_filial", 0) + 1
                    if sb is not None:
                        sb.table("cadastro_rca_mercos_processado").delete().eq("cnpj", cnpj).eq("mercos_empresa_id", empresa_id).execute()
                    continue

            tabela_ok = normalizar_tabela_preco(tabela_raw)
            condicao_ok = normalizar_condicao_pagamento(prazo_raw)

            registro["tabela_preco_aplicada"] = tabela_ok
            registro["condicao_pagamento_aplicada"] = condicao_ok
            registro["vendedor_mercos_email"] = vendedor["email"] if vendedor else None

            if tabela_ok is None:
                registro["status"] = "alerta_normalizacao_preco"
                contagem["alerta_preco"] += 1
            elif condicao_ok is None:
                registro["status"] = "alerta_normalizacao_pagamento"
                contagem["alerta_pagamento"] += 1
            elif vendedor is None:
                registro["status"] = "alerta_representante_nao_mapeado"
                contagem["alerta_representante"] += 1
            elif not cliente_id:
                # Middleware confirmou Mid Status=Sucesso + Res Api Status=201
                # mas o Res Api Doc Ref (ID do cliente no Mercos) veio vazio --
                # sem isso o escritor nao tem pra onde navegar (vira
                # "cliente_id=None" na URL, botao "Editar Vinculos" fica
                # desabilitado -- bug real encontrado 24/jul, ver
                # decisoes-e-plano.md). Nunca deixa cair em pendente sem isso.
                registro["status"] = "alerta_sem_cliente_id"
                contagem["alerta_sem_cliente_id"] += 1
            else:
                registro["status"] = "pendente"
                contagem["pronto"] += 1

            print(f"  {cnpj} | chamado {chamado} | {nome_rep} | {regiao} | status={registro['status']}")
            print(f"      tabela: '{tabela_raw}' -> {tabela_ok}"
                  f" | condicao: '{prazo_raw}' -> {condicao_ok}"
                  f" | vendedor({regiao}): {registro.get('vendedor_mercos_email')}")

            if sb is not None:
                sb.table("cadastro_rca_mercos_processado").upsert(registro, on_conflict="cnpj,mercos_empresa_id").execute()
            linhas_ledger += 1
            linhas_reais_do_cliente += 1

        # Fecha o placeholder -- ver o bloco comentado antes do loop.
        if linhas_reais_do_cliente:
            if sb is not None:
                sb.table("cadastro_rca_mercos_processado").delete().eq("cnpj", cnpj).eq("mercos_empresa_id", "").execute()
        else:
            # Confirmou em alguma filial, mas nenhuma linha real sobrou --
            # todas pertencem a filial em que o representante nao atua. O
            # estado verdadeiro continua sendo "esperando aparecer na filial
            # certa", entao o placeholder FICA e o cliente segue na fila.
            print(f"  {cnpj} | chamado {chamado} | {nome_rep} | confirmado so em filial onde o representante nao atua -- segue aguardando_mercos")
            contagem["aguardando_mercos"] += 1
            if sb is not None:
                sb.table("cadastro_rca_mercos_processado").upsert(
                    {**base, "mercos_empresa_id": "", "status": "aguardando_mercos"},
                    on_conflict="cnpj,mercos_empresa_id",
                ).execute()
            linhas_ledger += 1

    print("\n" + "=" * 60)
    print("RESUMO")
    print("=" * 60)
    for k, v in contagem.items():
        print(f"  {k}: {v}")
    print(f"  clientes unicos (planilha): {len(planilha)}")
    print(f"  linhas no ledger (cliente x filial): {linhas_ledger}")


if __name__ == "__main__":
    main()
