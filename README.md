# torre_actions — casca de execucao do Actions

Repo **publico** cuja unica funcao e hospedar workflows agendados da Torre B2B.
Publico porque repo publico tem minuto de Actions GitHub-hosted **ilimitado**; privado
nao tem, e o consumo medido da Torre (~3.900 min/mes) estoura o teto de 3.000 em ~19 dias.

## O que tem aqui -- e o que nunca pode entrar

Tem o ETL de `fato_pedidos` (`etl/fato-pedidos/` + `etl/shared/`) e o bloco
Mercos/credito (`automacoes/`, 41 arquivos de codigo). Esse conjunto foi escolhido por medicao: ele **nao importa nada de `lib/` nem
de `app/`** -- so `etl/shared/*` e `pg` --, e a auditoria antes de trazer deu **zero
credencial, zero nome de cliente**; os CNPJ que aparecem sao os da **propria empresa**
(filiais), que sao registro publico.

Por isso **nao existe deploy key aqui**. A alternativa era manter o codigo no repo
privado e busca-lo no checkout, mas a chave que faz isso destranca o `torre_b2b`
inteiro: se ela vazar por um log derivado, vaza tudo. Sem chave, nao ha essa cadeia.

**O que nunca entra:** `lib/`, `app/`, `db/`, `docs/`, `components/`, `scripts/` (checagem
5) e arquivo de dado em `automacoes/` (checagem 7).

Tres coisas ficaram de fora do bloco Mercos por serem **dado**, nao codigo:

| O que | Tamanho | Onde esta agora |
|---|---|---|
| `carteiras/*.txt` + `cnpjs_bel.txt` | 2.587 CNPJ de cliente por representante | so no repo privado |
| `usuarios_mercos.csv` | 75 nomes e e-mails corporativos | **secret** `USUARIOS_MERCOS_CSV` |
| `upload-artifact` (6 workflows) | screenshot da tela do Mercos com CNPJ e limite | removido |

O artifact saiu porque em repo publico ele e baixavel por qualquer um por 90 dias. Custo
real e baixo: a auditoria do robo de credito e `credito_reposicao_log` no banco, e
`v_credito_reposicao_conferir` continua respondendo "escreveu e nao confirmou". O print
era complementar.

**Residuo declarado:** sobram **12 CNPJ de cliente** em comentario de incidente e fixture
de teste dentro dos `.py` (VEMAC, KIMAKE, PERFUMARIA). Ordem de grandeza diferente das
listas, e CNPJ e registro publico -- mas esta aqui declarado, nao esquecido.

## Os oito invariantes (travados em `guard-invariantes.yml`)

**1. Nenhum trigger que entrega secret a gente de fora.** Num repo publico qualquer
pessoa pode abrir issue, comentar, dar star e forkar. Estes eventos rodam no contexto do
repo BASE, **com acesso aos secrets**:

| Trigger | Quem dispara | Recebe secret |
|---|---|---|
| `pull_request` de fork | qualquer um | nao (contexto do fork) |
| `pull_request_target` | qualquer um | **sim** |
| `issue_comment` / `issues` | qualquer um | **sim** |
| `watch` (star) / `fork` | qualquer um | **sim** |
| `schedule` / `workflow_dispatch` / `workflow_run` | so quem tem write | sim, ok |

So os tres de baixo sao permitidos. Issues, wiki, projects e discussions estao
desligados no repo, o que ja remove a superficie -- o guard e a segunda camada, para o
dia em que alguem religar.

**2. Nenhum `upload-artifact`.** Artifact de repo publico e baixavel por qualquer um
durante 90 dias. Os robos do Mercos sobem screenshot e stdout: nome de cliente, CNPJ,
limite de credito. Por isso eles **nao vem para este repo** (e tambem nao passariam,
porque dependem de sair de fora do firewall da empresa).

**3. Nenhum arquivo de credencial versionado.**

**4. So os caminhos da allowlist.** `.github/`, `README.md`, `.gitignore` e o conjunto
do ETL de fato_pedidos.

**5. Nenhum modulo sensivel da Torre** (`lib/`, `app/`, `db/`, `docs/`, ...).

**6. `package.json` e `package-lock.json` em sincronia** -- fora de sincronia o `npm ci`
quebra so na hora do run agendado.

**7. Nenhum arquivo de DADO em `automacoes/`.** Codigo pode ser publico; lista pode nao.
A regra e por **extensao**, nao por nome: filtrar por nome ja deixou passar
`cnpjs_bel.txt`, que estava fora de `carteiras/`.

**8. Quem le `usuarios_mercos.csv` escreve o arquivo a partir do secret.** Sem o step,
`decisor.py` quebra com FileNotFoundError so na hora do run agendado.

## O que o mascaramento do GitHub cobre -- e o que nao cobre

O Actions mascara o **valor exato** do secret no log. Nao mascara derivado: base64 dele,
URL montada com ele em query string, JSON que o contenha, stack trace que despeje o
`env`. **Log de run de repo publico e legivel por qualquer um, para sempre.**

Antes de trazer um workflow para ca, confira que o caminho dele nao imprime CNPJ, nome
de cliente, valor, nem monta credencial dentro de URL.

## Endurecimento aplicado no repo

- issues, wiki, projects: **off**
- `GITHUB_TOKEN` default: **read**, sem poder aprovar PR
- actions permitidas: so `actions/checkout@*` e `actions/setup-node@*` (whitelist)

## Regra de operacao

Workflow novo aqui: so `schedule` + `workflow_dispatch`, sem artifact, sem codigo.
O guard falha o push se qualquer uma das quatro for violada.

## Migracao -- o perigo e rodar em dois lugares

`concurrency` e **por repositorio**. Se este repo e o antigo estiverem os dois com a
guarda ligada, o ETL roda **duas vezes no mesmo banco**, possivelmente em paralelo.

Ordem obrigatoria:

1. cadastrar os secrets aqui
2. testar por `workflow_dispatch` com `ETL_FATO_PEDIDOS_ATIVO` **ainda ausente** (o
   schedule nao dispara) e ler o log inteiro procurando CNPJ, nome de cliente, valor e
   credencial montada dentro de URL
3. **desligar no repo antigo**: `gh variable delete RODIZIO_ATIVO` e
   `gh variable delete ETL_FATO_PEDIDOS_FULL_ATIVO`
4. so entao ligar aqui, uma chave por bloco:
   `gh variable set ETL_FATO_PEDIDOS_ATIVO --body true` (os 2 ETL) e
   `gh variable set MERCOS_ATIVO --body true` (os 11 do Mercos/credito)
5. e so depois de tudo verde, virar o repo publico

O passo 2 nao da para pular: depois de publico, log de run e permanente e indexavel.
