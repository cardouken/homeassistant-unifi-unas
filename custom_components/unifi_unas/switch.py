from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UNASConfigEntry
from .const import DOMAIN
from .coordinator import UNASServiceCoordinator
from .devices import backup_device
from .entity import UNASEntity

PARALLEL_UPDATES = 1

SCHEDULE = SwitchEntityDescription(
    key="schedule_enabled", translation_key="backup_schedule_enabled"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.service

    @callback
    def add_backups(task_ids: list[str]) -> None:
        async_add_entities(
            BackupScheduleSwitch(coordinator, coordinator.find_backup_task(task_id))
            for task_id in task_ids
        )

    entry.runtime_data.reconciler.register("backups", add_backups)


class BackupScheduleSwitch(UNASEntity, SwitchEntity):
    coordinator: UNASServiceCoordinator

    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(
            coordinator, f"backup_{task['id']}_schedule_enabled", SCHEDULE,
            device=backup_device(coordinator.hass, coordinator.entry, task),
        )
        self._task_id = task["id"]

    @property
    def available(self) -> bool:
        task = self.coordinator.find_backup_task(self._task_id)
        return self.coordinator.ready and task is not None

    @property
    def is_on(self) -> bool:
        task = self.coordinator.find_backup_task(self._task_id)
        return bool(task and (task.get("schedule") or {}).get("enable", False))

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_schedule_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_schedule_enabled(False)

    async def _set_schedule_enabled(self, enabled: bool) -> None:
        try:
            await self.coordinator.ssh_manager.set_backup_schedule(self._task_id, enabled)
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="schedule_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_request_refresh()
