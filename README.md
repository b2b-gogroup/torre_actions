# torre_actions — casca de execucao do Actions

Repo **publico** cuja unica funcao e hospedar workflows agendados da Torre B2B.
Publico porque repo publico tem minuto de Actions GitHub-hosted **ilimitado**; privado
nao tem, e o consumo medido da Torre (~3.900 min/mes) estoura o teto de 3.000 em ~19 dias.

## O que este repo NAO tem

**Codigo.** Os workflows fazem checkout do repo privado `b2b-gogroup/torre_b2b` e rodam
de la. Se o codigo morasse aqui, ele seria publico -- e o ETL importa ~20 modulos de
`lib/` que carregam politica de credito, regua de avaliacao da IA e formula de KPI de
inadimplencia. O passo 4 do guard impede que isso aconteca por descuido.

## Os quatro invariantes (travados em `guard-invariantes.yml`)

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

**4. Este repo e casca.** So `.github/`, `README.md` e `.gitignore`.

## O que o mascaramento do GitHub cobre -- e o que nao cobre

O Actions mascara o **valor exato** do secret no log. Nao mascara derivado: base64 dele,
URL montada com ele em query string, JSON que o contenha, stack trace que despeje o
`env`. **Log de run de repo publico e legivel por qualquer um, para sempre.**

Antes de trazer um workflow para ca, confira que o caminho dele nao imprime CNPJ, nome
de cliente, valor, nem monta credencial dentro de URL.

## Como o codigo privado e alcancado

`actions/checkout` com **deploy key read-only** registrada em `torre_b2b`, nunca um PAT:
a deploy key e escopada a um repo so, e revogavel numa chamada. Um PAT com escopo `repo`
daria escrita em todos os repos da conta se vazasse.

```yaml
- uses: actions/checkout@v4
  with:
    repository: b2b-gogroup/torre_b2b
    ssh-key: ${{ secrets.TORRE_DEPLOY_KEY }}
```

## Endurecimento aplicado no repo

- issues, wiki, projects: **off**
- `GITHUB_TOKEN` default: **read**, sem poder aprovar PR
- actions permitidas: so `actions/checkout@*` e `actions/setup-node@*` (whitelist)

## Regra de operacao

Workflow novo aqui: so `schedule` + `workflow_dispatch`, sem artifact, sem codigo.
O guard falha o push se qualquer uma das quatro for violada.
