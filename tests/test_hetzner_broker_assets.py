"""Static guardrails for the dedicated broker host bundle."""

from pathlib import Path


_BROKER = Path(__file__).resolve().parents[1] / "deploy" / "broker"


def test_broker_compose_publishes_only_to_host_loopback():
    compose = (_BROKER / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8181:8000"' in compose
    assert '"443:' not in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose


def test_broker_caddy_requires_mtls_and_proxies_only_local_broker():
    caddy = (_BROKER / "Caddyfile").read_text(encoding="utf-8")
    assert "admin off" in caddy
    assert "strict_sni_host on" in caddy
    assert "mode require_and_verify" in caddy
    assert "trust_pool file /etc/caddy/broker-tls/mc-client-ca.crt" in caddy
    assert "@broker_routes path /health /v1/provision /v1/destroy" in caddy
    assert "reverse_proxy 127.0.0.1:8181" in caddy


def test_broker_environment_example_keeps_cloud_token_out_of_mc_configuration():
    env = (_BROKER / "broker.env.example").read_text(encoding="utf-8")
    assert "HETZNER_BROKER_API_TOKEN=" in env
    assert "ONEBRAIN_HETZNER_API_TOKEN" not in env
    assert "HETZNER_BROKER_MC_TOKEN_HASH=" in env
