#!/usr/bin/env bash
# Roda o vinculo de um vendedor ATE FECHAR, insistindo atraves das quedas de sessao.
#
#   bash insistir_vinculo.sh es "Edilberto Lobo" carteiras/cnpjs_edilberto_lobo.txt 30
#
# POR QUE EXISTE (17/set/2026)
# ----------------------------
# O Mercos aceita 1 sessao por usuario e a conta do robo e a de uma PESSOA. Quando ela
# esta em uso, a rodada morre -- as vezes ANTES de comecar, no proprio login. O robo ja
# espera 60s e volta, mas se a disputa continuar ele desiste e sai PARCIAL.
#
# Aqui a insistencia e externa: cada tentativa e um processo novo, com sessao nova. O
# vinculo e IDEMPOTENTE (clicar em `habilitar_` num cliente ja liberado nao faz nada),
# entao repetir nao tem custo alem do tempo.
#
# ⚠️ PARA POR PROGRESSO, NAO POR TENTATIVAS: se duas rodadas seguidas nao vincularem
# NINGUEM novo, o obstaculo nao e a sessao e insistir so gera log. O criterio e o mesmo
# do `rodar_ate_acabar.py`.
#
# ⚠️ NAO CHAMA `limitar_acesso_clientes.py`. Este script so REPOE carteira; quem ja foi
# limitado continua limitado, e limitar de novo seria clicar num toggle.
set -u

FILIAL="${1:?filial (es|rj)}"
VENDEDOR="${2:?nome do vendedor}"
ARQUIVO="${3:?arquivo de cnpjs}"
MAX="${4:-20}"

TOTAL=$(grep -cE '^[0-9]{14}$' "$ARQUIVO")
echo "== $VENDEDOR ($FILIAL) | alvo: $TOTAL CNPJs | ate $MAX tentativas =="

anterior=-1
for i in $(seq 1 "$MAX"); do
  log="/tmp/insistir_${FILIAL}_$$_$i.log"
  echo "-- tentativa $i/$MAX  ($(date +%H:%M:%S))"
  PYTHONIOENCODING=utf-8 python -u ferramentas/_env.py ../../.env.local \
      vincular_carteira_lote.py --filial "$FILIAL" --vendedor "$VENDEDOR" \
      --cnpjs-arquivo "$ARQUIVO" > "$log" 2>&1

  lib=$(grep -c '\[ok\] liberado' "$log")
  nt=$(grep -oE 'nao_tentado +[0-9]+' "$log" | grep -oE '[0-9]+' | tail -1)
  nt=${nt:-0}
  quedas=$(grep -c 'sessao. caiu' "$log")
  echo "   liberou=$lib  nao_tentado=$nt  quedas=$quedas"

  # ⚠️ A rodada pode morrer ANTES de imprimir RESUMO (excecao no login/abertura). Nesse
  # caso nao ha "nao_tentado" nenhum -- e tratar a ausencia como zero faria este laco
  # declarar sucesso sobre uma rodada que nem comecou. Por isso o criterio de parada
  # exige ter visto o RESUMO.
  if grep -q 'RESUMO' "$log" && [ "$nt" -eq 0 ]; then
    echo "== FECHOU na tentativa $i =="
    grep -A6 'RESUMO' "$log" | tail -6
    exit 0
  fi

  if [ "$lib" -eq 0 ] && [ "$anterior" -eq 0 ]; then
    echo "== 2 tentativas seguidas sem vincular ninguem -- o obstaculo nao e a sessao. Parando. =="
    tail -5 "$log"
    exit 1
  fi
  anterior=$lib
  sleep 20
done

echo "== teto de $MAX tentativas atingido =="
exit 1
