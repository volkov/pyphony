from __future__ import annotations

from jinja2 import (
    Environment,
    StrictUndefined,
    TemplateAssertionError,
    TemplateSyntaxError,
    UndefinedError,
)

from pyphony.errors import TemplateParseError, TemplateRenderError
from pyphony.models import Issue
from pyphony.normalization import normalize_label

DEFAULT_PROMPT = "You are working on an issue from Linear."

_PLAN_REQUIRED_SUFFIX = """

---
**Этот тикет помечен как «plan required».**
Твоя задача — исследовать кодовую базу и составить детальный план реализации.

**НЕ** пиши код и не вноси изменений в файлы. Только исследуй и планируй.

Когда готово — напиши детальный план реализации и [DONE] в последнем сообщении.
"""

_RESOLVE_CONFLICT_SUFFIX = """

---
**Этот тикет помечен как «resolve-conflict».**
PR этого тикета не смог быть смержен из-за конфликтов с main веткой.

Твоя задача — разрешить merge-конфликты:

1. `git fetch origin main` — получить последний main
2. `git rebase origin/main` или `git merge origin/main` — интегрировать изменения
3. Разрешить все конфликты вручную
4. `git push --force-with-lease` — запушить обновлённую ветку
5. Убедиться что код компилируется/проходит базовые проверки

Когда конфликты разрешены и ветка запушена — напиши [DONE] в последнем сообщении.
"""


def render_prompt(
    template: str,
    issue: Issue,
    attempt: int | None = None,
    comments: list[dict] | None = None,
) -> str:
    body = template.strip()
    if not body:
        rendered = DEFAULT_PROMPT
    else:
        env = Environment(undefined=StrictUndefined)

        try:
            tpl = env.from_string(body)
        except TemplateAssertionError as exc:
            raise TemplateRenderError(str(exc)) from exc
        except TemplateSyntaxError as exc:
            raise TemplateParseError(str(exc)) from exc

        try:
            rendered = tpl.render(issue=issue.model_dump(), attempt=attempt)
        except UndefinedError as exc:
            raise TemplateRenderError(str(exc)) from exc
        except Exception as exc:
            raise TemplateRenderError(str(exc)) from exc

    if comments:
        updated_str = ""
        if issue.updated_at:
            updated_str = f" (updated: {issue.updated_at.strftime('%Y-%m-%d %H:%M UTC')})"
        rendered += f"\n\n---\n## Comments on this issue{updated_str}:\n"
        for comment in comments:
            user = comment.get("user", "Unknown")
            created_at = comment.get("created_at", "")
            comment_body = comment.get("body", "")
            rendered += f"\n**{user}** ({created_at}):\n{comment_body}\n"

    # Append special instructions based on labels
    issue_labels_normalized = [normalize_label(label) for label in issue.labels]
    if "plan required" in issue_labels_normalized:
        rendered += _PLAN_REQUIRED_SUFFIX
    elif "resolve conflict" in issue_labels_normalized:
        rendered += _RESOLVE_CONFLICT_SUFFIX

    return rendered
