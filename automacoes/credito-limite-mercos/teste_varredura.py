"""Teste offline da VARREDURA — sem navegador, sem banco, sem Mercos.

O que prova (tudo que faltou na varredura de 22/set/2026, run 35704635785, que
levou 58 min para fazer 5 min de trabalho):

  1. queda de sessao NAO vira "cliente nao existe no Mercos" -- `SessaoDerrubada`
     sobe inteira e NADA e escrito no espelho. Era aqui o defeito: o
     `except RuntimeError` de `varrer_um` engolia a queda (SessaoDerrubada e
     subclasse de RuntimeError), carimbava o espelho e devolvia `PulaSemErro`
  2. ausencia PROVADA (`ClienteInexistente`) continua carimbando `nao_encontrado`
  3. leitura bem-sucedida DESMENTE a marca antiga (`nao_encontrado: False`) --
     sem isso o carimbo errado ficava para sempre, porque o upsert so toca as
     chaves que manda
  4. com a queda voltando a ser queda, o laco ABORTA a rodada em vez de varrer a
     fila inteira errando (e, com `CREDITO_VOLTAS_SESSAO=0`, aborta na hora)
  5. a rede de seguranca generica: N falhas SEGUIDAS param a rodada
  6. o contador de seguidas ZERA em qualquer desfecho bom -- falha espalhada ao
     longo da fila nao aborta nada

Roda com `python teste_varredura.py`. Nao toca em rede, banco nem Mercos.
"""
import sys, types, os, time
from pathlib import Path

# ── Dubles dos modulos externos, antes do import (mesmo padrao de teste_sessao) ──
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
import repositor as R  # noqa: E402

FALHAS: list[str] = []


def ok(cond, msg):
    print(("  OK   " if cond else "  FALHA ") + msg)
    if not cond:
        FALHAS.append(msg)


class SbEspiao:
    """Guarda o que foi mandado pro espelho, para o teste poder cobrar."""

    def __init__(self):
        self.upserts: list[dict] = []

    def table(self, _nome):
        q = types.SimpleNamespace()
        q.select = lambda *a, **k: q
        q.eq = lambda *a, **k: q
        q.limit = lambda *a, **k: q
        q.update = lambda *a, **k: q
        q.upsert = lambda payload, **k: (self.upserts.append(payload), q)[1]
        q.execute = lambda: types.SimpleNamespace(data=[])
        return q


class AudFake:
    rodada_id = "r-teste"
    quem = "teste"
    sb = None

    def __init__(self):
        self.fechou = {}

    def fase(self, *a, **k):
        pass

    def evento_json(self, **k):
        pass

    def abrir(self):
        return "r-teste"

    def fechar(self, **kw):
        self.fechou = kw


# ══════════════════════════════════════════════════════════════════════════
print("1) queda de sessao NAO vira 'nao existe no Mercos'")
# ⚠️ ESTE E O TESTE DO BUG DE 22/set. Antes do conserto ele falhava nos dois
# pontos: `varrer_um` devolvia `PulaSemErro` (e o laco seguia em frente) E
# escrevia `nao_encontrado: True` no espelho -- uma afirmacao falsa sobre um
# cliente que ninguem chegou a procurar.
sb = SbEspiao()
R.resolver_cliente = lambda page, cnpj: (_ for _ in ()).throw(
    R.SessaoDerrubada("a conta foi usada em outro computador"))
try:
    R.varrer_um(None, sb, {"cnpj": "11111111111111", "nome": "FULANO"}, AudFake())
    subiu = None
except BaseException as e:  # noqa: BLE001 -- o teste e sobre QUAL excecao sobe
    subiu = e
ok(isinstance(subiu, R.SessaoDerrubada), f"subiu SessaoDerrubada (subiu {type(subiu).__name__})")
ok(not isinstance(subiu, R.PulaSemErro), "NAO virou PulaSemErro (era o defeito)")
ok(sb.upserts == [], f"nao escreveu nada no espelho (escreveu {len(sb.upserts)})")

# ══════════════════════════════════════════════════════════════════════════
print("\n2) ausencia PROVADA continua carimbando")
sb = SbEspiao()
R.resolver_cliente = lambda page, cnpj: (_ for _ in ()).throw(
    R.ClienteInexistente(f"CNPJ {cnpj} nao foi encontrado em nenhuma empresa"))
try:
    R.varrer_um(None, sb, {"cnpj": "22222222222222", "nome": "BELTRANO"}, AudFake())
    subiu = None
except BaseException as e:  # noqa: BLE001
    subiu = e
ok(isinstance(subiu, R.PulaSemErro), f"virou PulaSemErro (subiu {type(subiu).__name__})")
ok(len(sb.upserts) == 1 and sb.upserts[0].get("nao_encontrado") is True,
   f"carimbou nao_encontrado (upserts={sb.upserts})")

# ══════════════════════════════════════════════════════════════════════════
print("\n3) leitura boa DESMENTE a marca antiga")
# ⚠️ Segundo bug, independente do primeiro: o upsert so toca as chaves que manda,
# entao sem `nao_encontrado: False` aqui um carimbo errado ficava eterno -- 299
# linhas diziam "nao existe no Mercos" carregando o id do proprio Mercos.
sb = SbEspiao()
R.gravar_espelho(sb, cnpj="33333333333333", empresa_id="424525", cliente_id="9",
                 nome="CICRANO", disponivel=1000, total=5000, origem="varredura",
                 rodada_id="r-teste")
ok(len(sb.upserts) == 1 and sb.upserts[0].get("nao_encontrado") is False,
   f"sucesso grava nao_encontrado=False (upserts={sb.upserts})")

# ══════════════════════════════════════════════════════════════════════════
# Andaimes para rodar o laco de `main()` sem navegador nem banco.
class FakePage:
    url = "https://app.mercos.com/424525/guia_inicial/"

    def close(self, *a, **k):
        pass


class CtxPW:
    def __enter__(self):
        pagina = FakePage()
        return types.SimpleNamespace(chromium=types.SimpleNamespace(
            launch=lambda **k: types.SimpleNamespace(
                new_page=lambda: pagina, close=lambda: None)))

    def __exit__(self, *a):
        return False


def montar_laco(varrer_fake, alvos, voltas=0):
    R.VOLTAS_POR_RODADA = voltas
    R._COMECOU_EM = time.time()
    aud = AudFake()
    R.varrer_um = varrer_fake
    R.fila_varredura = lambda *a, **k: alvos
    R.login_mercos = lambda *a, **k: None
    R.conferir_credencial_gmail = lambda *a, **k: None
    R.conferir_backfill = lambda sb: None
    R.create_client = lambda *a, **k: SbEspiao()
    R.avisar_falha = lambda **k: False
    R.destinatarios = lambda: ["teste@exemplo.com"]
    R.Auditoria = lambda *a, **k: aud
    R.sync_playwright = lambda: CtxPW()
    return aud


ALVOS = [{"cnpj": f"{i:014d}", "nome": f"CLIENTE {i}", "prioridade": 9} for i in range(1, 31)]

print("\n4) queda de sessao ABORTA a rodada em vez de varrer errando")
vistos: list[str] = []


def varrer_cai_no_terceiro(page, sb, alvo, aud):
    vistos.append(alvo["cnpj"])
    if len(vistos) >= 3:
        raise R.SessaoDerrubada("a conta foi usada em outro computador")
    return f"{alvo['nome']}: disponivel 1,00 de 2,00"


aud = montar_laco(varrer_cai_no_terceiro, ALVOS, voltas=0)
sys.argv = ["repositor.py", "--varredura", "--limite", "30"]
R.main()
ok(len(vistos) == 3, f"parou no 3o cliente, nao varreu os 30 (viu {len(vistos)})")
ok(aud.fechou.get("abortou_por"), f"registrou o abort ({aud.fechou.get('abortou_por')})")
ok(aud.fechou.get("ok") is False, "rodada fechou como NAO-ok (e o que dispara o e-mail)")

# ══════════════════════════════════════════════════════════════════════════
print("\n5) N falhas SEGUIDAS param a rodada (rede de seguranca generica)")
# ⚠️ Nao substitui o item 4: existe para o caso em que a tela do Mercos muda e
# TODO cliente passa a dar erro generico -- sem isso o robo varre a fila inteira
# errando, que foi o custo de 22/set (52 min).
vistos.clear()
R.FALHAS_SEGUIDAS_LIMITE = 5


def varrer_sempre_falha(page, sb, alvo, aud):
    vistos.append(alvo["cnpj"])
    raise RuntimeError("seletor sumiu da tela")


aud = montar_laco(varrer_sempre_falha, ALVOS, voltas=0)
sys.argv = ["repositor.py", "--varredura", "--limite", "30"]
R.main()
ok(len(vistos) == 5, f"parou em 5 falhas seguidas, nao nos 30 (viu {len(vistos)})")
ok("seguidos falharam" in (aud.fechou.get("abortou_por") or ""),
   f"o motivo diz o que aconteceu ({aud.fechou.get('abortou_por')})")

# ══════════════════════════════════════════════════════════════════════════
print("\n6) falha ESPALHADA nao aborta -- o alvo e a sequencia")
vistos.clear()
R.FALHAS_SEGUIDAS_LIMITE = 5


def varrer_falha_alternado(page, sb, alvo, aud):
    vistos.append(alvo["cnpj"])
    if len(vistos) % 2 == 0:
        raise RuntimeError("tela lenta")
    return f"{alvo['nome']}: disponivel 1,00 de 2,00"


aud = montar_laco(varrer_falha_alternado, ALVOS, voltas=0)
sys.argv = ["repositor.py", "--varredura", "--limite", "30"]
R.main()
ok(len(vistos) == 30, f"varreu a fila inteira (viu {len(vistos)})")
ok(not aud.fechou.get("abortou_por"), "nao abortou com 15 falhas espalhadas")

print()
print("=== TODOS OS TESTES PASSARAM ===" if not FALHAS
      else f"=== {len(FALHAS)} FALHA(S): " + " · ".join(FALHAS) + " ===")
sys.exit(1 if FALHAS else 0)
