from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx


class MerakiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MerakiPendingError(MerakiError):
    """The Dashboard accepted a change but the device API is not ready yet."""


class MerakiClient:
    """Small API v1 client with bounded retry and Link-header pagination."""

    base_url = "https://api.meraki.com/api/v1"

    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-Cisco-Meraki-API-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
        )
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None) -> httpx.Response:
        last_error = ""
        for attempt in range(5):
            async with self._request_lock:
                delay = self._next_request_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                # Stay below the documented default organization request budget.
                self._next_request_at = time.monotonic() + 0.125
                response = await self._client.request(method, path, json=json, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                last_error = response.text
                retry_after = float(response.headers.get("Retry-After", min(2**attempt, 15)))
                await asyncio.sleep(retry_after)
                continue
            if response.is_error:
                try:
                    detail = response.json().get("errors") or response.json()
                except ValueError:
                    detail = response.text
                raise MerakiError(f"{method} {path}: {detail}", response.status_code)
            return response
        raise MerakiError(f"{method} {path}: API indisponível após tentativas: {last_error}")

    async def request(self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None) -> Any:
        response = await self._send(method, path, json=json, params=params)
        return response.json() if response.content else None

    async def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Follow RFC 5988 next links so large organizations are never truncated."""
        next_path: str | None = path
        next_params = params
        collected: list[Any] = []
        while next_path:
            response = await self._send("GET", next_path, params=next_params)
            payload = response.json()
            collected.extend(payload if isinstance(payload, list) else [payload])
            link = response.links.get("next")
            next_path = link["url"] if link else None
            next_params = None
        return collected

    async def organizations(self) -> list[dict[str, Any]]:
        return await self.get_all("/organizations")

    async def networks(self, organization_id: str) -> list[dict[str, Any]]:
        return await self.get_all(f"/organizations/{organization_id}/networks", {"perPage": 1000})

    async def devices(self, network_id: str) -> list[dict[str, Any]]:
        return await self.get_all(f"/networks/{network_id}/devices", {"perPage": 1000})

    async def stacks(self, network_id: str) -> list[dict[str, Any]]:
        return await self.get_all(f"/networks/{network_id}/switch/stacks", {"perPage": 1000})

    async def remove_device(self, network_id: str, serial: str) -> None:
        await self.request("POST", f"/networks/{network_id}/devices/remove", json={"serial": serial})

    async def claim_devices(self, network_id: str, serials: list[str]) -> None:
        result = await self.request("POST", f"/networks/{network_id}/devices/claim", json={"serials": serials, "addAtomically": True})
        if result and result.get("errors"):
            raise MerakiError(f"Claim recusado: {result['errors']}")

    async def device(self, serial: str) -> dict[str, Any]:
        return await self.request("GET", f"/devices/{serial}")

    async def update_device(self, serial: str, payload: dict[str, Any]) -> Any:
        return await self.request("PUT", f"/devices/{serial}", json=payload)

    async def device_in_network(self, serial: str, network_id: str) -> bool:
        """Check both views because a recent Catalyst claim can lag on GET /devices."""
        try:
            if (await self.device(serial)).get("networkId") == network_id:
                return True
        except MerakiError as exc:
            # A 404 just after a claim is expected while inventory propagates.
            if exc.status_code != 404:
                raise
        return any(device.get("serial") == serial for device in await self.devices(network_id))

    async def wait_for_network(self, serial: str, network_id: str, attempts: int = 90) -> None:
        """Wait up to 15 minutes for eventual consistency after a device claim."""
        for _ in range(attempts):
            if await self.device_in_network(serial, network_id):
                return
            await asyncio.sleep(10)
        raise MerakiPendingError(
            f"{serial} foi reivindicado, mas a Dashboard API ainda não confirmou a associação "
            "com a network de destino após 15 minutos"
        )

