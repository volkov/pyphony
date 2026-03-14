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
claude:
  command: claude
  stall_timeout_ms: 1800000
---
Ты работаешь над {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

Ты работаешь в локальном репозитории без remote.
Все файлы и изменения делай прямо в текущей директории (cwd).
НЕ создавай дополнительных worktree или веток — просто работай в текущей папке.

Когда задача выполнена:
1. Закоммить изменения в текущей ветке
2. Напиши [DONE] в последнем сообщении.
