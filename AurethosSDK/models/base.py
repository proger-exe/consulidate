import json
import logging
import os
from typing import Any

from AurethosSDK.types.base_model import Model


class BaseModel:
    "Class of neural network model operation"

    def __init__(self, settings: Model, **kwargs):
        """Model Initialization Function

        Args:
            settings (Model): Generation parameters and framework configuration
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        self.settings = settings
        for k, v in kwargs.items():
            setattr(self, k, v)

        if settings.upload_from_file:
            self.settings.system_instruction = self._read_description_from_file(
                settings.system_instruction
            )

    def _read_description_from_file(self, path: str):
        """
        Reads the model system instruction from a file.
        This method loads and returns the contents of the specified file path.

        Args:
            path (str): The file path to read the system instruction from.

        Returns:
            str: The contents of the system instruction file.

        Raises:
            FileNotFoundError: If the specified file does not exist or is not a file.
        """
        self.logger.debug("Attempting to read system instruction from file: %s", path)
        if os.path.exists(path) and os.path.isfile(path):
            with open(path, "r+", encoding="utf-8") as description:
                return description.read()
        else:
            self.logger.error("File not found or is not a file: %s", path)
            raise FileNotFoundError(
                "The Model.system_instruction path does not exist (maybe you forgot to turn off `FromFile`?)"
            )

    def __str__(self):
        return self.settings.name


# знаю что это нахуя не нужно, но пусть будет, может пригодится)
# class BaseModelEncoder(json.JSONEncoder):
#     def default(self, obj: Any) -> Any:
#         # Handle BaseModel instances
#         if isinstance(obj, BaseModel):
#             result = {}
#             for key, value in obj.__dict__.items():
#                 if key == 'logger':
#                     continue  # Skip logger
#                 elif key == 'settings' and isinstance(value, Model):
#                     result[key] = self._serialize_model(value)
#                 else:
#                     result[key] = self._serialize_value(value)
#             return result
#         return super().default(obj)

#     def _serialize_model(self, model: 'Model') -> dict:
#         """Special handling for Model class using Pydantic v2's model_dump()"""
#         return model.model_dump()

#     def _serialize_value(self, value: Any) -> Any:
#         """Recursively handle different value types"""
#         if hasattr(value, '__dict__'):
#             return {k: self._serialize_value(v) for k, v in value.__dict__.items()}
#         elif isinstance(value, (list, tuple, set)):
#             return [self._serialize_value(x) for x in value]
#         elif isinstance(value, dict):
#             return {k: self._serialize_value(v) for k, v in value.items()}
#         else:
#             return value
