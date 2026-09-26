"""Teste offline da QUEDA DE SESSAO e da volta — sem navegador, sem banco.

O que prova (tudo que faltou na rodada do cron de 25/ago/2026, run 32908295684):
  1. timeout no campo de busca com a TELA DE LOGIN na frente = sessao caiu,
     e vira `SessaoDerrubada` -- nao `playwright.TimeoutError` solto, que era
     o que escapava do laco e matava a rodada inteira
  2. o mesmo timeout com a sessao VIVA nao vira queda: recarrega e segue
  3. campo que nunca aparece com sessao viva vira erro DESTE cliente
     (RuntimeError), nao da rodada
  4. a volta ESPERA os 15 min antes de reentrar, e nao reentra na hora
  5. a volta e CANCELADA quando nao cabe na janela do Actions (e nao dorme)
  6. o laco da fila REFAZ o cliente que caiu e termina o resto da fila
  7. o e-mail de crash diz quanto JA tinha sido escrito, em vez de "nada"

Roda com `python teste_sessao.py`. Nao toca em rede, banco nem Mercos.
"""
import sys, types, os, time
from pathlib import Path

# ── Dubles dos modulos externos, antes do import (mesmo padrao de teste_grupo) ──
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

os.environ.setdefault("SUPABASE_URL", "http://x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
os.environ.setdefault("MERCOS_EMAIL", "robo@exemplo.com")
os.environ.setdefault("MERCOS_SENHA", "x")
os.environ.setdefault("GMAIL_USER", "x"); os.environ.setdefault("GMAIL_SENHA", "x")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cadastro-rca-mercos"))
import repositor as R
import mercos_ui as UI

FALHAS = []


def ok(cond, oq):
    print(f"  {'OK  ' if cond else 'FALHOU'} {oq}")
    if not cond:
        FALHAS.append(oq)


SENHA_SEL = '[name="senha"]'


class FakePage:
    """Pagina de mentira. `campo_em` diz em qual tentativa o campo de busca
    aparece (0 = nunca); `login` diz se a tela de login esta na frente."""

    def __init__(self, campo_em=1, login=False, links=None):
        self.campo_em, self.login, self.links = campo_em, login, links or []
        self.tentativas = 0
        self.idas = 0

    def goto(self, *a, **k):
        self.idas += 1

    def wait_for_selector(self, sel, **k):
        if sel == "#id_nome_rapido":
            self.tentativas += 1
            if self.campo_em and self.tentativas >= self.campo_em:
                return True
            raise pwapi.TimeoutError("Timeout 20000ms exceeded.")
        return True

    def fill(self, *a, **k):
        pass

    def content(self):
        return "<form>senha</form>" if self.login else "<div>clientes</div>"

    @property
    def keyboard(self):
        return types.SimpleNamespace(press=lambda *a, **k: None)

    def locator(self, sel):
        if sel == SENHA_SEL:
            return types.SimpleNamespace(count=lambda: 1 if self.login else 0)
        alvo = self.links
        return types.SimpleNamespace(
            count=lambda: len(alvo),
            nth=lambda i: types.SimpleNamespace(get_attribute=lambda _a: alvo[i]))


class AudFake:
    rodada_id = "r1"
    quem = "teste"
    origem = "teste"
    run_url = None
    modo = "dry-run"

    def __init__(self):
        self.eventos = []

    def evento_json(self, **kv):
        self.eventos.append(kv)

    def fase(self, *a, **k):
        pass


print("\n1) timeout com a tela de login na frente = sessao caiu")
p = FakePage(campo_em=0, login=True)
try:
    UI.buscar_cliente_id_por_cnpj(p, "424525", "01894968000171", log=lambda *_: None)
    ok(False, "levantou SessaoCaiu")
except UI.SessaoCaiu:
    ok(True, "levantou SessaoCaiu (e nao TimeoutError, o bug de 25/ago)")
except pwapi.TimeoutError:
    ok(False, "levantou SessaoCaiu (veio TimeoutError -- o bug de 25/ago voltou)")

print("\n2) timeout com sessao viva: recarrega e segue")
p = FakePage(campo_em=2, login=False, links=["/424525/clientes/61221805/"])
cid = UI.buscar_cliente_id_por_cnpj(p, "424525", "01894968000171", log=lambda *_: None)
ok(cid == "61221805", f"achou o cliente na 2a tentativa (veio {cid})")
ok(p.idas == 2, f"recarregou a pagina uma vez (idas={p.idas})")

print("\n3) campo que nunca aparece com sessao viva = erro DESTE cliente")
p = FakePage(campo_em=0, login=False)
try:
    UI.buscar_cliente_id_por_cnpj(p, "424525", "01894968000171", log=lambda *_: None)
    ok(False, "levantou erro")
except UI.SessaoCaiu:
    ok(False, "nao pode culpar a sessao: ela esta viva")
except RuntimeError as e:
    ok("campo de busca" in str(e), "RuntimeError nomeando o que faltou")

print("\n4) a volta espera 15 min antes de reentrar")
dormiu = []
# dorme de mentira: o teste mede a INTENCAO de esperar, nao o relogio
R.time = types.SimpleNamespace(sleep=lambda s: dormiu.append(s), time=time.time)
logins = []
R.login_mercos = lambda *a, **k: logins.append(1)
aud = AudFake()
R._COMECOU_EM = time.time()
voltou = R.reconquistar_sessao(FakePage(), aud, "a conta foi usada em outro lugar", 1, 2)
ok(voltou is True, "voltou")
ok(dormiu == [900], f"esperou 15 min antes de reentrar (dormiu {dormiu}s)")
ok(logins == [1], "logou de novo exatamente uma vez")
ok(any(e.get("etapa") == "sessao_voltou" for e in aud.eventos), "gravou a volta na auditoria")

print("\n5) sem janela pra esperar, nao dorme e nao volta")
dormiu.clear()
logins.clear()
aud = AudFake()
R._COMECOU_EM = time.time() - (R.JANELA_MIN * 60 - 300)   # sobram 5 min de janela
voltou = R.reconquistar_sessao(FakePage(), aud, "caiu", 1, 2)
ok(voltou is False, "desistiu da volta")
ok(dormiu == [], "NAO dormiu (seria morto pelo Actions no meio do sono)")
ok(any(e.get("etapa") == "sessao_sem_janela" for e in aud.eventos),
   "declarou o motivo na auditoria")

print("\n6) o laco refaz o cliente que caiu e termina a fila")
dormiu.clear()
R._COMECOU_EM = time.time()
FILA = [{"id": f"e{i}", "id_boleto": f"b{i}", "cnpj": c, "valor_pago": 100.0}
        for i, c in enumerate(["11111111111111", "22222222222222", "33333333333333"], start=1)]
vistos = []
ja_caiu = {"sim": False}


def processar_grupo_fake(page, sb, grupo, aplicar, aud):
    cnpj = grupo[0]["cnpj"]
    if cnpj == "22222222222222" and not ja_caiu["sim"]:
        ja_caiu["sim"] = True
        vistos.append(cnpj + " (caiu)")
        raise R.SessaoDerrubada("a conta foi usada em outro computador")
    vistos.append(cnpj)
    return (f"ok {cnpj}", grupo, [])


class AudMain(AudFake):
    def abrir(self):
        return "r1"

    def fechar(self, **kw):
        self.fechou = kw


class CtxPW:
    def __enter__(self):
        pagina = FakePage()
        return types.SimpleNamespace(chromium=types.SimpleNamespace(
            launch=lambda **k: types.SimpleNamespace(
                new_page=lambda: pagina, close=lambda: None)))

    def __exit__(self, *a):
        return False


AUD_MAIN = AudMain()
R.processar_grupo = processar_grupo_fake
R.carregar_fila = lambda *a, **k: FILA
R.emperrados_da_fila = lambda sb: {}
R.resumir_emperrados = lambda *a, **k: None
R.marcar_processados = lambda *a, **k: None
R.marcar_processado = lambda *a, **k: None
R.conferir_credencial_gmail = lambda *a, **k: None
R.conferir_backfill = lambda sb: None
R.create_client = lambda *a, **k: types.SimpleNamespace(table=lambda t: None)
R.avisar_falha = lambda **k: False
R.destinatarios = lambda: ["teste@exemplo.com"]
R.Auditoria = lambda *a, **k: AUD_MAIN
R.sync_playwright = lambda: CtxPW()
sys.argv = ["repositor.py", "--limite", "10"]
R.main()
ok(vistos == ["11111111111111", "22222222222222 (caiu)", "22222222222222", "33333333333333"],
   f"refez o que caiu e seguiu a fila -> {vistos}")
ok(getattr(AUD_MAIN, "fechou", {}).get("abortou_por") is None, "nao abortou a rodada")
ok(dormiu == [900], f"esperou os 15 min uma vez (dormiu {dormiu})")

print("\n7) e-mail de crash diz o que JA tinha sido escrito")


class SbCrash:
    def table(self, t):
        q = types.SimpleNamespace()
        q.select = lambda *a, **k: q
        q.eq = lambda *a, **k: q
        q.update = lambda *a, **k: q
        q.execute = lambda: types.SimpleNamespace(
            data=[{"delta": 1798.74, "escreveu": True}, {"delta": 62006.01, "escreveu": True}])
        return q


aud = AudFake()
aud.sb = SbCrash()
frase, n, valor = R._fechar_rodada_no_crash(aud, RuntimeError("morri"))
ok(n == 2 and abs(valor - 63804.75) < 0.01, f"contou o que foi escrito ({n} / {valor})")
ok("JA tinham sido repostos" in frase, "a frase diz que houve escrita")
ok("Nada foi escrito" not in frase, "NAO repete a mentira de 25/ago")

print()
print("=== TODOS OS TESTES PASSARAM ===" if not FALHAS
      else f"=== {len(FALHAS)} FALHA(S): " + " · ".join(FALHAS) + " ===")
sys.exit(1 if FALHAS else 0)
