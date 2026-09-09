class AuthoringError(RuntimeError):
    """Base error for the isolated authoring runtime."""


class StateCorruptionError(AuthoringError):
    pass


class ToolAuthorizationError(AuthoringError):
    pass


class ToolConflictError(AuthoringError):
    pass


class TerminalPostconditionError(AuthoringError):
    pass


class LeaseUnavailableError(AuthoringError):
    pass


class ContextCompactionError(AuthoringError):
    pass
