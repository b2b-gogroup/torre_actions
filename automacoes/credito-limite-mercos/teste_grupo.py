"""Teste offline de processar_grupo — sem navegador, sem banco, sem Mercos.

Prova o que o agrupamento tem que garantir:
  1. N boletos do mesmo cliente = UMA escrita, com a soma certa
  2. boleto de valor zerado sai da soma e NAO derruba os bons do mesmo cliente
  3. dry-run nao escreve e faz UM calculo por cliente
  4. boleto consumido por outra rodada sai da soma (e a soma e recalculada)
  5. delta aparece em UMA linha de auditoria (a principal), nunca em N
  6. o carimbo da fila e UMA chamada com os N ids
"""
import sys, types, os
from pathlib import Path

# ── Dublês dos módulos externos, antes do import ────────────────────────────
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

# ── Dublês do estado ────────────────────────────────────────────────────────
ESCRITAS = []      # cada clique em Salvar
CARIMBOS = []      # cada chamada de update na fila
LINHAS = {}        # id da linha de auditoria -> campos

class FakeQuery:
    def __init__(self, tabela, store): self.t, self.s, self.ids, self.payload = tabela, store, [], None
    def update(self, campos): self.payload = campos; return self
    def insert(self, campos): self.payload = campos; return self
    def select(self, *a, **k): return self
    def eq(self, col, val): self.ids = [val] if col == "id" else self.ids; self._eq = (col, val); return self
    def in_(self, col, vals): self.ids = list(vals); return self
    def limit(self, n): return self
    def order(self, *a, **k): return self
    def execute(self):
        if self.t == "credito_evento_pagamento":
            if self.payload and self.payload.get("processado"):
                CARIMBOS.append({"ids": list(self.ids), "resultado": self.payload.get("resultado")})
                return types.SimpleNamespace(data=[])
            # ainda_pendente: devolve o estado do dublê
            pid = self.ids[0] if self.ids else None
            return types.SimpleNamespace(data=[{"processado": PENDENCIA.get(pid, False)}])
        if self.t == "credito_reposicao_log":
            if self.payload is not None and not self.ids:      # insert
                novo = f"log{len(LINHAS)+1}"; LINHAS[novo] = dict(self.payload)
                return types.SimpleNamespace(data=[{"id": novo}])
            for i in self.ids: LINHAS.setdefault(i, {}).update(self.payload or {})
            return types.SimpleNamespace(data=[])
        return types.SimpleNamespace(data=[])

class FakeSB:
    def table(self, t): return FakeQuery(t, None)

class FakeAud:
    modo = "aplicar"; rodada_id = "rod1"
    def __init__(self): self.sb = FakeSB(); self.eventos = []
    def abrir_linha(self, ev, **kw):
        q = FakeQuery("credito_reposicao_log", None)
        campos = {"id_boleto": ev.get("id_boleto"), "valor_pago": ev.get("valor_pago"), **kw}
        return q.insert(campos).execute().data[0]["id"]
    def fechar_linha(self, log_id, **kw):
        if not log_id: return
        if kw.get("disponivel_depois") is not None and kw.get("disponivel_antes") is not None:
            kw["delta"] = round(kw["disponivel_depois"] - kw["disponivel_antes"], 2)
        LINHAS.setdefault(log_id, {}).update(kw)
    def fase(self, *a, **k): pass
    def evento_json(self, **kv): self.eventos.append(kv)

PENDENCIA = {}     # id do evento -> processado?

# ── Dublês das funções que tocam o mundo ────────────────────────────────────
DISPONIVEL = [0.0]; TOTAL = [0.0]
R.nome_do_cliente = lambda sb, cnpj: "CLIENTE TESTE"
R.resolver_cliente = lambda page, cnpj: ("424525", "999")
R.abrir_modal_limite = lambda page, e, c: None
R.ler_limites = lambda page: (DISPONIVEL[0], TOTAL[0])
R.fechar_modal = lambda page: None
R.gravar_espelho = lambda *a, **k: None
R._reler_limites_silencioso = lambda page: (DISPONIVEL[0], TOTAL[0])
def _escrever(page, novo, esperado_total):
    ESCRITAS.append({"novo": round(novo, 2), "total": esperado_total})
    DISPONIVEL[0] = novo
    return f"disponivel gravado em {novo}"
R.escrever_disponivel = _escrever

def ev(bid, valor, nf, eid=None):
    return {"id": eid or f"e{bid}", "id_boleto": str(bid), "cnpj": "01894968000171",
            "referencia": nf, "valor_pago": valor, "data_pagamento": "2026-08-20"}

def reset(disp, total):
    ESCRITAS.clear(); CARIMBOS.clear(); LINHAS.clear(); PENDENCIA.clear()
    DISPONIVEL[0] = disp; TOTAL[0] = total

falhas = []
def check(nome, cond, detalhe=""):
    print(("  OK   " if cond else "  FALHOU ") + nome + (f" -- {detalhe}" if detalhe and not cond else ""))
    if not cond: falhas.append(nome)

# ── 1. quatro boletos, uma escrita ─────────────────────────────────────────
print("\n1) 4 boletos do mesmo cliente -> 1 escrita da soma")
reset(2000.0, 15000.0)
grupo = [ev(1, 1000, "101"), ev(2, 2000, "102"), ev(3, 500, "103"), ev(4, 1500, "104")]
res, usados, desc = R.processar_grupo(None, FakeSB(), grupo, True, FakeAud())
check("uma escrita so", len(ESCRITAS) == 1, f"{len(ESCRITAS)} escritas")
check("somou 5.000 sobre 2.000 = 7.000", ESCRITAS[0]["novo"] == 7000.0, str(ESCRITAS))
check("4 boletos usados", len(usados) == 4 and not desc)
check("nao mexeu no teto", ESCRITAS[0]["total"] == 15000.0)

# ── 2. delta em UMA linha ──────────────────────────────────────────────────
print("\n2) delta so na linha principal (somar a coluna nao conta 4x)")
com_delta = [i for i, c in LINHAS.items() if c.get("delta") is not None]
check("1 linha com delta", len(com_delta) == 1, f"{len(com_delta)} linhas")
check("delta = 5.000", LINHAS[com_delta[0]]["delta"] == 5000.0 if com_delta else False)
escreveu = [i for i, c in LINHAS.items() if c.get("escreveu") is True]
check("1 linha com escreveu=true", len(escreveu) == 1, f"{len(escreveu)}")
check("4 linhas de auditoria (1 por boleto)", len(LINHAS) == 4, f"{len(LINHAS)}")
check("valor_pago soma 5.000 nas 4 linhas",
      round(sum(c.get("valor_pago") or 0 for c in LINHAS.values()), 2) == 5000.0)

# ── 3. valor zerado nao derruba o grupo ────────────────────────────────────
print("\n3) boleto zerado sai da soma e os bons continuam")
reset(1000.0, 10000.0)
grupo = [ev(5, 0, "105"), ev(6, 800, "106"), ev(7, 200, "107")]
res, usados, desc = R.processar_grupo(None, FakeSB(), grupo, True, FakeAud())
check("uma escrita", len(ESCRITAS) == 1)
check("somou 1.000 (so os validos)", ESCRITAS[0]["novo"] == 2000.0, str(ESCRITAS))
check("2 usados, 1 descartado", len(usados) == 2 and len(desc) == 1)
check("o descartado e o zerado", desc[0][0]["id_boleto"] == "5")

# ── 4. dry-run nao escreve ─────────────────────────────────────────────────
print("\n4) dry-run: um calculo por cliente, nenhuma escrita")
reset(500.0, 9000.0)
grupo = [ev(8, 100, "108"), ev(9, 250, "109")]
res, usados, desc = R.processar_grupo(None, FakeSB(), grupo, False, FakeAud())
check("nenhuma escrita", len(ESCRITAS) == 0)
check("nenhum carimbo na fila", len(CARIMBOS) == 0)
check("previu 850", "850" in res.replace(".", "").replace(",00", "") or "850,00" in res, res)
com_delta = [i for i, c in LINHAS.items() if c.get("delta") is not None]
check("1 linha com o numero do ensaio", len(com_delta) == 1, f"{len(com_delta)}")

# ── 5. boleto consumido por outra rodada sai da soma ───────────────────────
print("\n5) outra rodada consumiu 1 dos 3 -> soma recalculada")
reset(3000.0, 20000.0)
grupo = [ev(10, 1000, "110"), ev(11, 2000, "111"), ev(12, 3000, "112")]
PENDENCIA["e11"] = True          # ja processado
res, usados, desc = R.processar_grupo(None, FakeSB(), grupo, True, FakeAud())
check("uma escrita", len(ESCRITAS) == 1)
check("somou 4.000 (sem o de 2.000)", ESCRITAS[0]["novo"] == 7000.0, str(ESCRITAS))
check("2 usados", len(usados) == 2 and {e["id_boleto"] for e in usados} == {"10", "12"})

# ── 6. clamp no teto ───────────────────────────────────────────────────────
print("\n6) soma acima do teto e limitada ao teto")
reset(9000.0, 10000.0)
grupo = [ev(13, 800, "113"), ev(14, 900, "114")]
res, usados, desc = R.processar_grupo(None, FakeSB(), grupo, True, FakeAud())
check("escreveu exatamente o teto", ESCRITAS[0]["novo"] == 10000.0, str(ESCRITAS))
check("avisou do teto", "teto" in res, res)

# ── 7. carimbo em lote: uma chamada, N ids ─────────────────────────────────
print("\n7) carimbo da fila em UMA chamada")
reset(1000.0, 50000.0)
grupo = [ev(15, 100, "115"), ev(16, 200, "116"), ev(17, 300, "117")]
res, usados, desc = R.processar_grupo(None, FakeSB(), grupo, True, FakeAud())
R.marcar_processados(FakeSB(), usados, res)
check("1 chamada de carimbo", len(CARIMBOS) == 1, f"{len(CARIMBOS)}")
check("com os 3 ids", CARIMBOS and sorted(CARIMBOS[0]["ids"]) == ["e15", "e16", "e17"])

print("\n" + ("=== TODOS OS TESTES PASSARAM ===" if not falhas
              else f"=== {len(falhas)} FALHA(S): {falhas} ==="))
sys.exit(1 if falhas else 0)
