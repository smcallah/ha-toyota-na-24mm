import asyncio
import base64
import json
import uuid
from urllib.parse import urlencode

import aiohttp

from .patch_client import (
    APPSYNC_API_KEY,
    GRAPHQL_ENDPOINT,
    RESOLVER_API_KEY,
    USER_AGENT,
)

GRAPHQL_WS_ENDPOINT = "wss://oa-api.telematicsct.com/graphql/realtime"
GRAPHQL_HOST = "oa-api.telematicsct.com"

GRAPHQL_REMOTE_COMMAND_STATUS = """subscription ReceiveRemoteCommandStatus($vin: String!) {
  onPostRemoteCallback(vin: $vin) {
    appRequestNo type category remoteCommandType message status vin command commandEnded
  }
}"""

GRAPHQL_SEND_REMOTE_COMMAND = """mutation SendRemoteCommand($command: String!, $autoFixCommands: [String]!) {
  executeRemoteCommand(commandInputBody: {
    command: $command
    autofixCommands: $autoFixCommands
  }) {
    payload { requestNo correlationId returnCode }
    status { messages { responseCode description detailedDescription } }
  }
}"""


def _remote_socket_error(message):
    payload = message.get("payload") or {}
    errors = payload.get("errors") if isinstance(payload, dict) else None
    error = errors[0] if errors else payload
    if isinstance(error, dict):
        return (
            error.get("message")
            or error.get("error")
            or "Toyota rejected the AppSync remote-command subscription."
        )
    return "Toyota rejected the AppSync remote-command subscription."


async def _receive_remote_socket_message(ws, timeout):
    message = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if message.type == aiohttp.WSMsgType.TEXT:
        return json.loads(message.data)
    if message.type in (
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.ERROR,
    ):
        raise RuntimeError("Toyota closed the AppSync remote-command connection.")
    return {}


async def _wait_for_remote_socket_event(ws, expected_type, subscription_id=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15

    while loop.time() < deadline:
        message = await _receive_remote_socket_message(
            ws,
            max(1, deadline - loop.time()),
        )
        message_type = message.get("type")

        if message_type == "ka":
            continue

        if message_type in ("connection_error", "error"):
            raise RuntimeError(_remote_socket_error(message))

        if message_type == expected_type and (
            subscription_id is None or message.get("id") == subscription_id
        ):
            return

    raise RuntimeError("Toyota's AppSync remote-command connection timed out.")


async def _wait_for_remote_command_result(ws, vin, subscription_id):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60

    while loop.time() < deadline:
        message = await _receive_remote_socket_message(
            ws,
            max(1, deadline - loop.time()),
        )
        message_type = message.get("type")

        if message_type == "ka":
            continue

        if message_type in ("connection_error", "error"):
            raise RuntimeError(_remote_socket_error(message))

        if message_type != "data" or message.get("id") != subscription_id:
            continue

        callback = (
            ((message.get("payload") or {}).get("data") or {}).get(
                "onPostRemoteCallback"
            )
            or {}
        )

        if callback.get("vin") != vin:
            continue

        status = str(callback.get("status", "")).lower()
        detail = callback.get("message")

        if status == "completed":
            return callback

        if status == "in_progress":
            continue

        if status in ("error", "timeout") or callback.get("commandEnded") is True:
            raise RuntimeError(
                detail or "Toyota ended the remote command with status %s." % status
            )

    raise RuntimeError(
        "Toyota accepted the command but did not report completion within 60 seconds."
    )


async def _graphql_send_remote_command(client, vin, command):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": APPSYNC_API_KEY,
        "x-resolver-api-key": RESOLVER_API_KEY,
        "Authorization": "Bearer " + await client.auth.get_access_token(),
        "vin": vin,
        "x-guid": await client.auth.get_guid(),
        "x-deviceid": client.auth.get_device_id(),
        "X-APPBRAND": "T",
        "x-brand": "T",
        "x-region": "US",
        "x-channel": "ONEAPP",
        "X-APPVERSION": "3.4.0",
        "X-OSNAME": "Android",
        "X-OSVERSION": "14",
        "X-LOCALE": "en-US",
        "User-Agent": USER_AGENT,
    }

    payload = json.dumps(
        {
            "operationName": "SendRemoteCommand",
            "query": GRAPHQL_SEND_REMOTE_COMMAND,
            "variables": {
                "command": command,
                "autoFixCommands": [],
            },
        }
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            GRAPHQL_ENDPOINT,
            headers=headers,
            data=payload,
        ) as resp:
            body = await resp.text()

            if resp.status >= 400:
                raise RuntimeError(
                    "Toyota GraphQL SendRemoteCommand failed with HTTP %d" % resp.status
                )

            result = json.loads(body)

            if result.get("errors"):
                err = result["errors"][0]
                extensions = err.get("extensions") or {}
                code = (
                    extensions.get("responseCode")
                    or extensions.get("code")
                    or err.get("errorType")
                )
                detail = (
                    extensions.get("detailedDescription")
                    or err.get("message")
                    or "Toyota rejected the AppSync request"
                )
                if code:
                    detail = "%s [%s]" % (detail, code)
                raise RuntimeError(detail)

            data = result.get("data") or {}
            execution = data.get("executeRemoteCommand") or {}
            correlation_id = (execution.get("payload") or {}).get("correlationId")

            if correlation_id:
                return execution

            messages = (execution.get("status") or {}).get("messages") or []
            message = messages[0] if messages else {}
            detail = (
                message.get("detailedDescription")
                or message.get("description")
                or "Toyota did not return a correlation ID for the remote command."
            )
            code = message.get("responseCode")
            if code:
                detail = "%s [%s]" % (detail, code)
            raise RuntimeError(detail)


async def remote_request_24mm(client, vin, command):
    """Run a 24MM remote command through AppSync and wait for completion."""
    token = await client.auth.get_access_token()
    guid = await client.auth.get_guid()

    authorization = {
        "host": GRAPHQL_HOST,
        "x-api-key": APPSYNC_API_KEY,
        "Authorization": "Bearer " + token,
        "x-channel": "ONEAPP",
        "vin": vin,
        "x-guid": guid,
    }

    query = urlencode(
        {
            "header": base64.b64encode(
                json.dumps(authorization).encode()
            ).decode(),
            "payload": base64.b64encode(b"{}").decode(),
        }
    )
    websocket_url = "%s?%s" % (GRAPHQL_WS_ENDPOINT, query)

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            websocket_url,
            protocols=["graphql-ws"],
            heartbeat=30,
        ) as ws:
            await ws.send_json({"type": "connection_init"})
            await _wait_for_remote_socket_event(ws, "connection_ack")

            subscription_id = str(uuid.uuid4())
            await ws.send_json(
                {
                    "id": subscription_id,
                    "type": "start",
                    "payload": {
                        "data": json.dumps(
                            {
                                "query": GRAPHQL_REMOTE_COMMAND_STATUS,
                                "variables": {"vin": vin},
                            }
                        ),
                        "extensions": {"authorization": authorization},
                    },
                }
            )

            await _wait_for_remote_socket_event(
                ws,
                "start_ack",
                subscription_id,
            )

            await _graphql_send_remote_command(client, vin, command)

            return await _wait_for_remote_command_result(
                ws,
                vin,
                subscription_id,
            )
