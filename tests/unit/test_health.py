import pytest
from fastapi.testclient import TestClient

from backend.config import AppConfig
from backend.main import create_app


def test_health_reports_only_application_state() -> None:
    config = AppConfig()
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": config.version}
        assert client.get("/health").status_code == 404
        assert client.post("/api/v1/health").status_code == 405


def test_lifecycle_logs_start_and_stop(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="stock_manager"):
        with TestClient(create_app()):
            assert "application_started" in caplog.messages
        assert "application_stopped" in caplog.messages
