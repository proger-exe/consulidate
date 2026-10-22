import json
import logging
import random
import time
from typing import Optional

import requests
import yaml
from fake_useragent import UserAgent

from AurethosSDK.models.base import BaseModel
from AurethosSDK.utils.clear_json import (
    clean_markdown_in_json_blocks,
    extract_first_json_from_markdown,
)
from AurethosSDK.utils.history import HistoryStorage


class ForgeMLClient(BaseModel):
    """
    Client for Text ForgeML endpoint,
    extending BaseModel and storing per-instance history.
    """

    # Class-level defaults
    base_url = "https://www.chatwithmono.xyz/api/chat"
    default_model = "gpt-4.1"
    proxies = None

    def __init__(self, model: BaseModel, checkup: bool = False, **kwargs):
        # Initialize BaseModel (sets up self.logger and self.settings)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.settings = model.settings
        self.model = model
        self.user_agent = UserAgent()  # setted for better avability, longer life of API
        for k, v in kwargs.items():
            setattr(self.model, k, v)

        # Per-instance history as list of dict entries
        self.history = (
            HistoryStorage()
        )  # Each entry: {role, content} or if role: 'assistant' or 'user' {role, content}

        # Overrideable defaults come from settings attributes if provided
        self.use_model = getattr(self.settings, "use_model", self.default_model)
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
        json_mode=None,
        retry_request=3,
    ) -> Optional[dict]:
        """
        Chat completions (text generation). Records per-instance history.
        """
        # time.sleep(7.5)
        self.history.add_entry("user", message)
        payload = {
            "messages": [
                (
                    {
                        **entry,
                        "content": (
                            str(entry["content"])
                            if not isinstance(entry["content"], str)
                            else entry["content"]
                        ),
                    }
                    if "content" in entry
                    else entry
                )
                for entry in self.history.get_all_entries()
            ],  # костыль для апи, json ему не нравится видите ли
            "model": model or self.use_model,
            "max_tokens": 10000,
        }
        payload.update(self.settings.generation_setting.model_dump())

        self.logger.info(
            f"Sending chat_completion request: {json.dumps(payload, ensure_ascii=False)}",
        )

        resp = self._send_request(
            url=self.base_url,
            method="POST",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent.random,
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
                    message, model, json_mode, retry_request - 1
                )

            else:
                self.logger.exception(
                    f"chat_completion request failed: {e}", exc_info=True
                )
                raise

        else:
            try:
                result = {}
                for chunk in resp.iter_content(chunk_size=None):
                    result = self.parse_response_content(chunk.decode("utf-8"))
                content = result["content"]

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
                                json_mode,
                            )
                    else:
                        content = res

                result["content"] = content

                if not result.get("content", None) or (
                    (json_mode or self.json_mode) and not isinstance(content, dict)
                ):
                    self.logger.error(
                        "Server given None or response didn't parsed. Retry..."
                    )
                    self.history.delete_last_by_count(1)
                    return self.chat_completion(message, model, json_mode)

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
                        json_mode,
                        retry_request - 1,
                    )
                else:
                    self.logger.exception(
                        f"Failed to decode chat_completion response JSON: {e}",
                        exc_info=True,
                    )
                    raise

    def parse_response_content(self, data: str) -> dict:
        result = {}
        content = ""

        for line in data.split("\n"):
            if not line:
                continue
            try:
                if line.startswith("f:"):
                    result.update(json.loads(line[2:]))
                elif line.startswith("0:"):
                    chunk_data = json.loads(line[2:])
                    content += chunk_data
                elif line.startswith("e:") or line.startswith("d:"):
                    result.update(json.loads(line[2:]))
                elif line.startswith("3:"):
                    result.update({"status": line[2:]})
                else:
                    result.update(json.loads(line))
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
            "max_tokens": 10000,
        }
        payload.update(self.settings.generation_setting.model_dump())

        resp = self._send_request(
            url=self.base_url,
            method="POST",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent.random,
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

        error_status = result.get("status", "")
        content = result.get("content", "")

        report = {
            "status_code": resp.status_code,
            "error_status": error_status,
            "content": content,
        }

        if content == "" and error_status != "":
            return (False, report)
        else:
            return (True, report)
