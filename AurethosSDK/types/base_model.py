from typing import Dict, List, Optional, Union

from pydantic import BaseModel


class Generation(BaseModel):
    """
    Represents the generation settings for a model.
    This class defines parameters that control the output generation behavior.

    Attributes:
        temperature (Optional[int | float]): Sampling temperature for generation.
        top_p (Optional[float]): Nucleus sampling probability.
        top_k (Optional[int]): Number of highest probability vocabulary tokens to keep for top-k-filtering.
        max_output_tokens (Optional[int]): Maximum number of tokens in the generated output.
        response_mime_type (Optional[str]): The MIME type of the response.
    """

    temperature: Optional[Union[int, float]] = 1
    top_p: Optional[float] = 0.95
    top_k: Optional[int] = 40
    max_output_tokens: Optional[int] = 8192
    response_mime_type: Optional[str] = "text/plain"


class Model(BaseModel):
    """
    Represents a model configuration for the SDK.
    This class holds metadata, system instructions, and generation settings for a model.

    Attributes:
        name (str): The name of the model.
        system_instruction (str): The system instruction for the model.
        upload_from_file (bool): Whether the model supports file uploads.
        priority (int): The priority level of the model.
        use_model (str): The identifier of the model to use.
        generation_setting (Generation): The generation settings for the model.
        answer_method (str): The method used to generate answers (default is "standart").
    """

    name: str
    system_instruction: str
    upload_from_file: bool = False

    priority: int = 1

    use_model: str = "gpt-4.1-nano-2025-04-14"
    use_model_cheap: Optional[str] = None
    generation_setting: Generation = Generation()

    codex_gateway_sandbox: Optional[str] = None

    def __str__(self):
        return self.name
