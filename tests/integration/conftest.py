"""Integration tests need GCP. Skip them cleanly when credentials are absent."""

import os
import pathlib

import pytest

ADC = pathlib.Path.home() / ".config/gcloud/application_default_credentials.json"


def pytest_collection_modifyitems(config, items):
    if ADC.exists() or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    skip = pytest.mark.skip(reason="no Application Default Credentials; integration tests need GCP")
    for item in items:
        item.add_marker(skip)
