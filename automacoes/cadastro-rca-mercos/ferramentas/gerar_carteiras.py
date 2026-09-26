"""Gera os arquivos de CNPJ por inside sales, a partir da Torre.

    python ferramentas/_env.py ../../.env.local ferramentas/gerar_carteiras.py [nome...]

Sem argumento, gera para todos os inside sales ativos com carteira.

A LISTA E A UNIAO DE TRES FONTES, e nao so a carteira formal
------------------------------------------------------------
Medido em 16/set/2026: a Mirella tem 7 clientes em `dim_cliente.vendedor_id` e lancou
45 CNPJs no Mercos desde 01/08. Usar so a carteira formal deixaria sem acesso justamente
quem ela atende todo dia. As tres fontes respondem perguntas diferentes:

  dim_cliente.vendedor_id              de quem o cliente E (carteira formal)
  mercos_vendas_detalhadas.vendedor    quem LANCOU o pedido
  fato_pedidos.vendedor_id             dono resolvido pelo trigger de override

⚠️ CORTA SEMPRE o placeholder 00000000000000 ("Cliente nao identificado"), que e
sentinela de ausencia e nunca e um cliente -- em 27/jul/2026 esse CNPJ chegou a ter DONO
em `carteira_vigencia` e mandava toda venda de cliente nao cadastrado pro vendedor
errado. Tambem corta doc que nao tenha 14 digitos (pessoa fisica nao entra: a PK de
`dim_cliente` e CNPJ).

⚠️ CONFLITO (dono na Torre e outro inside sales) ENTRA, por decisao do usuario em
16/set/2026, e o cabecalho do arquivo NOMEIA cada um: `habilitar_` so adiciona, entao o
cliente fica visivel para os dois no Mercos. Foi escolha consciente, e o arquivo declara
para nao virar descuido silencioso seis meses depois.

⚠️ `range()` EXPLICITO em toda leitura: o PostgREST corta em 1.000 linhas EM SILENCIO, e
uma carteira truncada nao da erro nenhum -- so deixa cliente sem acesso.
"""

import os
import re
import sys
from collections import defaultdict

from supabase import create_client

DESDE_MERCOS = "2026-08-01"
DESDE_FATO = 20260801
TETO = 200_000
SENTINELA = "VENDEDOR NÃO MAPEADO (SISTEMA)"


def _slug(nome: str) -> str:
    s = nome.lower()
    for de, para in (("á", "a"), ("ã", "a"), ("â", "a"), ("é", "e"), ("ê", "e"),
                     ("í", "i"), ("ó", "o"), ("ô", "o"), ("ú", "u"), ("ç", "c")):
        s = s.replace(de, para)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _dig(v) -> str:
    return re.sub(r"\D", "", v or "")


def main() -> int:
    sb = create_client(os.environ["NEXT_PUBLIC_SUPABASE_URL"],
                       os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    pedidos = [a for a in sys.argv[1:]]

    vend = sb.table("dim_vendedor").select("id,nome,tipo,ativo") \
             .eq("tipo", "inside_sales").eq("ativo", True).range(0, TETO).execute().data
    if pedidos:
        vend = [v for v in vend if v["nome"] in pedidos]
        faltou = set(pedidos) - {v["nome"] for v in vend}
        if faltou:
            # Nome que nao casa e erro de digitacao, nao "vendedor sem carteira" --
            # seguir em silencio geraria arquivo faltando gente.
            raise SystemExit(f"nao achei em dim_vendedor (inside_sales ativo): {sorted(faltou)}")
    por_id = {v["id"]: v["nome"] for v in vend}
    nomes = set(por_id.values())

    # dono atual de cada CNPJ, para NOMEAR o conflito no cabecalho
    cli = sb.table("dim_cliente").select("cnpj,vendedor_id,nome_cliente,ativo") \
            .range(0, TETO).execute().data
    dono = {}
    for c in cli:
        if c.get("ativo"):
            dono[(c["cnpj"] or "").strip()] = (por_id.get(c["vendedor_id"]) or
                                               ("_outro_" if c["vendedor_id"] else None))
    nome_cli = {(c["cnpj"] or "").strip(): c.get("nome_cliente") or "" for c in cli}
    dono_real = {}
    todos_vend = {v["id"]: v["nome"] for v in
                  sb.table("dim_vendedor").select("id,nome").range(0, TETO).execute().data}
    for c in cli:
        if c.get("ativo"):
            dono_real[(c["cnpj"] or "").strip()] = todos_vend.get(c["vendedor_id"])

    uni: dict[str, set[str]] = defaultdict(set)
    formal: dict[str, int] = defaultdict(int)

    for c in cli:
        n = por_id.get(c["vendedor_id"])
        if n and c.get("ativo"):
            uni[n].add((c["cnpj"] or "").strip())
            formal[n] += 1

    for m in sb.table("mercos_vendas_detalhadas").select("cnpj_cpf,vendedor,data_emissao") \
               .gte("data_emissao", DESDE_MERCOS).range(0, TETO).execute().data:
        v = (m.get("vendedor") or "").strip()
        for n in nomes:
            if v.lower() == n.lower():
                uni[n].add(_dig(m.get("cnpj_cpf")))

    for f in sb.table("fato_pedidos").select("cliente_id,vendedor_id") \
               .gte("data_pedido_id", DESDE_FATO).eq("excluido", False) \
               .in_("vendedor_id", list(por_id)).range(0, TETO).execute().data:
        n = por_id.get(f["vendedor_id"])
        if n:
            uni[n].add((f["cliente_id"] or "").strip())

    os.makedirs("carteiras", exist_ok=True)
    for n in sorted(uni, key=lambda k: len(uni[k])):
        bons, placeholder, invalido = [], 0, 0
        for c in uni[n]:
            if re.fullmatch(r"0+", c):
                placeholder += 1
            elif len(c) != 14 or not c.isdigit():
                invalido += 1
            else:
                bons.append(c)
        bons.sort()

        conflitos = [(c, dono_real.get(c), nome_cli.get(c, "")) for c in bons
                     if dono_real.get(c) and dono_real[c] not in (n, SENTINELA)]

        cab = [f"# {n} -- {len(bons)} CNPJs. Gerado por ferramentas/gerar_carteiras.py.",
               "#",
               f"# Uniao de 3 fontes desde {DESDE_MERCOS} (a carteira formal sozinha da {formal[n]}):",
               "#   dim_cliente.vendedor_id | mercos_vendas_detalhadas.vendedor | fato_pedidos.vendedor_id",
               f"# Cortados: {placeholder} placeholder(s) 00000000000000, {invalido} doc(s) fora de 14 digitos.",
               "#"]
        if conflitos:
            cab += [f"# {len(conflitos)} em CONFLITO -- o dono na Torre e outro inside sales. ENTRAM por",
                    "# decisao do usuario em 16/set/2026: `habilitar_` so ADICIONA, entao o cliente fica",
                    "# visivel para os DOIS no Mercos. Foi escolha consciente, nao descuido:"]
            for c, d, nm in sorted(conflitos)[:40]:
                cab.append(f"#     {c}  {nm[:38]:<38} -> tambem {d}")
            if len(conflitos) > 40:
                cab.append(f"#     ... e mais {len(conflitos) - 40}")
            cab.append("#")
        cab.append("# Doc: docs/processos/mercos-limitar-carteira-inside-sales.md")

        arq = f"carteiras/cnpjs_{_slug(n)}.txt"
        with open(arq, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(cab) + "\n" + "\n".join(bons) + "\n")
        print(f"{arq}: {len(bons)} CNPJs (formal {formal[n]}, conflito {len(conflitos)}, "
              f"placeholder {placeholder}, invalido {invalido})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
