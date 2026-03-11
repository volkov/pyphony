---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: 8bf0109d135b
polling:
  interval_ms: 30000
hooks:
  after_create: "git clone git@github.com:toloka-partners/taiga-examples.git . && git checkout -b $(basename $PWD)"
workspace:
  root: ~/symphony_workspaces
agent:
  max_concurrent_agents: 5
codex:
  command: claude
  stall_timeout_ms: 1800000
---
Ты работаешь над {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

Когда задача выполнена:
1. Закоммить изменения
2. Запушь ветку и создай Pull Request с помощью `gh pr create`
3. Напиши [DONE] в последнем сообщении.
