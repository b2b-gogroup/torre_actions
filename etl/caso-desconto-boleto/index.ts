/**
 * Robô "Desconto Boleto → caso no painel".
 *
 * Uma vez por dia (15h20 BRT): para cada pedido Mercos CONCLUÍDO, com o campo "Desconto Boleto (se
 * aplicável)" PREENCHIDO e já FATURADO (NF de venda do mesmo cliente), abre no painel de casos
 * (CRM "Devoluções Conectadas") um caso com a ocorrência "Alteração de Boleto" do tipo
 * DESCONTO e um comentário padrão com a condição informada. Nunca duas vezes o mesmo pedido.
 *
 * QUEM DECIDE O QUÊ
 *   · Torre (`fn_caso_desconto_boleto_candidatos`): quem é elegível e qual é a NF.
 *   · Torre (`fn_caso_desconto_boleto_reservar`): trava atômica "um pedido, uma vez".
 *   · Torre (`caso_desconto_boleto_config`): ligado/desligado, data de início, limite por
 *     rodada e o texto da mensagem — editáveis sem deploy, porque este código roda como CÓPIA
 *     em b2b-gogroup/torre_actions.
 *   · Painel: o fluxo da ocorrência (etapas, condições, SLA) é LIDO do próprio painel a cada
 *     rodada, não copiado para cá — se o painel mudar o fluxo, o robô acompanha, e se mudar de
 *     um jeito que o robô não entende, ele PARA (falha fechando) em vez de gravar errado.
 *
 * O QUE ELE GRAVA NO PAINEL (o mesmo que a tela grava, medido em 29/set/2026 no CASE-2026-0866):
 *   cases → case_team_history → occurrences → occurrence_stage_states → comments
 *   (+ clientes, quando o CNPJ não existe lá, como a tela faz com source='manual').
 *   Se já houver caso ABERTO para a NF, a ocorrência entra nele (modelo "1 nota → 1 caso →
 *   N ocorrências"); se já houver pedido de DESCONTO no boleto dessa NF, não grava nada.
 *
 * NUNCA DUAS VEZES — três camadas, cada uma cobrindo um buraco da outra:
 *   1. reserva atômica na Torre (UNIQUE regiao+numero_pedido): duas rodadas ao mesmo tempo
 *      não pegam o mesmo pedido;
 *   2. a chave `torre:desconto-boleto:ES:1234` vai DENTRO da ocorrência (field_values) e no
 *      rodapé do comentário: se a rodada morrer depois de gravar no painel e antes de anotar
 *      na Torre, a seguinte ACHA o que já existe e só completa o que faltou;
 *   3. pedido de desconto já existente para a NF (aberto por uma pessoa) é respeitado.
 *
 * LOG: só contagens. Este robô roda num repositório PÚBLICO, onde o log é permanente — nome e
 * CNPJ de cliente nunca vão para o stdout (o detalhe fica em `caso_desconto_boleto`, privado).
 *
 * Uso:
 *   MODO=dry      (padrão) lê tudo e diz o que faria, sem gravar NADA em lugar nenhum
 *   MODO=aplicar  grava
 *   SO_PEDIDO=ES:1234   restringe a um pedido (primeira rodada controlada)
 *   PREVIEW=1           (só local) imprime o caso que seria criado, campo a campo
 *
 *   cd etl && npx tsx caso-desconto-boleto/index.ts
 */

import { createClient } from "@supabase/supabase-js";
import {
  EM_CI, MODO, PREVIEW, SO_PEDIDO, carregarReferencia, exigirEnv, ok, processar, type Candidato,
} from "./fluxo.js";
import { erroSemDados } from "./regras.js";

// ── rodada ───────────────────────────────────────────────────────────────────────

async function main() {
  if (!["dry", "aplicar"].includes(MODO)) throw new Error(`MODO inválido: ${MODO} (use dry ou aplicar)`);
  const torre = createClient(exigirEnv("SUPABASE_URL"), exigirEnv("SUPABASE_SERVICE_ROLE_KEY"), { auth: { persistSession: false } });
  const painel = createClient(exigirEnv("DEVOLUCAO_SUPABASE_URL"), exigirEnv("DEVOLUCAO_SUPABASE_KEY"), { auth: { persistSession: false } });
  const t0 = Date.now();
  console.log(`[caso-desconto-boleto] modo=${MODO}${SO_PEDIDO ? " (restrito a 1 pedido)" : ""}`);

  const cfg = ok(await torre.from("caso_desconto_boleto_config").select("ativo,pedido_desde,max_por_rodada,mensagem").eq("id", true).single(), "configuração") as
    { ativo: boolean; pedido_desde: string; max_por_rodada: number; mensagem: string };
  if (!cfg.ativo) { console.log("[caso-desconto-boleto] desligado na configuração (caso_desconto_boleto_config.ativo = false)."); return; }

  const todos = ok(await torre.rpc("fn_caso_desconto_boleto_candidatos"), "candidatos") as Candidato[];
  const porSituacao: Record<string, number> = {};
  for (const c of todos) porSituacao[c.situacao] = (porSituacao[c.situacao] ?? 0) + 1;
  let elegiveis = todos.filter((c) => c.situacao === "elegivel");
  if (SO_PEDIDO) elegiveis = elegiveis.filter((c) => `${c.regiao}:${c.numero_pedido}` === SO_PEDIDO);
  const novos = elegiveis.filter((c) => !c.registro_status).length;
  console.log(`[caso-desconto-boleto] desde ${cfg.pedido_desde}: ${JSON.stringify(porSituacao)} · a processar: ${elegiveis.length} (${novos} novos)`);

  // Trava de volume: melhor não abrir nada (e acender vermelho) do que inundar a fila.
  if (novos > cfg.max_por_rodada) {
    console.error(`[caso-desconto-boleto] ABORTADO: ${novos} pedidos novos passam do limite de ${cfg.max_por_rodada} por rodada. Nada foi aberto. Conferir e, se estiver certo, subir caso_desconto_boleto_config.max_por_rodada.`);
    process.exitCode = 1;
    return;
  }
  if (!elegiveis.length) { console.log(`[caso-desconto-boleto] nada a fazer (${((Date.now() - t0) / 1000).toFixed(1)}s).`); return; }

  const ref = await carregarReferencia(painel);
  const preview = (titulo: string, obj: unknown) => {
    if (PREVIEW) console.log(`\n── ${titulo} ──\n${typeof obj === "string" ? obj : JSON.stringify(obj, null, 2)}`);
  };

  const contagem: Record<string, number> = {};
  let erros = 0;
  for (const c of elegiveis) {
    try {
      if (PREVIEW) console.log(`\n==================== ${c.regiao} pedido ${c.numero_pedido} ====================`);
      const r = await processar(torre, painel, ref, cfg.mensagem, c, preview);
      contagem[r] = (contagem[r] ?? 0) + 1;
    } catch (e) {
      erros++;
      const causa = (e as { causa?: unknown }).causa ?? e;
      console.error(`[caso-desconto-boleto] erro num pedido: ${e instanceof Error ? e.message.split(":")[0] : "erro"} (${erroSemDados(causa)})`);
      if (MODO === "aplicar") {
        // mensagem completa só no banco PRIVADO da Torre
        await torre.from("caso_desconto_boleto").update({
          status: "erro", ultimo_erro: e instanceof Error ? e.message : String(e), atualizado_em: new Date().toISOString(),
        }).eq("regiao", c.regiao).eq("numero_pedido", c.numero_pedido).neq("status", "criado").neq("status", "ja_existia");
      }
    }
  }
  console.log(`[caso-desconto-boleto] resultado: ${JSON.stringify(contagem)} · erros: ${erros} · ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  if (erros) process.exitCode = 1;
}

main().catch((e) => {
  console.error(`[caso-desconto-boleto] falhou: ${e instanceof Error ? e.message : e}`);
  process.exit(1);
});
