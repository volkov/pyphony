class PyphonyError(Exception):
    pass


class MissingWorkflowFile(PyphonyError):
    pass


class WorkflowParseError(PyphonyError):
    pass


class WorkflowFrontMatterNotAMap(PyphonyError):
    pass


class TemplateParseError(PyphonyError):
    pass


class TemplateRenderError(PyphonyError):
    pass


class ConfigValidationError(PyphonyError):
    pass


class TrackerError(PyphonyError):
    pass


class UnsupportedTrackerKind(TrackerError):
    pass


class MissingTrackerApiKey(TrackerError):
    pass


class MissingTrackerProjectSlug(TrackerError):
    pass


class LinearApiRequestError(TrackerError):
    pass


class LinearApiStatusError(TrackerError):
    pass


class LinearGraphQLError(TrackerError):
    pass


class LinearUnknownPayload(TrackerError):
    pass


class LinearMissingEndCursor(TrackerError):
    pass


class HookError(PyphonyError):
    pass


class HookTimeoutError(HookError):
    pass


class AgentError(PyphonyError):
    pass


class AgentNotFound(AgentError):
    pass


class InvalidWorkspaceCwd(AgentError):
    pass


class AgentProcessExit(AgentError):
    pass


class TurnTimeout(AgentError):
    pass
