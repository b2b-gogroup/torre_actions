"""Carrega `.env.local` e executa um script -- para rodar a automacao LOCALMENTE.

    python ferramentas/_env.py ../../.env.local decisor.py [--dry-run]

POR QUE ISTO EXISTE
-------------------
No GitHub Actions os segredos chegam por `env:` no workflow. Na maquina do Italo eles
estao no `.env.local` do repo, e `source`-ar aquilo no bash NAO funciona: o
`CADASTRO_RCA_SHEETS_SERVICE_ACCOUNT_JSON` e um JSON inteiro numa linha so, com
aspas e chaves, e o shell tenta executar pedacos dele ("service_account: command not
found", 11/set/2026). Este runner parseia linha a linha em Python e nao expande nada.

⚠️ `runpy.run_path` NAO coloca o diretorio do script no `sys.path` -- sem o ajuste
abaixo, `escritor.py` morre em `ModuleNotFoundError: mercos_login`.

⚠️ Nao sobrescreve variavel que ja esteja no ambiente (`setdefault`): permite passar
MERCOS_EMAIL/MERCOS_SENHA por fora sem editar o arquivo.
"""

import os
import pathlib
import runpy
import sys


def carregar(env_path: pathlib.Path) -> int:
    n = 0
    for linha in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        linha = linha.rstrip("\r")
        if not linha or linha.lstrip().startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave = chave.strip()
        if (valor.startswith('"') and valor.endswith('"')) or (valor.startswith("'") and valor.endswith("'")):
            valor = valor[1:-1]
        os.environ.setdefault(chave, valor)
        n += 1
    # A Torre (Next) usa NEXT_PUBLIC_SUPABASE_URL; os robos usam SUPABASE_URL.
    if "SUPABASE_URL" not in os.environ and "NEXT_PUBLIC_SUPABASE_URL" in os.environ:
        os.environ["SUPABASE_URL"] = os.environ["NEXT_PUBLIC_SUPABASE_URL"]
    return n


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    env_path = pathlib.Path(sys.argv[1])
    if not env_path.exists():
        raise SystemExit(f"nao achei {env_path}")
    carregar(env_path)

    alvo = pathlib.Path(sys.argv[2]).resolve()
    # sys.path: o proprio diretorio do alvo E a pasta do pipeline (um nivel acima,
    # quando o alvo esta em ferramentas/), para achar mercos_ui / mercos_login.
    for p in (alvo.parent, alvo.parent.parent):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    sys.argv = [str(alvo)] + sys.argv[3:]
    runpy.run_path(str(alvo), run_name="__main__")


if __name__ == "__main__":
    main()
