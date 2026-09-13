"""Configuration helpers for the FERC project."""

import os
from pathlib import Path

from dotenv import load_dotenv


# FERC Data API base URL
API_BASE_URL = "https://api.data.ferc.gov/v1"


# Find the root of the ferc_project folder and load its .env file.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def get_api_key() -> str:
    """Return the FERC Data API key."""

    api_key = os.getenv("FERC_API_KEY")

    if not api_key:
        raise RuntimeError(
            "FERC_API_KEY is not set. "
            "Add it to the .env file in the project root."
        )

    return api_key