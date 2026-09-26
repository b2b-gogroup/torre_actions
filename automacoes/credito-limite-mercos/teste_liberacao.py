"""Teste offline do modo --liberacoes (o TETO) — sem navegador, sem banco, sem Mercos.

Mesmo padrao de `teste_grupo.py`: dubles dos modulos externos antes do import, e as
funcoes que tocam a tela sao substituidas. O que fica sob teste e exatamente o que
nao da pra conferir olhando: a CONTA e as RECUSAS.

Prova:
   1. escreve OS DOIS campos, e o disponivel anda pelo DELTA
   2. NUNCA `disponivel = teto_novo` (devolveria o ja consumido de graca)
   3. reducao de teto derruba o disponivel junto
   4. piso em zero quando o delta negativo passaria do que o cliente tem
   5. clamp no teto novo quando o disponivel lido ja estava acima dele
   6. acima do teto automatico NAO recusa (a 2a aprovacao nunca existiu)
   7. cliente 0/0 -> RECUSA (escrever criaria bloqueio que nao existe)
   8. retrato divergente -> RECUSA (alguem mexeu no Mercos no meio)
   9. delta zero -> carimba sem escrever (senao volta na fila para sempre)
  10. dry-run nao escreve E nao carimba
  11. desfazer devolve `mercos_teto_antes`, nao `limite_de` (que e do Protheus)
  12. desfazer recusa se o teto foi mexido depois da nossa escrita
  13. desfazer com cliente que comprou no meio: piso em zero, e DECLARA

Rodar:  python teste_liberacao.py
"""
import sys, types, os
from pathlib import Path

# ── Dubles dos modulos externos, antes do import ────────────────────────────
pw = types.ModuleType("playwright"); pwapi = types.ModuleType("playwright.sync_api")
pwapi.Page = object
pwapi.sync_playwright = lambda: None
pwapi.TimeoutError = type("TimeoutError", (Exception,), {})
pw.sync_api = pwapi
sys.modules["playwright"] = pw; sys.modules["playwright.sync_api"] = pwapi
sb_mod = types.ModuleType("supabase"); sb_mod.create_client = lambda *a, **k: None
sys.modules["supabase"] = sb_mod
dot = types.ModuleType("dotenv"); dot.load_dotenv = lambda *a, **k: None
sys.modules["dotenv"] = dot

os.environ.setdefault("SUPABASE_URL", "http://x"); os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("MERCOS_EMAIL", "x"); os.environ.setdefault("MERCOS_SENHA", "x")
os.environ.setdefault("GMAIL_USER", "x"); os.environ.setdefault("GMAIL_SENHA", "x")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repositor as R

# ── Estado observavel ───────────────────────────────────────────────────────
ESCRITAS = []    # cada clique em Salvar: (disponivel, teto)
CARIMBOS = []    # cada chamada de fn_credito_liberacao_marcar_*
TELA = {}        # o que ler_limites devolve


class FakeSb:
    def rpc(self, nome, args):
        CARIMBOS.append({"fn": nome, **args})
        return types.SimpleNamespace(execute=lambda: types.SimpleNamespace(
            data=[{"codigo": "LC-TESTE", "ja_aplicada": False, "ja_revertida": False}]))

    def table(self, _t):
        q = types.SimpleNamespace()
        q.update = lambda *_a, **_k: q
        q.insert = lambda *_a, **_k: q
        q.select = lambda *_a, **_k: q
        q.eq = lambda *_a, **_k: q
        q.limit = lambda *_a, **_k: q
        q.execute = lambda: types.SimpleNamespace(data=[])
        return q


class FakeAud:
    modo = "liberacoes"
    quem = "teste"
    rodada_id = "r1"
    sb = FakeSb()

    def abrir_linha(self, *_a, **_k): return "log1"
    def fechar_linha(self, *_a, **_k): pass
    def fase(self, *_a, **_k): pass
    def evento_json(self, **_k): pass


def montar_tela(disponivel, total):
    TELA.clear(); TELA.update(disponivel=disponivel, total=total)


# ── Substitui tudo que toca a tela ──────────────────────────────────────────
R.resolver_cliente = lambda *_a, **_k: ("424525", "60382638")
R.abrir_modal_limite = lambda *_a, **_k: None
R.ler_limites = lambda *_a, **_k: (TELA["disponivel"], TELA["total"])
R.fechar_modal = lambda *_a, **_k: None
R.gravar_espelho = lambda *_a, **_k: None
R.nome_do_cliente = lambda *_a, **_k: "CLIENTE TESTE"
R._reler_limites_silencioso = lambda *_a, **_k: (TELA["disponivel"], TELA["total"])


def _escrever(_page, novo_disp, novo_total, esp_disp, esp_total):
    ESCRITAS.append((round(novo_disp, 2), round(novo_total, 2)))
    # Simula o Mercos gravando exatamente o que foi digitado (medido em 20/ago:
    # "o Mercos NAO recalcula o disponivel depois de salvar").
    montar_tela(novo_disp, novo_total)
    return f"ok -- teto {esp_total} -> {novo_total}, disponivel {esp_disp} -> {novo_disp}"


R.escrever_limites = _escrever

SB, AUD = FakeSb(), FakeAud()
FALHAS = []


def check(nome, cond, detalhe=""):
    print(f"  {'OK  ' if cond else 'FALHOU'}  {nome}" + (f"  [{detalhe}]" if detalhe and not cond else ""))
    if not cond:
        FALHAS.append(nome)


def lib(**kw):
    base = dict(id="11111111-1111-1111-1111-111111111111", codigo="LC-00001",
                cnpj="11647490000139", limite_para=40000.0, criado_por="alguem@x",
                criado_em="2026-08-25T10:00:00Z", nome_cliente="CLIENTE TESTE",
                mercos_teto_no_momento=30000.0, retrato_horas=2.0,
                exige_segunda_aprovacao=False)
    base.update(kw)
    return base


def rodar(fn, l, aplicar=True):
    ESCRITAS.clear(); CARIMBOS.clear()
    try:
        return fn(None, SB, l, aplicar, AUD), None
    except Exception as e:
        return None, e


print("\n=== teste_liberacao — o TETO, offline ===\n")

# 1 · 2 · a conta principal
montar_tela(22554.18, 30000.0)
_txt, err = rodar(R.processar_liberacao, lib(limite_para=40000.0))
check("1. escreve os DOIS campos, uma vez", err is None and len(ESCRITAS) == 1, str(err))
check("1b. disponivel anda pelo DELTA (22.554,18 + 10.000)",
      ESCRITAS and ESCRITAS[0] == (32554.18, 40000.0), str(ESCRITAS))
check("2. NUNCA disponivel = teto_novo", not ESCRITAS or ESCRITAS[0][0] != 40000.0)
check("2b. carimbou aplicada uma vez",
      len(CARIMBOS) == 1 and CARIMBOS[0]["fn"] == "fn_credito_liberacao_marcar_aplicada", str(CARIMBOS))

# 3 · reducao
montar_tela(22554.18, 30000.0)
rodar(R.processar_liberacao, lib(limite_para=20000.0))
check("3. reducao derruba o disponivel junto (delta -10.000)",
      ESCRITAS and ESCRITAS[0] == (12554.18, 20000.0), str(ESCRITAS))

# 4 · piso em zero
montar_tela(2000.0, 30000.0)
rodar(R.processar_liberacao, lib(limite_para=5000.0))
check("4. piso em zero (2.000 - 25.000 nao vira negativo)",
      ESCRITAS and ESCRITAS[0] == (0.0, 5000.0), str(ESCRITAS))

# 5 · clamp no teto novo
montar_tela(35000.0, 30000.0)
rodar(R.processar_liberacao, lib(limite_para=31000.0))
check("5. clamp no teto novo (disponivel lido acima do teto)",
      ESCRITAS and ESCRITAS[0] == (31000.0, 31000.0), str(ESCRITAS))

# 6 · acima do teto automatico da politica NAO e recusa (decisao do Kaique, 25/ago)
# A segunda aprovacao nunca existiu como fluxo -- o aviso de 13/ago diz "quando o
# fluxo estiver ligado". Este cenario existe para PROVAR que a trava nao voltou.
montar_tela(22554.18, 30000.0)
_t, err = rodar(R.processar_liberacao, lib(limite_para=200000.0, exige_segunda_aprovacao=True))
check("6. acima do teto automatico ESCREVE (nao ha 2a aprovacao)",
      err is None and ESCRITAS == [(192554.18, 200000.0)], f"{err} {ESCRITAS}")

# 7 · cliente 0/0
montar_tela(0.0, 0.0)
_t, err = rodar(R.processar_liberacao, lib(mercos_teto_no_momento=0.0))
check("7. cliente 0/0 -> PrecisaDeGente", isinstance(err, R.PrecisaDeGente), str(err))
check("7b. nao escreveu e NAO carimbou", not ESCRITAS and not CARIMBOS)

# 8 · retrato divergente
montar_tela(22554.18, 50000.0)
_t, err = rodar(R.processar_liberacao, lib(mercos_teto_no_momento=30000.0))
check("8. teto divergente do retrato -> PrecisaDeGente", isinstance(err, R.PrecisaDeGente), str(err))
check("8b. a recusa diz a idade do retrato", err is not None and "2.0h" in str(err), str(err))
check("8c. nao escreveu e NAO carimbou", not ESCRITAS and not CARIMBOS)

# 9 · delta zero
montar_tela(22554.18, 40000.0)
_t, err = rodar(R.processar_liberacao, lib(limite_para=40000.0, mercos_teto_no_momento=40000.0))
check("9. delta zero: nao escreve mas CARIMBA (senao volta pra sempre)",
      err is None and not ESCRITAS and len(CARIMBOS) == 1, f"{err} {ESCRITAS} {CARIMBOS}")

# 10 · dry-run
montar_tela(22554.18, 30000.0)
_t, err = rodar(R.processar_liberacao, lib(), aplicar=False)
check("10. dry-run nao escreve e nao carimba",
      err is None and not ESCRITAS and not CARIMBOS, f"{err} {ESCRITAS} {CARIMBOS}")

# ── DESPACHO DA ANALISE DE CREDITO: os dois modos (24/set/2026) ─────────────
# Exemplo do usuario: teto 5.000, disponivel 1.000.
#   "Novo limite" 10.000  -> teto 10.000, disponivel 6.000 (os 1.000 + os 5.000 novos)
#   "Acrescentar" 5.000   -> teto 10.000, disponivel 6.000
MSGS = []


class FakeSbMsg(FakeSb):
    def table(self, t):
        q = super().table(t)
        if t == "whatsapp_outbox":
            q.insert = lambda row, **_k: (MSGS.append(row["mensagem"]), q)[1]
        return q


def rodar_msg(l):
    ESCRITAS.clear(); CARIMBOS.clear(); MSGS.clear()
    try:
        return R.processar_liberacao(None, FakeSbMsg(), l, True, AUD), None
    except Exception as e:
        return None, e


# D1 · "Novo limite" (absoluto) vindo do despacho, com retrato VELHO do espelho: nao recusa
montar_tela(1000.0, 5000.0)
_t, err = rodar_msg(lib(origem="analise-credito", modo_aplicacao="absoluto", limite_para=10000.0,
                        mercos_teto_no_momento=3000.0, retrato_horas=300.0, validade="2026-12-22"))
check("D1. despacho absoluto ignora retrato velho e escreve 6.000 / 10.000",
      err is None and ESCRITAS == [(6000.0, 10000.0)], f"{err} {ESCRITAS}")
check("D1b. WhatsApp: 'Novo limite aprovado', total no Mercos, data DD/MM/AAAA, sem 'Analisado por'",
      bool(MSGS) and "Novo limite aprovado: *R$ 10.000,00*" in MSGS[0]
      and "Limite total no Mercos: *R$ 10.000,00*" in MSGS[0]
      and "Válido até: 22/12/2026" in MSGS[0] and "Analisado por" not in MSGS[0], str(MSGS))

# D2 · "Acrescentar" (incremental) vindo do despacho: soma ao teto lido
montar_tela(1000.0, 5000.0)
_t, err = rodar_msg(lib(origem="analise-credito", modo_aplicacao="incremental", limite_para=5000.0,
                        teto_alvo=10000.0, mercos_teto_no_momento=None))
check("D2. despacho incremental: 5.000 + 5.000 -> 6.000 / 10.000",
      err is None and ESCRITAS == [(6000.0, 10000.0)], f"{err} {ESCRITAS}")
check("D2b. WhatsApp diz 'Limite acrescentado' e o total",
      bool(MSGS) and "Limite acrescentado: *R$ 5.000,00*" in MSGS[0]
      and "Limite total no Mercos: *R$ 10.000,00*" in MSGS[0], str(MSGS))

# D3 · a guarda do retrato CONTINUA para o botao Liberar (origem gestao-credito)
montar_tela(1000.0, 5000.0)
_t, err = rodar(R.processar_liberacao, lib(origem="gestao-credito", limite_para=10000.0,
                                           mercos_teto_no_momento=3000.0))
check("D3. Liberar da Gestao de Credito segue recusando retrato divergente",
      isinstance(err, R.PrecisaDeGente) and not ESCRITAS, f"{err} {ESCRITAS}")

# ── DESFAZER ────────────────────────────────────────────────────────────────
def des(**kw):
    base = dict(id="22222222-2222-2222-2222-222222222222", codigo="LC-00002",
                cnpj="11647490000139", nome_cliente="CLIENTE TESTE",
                teto_que_foi_escrito=40000.0, teto_para_devolver=30000.0,
                teto_que_ficou=40000.0, encerrado_tipo="expirada",
                encerrado_em="2026-08-25T09:05:00Z", validade="2026-08-24",
                aplicado_no_mercos="2026-08-20T22:30:00Z", limite_de=0.0)
    base.update(kw)
    return base

# 11 · devolve o teto que o ROBO leu, nao o do Protheus (limite_de = 0 aqui)
montar_tela(32554.18, 40000.0)
_t, err = rodar(R.processar_desfazer, des())
check("11. desfazer devolve mercos_teto_antes (30.000), nao limite_de (0)",
      err is None and ESCRITAS and ESCRITAS[0] == (22554.18, 30000.0), f"{err} {ESCRITAS}")
check("11b. carimbou revertida",
      len(CARIMBOS) == 1 and CARIMBOS[0]["fn"] == "fn_credito_liberacao_marcar_revertida", str(CARIMBOS))

# 12a · alguem JA desfez a mao: teto ja e o valor a devolver -> carimba, nao prende (LC-00007, 24/set)
montar_tela(0.0, 0.0)
_t, err = rodar(R.processar_desfazer, des(teto_que_ficou=2000.0, teto_para_devolver=0.0))
check("12a. ja desfeito a mao -> carimba revertida, sem escrever e sem PrecisaDeGente",
      err is None and not ESCRITAS and len(CARIMBOS) == 1
      and CARIMBOS[0]["fn"] == "fn_credito_liberacao_marcar_revertida", f"{err} {ESCRITAS} {CARIMBOS}")

# 12 · alguem mexeu no teto depois de nos
montar_tela(32554.18, 55000.0)
_t, err = rodar(R.processar_desfazer, des(teto_que_ficou=40000.0))
check("12. teto mexido depois -> PrecisaDeGente", isinstance(err, R.PrecisaDeGente), str(err))
check("12b. nao escreveu e NAO carimbou", not ESCRITAS and not CARIMBOS)

# 13 · o cliente comprou entre a liberacao e o vencimento
montar_tela(3000.0, 40000.0)
_t, err = rodar(R.processar_desfazer, des())
check("13. desfazer com piso em zero (cliente comprou no meio)",
      err is None and ESCRITAS and ESCRITAS[0] == (0.0, 30000.0), f"{err} {ESCRITAS}")

print()
if FALHAS:
    print(f"=== {len(FALHAS)} FALHA(S): " + " · ".join(FALHAS) + " ===")
    sys.exit(1)
print("=== todos os cenarios passaram ===")
