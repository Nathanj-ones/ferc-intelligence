from __future__ import annotations

from typing import Any

import requests

from .config import API_BASE_URL, get_api_key


class FERCClient:
    """Small wrapper around the official FERC Data API."""

    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": get_api_key()})

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def data_assets(self) -> dict[str, Any]:
        return self.get("data-assets/")

    def dataset_details(self, dataset_id: int) -> dict[str, Any]:
        # Dataset endpoint shape may vary by current FERC API release.
        return self.get(f"datasets/{dataset_id}")

    def dataset_data(self, dataset_id: int) -> dict[str, Any]:
        # Dataset endpoint shape may vary by current FERC API release.
        return self.get(f"datasets/{dataset_id}/data")
