"""Persist ASR params (cookies, device_id, web_id) to a JSON file.

Mirrors ASRParamsStore.swift.
Location: $XDG_CONFIG_HOME/doubao-murmur/asr_params.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from doubao_input.doubao.config import get_params_path

logger = logging.getLogger(__name__)


@dataclass
class ASRParams:
    """Parameters needed to establish a WSS ASR connection."""

    cookies: dict[str, str]
    device_id: str
    web_id: str

    @property
    def cookie_header(self) -> str:
        """Build the Cookie header string for HTTP/WSS requests."""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


class ParamsStore:
    """Persist ASR params to JSON file."""

    @staticmethod
    def save(params: ASRParams) -> None:
        try:
            data = json.dumps(asdict(params), ensure_ascii=False, indent=2)
            get_params_path().write_text(data, encoding="utf-8")
            logger.info("Saved ASR params to %s", get_params_path())
        except Exception as e:
            logger.error("Failed to save params: %s", e)

    @staticmethod
    def load() -> ASRParams | None:
        path = get_params_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ASRParams(**data)
        except Exception as e:
            logger.error("Failed to load params: %s", e)
            return None

    @staticmethod
    def clear() -> None:
        try:
            get_params_path().unlink(missing_ok=True)
            logger.info("Cleared saved params")
        except Exception as e:
            logger.error("Failed to clear params: %s", e)

    @staticmethod
    def has_saved() -> bool:
        return get_params_path().exists()
