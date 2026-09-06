from toyota_na.vehicle.base_vehicle import ApiVehicleGeneration, RemoteRequestCommand

from .patch_remote_24mm import remote_request_24mm
from .patch_seventeen_cy_plus import SeventeenCYPlusToyotaVehicle


_original_send_command = SeventeenCYPlusToyotaVehicle.send_command


async def _send_command_with_24mm_climate(self, command: RemoteRequestCommand) -> None:
    """Route only 24MM remote climate start/stop through AppSync."""
    if (
        self.generation == ApiVehicleGeneration.MM24
        and command in (
            RemoteRequestCommand.EngineStart,
            RemoteRequestCommand.EngineStop,
        )
    ):
        await remote_request_24mm(
            self._client,
            self.vin,
            self._command_map[command],
        )
        return

    await _original_send_command(self, command)


SeventeenCYPlusToyotaVehicle.send_command = _send_command_with_24mm_climate
