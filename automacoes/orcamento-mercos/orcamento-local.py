#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Roda o orcamentista NESTA MAQUINA, com as credenciais que ja vivem aqui.

Por que existe
--------------
O ciclo pelo GitHub Actions custa ~3 min por tentativa e gasta um login completo
com 2FA -- e o Mercos aceita UMA sessao por usuario, entao cada rodada derruba
quem estiver logado. Debugar seletor assim e caro.

Local e mais barato por um motivo especifico, do guia da automacao Mercos da
Torre (secao 3): o Mercos confia no IP/dispositivo, nao so no cookie. Desta
maquina as automacoes registram "2FA nao necessario" na esmagadora maioria das
execucoes. Sem 2FA, a volta cai de ~3 min para segundos.

🔴 CREDENCIAIS: sao LIDAS de `relatorio_mercos/config.py` -- o mesmo arquivo que
as automacoes da Torre ja usam nesta maquina -- e repassadas ao processo filho
por variavel de ambiente. Este script NUNCA imprime, copia ou grava os valores;
so diz quais chaves encontrou. Nada de credencial entra no repositorio.

Uso
---
    python automacoes/orcamento-mercos/orcamento-local.py --dry --cnpj 57639055000163 \
        --itens BB02027:12,BB02038:12 --desconto 29

    # com o navegador aberto na tela, que e o ponto de rodar local:
    python automacoes/orcamento-mercos/orcamento-local.py --dry --headful --cnpj ...

⚠️ As tasks agendadas desta maquina usam a MESMA conta do Mercos e disparam em
:00 e :30 de toda hora (Pipeline Gobeaute, Pedidos Sem Saldo). Rodar em cima
desses minutos derruba a sessao de uma das duas. O script avisa.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

# ⚠️ Este script morava em `scripts/` no repo antigo do Gira, e calculava a raiz
# subindo dois niveis. Na Torre ele vive AO LADO do robo -- subir dois niveis
# apontaria para `automacoes/automacoes/orcamento-mercos`, que nao existe, e o
# erro so apareceria na hora de rodar.
ROBO = Path(__file__).resolve().parent
RAIZ = ROBO.parent.parent
CONFIG_PADRAO = Path(r"C:\Users\Notebook\Documents\relatorio_mercos\config.py")

# De onde sai cada variavel: (nome no ambiente, nome no config.py)
DE_PARA = [
    ("MERCOS_EMAIL", "MERCOS_EMAIL"),
    ("MERCOS_SENHA", "MERCOS_SENHA"),
    ("GMAIL_USER", "GMAIL_USER"),
    ("GMAIL_SENHA", "GMAIL_SENHA"),
    ("SUPABASE_URL", "SUPABASE_URL"),
    ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY"),
    # A Evolution nao vive no config.py da Torre -- vem do .env deste repo.
    ("EVOLUTION_URL", "EVOLUTION_URL"),
    ("EVOLUTION_APIKEY", "EVOLUTION_APIKEY"),
    ("EVOLUTION_INSTANCIA", "EVOLUTION_INSTANCIA"),
    ("SUPABASE_BUCKET_ARQUIVOS", "SUPABASE_BUCKET_ARQUIVOS"),
]


def carregar_dotenv(caminho: Path) -> dict[str, str]:
    """Le o .env da raiz. Fica ACIMA do config.py na precedencia.

    🔴 Precisa ficar acima por um motivo concreto: em 19/set/2026 o
    `config.py` desta maquina apontava GMAIL_USER para b2b@gobeaute.com.br com
    um App Password JA REVOGADO -- o IMAP respondeu
    `[AUTHENTICATIONFAILED] Invalid credentials`. Quem recebe o 2FA do Mercos
    hoje e a caixa do italo.costa@. Sem essa precedencia, so daria para
    consertar editando o arquivo que as automacoes de producao usam.
    """
    valores: dict[str, str] = {}
    if not caminho.exists():
        return valores
    for linha in caminho.read_text(encoding="utf-8", errors="replace").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valores[chave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def carregar_config(caminho: Path):
    if not caminho.exists():
        sys.exit(
            f"nao achei {caminho}.\n"
            "Aponte outro com  ORCAMENTO_CONFIG=C:\\caminho\\para\\config.py"
        )
    spec = importlib.util.spec_from_file_location("config_mercos_local", caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(caminho.parent))  # o config importa vizinhos
    spec.loader.exec_module(modulo)
    return modulo


def main() -> int:
    # ⚠️ PRIMEIRA COISA, antes de qualquer print. O console do Windows e cp1252
    # e este script imprime "⚠️" no aviso de colisao de horario -- que e
    # justamente o que ele tem a dizer quando sao :00 ou :30. Reconfigurar
    # depois derrubava o script no proprio aviso (19/set/2026 01:30).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    config = carregar_config(Path(os.environ.get("ORCAMENTO_CONFIG", CONFIG_PADRAO)))

    dotenv = carregar_dotenv(RAIZ / ".env")

    ambiente = os.environ.copy()
    achadas, faltando = [], []
    for nome_env, nome_config in DE_PARA:
        # Precedencia: ambiente da sessao > .env da raiz > config.py da Torre.
        # Permite testar com outra conta sem editar o arquivo que as automacoes
        # de producao usam.
        if ambiente.get(nome_env):
            achadas.append(f"{nome_env} (ambiente)")
            continue
        if dotenv.get(nome_env):
            ambiente[nome_env] = dotenv[nome_env]
            achadas.append(f"{nome_env} (.env)")
            continue
        valor = getattr(config, nome_config, "")
        if valor:
            ambiente[nome_env] = str(valor)
            achadas.append(f"{nome_env} (config.py)")
        else:
            faltando.append(nome_env)

    print("credenciais:", ", ".join(achadas) or "nenhuma")
    if faltando:
        # EVOLUTION_* ausente nao impede a rodada -- so tira a imagem do QR.
        print("faltando   :", ", ".join(faltando))
    if not ambiente.get("MERCOS_SENHA"):
        return 2

    ambiente["MERCOS_MODULOS_DIR"] = str(ROBO / "vendor")
    ambiente["PYTHONUNBUFFERED"] = "1"
    # ⚠️ O robo escreve emoji e acento; o console do Windows e cp1252. Sem isto
    # ele MORRE no meio da rodada com UnicodeEncodeError -- aconteceu em
    # 19/set/2026 01:28, matando a rodada logo depois de selecionar o produto,
    # por causa de um "⚠️" numa linha de aviso. No Actions o ambiente ja e UTF-8.
    ambiente["PYTHONIOENCODING"] = "utf-8"
    ambiente["PYTHONUTF8"] = "1"
    ambiente.setdefault("MERCOS_EMPRESA_ID", "424525")

    agora = dt.datetime.now()
    if agora.minute in range(0, 4) or agora.minute in range(30, 34):
        print(
            f"⚠️ sao {agora:%H:%M} -- as tasks agendadas desta maquina (Pipeline "
            "Gobeaute, Pedidos Sem Saldo) disparam em :00 e :30 com a MESMA conta "
            "do Mercos. Uma vai derrubar a sessao da outra."
        )

    logs = ROBO / "logs"
    logs.mkdir(exist_ok=True)
    destino = logs / f"local-{agora:%H%M%S}.log"

    # Popen, nao run: com `run` a saida so aparece no fim e uma rodada de 3 min
    # fica muda. Aqui cada linha vai para a tela e para o arquivo na hora.
    proc = subprocess.Popen(
        [sys.executable, "orcamentista.py", *sys.argv[1:]],
        cwd=ROBO,
        env=ambiente,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    with destino.open("w", encoding="utf-8") as arquivo:
        for linha in proc.stdout:  # type: ignore[union-attr]
            print(linha, end="")
            arquivo.write(linha)
    proc.wait()
    print(f"\nlog: {destino}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
