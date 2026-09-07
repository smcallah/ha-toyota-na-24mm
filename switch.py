"""Switch entities for controllable Toyota features."""

import asyncio
import logging
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


_LOGGER = logging.getLogger(__name__)
_REMOTE_START_RUNTIME_SECONDS = 20 * 60


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
        self._runtime_task: asyncio.Task[None] | None = None

    def _reported_state(self) -> bool | None:
        """Return Toyota's reported remote-start state when available."""
        remote_start = self.feature(VehicleFeatures.RemoteStartStatus)
        if isinstance(remote_start, ToyotaRemoteStart):
            return remote_start.on
        return None

    @property
    def is_on(self) -> bool | None:
        """Return command-tracked state, falling back to Toyota's report."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self._reported_state()

    @property
    def assumed_state(self) -> bool:
        """Indicate when switch state is being tracked locally."""
        return self._optimistic_state is not None or self._reported_state() is None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start remote climate/engine operation."""
        _LOGGER.warning("Toyota 24MM remote switch: ON requested")
        await self._send_remote_command(ENGINE_START, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop remote climate/engine operation."""
        _LOGGER.warning("Toyota 24MM remote switch: OFF requested")
        await self._send_remote_command(ENGINE_STOP, False)

    async def _send_remote_command(self, command: str, target_state: bool) -> None:
        vehicle = self.vehicle
        if vehicle is None:
            _LOGGER.warning("Toyota 24MM remote switch: no vehicle object found")
            return

        generation = getattr(vehicle, "generation", None)
        mapped_command = COMMAND_MAP[command]
        _LOGGER.warning(
            "Toyota 24MM remote switch: dispatching action=%s generation=%s enum=%s",
            command,
            generation,
            mapped_command,
        )

        try:
            await vehicle.send_command(mapped_command)
        except Exception:
            _LOGGER.exception("Toyota 24MM remote switch: command failed")
            raise

        _LOGGER.warning("Toyota 24MM remote switch: send_command completed")

        if target_state:
            self._optimistic_state = True
            self._start_runtime_timer()
        else:
            self._cancel_runtime_timer()
            self._optimistic_state = False

        self.async_write_ha_state()
        self.hass.async_create_task(self._background_refresh())

    def _start_runtime_timer(self) -> None:
        """Track Toyota's normal 20-minute remote-start runtime locally."""
        self._cancel_runtime_timer()
        self._runtime_task = self.hass.async_create_task(self._runtime_expired())

    def _cancel_runtime_timer(self) -> None:
        """Cancel the local runtime timer if one is active."""
        if self._runtime_task is not None and not self._runtime_task.done():
            self._runtime_task.cancel()
        self._runtime_task = None

    async def _runtime_expired(self) -> None:
        """Return the switch to off when Toyota's remote-start timer expires."""
        try:
            await asyncio.sleep(_REMOTE_START_RUNTIME_SECONDS)
        except asyncio.CancelledError:
            return

        self._runtime_task = None
        self._optimistic_state = False
        self.async_write_ha_state()
        _LOGGER.debug("Toyota remote-start 20-minute runtime expired")

    async def _background_refresh(self) -> None:
        """Refresh Toyota data without overwriting command-tracked state."""
        vehicle = self.vehicle
        if vehicle is not None:
            await vehicle.poll_vehicle_refresh()

        await asyncio.sleep(10)
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up the local runtime timer when the entity is unloaded."""
        self._cancel_runtime_timer()
        await super().async_will_remove_from_hass()

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
