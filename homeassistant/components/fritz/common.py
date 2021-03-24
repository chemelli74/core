"""Support for AVM Fritz!Box classes."""
from collections import namedtuple
from datetime import datetime, timedelta
import logging
import socket
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant.const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util, get_local_ip

from .const import (
    ATTR_HOST,
    CONF_PROFILES,
    CONF_USE_DEFLECTIONS,
    CONF_USE_PORT,
    CONF_USE_PROFILES,
    CONF_USE_TRACKER,
    CONF_USE_WIFI,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_PROFILES,
    DEFAULT_USE_DEFLECTIONS,
    DEFAULT_USE_PORT,
    DEFAULT_USE_PROFILES,
    DEFAULT_USE_TRACKER,
    DEFAULT_USE_WIFI,
    DEFAULT_USERNAME,
    DOMAIN,
    ERROR_CONNECTION_ERROR,
    ERROR_CONNECTION_ERROR_PROFILES,
    ERROR_PROFILE_NOT_FOUND,
    TRACKER_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

Device = namedtuple("Device", ["mac", "ip", "name"])


def ensure_unique_hosts(value):
    """Validate that all configs have a unique host."""
    vol.Schema(vol.Unique("duplicate host entries found"))(
        [socket.gethostbyname(entry[CONF_HOST]) for entry in value]
    )
    return value


CONFIG_SCHEMA = vol.Schema(
    vol.All(
        cv.deprecated(DOMAIN),
        {
            DOMAIN: vol.Schema(
                {
                    vol.Required(CONF_DEVICES): vol.All(
                        cv.ensure_list,
                        [
                            vol.Schema(
                                {
                                    vol.Optional(CONF_HOST): cv.string,
                                    vol.Optional(CONF_PORT): cv.port,
                                    vol.Required(CONF_USERNAME): cv.string,
                                    vol.Required(CONF_PASSWORD): cv.string,
                                    vol.Optional(CONF_PROFILES): vol.All(
                                        cv.ensure_list, [cv.string]
                                    ),
                                    vol.Optional(CONF_USE_TRACKER): cv.string,
                                    vol.Optional(CONF_USE_PROFILES): cv.string,
                                    vol.Optional(CONF_USE_PORT): cv.string,
                                    vol.Optional(CONF_USE_WIFI): cv.string,
                                    vol.Optional(CONF_USE_DEFLECTIONS): cv.string,
                                }
                            )
                        ],
                        ensure_unique_hosts,
                    )
                }
            )
        },
    ),
    extra=vol.ALLOW_EXTRA,
)

SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_HOST): cv.string})


class FritzBoxTools:
    """FrtizBoxTools class."""

    def __init__(
        self,
        hass,
        password,
        username=DEFAULT_USERNAME,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        profile_list=DEFAULT_PROFILES,
        use_port=DEFAULT_USE_PORT,
        use_deflections=DEFAULT_USE_DEFLECTIONS,
        use_wifi=DEFAULT_USE_WIFI,
        use_profiles=DEFAULT_USE_PROFILES,
        use_tracker=DEFAULT_USE_TRACKER,
    ):
        """Initialize FritzboxTools class."""
        # pylint: disable=import-error
        from fritzconnection import FritzConnection
        from fritzconnection.core.exceptions import FritzConnectionException
        from fritzconnection.lib.fritzhosts import FritzHosts
        from fritzconnection.lib.fritzstatus import FritzStatus
        from fritzprofiles import FritzProfileSwitch

        # general timeout for all requests to the router. Some calls need quite some time.

        try:
            self.connection = FritzConnection(
                address=host, port=port, user=username, password=password, timeout=60.0
            )
            if profile_list != DEFAULT_PROFILES:
                self.profile_switch = {
                    profile: FritzProfileSwitch(
                        "http://" + host, username, password, profile
                    )
                    for profile in profile_list
                }
            else:
                self.profile_switch = {}

            self.fritzstatus = FritzStatus(fc=self.connection)
            self._unique_id = self.connection.call_action("DeviceInfo:1", "GetInfo")[
                "NewSerialNumber"
            ]
            self.fritzhosts = FritzHosts(fc=self.connection)
            self._device_info = self._fetch_device_info()
            self.success = True
            self.error = False
        except FritzConnectionException:
            self.success = False
            self.error = ERROR_CONNECTION_ERROR
        except PermissionError:
            self.success = False
            self.error = ERROR_CONNECTION_ERROR_PROFILES
        except AttributeError:
            self.success = False
            self.error = ERROR_PROFILE_NOT_FOUND

        self.hass = hass
        self.ha_ip = get_local_ip()
        self.profile_list = profile_list

        self.username = username
        self.password = password
        self.port = port
        self.host = host

        self.use_wifi = use_wifi
        self.use_port = use_port
        self.use_deflections = use_deflections
        self.use_profiles = use_profiles
        self.use_tracker = use_tracker

        self._devices: Dict[str, Any] = {}
        self.scan_devices()

        async_track_time_interval(
            self.hass, self.scan_devices, timedelta(seconds=TRACKER_SCAN_INTERVAL)
        )

    def is_ok(self):
        """Return status."""
        return self.success, self.error

    @property
    def unique_id(self):
        """Return unique id."""
        return self._unique_id

    @property
    def fritzbox_model(self):
        """Return model."""
        return self._device_info["model"].replace("FRITZ!Box ", "")

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    @property
    def devices(self) -> Dict[str, Any]:
        """Return devices."""
        return self._devices

    @property
    def signal_device_new(self) -> str:
        """Event specific per Fritzbox entry to signal new device."""
        return f"{DOMAIN}-device-new"

    @property
    def signal_device_update(self) -> str:
        """Event specific per Fritzbox entry to signal updates in devices."""
        return f"{DOMAIN}-device-update"

    def _update_info(self):
        """Retrieve latest information from the FRITZ!Box."""
        if not self.success:
            return None

        return self.fritzhosts.get_hosts_info()

    def scan_devices(self, now: Optional[datetime] = None) -> None:
        """Scan for new devices and return a list of found device ids."""

        _LOGGER.debug("Checking devices for Fritz!Box router %s", self.host)

        new_device = False
        for known_host in self._update_info():
            if not known_host.get("mac"):
                continue

            dev_mac = known_host["mac"]
            dev_name = known_host["name"]
            dev_ip = known_host["ip"]
            dev_home = known_host["status"]

            dev_info = Device(dev_mac, dev_ip, dev_name)

            if dev_mac in self._devices:
                self._devices[dev_mac].update(dev_info, dev_home)
            else:
                device = FritzScannerEntity(dev_mac)
                device.update(dev_info, dev_home)
                self._devices[dev_mac] = device
                new_device = True

        async_dispatcher_send(self.hass, self.signal_device_update)
        if new_device:
            async_dispatcher_send(self.hass, self.signal_device_new)

    def _fetch_device_info(self):
        """Fetch device info."""
        info = self.connection.call_action("DeviceInfo:1", "GetInfo")
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self.unique_id)
            },
            "name": info.get("NewModelName"),
            "manufacturer": "AVM",
            "model": info.get("NewModelName"),
            "sw_version": info.get("NewSoftwareVersion"),
        }


class FritzScannerEntity:
    """FritzScanner device."""

    def __init__(self, mac, name=None):
        """Initialize device info."""
        self._mac = mac
        self._name = name
        self._ip_address = None
        self._last_activity = None
        self._connected = False

    def update(self, dev_info, dev_home):
        """Update device info."""
        utc_point_in_time = dt_util.utcnow()
        if not self._name:
            self._name = dev_info.name or self._mac.replace(":", "_")
        self._connected = dev_home

        if not self._connected:
            self._ip_address = None
            self._icon = "mdi:lan-disconnect"
        else:
            self._last_activity = utc_point_in_time
            self._ip_address = dev_info.ip
            self._icon = "mdi:lan-connect"

    @property
    def is_connected(self):
        """Return connected status."""
        return self._connected

    @property
    def mac(self):
        """Get MAC address."""
        return self._mac

    @property
    def name(self):
        """Get Name."""
        return self._name

    @property
    def ip_address(self):
        """Get IP address."""
        return self._ip_address

    @property
    def last_activity(self):
        """Return device last activity."""
        return self._last_activity

    @property
    def icon(self):
        """Return device icon."""
        return self._icon
