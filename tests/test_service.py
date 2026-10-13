from pathlib import Path

import pytest

from app.models import JobState
from app.security import TestCipher
from app.service import MigrationService
from app.storage import Store


class FakeMeraki:
    def __init__(self) -> None:
        self.device_network = {"Q1": "source"}
        self.port_config = {"Q1": [{"portId": "1", "name": "desk", "enabled": True}]}
        self.network_config = {"source": {"vlan": 1}, "destination": {"vlan": 2}}

    async def networks(self, org):
        return [
            {"id": "source", "name": "Source", "productTypes": ["switch"]},
            {"id": "destination", "name": "Destination", "productTypes": ["switch"]},
        ]

    async def devices(self, network):
        return [{"serial": serial, "productType": "switch", "name": serial} for serial, location in self.device_network.items() if location == network]

    async def stacks(self, network): return []
    async def request(self, method, path, json=None, params=None):
        if path.endswith("/switch/settings"):
            network = path.split("/")[2]
            if method == "GET": return self.network_config[network]
            self.network_config[network] = json; return json
        if path.endswith("/switch/ports") and method == "GET": return self.port_config[path.split("/")[2]]
        if "/switch/ports/" in path and method == "PUT":
            serial, port_id = path.split("/")[2], path.rsplit("/", 1)[1]
            self.port_config[serial][int(port_id)-1].update(json); return json
        raise AssertionError((method, path, json))

    async def device(self, serial): return {"serial": serial, "networkId": self.device_network[serial], "name": serial}
    async def update_device(self, serial, payload): return payload
    async def remove_device(self, network, serial):
        assert self.device_network[serial] == network
        self.device_network[serial] = "inventory"
    async def claim_devices(self, network, serials):
        for serial in serials:
            assert self.device_network[serial] == "inventory"
            self.device_network[serial] = network
    async def wait_for_network(self, serial, network, attempts=30): assert self.device_network[serial] == network


@pytest.fixture
def service(tmp_path: Path) -> MigrationService:
    return MigrationService(Store(tmp_path / "db.sqlite", TestCipher()))


async def test_plan_and_confirm_single_switch(service: MigrationService) -> None:
    api = FakeMeraki()
    plan = await service.build_plan(api, "org", "source", "destination", ["Q1"], ["network_settings", "ports"])
    job = await service.create_job(plan.id)
    result = await service.confirm_current(api, job.id, "Q1")
    assert result.state == JobState.COMPLETED
    assert api.device_network["Q1"] == "destination"
    assert api.network_config["destination"] == {"vlan": 1}


async def test_partial_stack_is_rejected(service: MigrationService) -> None:
    api = FakeMeraki()
    api.stacks = lambda network: _stack()  # type: ignore[method-assign]
    with pytest.raises(Exception, match="stack"):
        await service.build_plan(api, "org", "source", "destination", ["Q1"], [])


async def _stack():
    return [{"id": "stack-1", "name": "Core", "serials": ["Q1", "Q2"]}]
