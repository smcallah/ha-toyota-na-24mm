import datetime
import logging

from toyota_na.client import ToyotaOneClient
from toyota_na.vehicle.base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    ToyotaVehicle,
    VehicleFeatures,
)
from toyota_na.vehicle.entity_types.ToyotaLocation import ToyotaLocation
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric
from toyota_na.vehicle.entity_types.ToyotaOpening import ToyotaOpening
from toyota_na.vehicle.entity_types.ToyotaRemoteStart import ToyotaRemoteStart

from .patch_client import graphql_get_vehicle_status

_LOGGER = logging.getLogger(__name__)


def _closed_from_status(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in ("close", "closed"):
        return True
    if value in ("open", "opened"):
        return False
    return None


def _locked_from_status(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in ("lock", "locked"):
        return True
    if value in ("unlock", "unlocked"):
        return False
    return None


class SeventeenCYPlusToyotaVehicle(ToyotaVehicle):

    _has_remote_subscription = False
    _has_electric = False

    _command_map = {
        RemoteRequestCommand.DoorLock: "door-lock",
        RemoteRequestCommand.DoorUnlock: "door-unlock",
        RemoteRequestCommand.EngineStart: "engine-start",
        RemoteRequestCommand.EngineStop: "engine-stop",
        RemoteRequestCommand.HazardsOn: "hazard-on",
        RemoteRequestCommand.HazardsOff: "hazard-off",
        RemoteRequestCommand.Refresh: "refresh",
    }

    _vehicle_status_category_map = {
        "Driver Side Door": VehicleFeatures.FrontDriverDoor,
        "Driver Side Window": VehicleFeatures.FrontDriverWindow,
        "Passenger Side Door": VehicleFeatures.FrontPassengerDoor,
        "Passenger Side Window": VehicleFeatures.FrontPassengerWindow,
        "Driver Side Rear Door": VehicleFeatures.RearDriverDoor,
        "Driver Side Rear Window": VehicleFeatures.RearDriverWindow,
        "Passenger Side Rear Door": VehicleFeatures.RearPassengerDoor,
        "Passenger Side Rear Window": VehicleFeatures.RearPassengerWindow,
        "Other Hatch": VehicleFeatures.Trunk,
        "Other Trunk": VehicleFeatures.Trunk,
        "Other Moonroof": VehicleFeatures.Moonroof,
        "Other Hood": VehicleFeatures.Hood,
    }

    _vehicle_telemetry_map = {
        "distanceToEmpty": VehicleFeatures.DistanceToEmpty,
        "flTirePressure": VehicleFeatures.FrontDriverTire,
        "frTirePressure": VehicleFeatures.FrontPassengerTire,
        "rlTirePressure": VehicleFeatures.RearDriverTire,
        "rrTirePressure": VehicleFeatures.RearPassengerTire,
        "fuelLevel": VehicleFeatures.FuelLevel,
        "odometer": VehicleFeatures.Odometer,
        "spareTirePressure": VehicleFeatures.SpareTirePressure,
        "tripA": VehicleFeatures.TripDetailsA,
        "tripB": VehicleFeatures.TripDetailsB,
        "vehicleLocation": VehicleFeatures.ParkingLocation,
        "nextService": VehicleFeatures.NextService,
        "speed": VehicleFeatures.Speed,
        "driverWindow": VehicleFeatures.FrontDriverWindow,
        "passengerWindow": VehicleFeatures.FrontPassengerWindow,
        "rlWindow": VehicleFeatures.RearDriverWindow,
        "rrWindow": VehicleFeatures.RearPassengerWindow,
        "sunRoof": VehicleFeatures.Moonroof,
    }

    _graphql_door_map = {
        "driverSide": VehicleFeatures.FrontDriverDoor,
        "passengerSide": VehicleFeatures.FrontPassengerDoor,
        "rearDriverSide": VehicleFeatures.RearDriverDoor,
        "rearPassengerSide": VehicleFeatures.RearPassengerDoor,
    }

    _graphql_window_map = {
        "driverSide": VehicleFeatures.FrontDriverWindow,
        "passengerSide": VehicleFeatures.FrontPassengerWindow,
        "rearDriverSide": VehicleFeatures.RearDriverWindow,
        "rearPassengerSide": VehicleFeatures.RearPassengerWindow,
    }

    _graphql_tire_map = {
        "frontLeft": VehicleFeatures.FrontDriverTire,
        "frontRight": VehicleFeatures.FrontPassengerTire,
        "rearLeft": VehicleFeatures.RearDriverTire,
        "rearRight": VehicleFeatures.RearPassengerTire,
        "spare": VehicleFeatures.SpareTirePressure,
    }

    def __init__(
        self,
        client: ToyotaOneClient,
        has_remote_subscription: bool,
        has_electric: bool,
        model_name: str,
        model_year: str,
        vin: str,
        region: str,
        generation: ApiVehicleGeneration = ApiVehicleGeneration.CY17PLUS,
    ):
        self._has_remote_subscription = has_remote_subscription
        self._has_electric = has_electric
        self._last_vehicle_status = None
        self._last_graphql_status = None

        ToyotaVehicle.__init__(
            self,
            client,
            has_remote_subscription,
            has_electric,
            model_name,
            model_year,
            vin,
            region,
            generation,
        )

    async def update(self):
        try:
            if self._has_remote_subscription:
                vehicle_status = await self._client.get_vehicle_status_17cyplus(self._vin)
                if vehicle_status:
                    self._last_vehicle_status = vehicle_status
                    self._parse_vehicle_status(vehicle_status)
                elif self._last_vehicle_status:
                    self._parse_vehicle_status(self._last_vehicle_status)

                # PR #182: 24MM must query current state directly through AppSync.
                if self._generation == ApiVehicleGeneration.MM24:
                    graphql_status = await graphql_get_vehicle_status(
                        self._client,
                        self._vin,
                        backdoor_type="hatch",
                        region=self._region,
                    )
                    if graphql_status:
                        self._last_graphql_status = graphql_status
                        self._parse_graphql_vehicle_status(graphql_status)
                    elif self._last_graphql_status:
                        self._parse_graphql_vehicle_status(self._last_graphql_status)

                ws_handler = getattr(self._client, "_ws_handler", None)
                if ws_handler:
                    ws_status = ws_handler.get_cached_status(self._vin)
                    if ws_status and ws_status.get("vehicleState"):
                        self._last_graphql_status = ws_status
                        self._parse_graphql_vehicle_status(ws_status)
        except Exception as e:
            _LOGGER.debug("Error fetching vehicle status: %s", e)

        try:
            telemetry = await self._client.get_telemetry(self._vin, self._region)
            if telemetry:
                self._parse_telemetry(telemetry)
        except Exception as e:
            _LOGGER.debug("Error fetching telemetry: %s", e)

        try:
            if self._has_remote_subscription:
                engine_status = await self._client.get_engine_status_17cyplus(self._vin)
                if engine_status:
                    _LOGGER.debug(
                        "Engine status received for VIN %s",
                        self._vin[-4:],
                    )
                    self._parse_engine_status(engine_status)
                else:
                    _LOGGER.debug(
                        "Engine status returned None for VIN %s",
                        self._vin[-4:],
                    )
        except Exception as e:
            _LOGGER.debug("Error fetching engine status: %s", e)

        # Keep legacy REST electric status for older generations. 24MM gets its
        # EV/PHEV state from the AppSync GetVehicleStatus query above.
        try:
            if (
                self._has_electric
                and self._generation != ApiVehicleGeneration.MM24
            ):
                electric_status = await self._client.get_electric_status(self.vin)
                if electric_status:
                    self._parse_electric_status(electric_status)
        except Exception as e:
            _LOGGER.debug("Error parsing electric status: %s", e)

    async def poll_vehicle_refresh(self) -> None:
        """Instruct Toyota's systems to ping the vehicle to upload a fresh status."""
        try:
            guid = await self._client.auth.get_guid()
            await self._client.graphql_pre_wake(guid)
        except Exception as e:
            _LOGGER.debug("GraphQL pre-wake failed: %s", e)

        try:
            await self._client.graphql_confirm_subscription(self._vin)
        except Exception as e:
            _LOGGER.debug("GraphQL confirm subscription failed: %s", e)

        try:
            await self._client.graphql_refresh_status(self._vin)
        except Exception as e:
            _LOGGER.debug("GraphQL refresh status failed: %s", e)

        try:
            await self._client.send_refresh_request_17cyplus(self._vin)
        except Exception as e:
            _LOGGER.debug("REST refresh request failed: %s", e)

        try:
            if (
                self._has_electric
                and self._generation != ApiVehicleGeneration.MM24
            ):
                electric_status = await self._client.get_electric_realtime_status(
                    self.vin
                )
                if electric_status:
                    self._parse_electric_status(electric_status)
        except Exception as e:
            _LOGGER.debug("Error refreshing electric status: %s", e)

    async def send_command(self, command: RemoteRequestCommand) -> None:
        """Send the existing v2.7.0 remote command path."""
        await self._client.remote_request_17cyplus(
            self._vin,
            self._command_map[command],
        )

    def _parse_engine_status(self, engine_status: dict) -> None:
        if not engine_status or "status" not in engine_status:
            return

        self._features[VehicleFeatures.RemoteStartStatus] = ToyotaRemoteStart(
            date=engine_status.get("date"),
            on=engine_status["status"] == "1",
            timer=engine_status.get("timer"),
        )

    def _parse_electric_status(self, electric_status: dict) -> None:
        if not electric_status or "vehicleInfo" not in electric_status:
            return

        chargeInfo = electric_status["vehicleInfo"].get("chargeInfo", {})
        if not chargeInfo:
            return

        self._features[VehicleFeatures.ChargeDistance] = ToyotaNumeric(
            chargeInfo.get("evDistance"),
            chargeInfo.get("evDistanceUnit"),
        )
        self._features[VehicleFeatures.ChargeDistanceAC] = ToyotaNumeric(
            chargeInfo.get("evDistanceAC"),
            chargeInfo.get("evDistanceUnit"),
        )
        self._features[VehicleFeatures.ChargeLevel] = ToyotaNumeric(
            chargeInfo.get("chargeRemainingAmount"),
            "%",
        )
        self._features[VehicleFeatures.PlugStatus] = ToyotaNumeric(
            chargeInfo.get("plugStatus"),
            "",
        )
        self._features[VehicleFeatures.RemainingChargeTime] = ToyotaNumeric(
            chargeInfo.get("remainingChargeTime"),
            "",
        )
        self._features[VehicleFeatures.EvTravelableDistance] = ToyotaNumeric(
            chargeInfo.get("evTravelableDistance"),
            "",
        )
        self._features[VehicleFeatures.ChargeType] = ToyotaNumeric(
            chargeInfo.get("chargeType"),
            "",
        )
        self._features[VehicleFeatures.ConnectorStatus] = ToyotaNumeric(
            chargeInfo.get("connectorStatus"),
            "",
        )
        self._features[VehicleFeatures.ChargingStatus] = ToyotaOpening(
            chargeInfo.get("connectorStatus") != 5
        )

    def _isClosed(self, section) -> bool:
        values = section.get("values", [])
        if not values:
            return False
        return _closed_from_status(values[0].get("value")) is True

    def _isLocked(self, section) -> bool:
        values = section.get("values", [])
        if len(values) < 2:
            return False
        return _locked_from_status(values[1].get("value")) is True

    def _parse_vehicle_status(self, vehicle_status: dict) -> None:
        if not vehicle_status:
            return

        if "latitude" in vehicle_status and "longitude" in vehicle_status:
            self._features[VehicleFeatures.ParkingLocation] = ToyotaLocation(
                vehicle_status["latitude"],
                vehicle_status["longitude"],
            )

        if (
            "vehicleStatus" not in vehicle_status
            or vehicle_status["vehicleStatus"] is None
        ):
            return

        for category in vehicle_status["vehicleStatus"]:
            if not category or "sections" not in category:
                continue
            for section in category["sections"]:
                if not section:
                    continue

                category_type = category.get("category")
                section_type = section.get("section")
                key = f"{category_type} {section_type}"

                if self._vehicle_status_category_map.get(key) is not None:
                    values = section.get("values", [])
                    if not values:
                        continue

                    closed = _closed_from_status(values[0].get("value"))
                    if closed is None:
                        continue

                    if len(values) == 1:
                        self._features[
                            self._vehicle_status_category_map[key]
                        ] = ToyotaOpening(closed)
                    elif len(values) >= 2:
                        locked = _locked_from_status(values[1].get("value"))
                        if locked is None:
                            continue
                        self._features[
                            self._vehicle_status_category_map[key]
                        ] = ToyotaLockableOpening(
                            closed=closed,
                            locked=locked,
                        )

    def _parse_graphql_vehicle_status(self, status: dict) -> None:
        """Parse GraphQL GetVehicleStatus response into vehicle features."""
        if not status:
            return

        location = status.get("location")
        if (
            location
            and location.get("latitude") is not None
            and location.get("longitude") is not None
        ):
            self._features[VehicleFeatures.ParkingLocation] = ToyotaLocation(
                location["latitude"],
                location["longitude"],
            )

        vehicle_state = status.get("vehicleState") or {}

        doors = vehicle_state.get("doors") or {}
        for door_key, feature in self._graphql_door_map.items():
            door = doors.get(door_key) or {}
            closed = _closed_from_status(
                (door.get("position") or {}).get("status")
            )
            locked = _locked_from_status(
                (door.get("lock") or {}).get("status")
            )
            if closed is not None and locked is not None:
                self._features[feature] = ToyotaLockableOpening(
                    closed=closed,
                    locked=locked,
                )

        windows = vehicle_state.get("windows") or {}
        for window_key, feature in self._graphql_window_map.items():
            window = windows.get(window_key) or {}
            closed = _closed_from_status(
                (window.get("position") or {}).get("status")
            )
            if closed is not None:
                self._features[feature] = ToyotaOpening(closed=closed)

        for opening_key in ("hatch", "trunk", "tailgate"):
            opening = vehicle_state.get(opening_key) or {}
            if not opening:
                continue

            closed = _closed_from_status(
                (opening.get("position") or {}).get("status")
            )
            locked = _locked_from_status(
                (opening.get("lock") or {}).get("status")
            )

            if closed is None:
                continue

            if locked is None:
                self._features[VehicleFeatures.Trunk] = ToyotaOpening(
                    closed=closed
                )
            else:
                self._features[VehicleFeatures.Trunk] = ToyotaLockableOpening(
                    closed=closed,
                    locked=locked,
                )
            break

        hood = vehicle_state.get("hood") or {}
        if hood:
            closed = _closed_from_status(
                (hood.get("position") or {}).get("status")
            )
            if closed is not None:
                self._features[VehicleFeatures.Hood] = ToyotaOpening(
                    closed=closed
                )

        moonroof = vehicle_state.get("moonroof") or {}
        if moonroof:
            closed = _closed_from_status(
                (moonroof.get("position") or {}).get("status")
            )
            if closed is not None:
                self._features[VehicleFeatures.Moonroof] = ToyotaOpening(
                    closed=closed
                )

        tires = vehicle_state.get("tires") or {}
        for tire_key, feature in self._graphql_tire_map.items():
            tire = tires.get(tire_key) or {}
            for pressure_key, default_unit in (
                ("psi", "psi"),
                ("kpa", "kPa"),
                ("bar", "bar"),
            ):
                pressure = tire.get(pressure_key)
                if pressure is None:
                    continue

                if isinstance(pressure, dict):
                    value = pressure.get("value")
                    unit = pressure.get("unit") or default_unit
                else:
                    value = pressure
                    unit = default_unit

                if value is not None:
                    self._features[feature] = ToyotaNumeric(value, unit)
                    break

        engine = vehicle_state.get("engine") or {}
        if engine:
            running = engine.get("running")
            if running is not None:
                self._features[
                    VehicleFeatures.RemoteStartStatus
                ] = ToyotaRemoteStart(
                    date=None,
                    on=bool(running),
                    timer=None,
                )

        telemetry = status.get("telemetry") or {}
        if telemetry:
            odo = telemetry.get("odo")
            if odo and odo.get("value") is not None:
                self._features[VehicleFeatures.Odometer] = ToyotaNumeric(
                    odo["value"],
                    odo.get("unit", ""),
                )

            fugage = telemetry.get("fugage")
            if fugage and fugage.get("value") is not None:
                self._features[VehicleFeatures.FuelLevel] = ToyotaNumeric(
                    fugage["value"],
                    fugage.get("unit", "%"),
                )

            range_val = telemetry.get("range")
            if range_val and range_val.get("value") is not None:
                self._features[
                    VehicleFeatures.DistanceToEmpty
                ] = ToyotaNumeric(
                    range_val["value"],
                    range_val.get("unit", ""),
                )

        self._parse_graphql_electric_status(status.get("electric"))

    def _parse_graphql_electric_status(self, electric: dict) -> None:
        """Parse PR #182's 24MM EV/PHEV electric document."""
        if not electric:
            _LOGGER.warning("Toyota 24MM: no electric document in GraphQL status")
            return

        battery = electric.get("battery") or {}

        charge_level = (
            battery.get("stateOfChargeDisplay")
            or battery.get("plugInEnergy")
            or battery.get("chargeRemainingAmount")
        )
        if charge_level and charge_level.get("value") is not None:
            self._features[VehicleFeatures.ChargeLevel] = ToyotaNumeric(
                charge_level["value"],
                charge_level.get("unit", "%"),
            )

        electric_range = battery.get("travelableDistance")
        if electric_range and electric_range.get("value") is not None:
            measurement = ToyotaNumeric(
                electric_range["value"],
                electric_range.get("unit", ""),
            )
            self._features[VehicleFeatures.ChargeDistance] = measurement
            self._features[
                VehicleFeatures.EvTravelableDistance
            ] = measurement

        electric_range_ac = battery.get("travelableDistanceAC")
        if (
            electric_range_ac
            and electric_range_ac.get("value") is not None
        ):
            self._features[
                VehicleFeatures.ChargeDistanceAC
            ] = ToyotaNumeric(
                electric_range_ac["value"],
                electric_range_ac.get("unit", ""),
            )

        charging = electric.get("charging") or {}

        charge_type = charging.get("chargeType")
        if charge_type is not None:
            self._features[VehicleFeatures.ChargeType] = ToyotaNumeric(
                charge_type,
                "",
            )

        remaining = charging.get("remainingChargeTime") or {}
        if remaining.get("value") is not None:
            self._features[
                VehicleFeatures.RemainingChargeTime
            ] = ToyotaNumeric(
                remaining["value"],
                remaining.get("unit", ""),
            )

        connector = charging.get("connector") or {}

        connector_status = connector.get("status")
        if connector_status is not None:
            self._features[
                VehicleFeatures.ConnectorStatus
            ] = ToyotaNumeric(
                connector_status,
                "",
            )

        plug_status = connector.get("plugStatus")
        if plug_status is None:
            plug_status = connector.get("plugInInfo")
        if plug_status is None:
            plug_status = charging.get("chargingState")

        if plug_status is not None:
            self._features[VehicleFeatures.PlugStatus] = ToyotaNumeric(
                plug_status,
                "",
            )

        charging_state = str(
            charging.get("chargingState") or ""
        ).lower()
        charging_status = str(
            charging.get("chargingStatus") or ""
        ).lower()

        is_charging = None

        if charging_state in ("charging", "40", "56"):
            is_charging = True
        elif charging_state:
            is_charging = False
        elif charging_status:
            is_charging = charging_status in (
                "charging",
                "active",
                "in_progress",
                "in-progress",
            )

        if is_charging is not None:
            self._features[
                VehicleFeatures.ChargingStatus
            ] = ToyotaOpening(
                closed=not is_charging
            )

        _LOGGER.warning(
            "Toyota 24MM EV parsed: charge_level=%s range=%s range_ac=%s "
            "plug=%s connector=%s charging=%s",
            getattr(
                self._features.get(VehicleFeatures.ChargeLevel),
                "value",
                None,
            ),
            getattr(
                self._features.get(VehicleFeatures.ChargeDistance),
                "value",
                None,
            ),
            getattr(
                self._features.get(VehicleFeatures.ChargeDistanceAC),
                "value",
                None,
            ),
            getattr(
                self._features.get(VehicleFeatures.PlugStatus),
                "value",
                None,
            ),
            getattr(
                self._features.get(VehicleFeatures.ConnectorStatus),
                "value",
                None,
            ),
            is_charging,
        )

    def _parse_telemetry(self, telemetry: dict) -> None:
        if not telemetry:
            return

        for key, value in telemetry.items():
            if value is None:
                continue

            if key == "lastTimestamp":
                self._features[
                    VehicleFeatures.LastTimeStamp
                ] = ToyotaNumeric(
                    datetime.datetime.strptime(
                        value,
                        "%Y-%m-%dT%H:%M:%SZ",
                    )
                    .replace(tzinfo=datetime.timezone.utc)
                    .timestamp(),
                    "",
                )
                continue

            if key == "tirePressureTimestamp":
                self._features[
                    VehicleFeatures.LastTirePressureTimeStamp
                ] = ToyotaNumeric(
                    datetime.datetime.strptime(
                        value,
                        "%Y-%m-%dT%H:%M:%SZ",
                    )
                    .replace(tzinfo=datetime.timezone.utc)
                    .timestamp(),
                    "",
                )
                continue

            if key == "fuelLevel":
                self._features[VehicleFeatures.FuelLevel] = ToyotaNumeric(
                    value,
                    "%",
                )
                continue

            if key == "vehicleLocation":
                self._features[
                    VehicleFeatures.RealTimeLocation
                ] = ToyotaLocation(
                    value.get("latitude"),
                    value.get("longitude"),
                )
                continue

            if "Window" in key or "Roof" in key:
                if value not in (1, 2):
                    continue
                self._features[
                    self._vehicle_telemetry_map.get(key, key)
                ] = ToyotaOpening(
                    closed=(value == 2)
                )
                continue

            if self._vehicle_telemetry_map.get(key) is not None:
                if isinstance(value, dict) and "value" in value:
                    self._features[
                        self._vehicle_telemetry_map[key]
                    ] = ToyotaNumeric(
                        value["value"],
                        value.get("unit", ""),
                    )
                else:
                    self._features[
                        self._vehicle_telemetry_map[key]
                    ] = ToyotaNumeric(
                        value,
                        "",
                    )
                continue
