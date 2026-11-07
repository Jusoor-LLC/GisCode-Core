class OCRError(Exception):
    pass


class OCRRuntimeUnavailableError(OCRError):
    pass


class OCRTimeoutError(OCRError):
    pass


class OCRCancelledError(OCRError):
    pass


class OCRValidationError(OCRError):
    pass


class OCRWorkspaceError(OCRError):
    pass


class OCRPreprocessingError(OCRError):
    pass
