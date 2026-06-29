import json
import random
import subprocess
import time
from base64 import b64encode
from typing import Any

import httpx
from django.conf import settings


class OpenCodeClientError(Exception):
    """Raised when OpenCode API requests fail."""


class OpenCodeClient:
    def __init__(self, port: int):
        self.base_url = f"http://127.0.0.1:{port}"
        self.timeout = settings.OPENCODE_REQUEST_TIMEOUT_SECONDS
        self.max_retries = max(1, settings.OPENCODE_CLIENT_MAX_RETRIES)
        self.backoff_base_seconds = max(0.1, settings.OPENCODE_CLIENT_BACKOFF_BASE_SECONDS)
        self.circuit_breaker_threshold = max(1, settings.OPENCODE_CIRCUIT_BREAKER_THRESHOLD)
        self.circuit_breaker_cooldown_seconds = max(1, settings.OPENCODE_CIRCUIT_BREAKER_COOLDOWN_SECONDS)
        self.headers = self._build_headers()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _build_headers(self) -> dict[str, str]:
        password = self._resolve_server_password()
        if not password:
            return {}
        token = b64encode(f"opencode:{password}".encode("utf-8")).decode("utf-8")
        return {"Authorization": f"Basic {token}"}

    def _resolve_server_password(self) -> str:
        configured = settings.__dict__.get("OPENCODE_SERVER_PASSWORD") or None
        if configured:
            return configured

        try:
            result = subprocess.run(
                [settings.OPENCODE_BINARY_PATH, "service", "password"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return ""

        return result.stdout.strip()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if time.monotonic() < self._circuit_open_until:
            wait_seconds = round(self._circuit_open_until - time.monotonic(), 2)
            raise OpenCodeClientError(
                f"OpenCode circuit breaker is open. Retry after ~{wait_seconds}s.",
            )

        timeout = httpx.Timeout(self.timeout)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(base_url=self.base_url, timeout=timeout, headers=self.headers) as client:
                    response = client.request(method, path, params=params, json=json_body)

                if response.status_code >= 500 or response.status_code == 429:
                    raise OpenCodeClientError(
                        f"OpenCode server error ({response.status_code}) on {method} {path}: {response.text}",
                    )

                response.raise_for_status()
                self._consecutive_failures = 0

                if not response.content:
                    return None
                return response.json()
            except (httpx.TransportError, httpx.TimeoutException, OpenCodeClientError, httpx.HTTPStatusError) as error:
                last_error = error
                self._consecutive_failures += 1

                if self._consecutive_failures >= self.circuit_breaker_threshold:
                    self._circuit_open_until = time.monotonic() + self.circuit_breaker_cooldown_seconds

                if attempt >= self.max_retries or not self._should_retry(error):
                    break

                sleep_seconds = self.backoff_base_seconds * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
                time.sleep(sleep_seconds)

        error_message = str(last_error) if last_error else "Unknown OpenCode client error"
        raise OpenCodeClientError(error_message) from last_error

    @staticmethod
    def _should_retry(error: Exception) -> bool:
        if isinstance(error, (httpx.TransportError, httpx.TimeoutException)):
            return True
        if isinstance(error, OpenCodeClientError):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code >= 500 or error.response.status_code == 429
        return False

    def create_session(self, directory: str, title: str, agent: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/session",
            params={"directory": directory},
            json_body={"title": title, "agent": agent},
        )

    def sessions(self, directory: str) -> Any:
        return self._request(
            "GET",
            "/session",
            params={"directory": directory},
        )

    def session(self, session_id: str, directory: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/session/{session_id}",
            params={"directory": directory},
        )

    def prompt(self, session_id: str, directory: str, prompt: str, agent: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/session/{session_id}/message",
            params={"directory": directory},
            json_body={
                "agent": agent,
                "parts": [{"type": "text", "text": prompt}],
            },
        )

    def session_diff(self, session_id: str, directory: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/session/{session_id}/diff",
            params={"directory": directory},
        )

    def session_messages(self, session_id: str, directory: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/session/{session_id}/message",
            params={"directory": directory},
        )

    def session_status(self, directory: str) -> dict[str, Any]:
        return self._request(
            "GET",
            "/session/status",
            params={"directory": directory},
        )

    def run_shell_command(self, session_id: str, directory: str, command: str, agent: str = "build") -> dict[str, Any]:
        return self._request(
            "POST",
            f"/session/{session_id}/shell",
            params={"directory": directory},
            json_body={"agent": agent, "command": command},
        )

    def abort_session(self, session_id: str, directory: str) -> Any:
        return self._request(
            "POST",
            f"/session/{session_id}/abort",
            params={"directory": directory},
        )

    def interrupt_session(self, session_id: str, directory: str) -> Any:
        return self.abort_session(session_id, directory)

    def session_todos(self, session_id: str, directory: str) -> Any:
        return self._request(
            "GET",
            f"/session/{session_id}/todos",
            params={"directory": directory},
        )

    def fork_session(
        self,
        session_id: str,
        directory: str,
        *,
        title: str = "",
        agent: str = "",
    ) -> Any:
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if agent:
            payload["agent"] = agent
        return self._request(
            "POST",
            f"/session/{session_id}/fork",
            params={"directory": directory},
            json_body=payload,
        )

    def summarize_session(self, session_id: str, directory: str, prompt: str = "") -> Any:
        payload: dict[str, Any] = {}
        if prompt:
            payload["prompt"] = prompt
        return self._request(
            "POST",
            f"/session/{session_id}/summarize",
            params={"directory": directory},
            json_body=payload,
        )

    def wait_for_idle(self, session_id: str, directory: str, timeout_seconds: int | None = None) -> dict[str, Any]:
        timeout_limit = timeout_seconds or self.timeout
        started_at = time.monotonic()
        while time.monotonic() - started_at < timeout_limit:
            status_payload = self.session_status(directory)
            session_state = status_payload.get(session_id)
            if not session_state:
                return status_payload
            if session_state.get("type") == "idle":
                return status_payload
            time.sleep(1)
        raise OpenCodeClientError(f"Timed out waiting for session {session_id} to become idle.")


def extract_text_from_parts(parts: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for part in parts:
        if part.get("type") == "text" and part.get("text"):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def diff_to_text(diff_payload: list[dict[str, Any]]) -> str:
    return json.dumps(diff_payload, indent=2)
