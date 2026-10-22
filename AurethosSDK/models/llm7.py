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
    clear_html_block,
    extract_first_json_from_markdown,
    has_multiple_top_level_jsons,
)
from AurethosSDK.utils.history import HistoryStorage


class LLM7Client(BaseModel):
    """
    Client for Text LLM7 API endpoint,
    extending BaseModel and storing per-instance history.
    """

    # Class-level defaults
    base_url = "https://api.llm7.io/"
    default_model = "gpt-4.1-nano-2025-04-14"
    proxies = None

    def __init__(self, model: BaseModel, checkup: bool = False, **kwargs):
        # Initialize BaseModel (sets up self.logger and self.settings)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.settings = model.settings
        self.model = model
        self.user_agent = UserAgent()  # setted for better avability, longer life of API
        # self.device_id = uuid.uuid4().hex    # setted for better avability, longer life of API
        for k, v in kwargs.items():
            setattr(self.model, k, v)

        # Per-instance history as list of dict entries
        self.history = (
            HistoryStorage()
        )  # Each entry: {role, content} or if role: 'assistant' or 'user' {role, content}

        # Overrideable defaults come from settings attributes if provided
        self.use_model = getattr(self.settings, "use_model", self.default_model)
        self.temperature = getattr(self.model, "temperature", 0.7)
        self.stream = getattr(self.model, "stream", True)
        self.json_mode = getattr(self.model, "json_mode", False)
        self.token = getattr(self.model, "token", None)

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
        temperature=None,
        stream=None,
        json_mode=None,
        retry_request=3,
    ) -> Optional[dict]:
        """
        Chat completions (text generation). Records per-instance history.
        """
        # time.sleep(10)
        self.history.add_entry("user", message)
        payload = {
            "model": model or self.use_model,
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
            ],  # Overwrite 'content' with json.dumps, костыль для апи, json ему не нравится
            "temperature": temperature or self.temperature,
            "stream": stream or self.stream,
        }
        if self.token is not None:
            payload["token"] = self.token
        payload.update(self.settings.generation_setting.model_dump())

        self.logger.info(
            f"Sending chat_completion request: {json.dumps(payload, ensure_ascii=False)}",
        )

        resp = self._send_request(
            url=self.base_url + "v1/chat/completions",
            method="POST",
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent.random,
            },
            json=payload,
            proxies=self.proxies,
            stream=payload.get("stream", False),
        )

        resp.raise_for_status()
        if resp is None:
            if retry_request > 0:
                self.logger.error("Server overloaded. Wait 60 seconds to retry")
                time.sleep(60)
                self.history.delete_last_by_count(1)
                return self.chat_completion(
                    message, model, temperature, stream, json_mode, retry_request - 1
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
                content = clear_html_block(content)

                # if has_multiple_top_level_jsons(content):
                #     self.logger.error("Server response contains more than one JSON blocks. Retry...")
                #     self.history.delete_last_by_count(1)
                #     return self.chat_completion(
                #         message,
                #         model,
                #         temperature,
                #         stream,
                #         json_mode
                #     )

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
                                message, model, temperature, stream, json_mode
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
                    return self.chat_completion(
                        message, model, temperature, stream, json_mode
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
                        temperature,
                        stream,
                        json_mode,
                        retry_request - 1,
                    )
                else:
                    self.logger.exception(
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
                if line.startswith("data: "):
                    if line[6:] == "[DONE]":
                        break
                    chunk_data = json.loads(line[6:])
                    sorted_choices = sorted(
                        chunk_data["choices"], key=lambda x: x["index"]
                    )  # sort choices by "index"
                    for choice in sorted_choices:
                        if choice["finish_reason"] == "stop":
                            continue
                        content += choice.get("delta", {}).get("content", "")
                    if chunk_data.get("choices"):
                        del chunk_data["choices"]
                    result.update(chunk_data)
            except json.JSONDecodeError:
                self.logger.error(
                    f"Failed to decode chat_completion chunk response as JSON, line: {line}",
                )
        result["content"] = content

        return result

    def check_status(self) -> tuple[bool, Optional[dict[str, any]]]:
        resp = self._send_request(
            url=self.base_url + "healthy",
            method="GET",
            proxies=self.proxies,
        )

        if resp is None:
            return (False, None)
        if resp.status_code != 200:
            return (False, None)

        try:
            resp_json = resp.json()
        except:
            return (True, {"content": resp.text})

        error_status = resp_json.get("status", -1)
        error_text = resp_json.get("error", -1)

        report = {
            "status_code": resp.status_code,
            "error_status": error_status,
            "error_text": error_text,
        }

        if error_status == "ok":
            return (True, report)
        else:
            return (False, report)
