---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: 993e272ea19e
polling:
  interval_ms: 30000
workspace:
  root: ~/symphony_workspaces
  repo: ~/Downloads/candidates
agent:
  max_concurrent_agents: 3
codex:
  command: claude
  stall_timeout_ms: 1800000
---
Ты работаешь над {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

Ты работаешь в репозитории ~/Downloads/candidates — это локальный репозиторий без remote.
Все изменения делаются через git worktree в отдельной ветке.

Когда задача выполнена:
1. Закоммить изменения
2. Напиши [DONE] в последнем сообщении.
