"""The future desktop shell uses this read-only probe after starting the API."""

from fastapi.testclient import TestClient

from ankisense.llm import Settings


def test_health_identifies_the_local_service_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKISENSE_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("AI_MODE", "demo")
    from ankisense.app import create_app

    for mode in ("demo", "cloud"):
        settings = (Settings(mode="demo") if mode == "demo" else
                    Settings(mode="cloud", base_url="https://never-call.invalid",
                             model="fixture", api_key="PRIVATE-MODEL-KEY"))
        app = create_app(data_dir=tmp_path / mode, settings=settings)
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {
            "application": "AnkiSense", "api_version": 1,
            "status": "ok", "mode": mode,
            "app_version": "0.1.4", "capabilities": {"anki_setup": True, "ai_connection_test": True, "ai_task_switch": True},
        }
        assert "PRIVATE-MODEL-KEY" not in response.text
