class OpenHandsError(Exception):
    pass


class OpenHandsUnavailableError(OpenHandsError):
    pass


class OpenHandsTimeoutError(OpenHandsError):
    pass


class OpenHandsCancelledError(OpenHandsError):
    pass


class OpenHandsWorkspaceError(OpenHandsError):
    pass
