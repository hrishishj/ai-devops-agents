import httpx
import pytest

from devopsagents.domain.events import AlertEvent
from devopsagents.integrations.azure.alert_state import AlertStateVerifier

ARM = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.AlertsManagement/alerts/abc"


class Tok:
    def get_token(self, *scopes):
        return type("T", (), {"token": "tok"})()


def verifier(handler):
    return AlertStateVerifier(Tok(), httpx.MockTransport(handler))


def alert(alert_id=ARM):
    return AlertEvent(alert_id=alert_id, source="azure_monitor", title="t")


@pytest.mark.parametrize(("cond", "expected"), [("Resolved", True), ("Fired", False), ("Weird", None)])
def test_maps_monitor_condition(cond, expected):
    def handler(req):
        assert req.url.path == ARM and req.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"properties": {"essentials": {"monitorCondition": cond}}})

    assert verifier(handler).healthy(alert()) is expected


def test_unknown_when_api_errors_or_id_is_not_arm():
    assert verifier(lambda r: httpx.Response(403)).healthy(alert()) is None
    assert verifier(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))).healthy(alert()) is None
    assert verifier(lambda r: httpx.Response(200, json={})).healthy(alert("not-an-arm-id")) is None
