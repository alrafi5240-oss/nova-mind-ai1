import os

from fastapi.testclient import TestClient

from nova_agent.config import Config
from nova_agent.server import create_app, is_loopback, load_dotenv
from nova_agent.tasks import TaskManager


def test_load_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nNOVA_T_A=from-file\nNOVA_T_B="quoted"  # note\nNOVA_T_C=keep\nbroken line\n')
    monkeypatch.setenv("NOVA_T_C", "from-env")
    for key in ("NOVA_T_A", "NOVA_T_B"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(env)
    assert os.environ["NOVA_T_A"] == "from-file"
    assert os.environ["NOVA_T_B"] == "quoted"
    assert os.environ["NOVA_T_C"] == "from-env"
    for key in ("NOVA_T_A", "NOVA_T_B"):
        monkeypatch.delenv(key)


def test_is_loopback():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.5")


def test_config_reports_lan_url(tmp_path):
    config = Config(sandbox="local", data_dir=tmp_path)
    app = create_app(config, TaskManager(config), lan_url="http://192.168.1.5:8787")
    with TestClient(app) as client:
        assert client.get("/api/config").json()["lan_url"] == "http://192.168.1.5:8787"
