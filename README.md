# torre_actions

Casca de execução para rotinas agendadas. Contém apenas os workflows e o código que eles
executam.

- Só `schedule`, `workflow_dispatch` e `workflow_run`.
- `guard-invariantes.yml` roda em todo push e recusa o que sair do contrato deste repo.
- Configuração e credenciais ficam em variáveis e secrets do repositório, nunca em arquivo.

Documentação, contexto e procedimentos de operação não ficam aqui.
