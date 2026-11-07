class InferenceError(Exception):
    """Base inference error."""


class ModelNotFoundError(InferenceError):
    """Requested model could not be found."""


class ModelAlreadyLoadingError(InferenceError):
    """A model is already in the process of loading."""


class InsufficientResourceBudgetError(InferenceError):
    """Not enough system resources to load the model."""


class InferenceUnavailableError(InferenceError):
    """Inference engine is not available."""


class LoadFailureError(InferenceError):
    """Failed to load the model."""


class UnloadFailureError(InferenceError):
    """Failed to unload the model."""


class RequestCancelledError(InferenceError):
    """Generation request was cancelled."""
