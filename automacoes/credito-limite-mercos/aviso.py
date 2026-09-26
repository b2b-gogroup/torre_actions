"""
Aviso por e-mail quando a rodada do repositor falha.

Fecha a pendencia P3 de docs/kaique/README.md ("quem recebe o aviso quando o robo
falha"). Decisao do Kaique em 21/ago/2026: o aviso vai para o e-mail dele.

── POR QUE E-MAIL, E POR QUE NAO PRECISOU DE SECRET NOVO ────────────────────────
O robo ja recebe `GMAIL_USER` / `GMAIL_SENHA` -- um **App Password** do Google,
usado hoje para ler no IMAP o codigo de 2FA do login do Mercos. App Password do
Google vale para IMAP **e** para SMTP, entao o mesmo par que ja esta cadastrado no
workflow manda o e-mail. Zero secret novo = zero chance de a entrega quebrar por
alguem esquecer de cadastrar variavel (foi o que aconteceu com MERCOS_EMPRESAS,
ver o `or` defensivo no repositor).

⚠️ LIMITE HONESTO DESTE DESENHO: se a falha for a **propria credencial do Gmail**
(App Password revogado -- acontece quando a senha da conta muda, o 2SV e
reconfigurado ou o admin do Workspace bloqueia app password), o aviso NAO SAI. O
robo morre em segundos por `conferir_credencial_gmail` com a causa escrita no log,
e nesse caso o canal de aviso e o proprio Actions (a run fica vermelha). Nao ha
como contornar sem uma segunda credencial de outro provedor, o que seria um secret
novo -- exatamente o que este desenho evita. Registrado de propósito em vez de
deixar alguem descobrir no dia.

── QUANDO AVISA ─────────────────────────────────────────────────────────────────
So quando tem algo errado:
  · rodada ABORTADA (sessao do Mercos derrubada, auditoria fora do ar);
  · pagamento com ERRO (fica na fila, vai ser tentado de novo);
  · pagamento marcado CONFERIR NA MAO (saiu da fila SEM confirmacao -- e o caso
    mais grave, porque ninguem vai tropecar nele por acaso);
  · crash antes da fila (credencial do Mercos, Supabase fora).

Rodada limpa NAO gera e-mail, de propósito. Aviso que chega todo dia para dizer
"esta tudo bem" e aviso que ninguem le -- e no dia da falha de verdade ele parece
mais um.

⚠️ Dry-run com erro AVISA. Parece ruido, mas e o contrario: dry-run e justamente a
rodada que existe para descobrir problema ANTES de escrever no Mercos. Falha ali
descoberta uma semana depois nao serviu para nada.

── NUNCA DERRUBA A RODADA ───────────────────────────────────────────────────────
Toda falha de envio e engolida e vira linha de log. O e-mail e o aviso de uma
falha; se ele proprio falhar, o robo nao pode transformar isso numa segunda falha
e mudar o exit code -- o dinheiro ja mudou (ou nao) de lugar independente disso.
"""

import os
import smtplib
from email.message import EmailMessage

# Destinatario padrao: decisao do Kaique, 21/ago/2026.
# ⚠️ `or` em vez de `os.environ.get(k, default)`: variavel que EXISTE vazia (que e
# como `${{ vars.X }}` nao cadastrada chega no Actions) nao cai no default.
# ⚠️ MUDOU EM 17/set/2026, a pedido do usuario: o alerta de falha do robo vai para o Italo.
#    Achado no 1o desfazer real -- a variavel de repositorio `AVISO_PARA` esta VAZIA, entao quem
#    valia era este padrao, e o e-mail da falha foi parar no destinatario antigo. Padrao que
#    ninguem revisita e padrao que decide sozinho.
AVISO_PARA_PADRAO = "italo.costa@gobeaute.com.br"

SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 587)


def destinatarios() -> list[str]:
    """Lista de quem recebe. `AVISO_PARA` aceita varios separados por virgula --
    e o caminho para trocar de pessoa (ou virar um grupo) SEM mexer em codigo."""
    bruto = os.environ.get("AVISO_PARA") or AVISO_PARA_PADRAO
    return [e.strip() for e in bruto.split(",") if e.strip()]


def _enviar(assunto: str, corpo: str, log, html: str | None = None) -> bool:
    user = os.environ.get("GMAIL_USER")
    senha = os.environ.get("GMAIL_SENHA")
    para = destinatarios()

    if not user or not senha:
        log("[aviso] GMAIL_USER/GMAIL_SENHA ausentes -- o e-mail de falha NAO foi enviado.")
        return False
    if not para:
        log("[aviso] AVISO_PARA vazio -- ninguem para avisar. Nada enviado.")
        return False

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = f"Torre B2B - Robo de Credito <{user}>"
    msg["To"] = ", ".join(para)
    msg.set_content(corpo)
    if html:
        # texto puro fica como alternativa: cliente sem HTML e o log continuam legiveis
        msg.add_alternative(html, subtype="html")

    try:
        # timeout obrigatorio: SMTP sem timeout pendura o job ate o teto de 30min
        # do workflow, e o aviso de falha viraria uma segunda falha.
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(user, senha)
            s.send_message(msg)
    except Exception as e:  # nunca derruba a rodada -- ver o cabecalho
        log(f"[aviso] falhou ao enviar o e-mail de falha ({type(e).__name__}: {e}). "
            "A falha original segue registrada na auditoria e no log.")
        return False

    log(f"[aviso] e-mail de falha enviado para {', '.join(para)}")
    return True


def enviar_teste(log) -> bool:
    """Manda um e-mail de teste e nao toca em NADA (nem Mercos, nem fila).

    Existe porque a credencial do Gmail vive so como Secret do GitHub: nao da pra
    provar o canal da maquina de quem escreveu o codigo. Sem isto, a unica forma
    de descobrir que o aviso nao chega seria no dia da falha de verdade -- o pior
    dia possivel pra descobrir que o canal de aviso esta quebrado."""
    corpo = "\n".join([
        "Teste do canal de aviso do robo de credito.",
        "",
        "Se este e-mail chegou, o aviso de falha vai chegar tambem: e o mesmo",
        "caminho (App Password do Gmail -> SMTP) e o mesmo destinatario.",
        "",
        f"  destinatario(s) .. {', '.join(destinatarios())}",
        f"  remetente ........ {os.environ.get('GMAIL_USER')}",
        f"  servidor ......... {SMTP_HOST}:{SMTP_PORT}",
        "",
        "Nada foi lido nem escrito: nem no Mercos, nem na fila de pagamentos.",
        "",
        "-- Torre B2B, robo CreditoLimite-Mercos (modo testar-aviso)",
    ])
    return _enviar("[Robo de Credito] teste do canal de aviso", corpo, log)


def _br(v) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _doc(cnpj: str | None) -> str:
    d = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return cnpj or "—"


def _esc(t) -> str:
    return (str(t if t is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# ── O QUE CADA MODO FAZ (o e-mail antigo dizia "repoe o limite disponivel" ate no ──
# modo de liberacoes, que escreve o TETO -- e "repostos 1 (R$ 0,00)" la nao quer
# dizer nada para quem le). Cada modo tem a propria frase e o proprio nome da coluna.
_MODOS = {
    "liberacoes":     ("aplica no Mercos os limites aprovados na Torre (Análise de Crédito / botão Liberar)",
                       "aplicados"),
    "liberacoes-dry": ("simula a aplicação dos limites aprovados na Torre (dry-run, nada escrito)",
                       "simulados"),
    "aplicar":        ("repõe o limite disponível no Mercos a partir dos boletos pagos",
                       "repostos"),
    "dry-run":        ("simula a reposição do limite disponível (dry-run, nada escrito)",
                       "simulados"),
    "varredura":      ("lê o limite de cada cliente no Mercos para o espelho da Torre (só leitura)",
                       "lidos"),
    "mapear":         ("mapeia a tela do Mercos (não escreve nada)", "mapeados"),
    "crash":          ("parou antes de terminar", "concluídos"),
}


def _explicar(decisao: str | None, fase: str | None, motivo: str | None) -> tuple[str, str]:
    """Traduz o motivo cru do log em (o que aconteceu, o que fazer).

    ⚠️ Casa por TRECHO do motivo que o proprio robo escreve (ver repositor.py). Motivo
    que nao casar com nada cai no genérico e o texto cru segue embaixo -- nunca some."""
    m = (motivo or "").lower()
    if "nao foi encontrado em nenhuma empresa do mercos" in m:
        return ("Cliente ainda não está cadastrado no Mercos.",
                "Pedir o cadastro do cliente no Mercos. A liberação fica guardada: quando o "
                "cliente existir, o limite sobe sozinho na próxima rodada, sem despachar de novo.")
    if "derrubou a sessao" in m or "sessao do mercos caiu" in m or "tela de login" in m:
        return ("A sessão do Mercos caiu no meio da rodada (a conta foi usada em outro lugar).",
                "Nada foi escrito para este cliente. Ele volta na próxima rodada; se repetir, "
                "combinar um horário em que ninguém esteja usando a conta.")
    if "sem limite configurado" in m:
        return ("O cliente está sem limite nenhum no Mercos (0/0).",
                "O robô não escreve aqui de propósito: criaria um bloqueio que hoje não existe. "
                "Se o cliente deveria ter limite, liberar pela Torre.")
    if "alguem mexeu no teto" in m or fase == "teto_mexido_depois":
        return ("Alguém alterou o teto no Mercos depois da escrita do robô.",
                "O robô não sobrescreve decisão de outra pessoa. Conferir no Mercos qual valor vale.")
    if "retrato" in m or "diferente do" in m:
        return ("O teto no Mercos mudou entre a decisão e a escrita.",
                "Conferir no Mercos e liberar de novo se o valor ainda for o certo.")
    if "ja esta no teto" in m:
        return ("O disponível já estava no teto — nada a repor.", "Nenhuma ação.")
    if "botao de edicao" in m or "esperava 1" in m:
        return ("A tela do Mercos veio diferente do esperado (botão de edição não apareceu).",
                "Pode ser tela lenta ou mudança no Mercos. Se repetir em vários clientes, "
                "avisar quem mantém o robô.")
    if "timeout" in m or "err_aborted" in m or "net::" in m:
        return ("O Mercos demorou a responder ou a página não carregou.",
                "Instabilidade passageira. O cliente volta na próxima rodada.")
    if "auditoria" in m:
        return ("O robô não conseguiu registrar a auditoria, então não escreveu.",
                "Conferir se o Supabase está no ar e rodar de novo.")
    if decisao == "erro":
        return ("Erro não classificado.", "Ver o texto técnico abaixo e o log da execução.")
    return ("", "")


def _itens_da_rodada(aud) -> list[dict]:
    """Lê da auditoria as linhas desta rodada, com o nome do cliente e os dados da
    liberação. ⚠️ Nunca levanta: falha aqui so empobrece o e-mail, nao o impede."""
    sb = getattr(aud, "sb", None)
    rodada = getattr(aud, "rodada_id", None)
    if not sb or not rodada:
        return []
    try:
        linhas = (sb.table("credito_reposicao_log")
                  .select("cnpj,nf,cliente_nome,decisao,motivo,fase,escreveu,confirmado_na_tela,"
                          "total_antes,total_depois,disponivel_antes,disponivel_depois,"
                          "valor_pago,liberacao_id,em")
                  .eq("rodada_id", rodada).order("em").execute().data) or []
    except Exception:
        return []
    lib_ids = [l["liberacao_id"] for l in linhas if l.get("liberacao_id")]
    libs: dict = {}
    if lib_ids:
        try:
            for r in (sb.table("credito_liberacao")
                      .select("id,codigo,limite_para,modo_aplicacao,criado_por,proposta_id")
                      .in_("id", lib_ids).execute().data) or []:
                libs[r["id"]] = r
        except Exception:
            pass
    sem_nome = sorted({l["cnpj"] for l in linhas if l.get("cnpj") and not l.get("cliente_nome")})
    nomes: dict = {}
    if sem_nome:
        try:
            for r in (sb.table("dim_cliente").select("cnpj,nome_cliente")
                      .in_("cnpj", sem_nome).execute().data) or []:
                nomes[r["cnpj"].strip()] = r.get("nome_cliente")
        except Exception:
            pass
    # Cliente novo (1a compra) nao esta em dim_cliente -- o nome vem da proposta.
    prop_ids = [v["proposta_id"] for v in libs.values() if v.get("proposta_id")]
    nome_prop: dict = {}
    if prop_ids:
        try:
            for r in (sb.table("credito_proposta").select("id,razao_social,nome_fantasia")
                      .in_("id", prop_ids).execute().data) or []:
                nome_prop[r["id"]] = r.get("razao_social") or r.get("nome_fantasia")
        except Exception:
            pass
    for l in linhas:
        l["_lib"] = libs.get(l.get("liberacao_id")) or {}
        l["_nome"] = (l.get("cliente_nome") or nomes.get((l.get("cnpj") or "").strip())
                      or nome_prop.get(l["_lib"].get("proposta_id")) or "(sem nome na Torre)")
        l["_o_que"], l["_fazer"] = _explicar(l.get("decisao"), l.get("fase"), l.get("motivo"))
    return linhas


def _grupo(l: dict) -> str:
    """erro | conferir | ok | pulado -- mesma leitura que v_credito_reposicao_conferir."""
    d = (l.get("decisao") or "").lower()
    if d == "erro":
        return "erro"
    if l.get("escreveu") and l.get("confirmado_na_tela") is False:
        return "conferir"
    if d in ("aplicado", "reposto"):
        return "ok"
    return "pulado"


def _resultado_txt(l: dict) -> str:
    """Uma frase com o antes → depois, quando houver."""
    lib = l["_lib"]
    if lib:
        modo = "novo limite" if lib.get("modo_aplicacao") == "absoluto" else "acrescentar"
        base = f"{lib.get('codigo') or l.get('nf') or ''} · {_br(lib.get('limite_para'))} ({modo})"
    elif l.get("valor_pago") is not None:
        base = f"boleto {l.get('nf') or ''} · pago {_br(l.get('valor_pago'))}"
    else:
        base = l.get("nf") or ""
    if l.get("total_depois") is not None and l.get("total_antes") is not None \
            and l["total_depois"] != l["total_antes"]:
        base += f" · teto {_br(l['total_antes'])} → {_br(l['total_depois'])}"
    if l.get("disponivel_depois") is not None and l.get("disponivel_antes") is not None:
        base += f" · disponível {_br(l['disponivel_antes'])} → {_br(l['disponivel_depois'])}"
    return base.strip(" ·")


_COR = {"erro": ("#b42318", "#fef3f2", "ERRO"),
        "conferir": ("#b54708", "#fffaeb", "CONFERIR"),
        "ok": ("#067647", "#ecfdf3", "OK"),
        "pulado": ("#475467", "#f2f4f7", "PULADO")}


def _html(*, titulo: str, frase_modo: str, cor_topo: str, resumo: list[tuple[str, str]],
          abortou: str | None, itens: list[dict], detalhes: list[str], rodada: str,
          quem: str, run_url: str | None) -> str:
    cards = "".join(
        f'<td style="padding:10px 14px;border:1px solid #eaecf0;border-radius:8px;text-align:center">'
        f'<div style="font-size:20px;font-weight:700;color:#101828">{_esc(v)}</div>'
        f'<div style="font-size:12px;color:#667085">{_esc(k)}</div></td><td style="width:8px"></td>'
        for k, v in resumo)

    blocos = ""
    if abortou:
        blocos += (f'<div style="margin:16px 0;padding:12px 14px;background:#fef3f2;border-left:4px solid #b42318;'
                   f'border-radius:6px"><b style="color:#b42318">A rodada foi interrompida</b><br>'
                   f'<span style="color:#344054">{_esc(abortou)}</span><br>'
                   f'<span style="color:#667085;font-size:13px">O que sobrou da fila não foi tocado. '
                   f'Pode rodar de novo.</span></div>')

    ordem = {"erro": 0, "conferir": 1, "ok": 2, "pulado": 3}
    for l in sorted(itens, key=lambda x: ordem[_grupo(x)]):
        g = _grupo(l)
        cor, fundo, rot = _COR[g]
        fazer = (f'<div style="margin-top:6px;color:#344054"><b>O que fazer:</b> {_esc(l["_fazer"])}</div>'
                 if l["_fazer"] and g != "ok" else "")
        o_que = (f'<div style="margin-top:6px;color:#101828">{_esc(l["_o_que"])}</div>'
                 if l["_o_que"] and g != "ok" else "")
        tecnico = (f'<div style="margin-top:6px;color:#98a2b3;font-size:12px;font-family:Consolas,monospace">'
                   f'{_esc((l.get("motivo") or "")[:300])}</div>' if l.get("motivo") and g != "ok" else "")
        blocos += (
            f'<div style="margin:10px 0;padding:12px 14px;background:{fundo};border-left:4px solid {cor};'
            f'border-radius:6px">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;background:{cor};color:#fff;'
            f'font-size:11px;font-weight:700">{rot}</span> '
            f'<b style="color:#101828">{_esc(l["_nome"])}</b> '
            f'<span style="color:#667085;font-size:13px">{_esc(_doc(l.get("cnpj")))}</span>'
            f'<div style="margin-top:4px;color:#475467;font-size:13px">{_esc(_resultado_txt(l))}</div>'
            f'{o_que}{fazer}{tecnico}</div>')

    if detalhes and not itens:
        blocos += ('<div style="margin-top:16px;font-weight:600;color:#101828">O que o log disse</div>'
                   '<pre style="background:#f9fafb;padding:10px;border-radius:6px;font-size:12px;'
                   'white-space:pre-wrap">' + _esc("\n".join(detalhes[:20])) + '</pre>')

    link = (f'<a href="{_esc(run_url)}" style="display:inline-block;margin-top:16px;padding:9px 16px;'
            f'background:#101828;color:#fff;text-decoration:none;border-radius:6px;font-size:14px">'
            f'Abrir o log da execução</a>' if run_url else "")

    return f"""<!doctype html><html><body style="margin:0;background:#f2f4f7;font-family:Segoe UI,Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0"><tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden;max-width:640px">
<tr><td style="background:{cor_topo};padding:18px 22px;color:#fff">
<div style="font-size:12px;opacity:.85">Torre B2B · Robô de Crédito (Mercos)</div>
<div style="font-size:19px;font-weight:700;margin-top:4px">{_esc(titulo)}</div></td></tr>
<tr><td style="padding:20px 22px">
<div style="color:#475467;font-size:14px">Este robô {_esc(frase_modo)}.<br>Disparado por <b>{_esc(quem)}</b>.</div>
<table cellpadding="0" cellspacing="0" style="margin-top:14px"><tr>{cards}</tr></table>
{blocos}
{link}
<div style="margin-top:22px;padding-top:12px;border-top:1px solid #eaecf0;color:#98a2b3;font-size:12px">
Itens com erro continuam na fila e são tentados de novo na próxima rodada — nada foi escrito para eles.<br>
Rodada <span style="font-family:Consolas,monospace">{_esc(rodada)}</span> ·
auditoria: <span style="font-family:Consolas,monospace">v_credito_reposicao_auditoria</span></div>
</td></tr></table></td></tr></table></body></html>"""


def avisar_falha(*, log, aud=None, modo: str, abortou: str | None = None,
                 erros: int = 0, conferir: int = 0, repostos: int = 0,
                 fila_lida: int = 0, valor_reposto: float = 0.0,
                 detalhes: list[str] | None = None) -> bool:
    """Manda o e-mail de falha. Devolve se saiu, e registra a tentativa na
    auditoria (etapa `aviso_falha`) -- "avisa E fica salvo que falhou" foi a
    resposta do Kaique, e "fica salvo" tem que valer para o aviso tambem: senao
    ninguem consegue provar depois se o e-mail saiu.

    ⚠️ 25/set/2026: o e-mail passou a ter HTML (com texto puro de alternativa) e a
    ler o DETALHE POR CLIENTE da propria auditoria da rodada, em vez de depender de
    `detalhes` -- o modo de liberacoes nunca passava `detalhes`, entao o aviso dizia
    "2 com erro" sem dizer quais nem por que."""
    if not (abortou or erros or conferir):
        return False  # rodada limpa nao gera e-mail

    rodada = getattr(aud, "rodada_id", None) or "(sem rodada)"
    quem = getattr(aud, "quem", None) or "(desconhecido)"
    run_url = getattr(aud, "run_url", None)
    frase_modo, rotulo_ok = _MODOS.get(modo, (f"rodou no modo {modo}", "concluídos"))
    itens = _itens_da_rodada(aud)
    detalhes = detalhes or []

    # O assunto tem que dizer a gravidade sem abrir o e-mail.
    if abortou:
        cabeca, cor = "rodada interrompida", "#b42318"
    elif conferir:
        cabeca, cor = f"{conferir} para conferir na mão", "#b54708"
    else:
        cabeca, cor = f"{erros} com erro", "#b42318"
    ok_n = f"{repostos}" + (f" ({_br(valor_reposto)})" if valor_reposto else "")
    # Quando todo erro tem o mesmo motivo conhecido, ele vai no assunto.
    motivos = {l["_o_que"] for l in itens if _grupo(l) == "erro" and l["_o_que"]}
    if not abortou and not conferir and len(motivos) == 1:
        m1 = next(iter(motivos)).rstrip(".")
        cabeca += " · " + m1[:1].lower() + m1[1:]
    assunto = f"[Robô de Crédito] {cabeca} — {modo} ({ok_n} {rotulo_ok})"

    resumo = [("na fila", str(fila_lida)), (rotulo_ok, ok_n),
              ("com erro", str(erros)), ("conferir na mão", str(conferir))]

    # ── texto puro (cliente de e-mail sem HTML e o log do Actions) ──
    linhas = [f"Robô de Crédito (Mercos) — {cabeca}", "",
              f"Este robô {frase_modo}.", f"Disparado por {quem}.", ""]
    linhas += [f"  {k:.<18} {v}" for k, v in resumo]
    if abortou:
        linhas += ["", "RODADA INTERROMPIDA", f"  {abortou}",
                   "  O que sobrou da fila não foi tocado. Pode rodar de novo."]
    ordem = {"erro": 0, "conferir": 1, "ok": 2, "pulado": 3}
    for l in sorted(itens, key=lambda x: ordem[_grupo(x)]):
        g = _grupo(l)
        linhas += ["", f"[{_COR[g][2]}] {l['_nome']} — {_doc(l.get('cnpj'))}",
                   f"  {_resultado_txt(l)}"]
        if g != "ok":
            if l["_o_que"]:
                linhas.append(f"  O que aconteceu: {l['_o_que']}")
            if l["_fazer"]:
                linhas.append(f"  O que fazer: {l['_fazer']}")
            if l.get("motivo"):
                linhas.append(f"  (técnico) {(l['motivo'] or '')[:300]}")
    if detalhes and not itens:
        linhas += ["", "O QUE O LOG DISSE"] + [f"  {d}" for d in detalhes[:20]]
    if run_url:
        linhas += ["", f"Log da execução: {run_url}"]
    linhas += ["", "Itens com erro continuam na fila e são tentados de novo na próxima rodada.",
               f"Rodada {rodada} — SELECT * FROM v_credito_reposicao_auditoria "
               f"WHERE rodada_id = '{rodada}';",
               "", "-- Torre B2B, robô CreditoLimite-Mercos"]

    html = _html(titulo=f"Robô de Crédito — {cabeca}", frase_modo=frase_modo, cor_topo=cor,
                 resumo=resumo, abortou=abortou, itens=itens, detalhes=detalhes,
                 rodada=rodada, quem=quem, run_url=run_url)

    saiu = _enviar(assunto, "\n".join(linhas), log, html=html)
    if aud is not None:
        try:
            aud.evento_json(etapa="aviso_falha", enviado=saiu, para=destinatarios(),
                            motivo=cabeca, abortou_por=abortou, erros=erros,
                            conferir=conferir)
        except Exception:
            pass
    return saiu
