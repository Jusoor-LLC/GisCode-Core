class ResearchError(Exception):
    pass


class ResearchValidationError(ResearchError):
    pass


class ResearchProviderUnavailableError(ResearchError):
    pass


class ResearchTimeoutError(ResearchError):
    pass


class ResearchCancelledError(ResearchError):
    pass
