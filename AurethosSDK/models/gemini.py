import logging
import time
from typing import Optional, Type, Union

import pydantic
from google.genai import Client, types
from google.genai.chats import Chat
from google.genai.errors import APIError, ClientError, ServerError
from typing_extensions import deprecated

from AurethosSDK.models.base import BaseModel
from AurethosSDK.utils.gemini_json_clear import (
    MarkdownJSONError,
    load_json_from_markdown,
)


@deprecated("Gemini API is deprecated, use `APIRouter` instead.")
class GeminiModel(BaseModel):
    def __init__(self, model: BaseModel, client: Client, **kwargs):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.settings = model.settings
        self.client = client
        self.model = model
        self.chat: Chat

        for k, v in kwargs.items():
            setattr(self.model, k, v)

        self.json_mode = getattr(self.model, "json_mode", False)

    def get_conversation(self, schema: Optional[Type[pydantic.BaseModel]] = None):
        """
        Creates and returns a new chat conversation using the provided schema.
        This method configures the conversation with the current model settings and system instruction.

        Args:
            schema (pydantic.BaseModel): The schema to use for the conversation.

        Returns:
            The created chat conversation object.
        """
        self.logger.debug("Trying to create conversation with schema: %s", schema)
        kwargs = self.settings.generation_setting.dict()
        kwargs.update(
            system_instruction=[
                types.Part.from_text(text=self.settings.system_instruction),
            ]
        )
        generate_content_config = types.GenerateContentConfig(**kwargs)

        if self.json_mode:
            self.logger.debug(
                "Answer method is not 'standart', applying schema and mime type."
            )
            if schema:
                kwargs["response_schema"] = schema
            kwargs["response_mime_type"] = "application/json"

        self.logger.info("Conversation configuration ready, creating chat.")
        self.chat = self.client.chats.create(
            model=self.settings.use_model, config=generate_content_config
        )

        return self.chat

    def send_message(self, message: str) -> Optional[Union[str, dict]]:
        try:
            time.sleep(7.5)  # x2 speed
            res = self.chat.send_message(message)
        except (ServerError, ClientError, APIError):
            self.logger.error("Server overloaded. Wait 60 seconds to retry")
            time.sleep(60)
            return self.send_message(message)  # without retry!

        self.logger.info("Received send_message response: %s", res)

        if isinstance(res.parsed, pydantic.BaseModel):
            return res.parsed.dict()
        elif res.parsed:
            return res.parsed  # type: ignore  # possible because schema sets ONLY as BaseModel, if this not BaseModel - is dict.
        elif res.text and self.json_mode:
            try:
                res = load_json_from_markdown(res.text)
                return res

            except MarkdownJSONError:
                return self.send_message(message)

        return res.text
