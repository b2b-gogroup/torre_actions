> ## 🔴 ESTE NÃO É O ROBÔ QUE ESTÁ RODANDO (21/set/2026)
>
> O robô em produção vive em **`marianabarros-lab/gira`** — lá o workflow já tem
> os secrets e 20 execuções bem-sucedidas. `ORCAMENTO_GH_REPO` na Vercel aponta
> para lá, e é ele que o PIX acorda.
>
> Esta é a versão migrada, esperando os secrets. ⚠️ **Enquanto as duas existirem,
> conserto de seletor do Mercos passa pelos DOIS lugares** — é a mesma armadilha
> da cópia vendorizada, em outra forma. Detalhe em
> `docs/modulos/gira-campanhas.md` §8.1.

# Automação de Orçamento no Mercos + link de pagamento Pix

> O cliente confirma no link do Pedido Pronto → este robô cria o **orçamento** no Mercos, gera o
> **link de pagamento Pix**, captura o **QR Code** e devolve os dois para envio ao cliente.
>
> Quarto robô que escreve no Mercos, depois de `cadastro-rca-mercos`, `credito-limite-mercos` e
> `mercos-fotos`. Mesmo padrão: Playwright, login com 2FA por IMAP, modos `mapear → dry → aplicar`,
> auditoria em linha `AUDIT` e artifact com log + screenshots.

---

## 🔴 A regra que define este robô

> ## NUNCA, EM HIPÓTESE NENHUMA, CLICAR EM "GERAR PEDIDO"

O artefato final é um **orçamento** com link de pagamento. **Gerar o pedido é decisão humana,
tomada DEPOIS que o cliente paga.**

Isso não ficou só como comentário. Existe `_guarda_gerar_pedido()` no código, chamada **antes de
cada clique** dos passos que ficam perto do botão, e ela **aborta a rodada** se o botão proibido
estiver em foco:

```python
BOTAO_PROIBIDO = "Gerar pedido"

def _guarda_gerar_pedido(page, onde):
    focado = page.evaluate("() => (document.activeElement?.innerText || '').trim()")
    if BOTAO_PROIBIDO.lower() in (focado or "").lower():
        raise OrcamentoAbortado(...)
```

⚠️ **Por que uma guarda e não só "não clicar":** um seletor genérico que pegue o botão errado
transformaria um orçamento em pedido faturável — e isso não tem desfazer limpo no Mercos.

---

## 1 · O fluxo completo

```
Cliente confirma no link (Pedido Pronto, GoDeploy)
         │  tela diz: "você recebe o link de pagamento em até 5 minutos"
         ▼
   fila / disparo do workflow
         ▼
┌──────────────────────────────────────────────┐
│  ROBÔ (GitHub Actions + Playwright)          │
│  1. login Mercos ES (2FA por IMAP)           │
│  2. Criar pedido / orçamento                 │
│  3. escolher cliente por CNPJ                │
│  4-9. adicionar itens (conferindo estoque)   │
│  10-12. desconto do PEDIDO INTEIRO           │
│  13-17. Pix + transportadora CIF             │
│  18. F5                                      │
│  🔴 19. NUNCA "Gerar pedido"                 │
│  20-23. link de pagamento Pix                │
│  24. screenshot do QR Code                   │
└──────────────────────────────────────────────┘
         ▼
   link + QR → chat do cliente
```

---

## 2 · O mapeamento de tela, passo a passo

Cada passo abaixo é o que está implementado, com o seletor real.

| # | O quê | Seletor | Observação |
|---|---|---|---|
| 1 | Tela de pedidos | `https://app.mercos.com/424525/pedidos/` | 424525 = empresa **ES** |
| 2 | Criar pedido / orçamento | `#btn_criar_pedido` | `onclick="novoOrcamento()"` |
| 3 | Buscar cliente | `#id_codigo_cliente` | digita CNPJ, escolhe na lista |
| 4 | Buscar produto | `#produto_autocomplete` | por código do SKU |
| 5 | Quantidade | `#id_quantidade` | no modal que abre |
| 6 | **Tabela de preço** | — | **não tocar** — fica no preço de tabela cheio |
| 7 | Adicionar | `a[type="submit"].botao.medio.primario` | |
| 8 | Repetir 4–7 | | para cada item |
| 9 | Conferir estoque | texto `Estoque: N` no modal | lido e comparado com a quantidade |
| 10 | Desc. Acrés. | `#link_descontos` | coluna da tabela de itens |
| 11 | Desconto do pedido | `#id_form-0-desconto` | **%** do pedido inteiro |
| 12 | Salvar descontos | `#botao_salvar_descontos` | |
| 13 | Alterar detalhes | `#alterar_informacoes` | |
| 14 | **Vendedor** | — | **não tocar** — fica o dono da conta da automação |
| 15 | Cond. pagamento → **PIX** | `#select2-id_cond_pagamento-container` | select2 |
| 16 | Transportadora → **CIF** | `#select2-id_transportadora-container` | primeira que aparece |
| 17 | Salvar detalhes | `#botao-submit` | |
| 18 | Recarregar | `page.reload()` | F5 na mesma página |
| **19** | 🔴 **Gerar pedido** | — | ⛔ **NUNCA** |
| 20 | Mais opções | `a.dropdown-toggle.btn_mais` | |
| 21 | Links de pagamento | `a.links-de-pagamento-do-pedido` | |
| 22 | Criar link (Pix) | `button:has-text("Criar link")` | Pix já vem marcado |
| 23 | Copiar o link | `[data-testid="copiar-link"]` | |
| 24 | QR Code | aba nova no link público | screenshot do elemento |

### Três decisões embutidas no mapeamento

**Passo 6 + passo 11 andam juntos, e a razão é do próprio Mercos.** O modal de desconto avisa:

> *"Os descontos e acréscimos não serão aplicados em itens que tiveram o preço líquido alterado
> manualmente."*

Por isso o item entra a **preço de tabela cheio** e o desconto é aplicado ao **pedido inteiro**.
Mexer no preço do item faria o desconto do pedido **pular aquele item, em silêncio** — e o
orçamento sairia com valor diferente do combinado.

**Passo 9 — estoque insuficiente ABORTA, não reduz.** Se o estoque for menor que a quantidade
confirmada pelo cliente, o robô para. Reduzir sozinho criaria um pedido diferente do que a pessoa
aceitou na tela.

⚠️ **Estoque ilegível não é estoque zero.** Quando o robô não consegue ler o número, ele segue e
**declara na auditoria** — tratar "não sei" como zero recusaria item disponível.

**Passo 3 e 4 — nunca adivinhar.** Se a busca devolver mais de um cliente ou mais de um produto,
o robô **aborta e lista os candidatos**. É a regra de ouro herdada do `mercos_ui`: escolher no
palpite cria orçamento para a empresa errada.

---

## 2b · Duas exceções do negócio

### Ápice não entra no orçamento

Ápice é negociada em outro canal, e boa parte da carteira dela nem tem cadastro
no Mercos. Item de Ápice no orçamento seria linha que ninguém vai faturar por
aqui — então ele sai antes de o robô começar a adicionar, e cada exclusão vira
um `item_ignorado` na auditoria.

⚠️ **A marca vem do CADASTRO, não do código do SKU.** Medido em 18/set/2026: a
Ápice tem SKUs com prefixo `00`, `AP`, `ER` e `MC`, e o prefixo `ER` também
existe na Barbours. Filtrar por prefixo deixaria passar **17** itens Ápice e
cortaria **1** da Barbours — erro nos dois sentidos, e silencioso.

Se a consulta ao cadastro falhar, **nada é ignorado**, e o log declara. Ignorar
por engano tira item legítimo do pedido do cliente, o que é pior do que deixar
passar um Ápice: o primeiro é um pedido menor sem ninguém entender por quê; o
segundo alguém vê na tela e remove.

Se **todos** os itens forem de marca ignorada, o robô aborta — não cria
orçamento vazio.

Para mudar a lista: `ORCAMENTO_MARCAS_IGNORADAS=AP,YE` (padrão: `AP`).

### CNPJ sem cadastro cai no cliente de passagem

| | |
|---|---|
| CNPJ | `56.128.147/0001-16` |
| Razão social | 56 128 147 Bruna Jessica Pilati Me |
| O que o robô digita | `BRUNA` |
| Cidade | Mafra, SC |

Abortar por falta de cadastro deixaria o cliente sem link de pagamento **depois**
de ele ter confirmado o pedido — o pior lugar para parar.

🔴 **O orçamento fica no nome de outra empresa.** É deliberado e tem custo: quem
abrir o Mercos vai ver um orçamento que não é daquele cliente. Por isso cada uso
emite `cliente_generico` na auditoria, **com o CNPJ original junto** — sem esse
rastro o orçamento vira registro órfão que ninguém consegue explicar depois.

🔴 **Esta cliente compra de verdade** — é a diferença para a escolha anterior
(Vania, `46.067.790/0001-23`, **zero** pedido na base). Medido na Torre em
18/set/2026: a Bruna tem **5 pedidos e 53 itens faturados**, o último em
**31/jul/2026**, e tem vendedor atribuído. Duas consequências:

- o rascunho aparece no histórico de uma cliente ativa, e o vendedor dela vê;
- se alguém clicar **"Gerar pedido"** nele por engano, o valor entra na receita
  **dela** e contamina curva ABC, slow moving e comissão.

A decisão é do Ítalo (19/set/2026) e o raciocínio é explícito: **é orçamento,
nunca pedido**, e o botão "Gerar pedido" segue bloqueado pela
`_guarda_gerar_pedido`. O risco fica confinado ao segundo item — e ele depende
de uma ação humana que o processo proíbe. Quando os testes acabarem, apontar
`ORCAMENTO_CNPJ_GENERICO` para um cadastro sem histórico elimina os dois.

**Primeiro da lista vale — só aqui.** Se `BRUNA` devolver mais de um cadastro,
o robô pega o primeiro em vez de abortar (auditoria:
`generico_primeiro_da_lista`, com os descartados). É a única exceção à regra
"nunca adivinhar": errar aqui custa um rascunho no cadastro errado, não
dinheiro. Quando o operador pede um CNPJ específico e a busca volta ambígua, o
robô **continua abortando** — ali o palpite manda o link de pagamento para o
cliente errado. Desliga com `ORCAMENTO_GENERICO_PRIMEIRO=0`.

A recursão é de um nível só: se o próprio genérico não for encontrado, aborta de
verdade. Para trocar: `ORCAMENTO_CNPJ_GENERICO` e `ORCAMENTO_TERMO_GENERICO`.

---

## 3 · Como rodar

```bash
# 1 · Só confere que as telas respondem. Nunca escreve.
python orcamentista.py --mapear

# 2 · Percorre tudo e diz o que faria. Nunca escreve.
python orcamentista.py --dry --cnpj 56128147000100 --itens BB02009:12,BB02001:6 --desconto 29

# 3 · Escreve de verdade.
python orcamentista.py --aplicar --cnpj 56128147000100 --itens BB02009:12 --desconto 29

# Debug local com navegador visível
python orcamentista.py --dry --cnpj ... --itens ... --headful
```

**Sempre nesta ordem: `mapear` → `dry` → `aplicar` com UM cliente → conferir no Mercos → lote.**
É o mesmo protocolo dos outros três robôs, e ele existe porque cada um deles quebrou pelo menos
uma vez na primeira rodada real.

### Códigos de saída

| Código | Significa |
|---|---|
| `0` | deu certo |
| `1` | erro inesperado (screenshot no artifact) |
| `2` | sessão do Mercos derrubada, desistiu depois das tentativas |
| `3` | **abortado — precisa de gente** (cliente ambíguo, estoque insuficiente, link ilegível) |

---

## 3b · 🔴 O `--dry` não é totalmente inócuo

O README prometia "nunca escreve". **Não é verdade, e o screenshot de 18/09
23:57 provou:** clicar em "Criar pedido / orçamento" (passo 2) já cria um
rascunho no Mercos — o run deixou o **#9306** lá, com o selo "Em orçamento".

O que o `--dry` garante é o que importa: ele não adiciona item, não aplica
desconto, não salva condição de pagamento e **não gera link de pagamento**. O que
ele deixa é um orçamento vazio, em rascunho.

Consequência prática: cada rodada de teste suja a lista de pedidos do Mercos com
um rascunho. Vale apagá-los depois, e vale saber disso antes de rodar `dry`
dezenas de vezes num dia de trabalho.

⚠️ E o botão **"Gerar pedido"** fica visível no topo dessa tela, ao lado de
"Visualizar" e "Enviar por WhatsApp" — que é exatamente por que a
`_guarda_gerar_pedido()` existe e é chamada antes de cada clique da vizinhança.

---

## 4 · O que fazer no GitHub

### 4.1 · Ativar o workflow

O arquivo está aqui como entregável e **o GitHub ignora ele nesta pasta**. Para ligar:

```bash
cp Gira/automacao-orcamento/workflow-mercos-orcamento.yml .github/workflows/
```

⚠️ **`workflow_dispatch` só aparece depois que o arquivo estiver na branch default.** Empurrar
numa branch de trabalho **não** faz o botão aparecer — é a mesma pegadinha do robô de crédito.

### 4.2 · Secrets (Settings → Secrets and variables → Actions → Secrets)

Nenhum é novo em relação aos robôs existentes:

| Secret | O quê |
|---|---|
| `MERCOS_EMAIL` | e-mail da conta do robô no Mercos |
| `MERCOS_SENHA` | senha dessa conta |
| `GMAIL_USER` | conta Gmail que recebe o código de 2FA |
| `GMAIL_SENHA` | ⚠️ **App Password**, não a senha da conta |
| `SUPABASE_URL` | auditoria |
| `SUPABASE_SERVICE_ROLE_KEY` | auditoria — as tabelas têm RLS, a chave anon não escreve |

🔴 **Sobre a senha da conta do robô:** ela foi passada em texto puro num chat durante a
construção. **Trocar a senha depois de cadastrar o secret** — transcript de conversa fica gravado,
e senha que passou por ali deve ser considerada exposta.

⚠️ **O App Password do Gmail expira sozinho** — quando a senha da conta muda, quando o 2SV é
reconfigurado, ou quando o admin do Workspace bloqueia app password. **Nada disso avisa.** Por
isso `conferir_credencial_gmail()` roda **antes** do navegador: o run morre em segundos com a
causa escrita, em vez de subir Chromium, logar, pedir 2FA e estourar um traceback de `imaplib` na
última linha do log.

### 4.3 · Variables (mesma tela, aba Variables)

| Variável | Para quê | Se vazia |
|---|---|---|
| `MERCOS_EMPRESA_ID` | empresa do Mercos | cai em `424525` (ES) |
| `MERCOS_MODULOS_DIR` | onde estão `mercos_login.py` / `mercos_ui.py` | tenta os caminhos padrão |

### 4.4 · 🔴 Os módulos compartilhados — a decisão que falta

`mercos_login.py` e `mercos_ui.py` vivem em `automacoes/cadastro-rca-mercos/` do repo
`torre_de_performance_b2b`. **Não copiar**, e o aviso está no docstring do próprio módulo:

> *"O Mercos muda o HTML sem avisar, e com duas cópias o primeiro robô é consertado e o segundo
> continua quebrado — em silêncio, porque 'não achei o cliente' é uma saída válida da função."*

| Se o repo de destino é… | O que fazer |
|---|---|
| **um espelho do rodízio** (`torre_b2b`, `torre_etl`, `torre_de_performance_b2b`) | **nada** — os módulos já estão lá |
| **um repo diferente** | sincronizar por push fast-forward, como o rodízio do ETL já faz, e apontar `MERCOS_MODULOS_DIR` |

---

## 5 · As decisões que valem discussão

### 5.1 · Por que não tem `schedule`

O Mercos aceita **uma sessão por usuário** e **já há três robôs disputando**. Os outros rodam de
madrugada justamente por isso.

**Este é o único que roda em horário comercial** — orçamento é reativo, o cliente acabou de
confirmar. Por isso: **só `workflow_dispatch`**, disparado pela aplicação quando há pedido
confirmado. Um cron aqui derrubaria a sessão de quem estivesse usando o Mercos no meio do
expediente.

### 5.2 · Por que a espera de sessão é 45 s e não 15 min

O robô de crédito espera **900 s** antes de reentrar quando a sessão cai, e o motivo é bom:
reentrar na hora rouba a sessão de quem acabou de logar, a pessoa loga de novo, derruba o robô, e
os dois perdem a tarde.

Aqui a espera é **45 s**, e é escolha consciente: a tela do cliente promete **link em até 5
minutos**. Esperar 15 min quebraria a promessa.

⚠️ **O risco continua existindo.** Está declarado aqui e no código — é uma escolha, não um
descuido. O precedente é o despacho da Análise de Crédito, que usa a mesma espera curta pela mesma
razão (alguém está esperando agora).

### 5.3 · O QR Code

A página de pagamento é **pública** — o cliente abre sem login. Por isso o robô a abre numa **aba
nova**, sem arriscar a sessão do Mercos, e fotografa o elemento do QR.

Se não conseguir isolar o QR, salva a página inteira e **declara no log**. Screenshot errado é
melhor que screenshot ausente, desde que se saiba qual dos dois é.

### 5.4 · 🔴 Para onde o link vai — isso ainda não está resolvido

O robô produz **link + QR**. Quem envia ao cliente é outro passo, e há uma restrição:

| Canal | Fala com | Manda imagem? |
|---|---|---|
| **Evolution API** | ⚠️ **só o RCA — interno** | sim, `sendMedia` |
| **Tallos / RD Conversas** | **o cliente** | só no header de template aprovado |
| **O próprio Mercos** | o cliente | tem botão de WhatsApp na lista de links |

⚠️ **Evolution não fala com cliente externo** — está escrito em três lugares do repo. Então, para
o cliente, são duas opções: **Tallos com template de header de imagem** (precisa aprovação da
Meta, 24–48 h) ou **o botão de WhatsApp do próprio Mercos** (o ícone verde na lista de pagamentos,
ainda não mapeado).

**A segunda é muito mais barata se funcionar.** Vale um teste manual antes de escolher.

---

## 6 · Auditoria

Cada evento sai como uma linha `AUDIT {...}` em JSON no log:

| Evento | Quando |
|---|---|
| `item_adicionado` | por SKU, com a quantidade e o estoque lido |
| `desconto_aplicado` | com o % |
| `detalhes_salvos` | condição de pagamento e transportadora |
| `link_criado` | com a URL |
| `qrcode_capturado` | com o arquivo |
| `rodada_ok` | resumo final |
| `abortado` | com o motivo, quando precisa de gente |
| `sessao_derrubada_*` | tentativa e espera |

O workflow extrai essas linhas para o resumo do Actions, e o **artifact guarda log + screenshots +
QR por 90 dias** — a tela do Actions expira, o artifact não.

---

## 7 · O que ainda falta

- [ ] Rodar `--mapear` em produção e conferir os seletores contra o HTML real
- [ ] Rodar `--dry` com um cliente real
- [ ] Rodar `--aplicar` com **um** cliente e conferir o orçamento no Mercos antes de qualquer lote
- [ ] Decidir e mapear o **envio ao cliente** (§5.4)
- [ ] Ligar a **fila**: hoje o robô recebe CNPJ e itens por input; o disparo automático a partir
      do Pedido Pronto confirmado ainda não está escrito
- [ ] Tabelas de auditoria no Supabase (o código já emite os eventos; falta persistir)
- [ ] Trocar a senha da conta do Mercos (§4.2)
