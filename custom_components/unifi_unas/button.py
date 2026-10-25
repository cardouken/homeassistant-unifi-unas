from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UNASConfigEntry
from .const import DOMAIN
from .coordinator import UNASServiceCoordinator
from .deploy import Deployer
from .devices import backup_device
from .entity import UNASEntity

PARALLEL_UPDATES = 1

REINSTALL = ButtonEntityDescription(
    key="reinstall_scripts", translation_key="reinstall_scripts", entity_category=EntityCategory.CONFIG
)
REBOOT = ButtonEntityDescription(key="reboot", translation_key="reboot")
SHUTDOWN = ButtonEntityDescription(key="shutdown", translation_key="shutdown")
RUN_BACKUP = ButtonEntityDescription(key="run_backup", translation_key="run_backup")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.service

    async_add_entities([
        UNASReinstallScriptsButton(coordinator, entry.runtime_data.deployer),
        UNASCommandButton(coordinator, REBOOT, "reboot"),
        UNASCommandButton(coordinator, SHUTDOWN, "shutdown -h now"),
    ])

    @callback
    def add_backups(task_ids: list[str]) -> None:
        async_add_entities(
            UNASBackupTriggerButton(coordinator, coordinator.find_backup_task(task_id)) for task_id in task_ids
        )

    entry.runtime_data.reconciler.register("backups", add_backups)


class UNASButton(UNASEntity, ButtonEntity):
    coordinator: UNASServiceCoordinator

    @property
    def available(self) -> bool:
        return self.coordinator.ready


class UNASReinstallScriptsButton(UNASButton):
    def __init__(self, coordinator: UNASServiceCoordinator, deployer: Deployer) -> None:
        super().__init__(coordinator, REINSTALL.key, REINSTALL)
        self._deployer = deployer

    async def async_press(self) -> None:
        self._deployer.async_reinstall()


class UNASCommandButton(UNASButton):
    def __init__(self, coordinator: UNASServiceCoordinator, description: ButtonEntityDescription, command: str) -> None:
        super().__init__(coordinator, description.key, description)
        self._command = command

    async def async_press(self) -> None:
        await self.coordinator.ssh_manager.execute_command(self._command)


class UNASBackupTriggerButton(UNASButton):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(
            coordinator, f"backup_{task['id']}", RUN_BACKUP,
            device=backup_device(coordinator.hass, coordinator.entry, task),
        )
        self._task_id = task["id"]

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.find_backup_task(self._task_id) is not None

    async def async_press(self) -> None:
        try:
            await self.coordinator.ssh_manager.run_backup(self._task_id)
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="backup_failed", translation_placeholders={"error": str(err)}
            ) from err
        await self.coordinator.async_request_refresh()
