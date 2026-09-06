import base64
import hashlib
import json
import logging
import secrets
import time

import aiohttp

from urllib.parse import parse_qs, urlencode, urlparse

from toyota_na.exceptions import (
    LoginError,
    NotLoggedIn,
    TokenExpired,
)


_LOGGER = logging.getLogger(__name__)


AUTH_REALM = (
    "https://login.toyotadriverslogin.com/"
    "oauth2/realms/root/realms/tmna-native"
)

AUTHENTICATE_URL = (
    "https://login.toyotadriverslogin.com/"
    "json/realms/root/realms/tmna-native/authenticate"
    "?authIndexType=service&authIndexValue=OneAppSignIn"
)

AUTHORIZE_URL = f"{AUTH_REALM}/authorize"
ACCESS_TOKEN_URL = f"{AUTH_REALM}/access_token"

CLIENT_ID = "oneappsdkclient"
REDIRECT_URI = "com.toyota.oneapp:/oauth2Callback"
SCOPE = "openid profile write"

USER_AGENT = "okhttp/4.10.0"


def _generate_pkce():
    """Generate RFC7636 S256 PKCE verifier and challenge."""

    verifier = (
        base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    digest = hashlib.sha256(
        verifier.encode("ascii")
    ).digest()

    challenge = (
        base64.urlsafe_b64encode(
            digest
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    return verifier, challenge


def _decode_jwt_payload(token):
    """
    Decode a JWT payload without signature verification.

    Used only to inspect claims already returned by Toyota.
    """

    try:
        parts = token.split(".")

        if len(parts) < 2:
            return {}

        payload = parts[1]
        padding = "=" * (-len(payload) % 4)

        decoded = base64.urlsafe_b64decode(
            payload + padding
        )

        return json.loads(
            decoded.decode("utf-8")
        )

    except Exception:
        _LOGGER.exception(
            "Unable to decode Toyota ID token"
        )
        return {}


def _extract_guid(token_data):
    """
    Extract Toyota's account GUID from the ID token.

    Toyota NA currently falls back through:
        uuid
        extension_tmsguid
        sub
    """

    id_token = token_data.get("id_token")

    if not id_token:
        _LOGGER.warning(
            "Toyota identity diagnostics: no ID token present"
        )
        return None

    claims = _decode_jwt_payload(id_token)

    has_uuid = bool(
        claims.get("uuid")
    )

    has_tmsguid = bool(
        claims.get("extension_tmsguid")
    )

    has_sub = bool(
        claims.get("sub")
    )

    if has_uuid:
        source = "uuid"
        guid = claims["uuid"]

    elif has_tmsguid:
        source = "extension_tmsguid"
        guid = claims["extension_tmsguid"]

    elif has_sub:
        source = "sub"
        guid = claims["sub"]

    else:
        source = "NONE"
        guid = None

    _LOGGER.warning(
        "Toyota identity diagnostics: "
        "uuid=%s extension_tmsguid=%s sub=%s; GUID source=%s",
        has_uuid,
        has_tmsguid,
        has_sub,
        source,
    )

    _LOGGER.warning(
        "Toyota ID token diagnostics: "
        "aud=%s azp=%s client_id=%s scope_present=%s",
        claims.get("aud"),
        claims.get("azp"),
        claims.get("client_id"),
        bool(claims.get("scope")),
    )

    return guid


def _get_persistent_device_id(self):
    """
    Reuse ToyotaOneAuth's persistent device identity.

    toyota-na already maintains a device ID for API requests.
    Reusing it here prevents every interactive login from looking like
    a completely new Android device.
    """

    device_id = None

    getter = getattr(
        self,
        "get_device_id",
        None,
    )

    if callable(getter):
        try:
            device_id = getter()
        except Exception:
            _LOGGER.exception(
                "Unable to obtain Toyota persistent device ID "
                "from get_device_id()"
            )

    if not device_id:
        for attr_name in (
            "_device_id",
            "device_id",
        ):
            value = getattr(
                self,
                attr_name,
                None,
            )

            if value:
                device_id = value
                break

    if not device_id:
        """
        Last-resort fallback.

        This should normally never be needed because ToyotaOneAuth
        already has a device identity. If it is needed, create one once
        for this auth object rather than creating a new value for every
        callback round.
        """
        device_id = getattr(
            self,
            "_modern_fallback_device_id",
            None,
        )

        if not device_id:
            device_id = secrets.token_hex(16)

            self._modern_fallback_device_id = (
                device_id
            )

            _LOGGER.warning(
                "Toyota auth library exposed no persistent device ID; "
                "using a fallback device ID for this session"
            )

    return str(device_id)


def _device_print(self):
    """
    Build the Android-style Toyota devicePrint using one stable device ID.
    """

    device_id = _get_persistent_device_id(
        self
    )

    _LOGGER.warning(
        "Toyota device diagnostics: "
        "using persistent device identity=%s",
        bool(device_id),
    )

    return json.dumps(
        {
            "appId": "com.toyota.oneapp",
            "deviceType": "Android",
            "hardwareId": device_id,
            "model": "sdk_gphone64_x86_64",
            "systemOS": "14",
        }
    )


def _normalize_tokens(
    token_data,
    old_refresh_token=None,
    old_guid=None,
):
    """
    Convert current Toyota OAuth tokens into the structure expected
    by toyota-na 2.1.1.
    """

    now = time.time()

    if (
        old_refresh_token
        and "refresh_token" not in token_data
    ):
        token_data[
            "refresh_token"
        ] = old_refresh_token

    token_data[
        "updated_at"
    ] = now

    token_data[
        "expires_at"
    ] = (
        now
        + int(
            token_data.get(
                "expires_in",
                3600,
            )
        )
    )

    guid = _extract_guid(
        token_data
    )

    if not guid:
        guid = old_guid

    if not guid:
        _LOGGER.error(
            "Toyota OAuth token contains no usable GUID"
        )
        raise LoginError()

    token_data[
        "guid"
    ] = guid

    return token_data


async def _run_token_callback(
    self,
    token_data,
):
    """
    Persist refreshed tokens back into Home Assistant.

    Support multiple callback attribute names because this ancient
    library and its descendants have not exactly behaved like a
    beautifully preserved museum piece.
    """

    callback = None

    for callback_name in (
        "callback",
        "_callback",
        "_token_callback",
        "_tokens_callback",
    ):
        callback = getattr(
            self,
            callback_name,
            None,
        )

        if callback is not None:
            break

    if callback is None:
        _LOGGER.warning(
            "Toyota token refresh succeeded "
            "but no token callback was found"
        )
        return

    result = callback(
        token_data
    )

    if hasattr(
        result,
        "__await__",
    ):
        await result


async def check_tokens(self):
    """
    Replacement for toyota-na 2.1.1 check_tokens().

    A failed refresh is nonfatal while the current access token remains
    valid. Only an actually expired access token forces reauthentication.
    """

    expires_at = getattr(
        self,
        "_expires_at",
        None,
    )

    if expires_at is None:
        raise NotLoggedIn()

    now = time.time()

    updated_at = getattr(
        self,
        "_updated_at",
        None,
    )

    if updated_at is None:
        updated_at = now

    refresh_secs = getattr(
        self,
        "_refresh_secs",
        -300,
    )

    is_expired = (
        expires_at <= now
    )

    if refresh_secs > 0:
        should_refresh = (
            is_expired
            or now
            > updated_at
            + refresh_secs
        )

    elif refresh_secs < 0:
        should_refresh = (
            is_expired
            or now
            > expires_at
            + refresh_secs
        )

    else:
        should_refresh = True

    if not should_refresh:
        return

    _LOGGER.debug(
        "Toyota token requires refresh "
        "(expired=%s, expires_in=%d seconds)",
        is_expired,
        int(
            expires_at - now
        ),
    )

    try:
        await self.refresh_tokens()

        _LOGGER.debug(
            "Toyota OAuth token refreshed successfully"
        )

        return

    except Exception as e:
        if not is_expired:
            _LOGGER.warning(
                "Toyota refresh token was rejected, "
                "but the current access token is still valid "
                "for another %d seconds. Continuing with "
                "the existing access token.",
                int(
                    expires_at - now
                ),
            )

            return

        _LOGGER.error(
            "Toyota access token has expired "
            "and refresh failed: %s",
            e,
        )

        raise TokenExpired() from e


async def authorize(
    self,
    username,
    password,
    otp=None,
):
    """
    Authenticate against current Toyota North America ForgeRock OAuth.
    """

    headers = {
        "Accept-API-Version":
            "resource=2.1, protocol=1.0",

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

        "X-Region":
            "US",

        "Region":
            "US",

        "Brand":
            "T",

        "X-Brand":
            "T",

        "User-Agent":
            USER_AGENT,
    }

    if otp is None:
        data = {}
    else:
        data = self.otp_callbacks

    cookie_jar = aiohttp.CookieJar()

    if otp is not None:
        saved_cookies = getattr(
            self,
            "_modern_auth_cookies",
            None,
        )

        if saved_cookies:
            cookie_jar.update_cookies(
                saved_cookies
            )

    async with aiohttp.ClientSession(
        cookie_jar=cookie_jar
    ) as session:

        for _ in range(15):

            if "callbacks" in data:

                for cb in data[
                    "callbacks"
                ]:

                    cb_type = cb.get(
                        "type",
                        "",
                    )

                    cb_id = cb.get(
                        "_id",
                        "",
                    )

                    output = cb.get(
                        "output",
                        [],
                    )

                    prompt = ""

                    if output:
                        prompt = str(
                            output[0].get(
                                "value",
                                "",
                            )
                        )

                    _LOGGER.debug(
                        "Toyota auth callback: %s / %s",
                        cb_type,
                        prompt,
                    )

                    if cb_type == "NameCallback":

                        if (
                            "User Name"
                            in prompt
                        ):
                            cb[
                                "input"
                            ][0][
                                "value"
                            ] = username

                        elif prompt in (
                            "Market Locale",
                            "Internationalization",
                            "UI Locales",
                            "ui_locales",
                        ):
                            cb[
                                "input"
                            ][0][
                                "value"
                            ] = "en-US"

                    elif (
                        cb_type
                        == "PasswordCallback"
                    ):

                        if (
                            "One Time Password"
                            in prompt
                        ):

                            if otp is None:

                                self.otp_callbacks = (
                                    data
                                )

                                self._modern_auth_cookies = {
                                    cookie.key:
                                        cookie.value
                                    for cookie
                                    in session.cookie_jar
                                }

                                _LOGGER.debug(
                                    "Toyota authentication "
                                    "paused for OTP"
                                )

                                return data

                            cb[
                                "input"
                            ][0][
                                "value"
                            ] = otp

                        else:
                            cb[
                                "input"
                            ][0][
                                "value"
                            ] = password

                    elif (
                        cb_type
                        == "ChoiceCallback"
                    ):
                        cb[
                            "input"
                        ][0][
                            "value"
                        ] = 0

                    elif (
                        cb_type
                        == "ConfirmationCallback"
                    ):
                        cb[
                            "input"
                        ][0][
                            "value"
                        ] = 0

                    elif (
                        cb_type
                        == "HiddenValueCallback"
                    ):
                        if (
                            cb_id
                            == "devicePrint"
                        ):

                            device_print = getattr(
                                self,
                                "_modern_device_print",
                                None,
                            )

                            if device_print is None:
                                device_print = (
                                    _device_print(
                                        self
                                    )
                                )

                                self._modern_device_print = (
                                    device_print
                                )

                            cb[
                                "input"
                            ][0][
                                "value"
                            ] = device_print

                    elif (
                        cb_type
                        == "TextOutputCallback"
                    ):

                        if (
                            "Invalid OTP"
                            in prompt
                        ):
                            raise LoginError()

            async with session.post(
                AUTHENTICATE_URL,
                json=data,
                headers=headers,
            ) as resp:

                body = await resp.text()

                if resp.status != 200:
                    _LOGGER.error(
                        "Toyota authentication failed: "
                        "HTTP %s: %s",
                        resp.status,
                        body[:500],
                    )

                    raise LoginError()

                try:
                    data = json.loads(
                        body
                    )

                except Exception:
                    _LOGGER.error(
                        "Toyota authentication returned "
                        "invalid JSON"
                    )

                    raise LoginError()

            if "tokenId" in data:
                break

        if "tokenId" not in data:
            _LOGGER.error(
                "Toyota authentication completed "
                "without tokenId"
            )

            raise LoginError()

        verifier, challenge = (
            _generate_pkce()
        )

        self._modern_pkce_verifier = (
            verifier
        )

        auth_params = {
            "client_id":
                CLIENT_ID,

            "scope":
                SCOPE,

            "response_type":
                "code",

            "redirect_uri":
                REDIRECT_URI,

            "code_challenge":
                challenge,

            "code_challenge_method":
                "S256",
        }

        headers[
            "Cookie"
        ] = (
            f"iPlanetDirectoryPro="
            f"{data['tokenId']}"
        )

        authorize_url = (
            f"{AUTHORIZE_URL}"
            f"?{urlencode(auth_params)}"
        )

        async with session.get(
            authorize_url,
            headers=headers,
            allow_redirects=False,
        ) as resp:

            if resp.status != 302:
                body = (
                    await resp.text()
                )

                _LOGGER.error(
                    "Toyota authorization failed: "
                    "HTTP %s: %s",
                    resp.status,
                    body[:500],
                )

                raise LoginError()

            redirect = (
                resp.headers.get(
                    "Location",
                    "",
                )
            )

        query = parse_qs(
            urlparse(
                redirect
            ).query
        )

        if "code" not in query:
            _LOGGER.error(
                "Toyota authorization redirect "
                "contained no code"
            )

            raise LoginError()

        return query[
            "code"
        ][0]


async def request_tokens(
    self,
    authorization_code,
):
    """
    Exchange Toyota authorization code for OAuth tokens.
    """

    verifier = getattr(
        self,
        "_modern_pkce_verifier",
        None,
    )

    if not verifier:
        _LOGGER.error(
            "Toyota PKCE verifier is missing "
            "during token exchange"
        )

        raise LoginError()

    headers = {
        "Content-Type":
            "application/x-www-form-urlencoded",

        "Accept":
            "application/json",

        "User-Agent":
            USER_AGENT,
    }

    data = {
        "client_id":
            CLIENT_ID,

        "code":
            authorization_code,

        "redirect_uri":
            REDIRECT_URI,

        "grant_type":
            "authorization_code",

        "code_verifier":
            verifier,
    }

    async with aiohttp.ClientSession() as session:

        async with session.post(
            ACCESS_TOKEN_URL,
            headers=headers,
            data=data,
        ) as resp:

            body = await resp.text()

            if resp.status != 200:
                _LOGGER.error(
                    "Toyota token exchange failed: "
                    "HTTP %s: %s",
                    resp.status,
                    body[:500],
                )

                raise LoginError()

            try:
                token_data = json.loads(
                    body
                )

            except Exception:
                _LOGGER.error(
                    "Toyota token exchange returned "
                    "invalid JSON"
                )

                raise LoginError()

    token_data = _normalize_tokens(
        token_data
    )

    self.set_tokens(
        token_data
    )

    await _run_token_callback(
        self,
        token_data,
    )

    return token_data


async def refresh_tokens(self):
    """
    Refresh current Toyota OAuth tokens.
    """

    tokens = self.get_tokens()

    refresh_token = tokens.get(
        "refresh_token"
    )

    old_guid = tokens.get(
        "guid"
    )

    if not refresh_token:
        _LOGGER.error(
            "Toyota refresh token is missing"
        )

        raise LoginError()

    headers = {
        "Content-Type":
            "application/x-www-form-urlencoded",

        "Accept":
            "application/json",

        "User-Agent":
            USER_AGENT,
    }

    data = {
        "client_id":
            CLIENT_ID,

        "redirect_uri":
            REDIRECT_URI,

        "grant_type":
            "refresh_token",

        "refresh_token":
            refresh_token,
    }

    async with aiohttp.ClientSession() as session:

        async with session.post(
            ACCESS_TOKEN_URL,
            headers=headers,
            data=data,
        ) as resp:

            body = await resp.text()

            if resp.status != 200:
                _LOGGER.error(
                    "Toyota token refresh failed: "
                    "HTTP %s: %s",
                    resp.status,
                    body[:500],
                )

                raise LoginError()

            try:
                token_data = json.loads(
                    body
                )

            except Exception:
                _LOGGER.error(
                    "Toyota token refresh returned "
                    "invalid JSON"
                )

                raise LoginError()

    token_data = _normalize_tokens(
        token_data,
        old_refresh_token=refresh_token,
        old_guid=old_guid,
    )

    self.set_tokens(
        token_data
    )

    await _run_token_callback(
        self,
        token_data,
    )

    return token_data


async def login(
    self,
    username,
    password,
    otp,
):
    """
    Complete Toyota login after OTP.
    """

    authorization_code = (
        await self.authorize(
            username,
            password,
            otp,
        )
    )

    await self.request_tokens(
        authorization_code
    )
