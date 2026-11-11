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
    """Generation request was cancelled.

    ``partial_text`` carries whatever the caller already displayed, so a
    cancelled answer can be kept and labelled instead of being presented as a
    completed reply.
    """

    partial_text: str = ""


class InferenceTimeoutError(InferenceError):
    """The provider stopped sending before the answer completed."""

    partial_text: str = ""
