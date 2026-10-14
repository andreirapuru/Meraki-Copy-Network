from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from .configuration import GROUPS, apply_device_group, apply_network_group, capture_device_group, capture_network_group, diff
from .meraki import MerakiClient, MerakiError, MerakiPendingError
from .models import Job, JobState, MigrationUnit, Plan
from .storage import Store


class MigrationService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def inventory(self, api: MerakiClient, organization_id: str) -> dict[str, Any]:
        networks = await api.networks(organization_id)
        return {"networks": [n for n in networks if "switch" in n.get("productTypes", [])]}

    async def build_plan(
        self,
        api: MerakiClient,
        organization_id: str,
        source_network_id: str,
        destination_network_id: str,
        selected_serials: list[str],
        groups: list[str],
    ) -> Plan:
        if source_network_id == destination_network_id:
            raise MerakiError("A network de origem deve ser diferente da de destino")
        unknown = set(groups) - set(GROUPS)
        if unknown:
            raise MerakiError(f"Grupos desconhecidos: {', '.join(sorted(unknown))}")
        blocked = [GROUPS[group].label for group in groups if not GROUPS[group].readable]
        if blocked:
            raise MerakiError("Grupos ainda não suportados com segurança: " + ", ".join(blocked))

        all_networks = await api.networks(organization_id)
        by_id = {n["id"]: n for n in all_networks}
        source, destination = by_id.get(source_network_id), by_id.get(destination_network_id)
        if not source or not destination:
            raise MerakiError("Origem ou destino não pertence à organização selecionada")
        for network, role in ((source, "origem"), (destination, "destino")):
            if "switch" not in network.get("productTypes", []):
                raise MerakiError(f"A network de {role} não possui o produto switch")
            if network.get("isBoundToConfigTemplate"):
                raise MerakiError(f"A network de {role} está vinculada a Configuration Template")

        devices = [
            d for d in await api.devices(source_network_id)
            if "switch" in str(d.get("productType", "")).lower() or str(d.get("model", "")).upper().startswith(("MS", "C9", "C1"))
        ]
        serials = {d["serial"] for d in devices}
        chosen = set(selected_serials)
        if not chosen or not chosen <= serials:
            raise MerakiError("A seleção contém switch inexistente ou não-Meraki MS")

        stacks = await api.stacks(source_network_id)
        units: list[MigrationUnit] = []
        consumed: set[str] = set()
        for stack in stacks:
            members = set(stack.get("serials", []))
            selected_members = chosen & members
            if selected_members and selected_members != members:
                raise MerakiError(f"O stack {stack.get('name', stack.get('id'))} deve ser selecionado por completo")
            if selected_members:
                units.append(MigrationUnit(id=stack["id"], label=stack.get("name") or stack["id"], serials=sorted(members), stack_name=stack.get("name")))
                consumed |= members
        for device in devices:
            if device["serial"] in chosen - consumed:
                units.append(MigrationUnit(id=device["serial"], label=device.get("name") or device["serial"], serials=[device["serial"]]))

        source_snapshot: dict[str, Any] = {"network": {}, "devices": {}}
        destination_baseline: dict[str, Any] = {"network": {}}
        for group in groups:
            if GROUPS[group].scope == "network":
                source_snapshot["network"][group] = await capture_network_group(api, source_network_id, group)
                destination_baseline["network"][group] = await capture_network_group(api, destination_network_id, group)
        for serial in sorted(chosen):
            source_snapshot["devices"][serial] = {}
            for group in groups:
                if GROUPS[group].scope == "device":
                    source_snapshot["devices"][serial][group] = await capture_device_group(api, serial, group)

        warnings = [
            "Configurações selecionadas da origem substituirão os itens equivalentes no destino.",
            "O corte de cada unidade exige confirmação e poderá causar indisponibilidade temporária.",
        ]
        plan = Plan.new(
            organization_id=organization_id,
            source_network_id=source_network_id,
            destination_network_id=destination_network_id,
            units=units,
            groups=groups,
            source_snapshot=source_snapshot,
            destination_baseline=destination_baseline,
            warnings=warnings,
        )
        self.store.save_plan(plan)
        return plan

    async def create_job(self, plan_id: str) -> Job:
        self.store.get_plan(plan_id)
        job = Job.new(plan_id)
        self.store.save_job(job)
        self.store.event(job.id, "info", "Lote criado e aguardando confirmação da primeira unidade.")
        return job

    def recoverable_jobs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for job, updated_at in self.store.recoverable_jobs():
            plan = self.store.get_plan(job.plan_id)
            if job.current_index >= len(plan.units):
                continue
            unit = plan.units[job.current_index]
            items.append({"job": job.to_dict(), "unit": {"id": unit.id, "label": unit.label}, "updatedAt": updated_at})
        return items

    async def confirm_current(self, api: MerakiClient, job_id: str, unit_id: str) -> Job:
        async with self._locks[job_id]:
            job, plan = self.store.get_job(job_id), self.store.get_plan(self.store.get_job(job_id).plan_id)
            if job.state != JobState.WAITING_CONFIRMATION:
                raise MerakiError("O lote não está aguardando confirmação")
            if job.current_index >= len(plan.units) or plan.units[job.current_index].id != unit_id:
                raise MerakiError("A confirmação não corresponde à próxima unidade da fila")
            job.state = JobState.RUNNING
            self.store.save_job(job)
            self.store.event(job.id, "info", f"Corte confirmado para {plan.units[job.current_index].label}.")
            try:
                await self._run_unit(api, job, plan, plan.units[job.current_index])
                job.completed_unit_ids.append(unit_id)
                job.current_index += 1
                if job.current_index == len(plan.units):
                    job.state = JobState.COMPLETED
                    self.store.event(job.id, "success", "Lote concluído e validado.")
                else:
                    job.state = JobState.WAITING_CONFIRMATION
                    self.store.event(job.id, "info", "Unidade concluída; aguardando confirmação da próxima.")
                self.store.save_job(job)
            except MerakiPendingError as exc:
                # A recently claimed Catalyst can be visible in the Dashboard
                # before device-specific API endpoints have converged. Do not
                # guess its location and remove it again.
                self.store.event(job.id, "warning", str(exc))
                job.state = JobState.INTERRUPTED
                self.store.save_job(job)
                self.store.event(job.id, "warning", "Migração pausada sem rollback automático; use a recuperação após a API confirmar o equipamento.")
            except Exception as exc:
                self.store.event(job.id, "error", f"Falha: {exc}")
                job.state = JobState.ROLLING_BACK
                self.store.save_job(job)
                await self._rollback(api, job, plan, current=plan.units[job.current_index])
                job.state = JobState.FAILED
                self.store.save_job(job)
                self.store.event(job.id, "error", "Rollback finalizado; lote marcado como falho.")
            return job

    async def restore_current(self, api: MerakiClient, job_id: str, unit_id: str) -> Job:
        """Resume only the post-claim restore for an interrupted/failed unit.

        This path never removes or claims a device. It is intentionally limited
        to a device that the Dashboard API can prove is already in the planned
        destination network.
        """
        async with self._locks[job_id]:
            job = self.store.get_job(job_id)
            plan = self.store.get_plan(job.plan_id)
            if job.state not in {JobState.INTERRUPTED, JobState.FAILED}:
                raise MerakiError("A recuperação só está disponível para um lote interrompido ou falho")
            if job.current_index >= len(plan.units) or plan.units[job.current_index].id != unit_id:
                raise MerakiError("A recuperação não corresponde à unidade pendente")
            unit = plan.units[job.current_index]
            job.state = JobState.RUNNING
            self.store.save_job(job)
            self.store.event(job.id, "info", f"Recuperação confirmada para {unit.label}; verificando destino sem mover o equipamento.")
            try:
                await self._restore_claimed_unit(api, job, plan, unit)
                await self._verify(api, plan, unit)
                job.completed_unit_ids.append(unit.id)
                job.current_index += 1
                job.state = JobState.COMPLETED if job.current_index == len(plan.units) else JobState.WAITING_CONFIRMATION
                self.store.event(job.id, "success", f"Configurações restauradas e validadas em {unit.label}.")
                self.store.save_job(job)
            except MerakiPendingError as exc:
                job.state = JobState.INTERRUPTED
                self.store.save_job(job)
                self.store.event(job.id, "warning", str(exc))
            except Exception as exc:
                job.state = JobState.FAILED
                self.store.save_job(job)
                self.store.event(job.id, "error", f"Recuperação falhou sem mover o equipamento: {exc}")
            return job

    async def _run_unit(self, api: MerakiClient, job: Job, plan: Plan, unit: MigrationUnit) -> None:
        self.store.event(job.id, "info", f"Preparando {unit.label}.")
        if not job.completed_unit_ids:
            for group, value in plan.source_snapshot["network"].items():
                self.store.event(job.id, "info", f"Aplicando no destino: {GROUPS[group].label}.")
                await apply_network_group(api, plan.destination_network_id, group, value)
        for serial in unit.serials:
            await api.remove_device(plan.source_network_id, serial)
        self.store.event(job.id, "info", f"Equipamento removido da origem: {unit.label}.")
        self.store.event(job.id, "info", f"Reivindicando {unit.label} na network de destino.")
        await api.claim_devices(plan.destination_network_id, unit.serials)
        self.store.event(job.id, "info", "Claim aceito; aguardando a propagação do equipamento na Dashboard API.")
        await self._restore_claimed_unit(api, job, plan, unit)
        await self._verify(api, plan, unit)
        self.store.event(job.id, "success", f"{unit.label} migrado e validado.")

    async def _restore_claimed_unit(self, api: MerakiClient, job: Job, plan: Plan, unit: MigrationUnit) -> None:
        for serial in unit.serials:
            await api.wait_for_network(serial, plan.destination_network_id)
            for group, value in plan.source_snapshot["devices"][serial].items():
                await self._apply_device_group_when_ready(api, job, serial, group, value)

    async def _apply_device_group_when_ready(self, api: MerakiClient, job: Job, serial: str, group: str, value: Any) -> None:
        """Device APIs may be unavailable for a few minutes after a claim."""
        for attempt in range(30):
            try:
                self.store.event(job.id, "info", f"Reaplicando {GROUPS[group].label} em {serial}.")
                await apply_device_group(api, serial, group, value)
                return
            except MerakiError as exc:
                if exc.status_code not in {404, 409}:
                    raise
                if attempt == 0:
                    self.store.event(job.id, "warning", f"A API do dispositivo ainda não está pronta para {GROUPS[group].label}; aguardando.")
                await asyncio.sleep(10)
        raise MerakiPendingError(f"A API do dispositivo {serial} ainda não está pronta para restaurar {GROUPS[group].label}")

    async def _verify(self, api: MerakiClient, plan: Plan, unit: MigrationUnit) -> None:
        for serial in unit.serials:
            for group, expected in plan.source_snapshot["devices"][serial].items():
                actual = await capture_device_group(api, serial, group)
                if diff(expected, actual):
                    raise MerakiError(f"Validação divergente de {GROUPS[group].label} no switch {serial}")

    async def _rollback(self, api: MerakiClient, job: Job, plan: Plan, current: MigrationUnit) -> None:
        units_by_id = {u.id: u for u in plan.units}
        rollback_ids = [current.id, *reversed(job.completed_unit_ids)]
        for unit_id in rollback_ids:
            unit = units_by_id[unit_id]
            self.store.event(job.id, "warning", f"Revertendo {unit.label}.")
            try:
                to_claim: list[str] = []
                for serial in unit.serials:
                    device = await api.device(serial)
                    if device.get("networkId") == plan.destination_network_id:
                        await api.remove_device(plan.destination_network_id, serial)
                        to_claim.append(serial)
                if to_claim:
                    await api.claim_devices(plan.source_network_id, to_claim)
                for serial in unit.serials:
                    if serial in to_claim:
                        await api.wait_for_network(serial, plan.source_network_id)
                    for group, value in plan.source_snapshot["devices"][serial].items():
                        await apply_device_group(api, serial, group, value)
            except Exception as exc:  # Preserve evidence and continue trying the remainder.
                self.store.event(job.id, "error", f"Rollback incompleto de {unit.label}: {exc}")
        for group, baseline in plan.destination_baseline["network"].items():
            try:
                await apply_network_group(api, plan.destination_network_id, group, baseline)
            except Exception as exc:
                self.store.event(job.id, "error", f"Não foi possível restaurar baseline {GROUPS[group].label}: {exc}")
