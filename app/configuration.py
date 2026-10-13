from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .meraki import MerakiClient, MerakiError


def writable(value: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Keep only documented writable keys, never echo API metadata back to Meraki."""
    return {key: value[key] for key in allowed if key in value and value[key] is not None}


async def read_path(client: MerakiClient, path: str) -> Any:
    return await client.request("GET", path)


async def put_path(client: MerakiClient, path: str, value: Any) -> Any:
    return await client.request("PUT", path, json=value)


@dataclass(frozen=True, slots=True)
class Group:
    id: str
    label: str
    scope: str
    readable: bool = True
    detail: str = ""


GROUPS: dict[str, Group] = {
    "network_settings": Group("network_settings", "Configurações gerais de switching", "network"),
    "network_stp": Group("network_stp", "STP e proteção contra loops", "network"),
    "network_acls": Group("network_acls", "ACLs de switching", "network"),
    "device_metadata": Group("device_metadata", "Nome, tags e localização", "device"),
    "ports": Group("ports", "Configurações de portas", "device"),
    "management": Group("management", "Interface de gerenciamento", "device"),
    "l3": Group("l3", "Interfaces e rotas L3", "device", False, "Requer mapeamento específico do modelo; bloqueado nesta versão."),
    "aggregations": Group("aggregations", "Agregações de links", "network", False, "Requer recriação coordenada; bloqueado nesta versão."),
    "stack": Group("stack", "Configuração de stack", "network", False, "O stack é movido como unidade; ajustes avançados são bloqueados nesta versão."),
}

PORT_WRITABLE = {
    "name", "tags", "enabled", "poeEnabled", "type", "vlan", "voiceVlan", "allowedVlans",
    "isolationEnabled", "rstpEnabled", "stpGuard", "linkNegotiation", "portScheduleId",
    "accessPolicyType", "accessPolicyNumber", "udld", "macAllowList", "stickyMacAllowList",
}


async def capture_network_group(client: MerakiClient, network_id: str, group_id: str) -> Any:
    paths = {
        "network_settings": f"/networks/{network_id}/switch/settings",
        "network_stp": f"/networks/{network_id}/switch/stp",
        "network_acls": f"/networks/{network_id}/switch/accessControlLists",
    }
    if group_id not in paths:
        return None
    try:
        return await read_path(client, paths[group_id])
    except MerakiError as exc:
        raise MerakiError(f"Não foi possível ler {GROUPS[group_id].label}: {exc}") from exc


async def apply_network_group(client: MerakiClient, network_id: str, group_id: str, value: Any) -> None:
    paths = {
        "network_settings": f"/networks/{network_id}/switch/settings",
        "network_stp": f"/networks/{network_id}/switch/stp",
        "network_acls": f"/networks/{network_id}/switch/accessControlLists",
    }
    if group_id not in paths:
        raise MerakiError(f"Grupo {group_id} não possui aplicador seguro")
    await put_path(client, paths[group_id], value)


async def capture_device_group(client: MerakiClient, serial: str, group_id: str) -> Any:
    if group_id == "device_metadata":
        return writable(await client.device(serial), {"name", "tags", "lat", "lng", "address", "notes", "floorPlanId"})
    if group_id == "ports":
        ports = await read_path(client, f"/devices/{serial}/switch/ports")
        return [{"portId": port["portId"], **writable(port, PORT_WRITABLE)} for port in ports]
    if group_id == "management":
        return await read_path(client, f"/devices/{serial}/managementInterface")
    return None


async def apply_device_group(client: MerakiClient, serial: str, group_id: str, value: Any) -> None:
    if group_id == "device_metadata":
        await client.update_device(serial, value)
        return
    if group_id == "management":
        await put_path(client, f"/devices/{serial}/managementInterface", value)
        return
    if group_id == "ports":
        for port in value:
            port_id = port.get("portId")
            if port_id is None:
                raise MerakiError(f"Porta sem portId em {serial}")
            payload = writable(port, PORT_WRITABLE)
            await put_path(client, f"/devices/{serial}/switch/ports/{port_id}", payload)
        return
    raise MerakiError(f"Grupo {group_id} não possui aplicador seguro")


def diff(source: Any, destination: Any) -> bool:
    """A deliberately strict normalized comparison used by preview and verification."""
    return source != destination
