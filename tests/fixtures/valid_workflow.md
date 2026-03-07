---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: my-project
  active_states: Todo, In Progress
polling:
  interval_ms: 15000
workspace:
  root: ~/workspaces
agent:
  max_concurrent_agents: 5
codex:
  command: claude
---
You are working on {{ issue.identifier }}: {{ issue.title }}

Labels: {{ issue.labels }}
