import logging
import os
from typing import Any, Optional, Type

import requests

from AurethosSDK.models.base import BaseModel
from AurethosSDK.utils.clear_json import robust_parse_json
from AurethosSDK.utils.history import HistoryStorage
class CodexGatewayClient(BaseModel):
    """
    Client for external codex-gateway HTTP API.

    Uses gateway sessions internally, but keeps a local HistoryStorage so APIRouter
    can rebuild context and fall back to other backends when needed.
    """

    def __init__(self, model: BaseModel, checkup: bool = False, **kwargs):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.settings = model.settings
        self.model = model
        for k, v in kwargs.items():
            setattr(self.model, k, v)

        self.history = HistoryStorage()
        self.base_url = (
            os.environ.get("CODEX_GATEWAY_URL", "")
            .strip()
            .rstrip("/")
            or "http://127.0.0.1:8765"
        )
        self.token = (
            os.environ.get("CODEX_GATEWAY_TOKEN")
            or os.environ.get("CODEX_API_TOKEN")
            or None
        )
        self.use_model = (
            os.environ.get("CODEX_GATEWAY_MODEL", "").strip()
            or "gpt-5.4"
        )
        self.workspace = (
            os.environ.get("CODEX_GATEWAY_WORKSPACE", "").strip()
            or os.getcwd()
        )
        model_sandbox_env = f"CODEX_GATEWAY_SANDBOX_{self.settings.name}".upper()
        self.sandbox = (
            os.environ.get(model_sandbox_env, "")
            or (self.settings.codex_gateway_sandbox or "")
            or os.environ.get("CODEX_GATEWAY_SANDBOX", "")
        ).strip() or "workspace-write"
        try:
            self.timeout_seconds = int(
                os.environ.get("CODEX_GATEWAY_TIMEOUT_SECONDS", "600")
            )
        except ValueError:
            self.timeout_seconds = 600

        self.session_id: Optional[str] = None
        self.thread_id: Optional[str] = None
        self._sessions_by_model: dict[str, dict[str, Optional[str]]] = {}

        if not checkup:
            self.history.add_entry("system", self.settings.system_instruction)

    def _build_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[int], Any]:
        try:
            response = requests.request(
                method=method,
                url=f"{self.base_url}{path}",
                headers=self._build_headers(),
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            self.logger.debug("Codex gateway request failed: %s", exc)
            return None, {"error": str(exc)}

        try:
            body = response.json()
        except ValueError:
            body = response.text

        return response.status_code, body

    def _build_system_prompt(self) -> str:
        entries = self.history.get_all_entries()
        system_prompt = self.settings.system_instruction
        prior_entries = entries[:-1] if entries else []

        if prior_entries and prior_entries[0].get("role") == "system":
            system_content = prior_entries[0].get("content")
            if isinstance(system_content, str) and system_content.strip():
                system_prompt = system_content
        return system_prompt

    def _resolve_effective_model(self, model: Optional[str] = None) -> str:
        return model or self.use_model

    def _sync_active_session(self, model_name: str) -> dict[str, Optional[str]]:
        state = self._sessions_by_model.setdefault(
            model_name,
            {"session_id": None, "thread_id": None},
        )
        self.session_id = state["session_id"]
        self.thread_id = state["thread_id"]
        return state

    def _invalidate_session(self, model_name: str) -> None:
        state = self._sessions_by_model.setdefault(
            model_name,
            {"session_id": None, "thread_id": None},
        )
        state["session_id"] = None
        state["thread_id"] = None
        self.session_id = None
        self.thread_id = None

    def _create_session(self, model_name: str) -> bool:
        status_code, body = self._request_json(
            "POST",
            "/sessions",
            {
                "workspace": self.workspace,
                "model": model_name,
                "sandbox": self.sandbox,
            },
        )
        if (
            status_code is None
            or not (200 <= status_code < 300)
            or not isinstance(body, dict)
        ):
            self.logger.debug(
                "Codex gateway session create failed: status=%s body=%r",
                status_code,
                body,
            )
            return False

        state = self._sync_active_session(model_name)
        state["session_id"] = body.get("session_id")
        state["thread_id"] = body.get("thread_id")
        self.session_id = state["session_id"]
        self.thread_id = state["thread_id"]
        return bool(state["session_id"])

    def _chat_once(
        self,
        message: str,
        *,
        model_name: str,
        system_prompt: str,
    ) -> tuple[str, Any]:
        state = self._sync_active_session(model_name)
        if not state["session_id"]:
            if not self._create_session(model_name):
                return "error", {"detail": "session create failed"}
            state = self._sync_active_session(model_name)

        payload: dict[str, Any] = {
            "message": message,
            "session_id": state["session_id"],
            "response_format": "raw",
            "system_prompt": system_prompt,
        }

        status_code, body = self._request_json("POST", "/chat", payload)
        if status_code is None:
            return "error", body
        if (
            status_code == 404
            and isinstance(body, dict)
            and body.get("detail") == "session not found"
        ):
            return "missing_session", body
        if not (200 <= status_code < 300):
            return "error", body
        return "ok", body

    def check_status(self) -> tuple[bool, Optional[dict[str, Any]]]:
        if not self.token:
            self.logger.warning(
                "No CODEX_GATEWAY_TOKEN or CODEX_API_TOKEN set for CodexGatewayClient"
            )
            return (False, {"error": "missing bearer token"})

        status_code, body = self._request_json("GET", "/models/list")
        if (
            status_code is None
            or not (200 <= status_code < 300)
            or not isinstance(body, dict)
        ):
            return (
                False,
                {"status_code": status_code, "body": body},
            )

        models = body.get("models")
        if not isinstance(models, list):
            return (False, {"error": "invalid models payload", "body": body})
        if self.use_model not in models:
            return (
                False,
                {
                    "error": f"model `{self.use_model}` is not in gateway allowlist",
                    "models": models,
                },
            )

        return (
            True,
            {
                "default_model": body.get("default_model"),
                "models": models,
            },
        )

    def chat_completion(
        self,
        message,
        model=None,
        temperature=None,
        stream=None,
        json_mode=None,
        retry_request=3,
        response_schema: Optional[Type[Any]] = None,
    ) -> Optional[dict]:
        del temperature, stream, json_mode, retry_request, response_schema
        if not self.token:
            self.logger.warning("Codex gateway token is missing")
            return None

        model_name = self._resolve_effective_model(model)
        self.history.add_entry("user", message)
        system_prompt = self._build_system_prompt()

        reply: Any = None
        status, body = self._chat_once(
            message,
            model_name=model_name,
            system_prompt=system_prompt,
        )
        if status == "ok":
            if isinstance(body, dict):
                state = self._sync_active_session(model_name)
                state["thread_id"] = body.get("thread_id", state["thread_id"])
                self.thread_id = state["thread_id"]
                reply = body.get("reply")
        elif status == "missing_session":
            lost_session_id = self.session_id
            self.logger.debug(
                "Codex gateway session missing for model=%s session_id=%s",
                model_name,
                lost_session_id,
            )
            self.history.delete_last_by_count(1)
            self._invalidate_session(model_name)
            raise CodexGatewaySessionLostError(
                f"session for model `{model_name}` was lost; "
                "automatic context replay is disabled"
            )
        else:
            self.logger.debug("Codex gateway chat failed: %r", body)
            reply = None

        if reply is None:
            self.history.delete_last_by_count(1)
            return None

        self.history.add_entry("assistant", reply)
        # api_router and every handler downstream expect a dict for command/
        # expects validation. Other providers (llm7 et al) parse before
        # returning; do the same here. Falls back to raw string when reply
        # isn't JSON so free-form uses still work.
        parsed = robust_parse_json(reply)
        return parsed if parsed is not None else reply


class CodexGatewaySessionLostError(RuntimeError):
    """Raised when the remote gateway session is no longer available."""
