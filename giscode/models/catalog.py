from giscode.models.types import ModelMetadata, ModelRegistry


QWEN3_CODER_30B = ModelMetadata(
    name="Qwen3-Coder-30B-A3B-Instruct",
    family="Qwen3-Coder",
    path="",
    context_length=32768,
    gpu_layers=0,
    size_bytes=0,
    description="Qwen3-Coder 30B A3B Instruct - coding-focused model",
    tags=["coding", "planning", "instruct", "30b", "qwen3"],
    capabilities=["text", "chat", "coding"],
    source="catalog",
)


QWEN3_CODER_480B = ModelMetadata(
    name="Qwen3-Coder-480B-A35B-Instruct",
    family="Qwen3-Coder",
    path="",
    context_length=32768,
    gpu_layers=0,
    size_bytes=0,
    description="Qwen3-Coder 480B A35B Instruct - large coding model",
    tags=["coding", "instruct", "480b", "qwen3"],
    capabilities=["text", "chat", "coding"],
    source="catalog",
)


QWEN3_VL_4B = ModelMetadata(
    name="Qwen3-VL-4B-Instruct",
    family="Qwen3-VL",
    path="",
    context_length=4096,
    gpu_layers=0,
    size_bytes=0,
    description="Qwen3-VL 4B Instruct - vision-language model",
    tags=["vision", "vl", "instruct", "4b", "qwen3"],
    capabilities=["text", "chat", "vision"],
    source="catalog",
)


def register_default_models(registry: ModelRegistry) -> None:
    registry.register(QWEN3_CODER_30B)
    registry.register(QWEN3_CODER_480B)
    registry.register(QWEN3_VL_4B)
