import json
import logging
import random
import time
import uuid
from typing import Optional

import requests
import yaml
from fake_useragent import UserAgent

from AurethosSDK.models.base import BaseModel
from AurethosSDK.utils.clear_json import (
    clean_markdown_in_json_blocks,
    clear_html_block,
    extract_first_json_from_markdown,
)
from AurethosSDK.utils.history import HistoryStorage


class PandaAIClient(BaseModel):
    """
    Client for Text PandaRobot|RuoYI-AI endpoint,
    extending BaseModel and storing per-instance history.
    """

    # Class-level defaults
    base_url = "http://web.pandarobot.chat/api/chat/send"
    default_model = "deepseek/deepseek-r1"
    proxies = None

    def __init__(self, model: BaseModel, checkup: bool = False, **kwargs):
        # Initialize BaseModel (sets up self.logger and self.settings)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.settings = model.settings
        self.model = model
        self.user_agent = (
            UserAgent().random
        )  # setted for better avability, longer life of API
        for k, v in kwargs.items():
            setattr(self.model, k, v)

        # Per-instance history as list of dict entries
        self.history = (
            HistoryStorage()
        )  # Each entry: {role, content} or if role: 'assistant' or 'user' {role, content}

        # Overrideable defaults come from settings attributes if provided
        self.max_tokens = getattr(self.settings, "max_tokens", 1280000)
        self.temperature = getattr(self.model, "temperature", 0.7)
        self.top_p = getattr(self.model, "top_p", 1)
        self.presence_penalty = getattr(self.model, "presence_penalty", 0)
        self.frequency_penalty = getattr(self.model, "frequency_penalty", 0)
        self.use_model = getattr(self.model, "use_model", self.default_model)
        self.json_mode = getattr(self.model, "json_mode", False)

        if not checkup:
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
                f"HTTP POST to {response.url} returned status {response.status_code}, answer: {self.parse_response_content(response.text)}"
            )

            return response
        except requests.RequestException as e:
            return None

    def chat_completion(
        self,
        message,
        model=None,
        max_tokens=None,
        temperature=None,
        top_p=None,
        presence_penalty=None,
        frequency_penalty=None,
        json_mode=None,
        retry_request=3,
    ) -> Optional[dict]:
        """
        Chat completions (text generation). Records per-instance history.
        """
        # time.sleep(7.5)
        self.history.add_entry("user", message)
        payload = {
            "max_tokens": max_tokens or self.max_tokens,
            "model": model or self.use_model,
            "temperature": temperature or self.temperature,
            "top_p": top_p or self.top_p,
            "presence_penalty": presence_penalty or self.presence_penalty,
            "frequency_penalty": frequency_penalty or self.frequency_penalty,
            "messages": self.history.get_all_entries(),
            "stream": False,
            "kid": "",
            "chat_type": 0,
            "appId": "",
        }
        payload.update(self.settings.generation_setting.model_dump())

        self.logger.info(
            f"Sending chat_completion request: {json.dumps(payload, ensure_ascii=False)}",
        )

        resp = self._send_request(
            url=self.base_url,
            method="POST",
            headers={
                "Accept": "text/event-stream",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": self.user_agent,
            },
            json=payload,
            proxies=self.proxies,
            stream=payload.get("stream", False),
            verify=False,
            timeout=180,
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
                    max_tokens,
                    temperature,
                    top_p,
                    presence_penalty,
                    json_mode,
                    retry_request - 1,
                )

            else:
                self.logger.error(f"chat_completion request failed: {e}", exc_info=True)
                raise

        else:
            try:
                result = {}
                content = ""
                for chunk in resp.iter_content(chunk_size=None):
                    result = self.parse_response_content(chunk.decode("utf-8"))

                content = result["content"]
                # print("content111", content)
                content = clear_html_block(content)
                # print("content2222222222222", content)

                if json_mode or self.json_mode:
                    res = clean_markdown_in_json_blocks(content)
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
                                max_tokens,
                                temperature,
                                top_p,
                                presence_penalty,
                                json_mode,
                            )
                    else:
                        content = res

                result["content"] = content
                print("result[content]", result["content"])

                if not result.get("content", None) or (
                    (json_mode or self.json_mode) and not isinstance(content, dict)
                ):
                    self.logger.error(
                        "Server given None or response didn't parsed. Retry..."
                    )
                    self.history.delete_last_by_count(1)
                    return self.chat_completion(
                        message,
                        model,
                        max_tokens,
                        temperature,
                        top_p,
                        presence_penalty,
                        json_mode,
                    )

                self.logger.info("Received chat_completion response")
                self.logger.debug(
                    f"chat_completion response JSON: {json.dumps(result, ensure_ascii=False)}"
                )
                self.history.add_entry("assistant", result["content"])
                return result["content"]

            except Exception as e:
                resp.close()
                if retry_request > 0:
                    return self.chat_completion(
                        message,
                        model,
                        max_tokens,
                        temperature,
                        top_p,
                        presence_penalty,
                        json_mode,
                        retry_request - 1,
                    )
                else:
                    self.logger.error(
                        f"Failed to decode chat_completion response JSON: {e}",
                        exc_info=True,
                    )
                    raise

    def parse_response_content(self, response_text: str) -> dict:
        result = {}
        content = ""
        for line in response_text.split("\n"):
            if not line:
                continue
            try:
                if line.startswith("data:"):
                    chunk_data = json.loads(line[5:])
                    choices = chunk_data["choices"]
                    for choice in choices:
                        content += choice["delta"]["content"]
                    del chunk_data["choices"]
                    result.update(chunk_data)
            except json.JSONDecodeError:
                self.logger.error(
                    f"Failed to decode chat_completion chunk response as JSON, line: {line}",
                )
        result["content"] = content

        return result

    def check_status(self) -> tuple[bool, Optional[dict[str, any]]]:
        payload = {
            "messages": [{"role": "user", "content": "say hi"}],
            "model": self.use_model,
            "stream": False,
            "kid": "",
            "chat_type": 0,
            "appId": "",
        }
        payload.update(self.settings.generation_setting.model_dump())

        resp = self._send_request(
            url=self.base_url,
            method="POST",
            headers={
                "Accept": "text/event-stream",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": self.user_agent,
            },
            json=payload,
            stream=payload.get("stream", False),
            proxies=self.proxies,
        )

        if resp is None:
            return (False, None)

        try:
            result = {}
            for chunk in resp.iter_content(chunk_size=None):
                result = self.parse_response_content(chunk.decode("utf-8"))
            if resp.status_code != 200:
                raise  # @IgnoreException
        except:
            return (False, None)

        content = result.get("content", "")

        report = {
            "status_code": resp.status_code,
            "content": content,
        }

        if content == "":
            return (False, report)
        else:
            return (True, report)
