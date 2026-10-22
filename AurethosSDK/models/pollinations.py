import json
import logging
import random
import time
from typing import Optional

import requests
import yaml

from aurethos_coder.config import politoken
from AurethosSDK.models.base import BaseModel
from AurethosSDK.utils.clear_json import (
    clean_markdown_in_json_blocks,
    clear_html_block,
    extract_first_json_from_markdown,
)
from AurethosSDK.utils.history import HistoryStorage


class PollinationsClient(BaseModel):
    """
    Client for Text & Multimodal (OpenAI-compatible) Pollinations endpoint,
    extending BaseModel and storing per-instance history.
    """

    # Class-level defaults
    base_url = "https://text.pollinations.ai/"
    default_model = "deepseek-reasoning"
    default_voice = "nova"
    default_private = False
    proxies = None

    def __init__(self, model: BaseModel, checkup: bool = False, **kwargs):
        # Initialize BaseModel (sets up self.logger and self.settings)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.settings = model.settings
        self.model = model
        for k, v in kwargs.items():
            setattr(self.model, k, v)

        self.history = HistoryStorage()  # Each entry: {role, content}

        # Overrideable defaults come from settings attributes if provided
        self.use_model = getattr(self.settings, "use_model", self.default_model)
        self.voice = getattr(self.model, "voice", self.default_voice)
        self.private = getattr(self.model, "private", self.default_private)
        self.seed = getattr(self.model, "seed", random.randrange(1, 99999))
        self.json_mode = getattr(self.model, "json_mode", False)
        self.tools = getattr(self.model, "tools", None)
        self.tool_choice = getattr(
            self.model, "tool_choice", None
        )  # Methods: none / auto / required
        self.token = getattr(self.model, "token", politoken)

        if not checkup:
            # Per-instance history as list of dict entries
            self.history.add_entry("system", self.settings.system_instruction)

    def _send_request(
        self,
        url: str,
        method: str = "POST",
        headers: dict | None = None,
        proxies: dict | None = None,
        json: dict | None = None,
        stream: bool | None = None,
        **kwargs,
    ):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                proxies=proxies or self.proxies,
                json=json,
                stream=stream,
                **kwargs,
            )

            self.logger.debug(
                f"HTTP POST to {response.url} returned status {response.status_code}, answer: {response.text}"
            )

            return response
        except requests.RequestException as e:
            return None

    def chat_completion(
        self,
        message,
        model=None,
        seed=None,
        json_mode=None,
        tools=None,
        tool_choice=None,
        private=None,
        retry_request=3,
    ) -> Optional[dict]:
        """
        Chat completions (text generation). Records per-instance history.
        """
        # time.sleep(7.5)
        self.history.add_entry("user", message)
        payload = {
            "model": model or self.use_model,
            "messages": self.history.get_all_entries(),
            "private": self.private if private is None else private,
        }
        # Optional flags
        if seed is not None or self.seed is not None:
            payload["seed"] = seed if seed is not None else self.seed
        if json_mode or self.json_mode:
            payload["jsonMode"] = True
        if tools or self.tools:
            payload["tools"] = tools or self.tools
        if tool_choice or self.tool_choice:
            payload["tool_choice"] = tool_choice or self.tool_choice
        if self.token:
            payload["token"] = self.token
        payload.update(self.settings.generation_setting.model_dump())

        self.logger.info(
            f"Sending chat_completion request: {json.dumps(payload, ensure_ascii=False)}"
        )
        resp = self._send_request(
            url=self.base_url + "openai",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            json=payload,
            stream=payload.get("stream", False),
            proxies=self.proxies,
        )

        resp.raise_for_status()
        if resp is None:
            if retry_request > 0:
                self.logger.error("Server overloaded. Wait 60 seconds to retry")
                time.sleep(60)
                self.history.delete_last_by_count(1)
                return self.chat_completion(
                    message,
                    model,
                    seed,
                    json_mode,
                    tools,
                    tool_choice,
                    private,
                    retry_request - 1,
                )

            else:
                self.logger.exception(
                    f"chat_completion request failed: {e}", exc_info=True
                )
                raise

        else:
            try:
                result = resp.json()
                answer = (
                    result["choices"][0]["message"]["content"]
                    or result["choices"][0]["message"]["refusal"]
                )
                answer = clear_html_block(answer)
                if not answer:
                    self.logger.error("Server given None. Retry...")
                    self.history.delete_last_by_count(1)
                    return self.chat_completion(
                        message,
                        model,
                        seed,
                        json_mode,
                        tools,
                        tool_choice,
                        private,
                    )

                self.logger.info("Received chat_completion response")
                self.logger.debug(
                    f"chat_completion response JSON: {json.dumps(result, ensure_ascii=False)}"
                )
                self.history.add_entry("assistant", answer)
                if json_mode or self.json_mode:
                    res = clean_markdown_in_json_blocks(answer)
                    res = extract_first_json_from_markdown(res)
                    if not res:
                        self.logger.error(
                            "Server given None or response didn't parsed. JSON invalid. Trying YAML safe_load..."
                        )
                        try:
                            content = yaml.safe_load(content)
                            if not isinstance(content, dict):
                                raise
                        except:
                            self.logger.error(
                                "Server given None or response didn't parsed. YAML safe_load didn't help. Retry..."
                            )
                            self.history.delete_last_by_count(1)
                            return self.chat_completion(
                                message,
                                model,
                                seed,
                                json_mode,
                                tools,
                                tool_choice,
                                private,
                            )
                    return res

                else:
                    return answer

            except Exception as e:
                if retry_request > 0:
                    return self.chat_completion(
                        message,
                        model,
                        seed,
                        json_mode,
                        tools,
                        tool_choice,
                        private,
                        retry_request - 1,
                    )
                else:
                    self.logger.exception(
                        f"Failed to decode chat_completion response JSON: {e}",
                        exc_info=True,
                    )
                    raise

    def check_status(self) -> tuple[bool, Optional[dict[str, any]]]:
        resp = self._send_request(
            url=self.base_url + f"sayhi?model={self.use_model or self.default_model}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self.token}",
            },
            proxies=self.proxies,
        )

        if resp is None:
            return (False, None)
        if resp.status_code != 200:
            return (False, None)

        try:
            resp_json = resp.json()  # @IgnoreException
        except:
            return (True, {"content": resp.text})

        error_code = resp_json.get("status", -1)
        error_text = resp_json.get("error", -1)

        report = {
            "status_code": resp.status_code,
            "error_code": error_code,
            "error_text": error_text,
        }

        return (False, report)
