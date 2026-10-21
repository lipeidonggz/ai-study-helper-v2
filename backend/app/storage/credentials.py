from __future__ import annotations

from typing import Protocol

import keyring
from keyring.errors import PasswordDeleteError

from app.ingestion.errors import IngestFailure

CREDENTIAL_SERVICE_NAME = "ai-study-helper"
EMBEDDING_API_KEY_USERNAME = "embedding-api-key"
CHAT_API_KEY_USERNAME = "chat-api-key"


class CredentialStore(Protocol):
    """API Key 安全存取的最小协议。"""

    def get(self, username: str) -> str | None:
        """读取指定凭证；不存在时返回 None。"""
        ...

    def set(self, username: str, value: str) -> None:
        """写入或替换指定凭证。"""
        ...

    def delete(self, username: str) -> None:
        """删除指定凭证；不存在时视为成功。"""
        ...


class WindowsCredentialStore:
    """通过 keyring 使用 Windows Credential Manager 保存 API Key。"""

    def __init__(self, service_name: str = CREDENTIAL_SERVICE_NAME) -> None:
        self._service_name = service_name

    def get(self, username: str) -> str | None:
        """读取 Credential Manager 中的单个凭证。"""

        return keyring.get_password(self._service_name, username)

    def set(self, username: str, value: str) -> None:
        """把非空密钥写入 Credential Manager。"""

        if not value.strip():
            raise IngestFailure(
                "credential_empty",
                "credential value must not be empty",
                422,
            )
        keyring.set_password(self._service_name, username, value)

    def delete(self, username: str) -> None:
        """删除凭证；keyring 未找到时保持幂等成功。"""

        try:
            keyring.delete_password(self._service_name, username)
        except PasswordDeleteError:
            return
