"""Escreve o arquivo de CNPJs que a varredura deve procurar no Mercos.

    python ferramentas/_env.py ../../.env.local ferramentas/alvos_varredura.py alvos.txt [cnpjs_extra_csv]

Sem o CSV extra, os alvos sao os do ledger em `aguardando_mercos` acima do
CHAMADO_MINIMO -- exatamente a fila que a aba da Torre mostra como "Aguardando
Mercos". Com o CSV, os dois conjuntos sao unidos (selecao manual continua
podendo furar o corte de chamado, mesmo criterio de `escritor.py --cnpjs`).

⚠️ Por que existe: com o middleware Protheus->Mercos parado (desde 09/set/2026)
o portao do decisor nunca confirma sozinho, e a varredura -- a 2a fonte -- era
rodada a mao com a lista digitada. Lista digitada envelhece; esta sai do ledger.
"""
import os
import re
import sys

from supabase import create_client

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _chamado_minimo() -> int:
    """Le `CHAMADO_MINIMO` do decisor SEM importar o modulo.

    ⚠️ `import decisor` executa os `os.environ[...]` do topo dele e exigiria
    METABASE_URL/API_KEY num passo que so precisa falar com o Supabase -- foi
    exatamente assim que a 1a rodada deste workflow morreu com
    `KeyError: 'METABASE_URL'`. Copiar o numero tambem nao serve: o corte ja
    vive em 3 arquivos e subir num so faz a Torre e o pipeline discordarem
    sobre o que existe.

    Falha ALTO se o padrao sumir -- default silencioso aqui varreria a fila
    inteira ou nenhuma, sem ninguem perceber."""
    caminho = os.path.join(BASE_DIR, "decisor.py")
    with open(caminho, encoding="utf-8") as fh:
        achados = re.findall(r"^CHAMADO_MINIMO\s*=\s*(\d+)", fh.read(), re.M)
    if len(achados) != 1:
        raise SystemExit(
            f"[alvos] esperava 1 definicao de CHAMADO_MINIMO em {caminho}, achei "
            f"{len(achados)}. Ajuste o padrao aqui antes de rodar."
        )
    return int(achados[0])


CHAMADO_MINIMO = _chamado_minimo()


def _acima_do_corte(valor) -> bool:
    try:
        return int(str(valor).strip()) >= CHAMADO_MINIMO
    except (ValueError, TypeError):
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    saida = sys.argv[1]
    extra = sys.argv[2] if len(sys.argv) > 2 else ""

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    linhas = (sb.table("cadastro_rca_mercos_processado")
              .select("cnpj,chamado_goservice,status")
              .eq("status", "aguardando_mercos")
              .execute().data or [])

    alvos: list[str] = []
    for r in linhas:
        if not _acima_do_corte(r.get("chamado_goservice")):
            continue
        cnpj = re.sub(r"\D", "", r.get("cnpj") or "")
        if len(cnpj) == 14:
            alvos.append(cnpj)
    do_ledger = len(set(alvos))

    manuais = 0
    for pedaco in extra.split(","):
        cnpj = re.sub(r"\D", "", pedaco)
        # ⚠️ Nada de zfill aqui: CNPJ curto na selecao manual e erro de digitacao,
        # e completar com zero a esquerda inventaria um documento que ninguem pediu
        # (a mesma armadilha do LPAD cego que virou CPF em CNPJ inexistente).
        if len(cnpj) == 14:
            alvos.append(cnpj)
            manuais += 1
        elif cnpj:
            print(f"  [!] ignorando '{pedaco.strip()}': {len(cnpj)} digitos, esperava 14")

    alvos = list(dict.fromkeys(alvos))
    with open(saida, "w", encoding="utf-8") as fh:
        fh.write("# alvos da varredura -- gerados por alvos_varredura.py\n")
        for c in alvos:
            fh.write(c + "\n")

    print(f"{len(alvos)} alvo(s) em {saida} "
          f"(ledger aguardando_mercos: {do_ledger} | manuais validos: {manuais})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
