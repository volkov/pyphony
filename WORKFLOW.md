---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: 1fc8e25cc22b
polling:
  interval_ms: 30000
hooks:
  after_create: "git clone git@github.com:volkov/pyphony.git . && git checkout -b $(basename $PWD)"
workspace:
  root: ~/symphony_workspaces
agent:
  max_concurrent_agents: 5
codex:
  command: claude
---
Ты работаешь над {{ issue.identifier }}: {{ issue.title }}

{{ issue.description }}

Когда задача выполнена, используй linear_graphql tool чтобы перевести issue в статус "Done".
