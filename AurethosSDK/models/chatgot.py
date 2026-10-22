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
    extract_first_json_from_markdown,
)
from AurethosSDK.utils.history import HistoryStorage


class ChatGotClient(BaseModel):
    """
    Client for Text ChatGot API endpoint,
    extending BaseModel and storing per-instance history.
    """

    # Class-level defaults
    base_url = "https://api-preview.chatgot.io/api/v1/char-gpt/conversations"
    default_model = "deepseek-r1"
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
        # time.sleep(10)
        self.history.add_entry("user", message)
        payload = {
            "device_id": uuid.uuid4().hex,
            "model_id": 1,
            "include_reasoning": True,
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
                "Connection": "keep-alive",
                "Content-Type": "application/json;charset=UTF-8",
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
                    message, model, json_mode, retry_request - 1
                )

            else:
                self.logger.error(f"chat_completion request failed: {e}", exc_info=True)
                raise

        else:
            try:
                result = {}
                reasoning_content = ""
                content = ""
                for chunk in resp.iter_content(chunk_size=None):
                    result = self.parse_response_content(chunk.decode("utf-8"))
                content = result["content"]
                reasoning_content = result["reasoning_content"]

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
                            return self.chat_completion(message, model, json_mode)
                    else:
                        content = res

                result["reasoning_content"] = reasoning_content
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
                self.history.add_entry(
                    "assistant", result["content"], reasoning_content=reasoning_content
                )
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
                    self.logger.error(
                        f"Failed to decode chat_completion response JSON: {e}",
                        exc_info=True,
                    )
                    raise

    def parse_response_content(self, data: str) -> dict:
        result = {}
        reasoning_content = ""
        content = ""

        for line in data.split("\n"):
            if not line:
                continue
            try:
                if line.startswith("data: "):
                    chunk_data = json.loads(line[5:])
                    if chunk_data["code"] == 202:
                        reasoning_content += chunk_data["data"]["reasoning_content"]
                        content += chunk_data["data"]["content"]
                    del chunk_data["data"]
                    result.update(chunk_data)
                elif not line.startswith("id:"):
                    result.update(json.loads(line))
            except json.JSONDecodeError:
                self.logger.error(
                    f"Failed to decode chat_completion chunk response as JSON, line: {line}",
                )
        result["reasoning_content"] = reasoning_content
        result["content"] = content

        return result

    def check_status(self) -> tuple[bool, Optional[dict[str, any]]]:
        payload = {
            "device_id": uuid.uuid4().hex,
            "model_id": 2,
            "include_reasoning": False,
            "messages": [{"role": "user", "content": "say hi"}],
        }

        resp = self._send_request(
            url=self.base_url,
            method="POST",
            headers={
                "Accept": "text/event-stream",
                "Connection": "keep-alive",
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": self.user_agent.random,
            },
            json=payload,
            proxies=self.proxies,
            stream=payload.get("stream", False),
        )

        if resp is None:
            return (False, None)

        try:
            result = {}
            for chunk in resp.iter_content(chunk_size=None):
                result = self.parse_response_content(chunk.decode("utf-8"))
            content = result["content"]
            if resp.status_code != 200:
                raise
        except:
            return (False, None)

        error_code = result.get("code", -1)
        error_message = result.get("message", "")
        error_list = result.get("errors", {})

        report = {
            "status_code": resp.status_code,
            "error_code": error_code,
            "content": content,
            "error_message": error_message,
            "error_list": error_list,
        }

        if error_message != "" and error_list != {}:
            return (False, report)
        else:
            return (True, report)
