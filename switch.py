"""Switch entities for controllable Toyota features."""

import asyncio
from typing import Any, cast

from toyota_na.vehicle.base_vehicle import ToyotaVehicle, VehicleFeatures
from toyota_na.vehicle.entity_types.ToyotaRemoteStart import ToyotaRemoteStart

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .base_entity import ToyotaNABaseEntity
from .const import COMMAND_MAP, DOMAIN, ENGINE_START, ENGINE_STOP


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Toyota remote-start/climate switches."""
    coordinator: DataUpdateCoordinator[list[ToyotaVehicle]] = hass.data[DOMAIN][
        config_entry.entry_id
    ]["coordinator"]

    entities: list[SwitchEntity] = []
    for vehicle in coordinator.data:
        if not vehicle.subscribed:
            continue

        entities.append(
            ToyotaRemoteStartSwitch(
                coordinator,
                "Remote Start",
                vehicle.vin,
            )
        )

    async_add_entities(entities, True)


class ToyotaRemoteStartSwitch(ToyotaNABaseEntity, SwitchEntity):
    """Start or stop Toyota remote climate/engine operation."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:car-clock"

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._optimistic_state: bool | None = None

    def _reported_state(self) -> bool | None:
        """Return Toyota's reported remote-start state when available."""
        remote_start = self.feature(VehicleFeatures.RemoteStartStatus)
        if isinstance(remote_start, ToyotaRemoteStart):
            return remote_start.on
        return None

    @property
    def is_on(self) -> bool | None:
        """Return the reported or temporarily optimistic state."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self._reported_state()

    @property
    def assumed_state(self) -> bool:
        """Indicate when Toyota is not currently reporting remote-start state."""
        return self._reported_state() is None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start remote climate/engine operation."""
        await self._send_remote_command(ENGINE_START, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop remote climate/engine operation."""
        await self._send_remote_command(ENGINE_STOP, False)

    async def _send_remote_command(self, command: str, target_state: bool) -> None:
        vehicle = self.vehicle
        if vehicle is None:
            return

        self._optimistic_state = target_state
        self.async_write_ha_state()

        try:
            await vehicle.send_command(COMMAND_MAP[command])
        except Exception:
            self._optimistic_state = None
            self.async_write_ha_state()
            raise

        self.hass.async_create_task(self._background_refresh())

    async def _background_refresh(self) -> None:
        """Refresh Toyota state after a completed remote command."""
        try:
            vehicle = self.vehicle
            if vehicle is not None:
                await vehicle.poll_vehicle_refresh()

            await asyncio.sleep(10)
            await self.coordinator.async_request_refresh()
        finally:
            self._optimistic_state = None
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose runtime details returned by Toyota."""
        remote_start = cast(
            ToyotaRemoteStart | None,
            self.feature(VehicleFeatures.RemoteStartStatus),
        )
        if remote_start is None:
            return None

        attributes = {
            "end_time": remote_start.end_time,
            "minutes_remaining": remote_start.time_left,
            "start_time": remote_start.start_time,
            "total_runtime": remote_start.timer,
        }
        return {key: value for key, value in attributes.items() if value is not None}

    @property
    def available(self) -> bool:
        """Return whether the vehicle can accept remote commands."""
        vehicle = self.vehicle
        return vehicle is not None and vehicle.subscribed
