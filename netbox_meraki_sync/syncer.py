"""
NetBox ORM syncer.

Takes CollectedDevice objects from collector.py and writes them to NetBox
using the Django ORM.  All writes are wrapped in atomic transactions so a
single device failure does not abort the whole network sync.

NetBox objects managed
-----------------------
dcim.Manufacturer         — "Cisco Meraki" (created once, reused)
dcim.DeviceType           — per Meraki model string
dcim.DeviceRole           — "Network" (or plugin default_device_role setting)
dcim.Device               — one per Meraki serial; matched by serial first
dcim.Interface            — one per port; type derived from model family
dcim.MACAddress           — one per unique MAC; linked to interface
ipam.IPAddress            — optional; created when sync_ip_addresses=True
extras.Tag                — "meraki" tag applied to every synced device

Custom fields on dcim.site
---------------------------
  meraki_network_id   — Meraki network ID; used to find which site to assign
  meraki_site_name    — Populated from the Meraki network name during sync

These custom fields are created by signals.py on plugin startup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from django.db import transaction
from django.utils.text import slugify

from .collector import CollectedDevice
from .models import SyncLog

log = logging.getLogger(__name__)

# Interface type mapping: Meraki family → NetBox interface type value
_FAMILY_IFACE_TYPE = {
    "MS": "1000base-t",        # refined per-port by speed below
    "MX": "other",
    "MR": "ieee802.11ax",      # WiFi 6; good default for modern MR kit
    "MG": "lte",
    "MV": "other",
    "MT": "other",
}

_SPEED_TO_IFACE_TYPE = {
    100:   "100base-tx",
    1000:  "1000base-t",
    2500:  "2.5gbase-t",
    5000:  "5gbase-t",
    10000: "10gbase-t",
    25000: "25gbase-x-sfp28",
    40000: "40gbase-x-qsfpp",
    100000:"100gbase-x-qsfp28",
}

_MERAKI_MANUFACTURER = "Cisco Meraki"
_MERAKI_TAG_SLUG     = "meraki"
_MERAKI_TAG_COLOR    = "1f7d3a"   # Meraki green (approximate)


class MerakiSyncer:
    """
    Sync collected Meraki device data into NetBox.

    Usage::

        syncer = MerakiSyncer(sync_log=log_obj, dry_run=False, sync_ips=False)
        syncer.sync_devices(devices, site=nb_site)
        syncer.close()   # finalises the SyncLog record
    """

    def __init__(
        self,
        sync_log: SyncLog,
        *,
        dry_run: bool = False,
        sync_ips: bool = False,
        default_role_slug: str = "network",
    ) -> None:
        self.log              = sync_log
        self.dry_run          = dry_run
        self.sync_ips         = sync_ips
        self.default_role_slug = default_role_slug

        # Cached lookups populated on first use
        self._manufacturer   = None
        self._meraki_tag     = None
        self._role_cache: dict[str, object]        = {}
        self._device_type_cache: dict[str, object] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_devices(self, devices: list[CollectedDevice], site) -> None:
        """
        Sync a list of CollectedDevice objects into NetBox under the given site.
        Updates self.log counters in place.
        """
        self.log.devices_seen = len(devices)

        for dev in devices:
            try:
                with transaction.atomic():
                    self._sync_device(dev, site)
            except Exception as exc:
                log.warning(
                    "Syncer: failed to sync device %s (%s): %s",
                    dev.serial, dev.name, exc
                )

        if not self.dry_run:
            self.log.save(update_fields=[
                "devices_seen", "devices_created", "devices_updated",
                "interfaces_synced", "macs_synced", "ips_synced",
            ])

    def close(self, *, success: bool = True, message: str = "") -> None:
        """Stamp the SyncLog with a completion time and status."""
        from .choices import SyncStatusChoices
        self.log.completed_at = datetime.now(tz=timezone.utc)
        self.log.status  = (
            SyncStatusChoices.SUCCESS if success else SyncStatusChoices.FAILED
        )
        self.log.message = message
        if not self.dry_run:
            self.log.save()

    # ------------------------------------------------------------------
    # Per-device sync
    # ------------------------------------------------------------------

    def _sync_device(self, dev: CollectedDevice, site) -> None:
        from dcim.models import Device

        manufacturer = self._get_manufacturer()
        device_type  = self._get_device_type(dev.model, manufacturer)
        role         = self._get_role(dev.family)
        tag          = self._get_meraki_tag()

        # Match existing device by serial number first, then fall back to name
        device = (
            Device.objects.filter(serial=dev.serial).first()
            or Device.objects.filter(
                site=site, name=dev.name
            ).first()
        )

        if device is None:
            if not self.dry_run:
                device = Device.objects.create(
                    name        = dev.name,
                    serial      = dev.serial,
                    device_type = device_type,
                    role        = role,
                    site        = site,
                    status      = "active",
                )
            self.log.devices_created += 1
            log.info("Syncer: created device %s (%s)", dev.name, dev.serial)
        else:
            changed = []
            if device.serial != dev.serial:
                device.serial = dev.serial
                changed.append("serial")
            if device.device_type_id != device_type.pk:
                device.device_type = device_type
                changed.append("device_type")
            if device.site_id != site.pk:
                device.site = site
                changed.append("site")
            if device.status != "active":
                device.status = "active"
                changed.append("status")
            if changed and not self.dry_run:
                device.save(update_fields=changed)
            self.log.devices_updated += 1
            log.debug("Syncer: updated device %s (%s)", dev.name, dev.serial)

        if device and tag and not self.dry_run:
            device.tags.add(tag)

        if device is None:
            # dry_run — nothing more to do
            return

        # Sync custom fields: firmware, Meraki serial cross-ref
        self._sync_device_custom_fields(device, dev)

        # Sync interfaces, MACs, IPs
        for port in dev.ports:
            iface = self._sync_interface(device, port, dev.family)
            if iface is None:
                continue

            # MACs on this port
            port_macs = [m for m in dev.macs if m.port_id == port.port_id]
            for cm in port_macs:
                self._sync_mac(iface, cm.mac)

            # LAN IP on the first port (for switches and APs)
            if self.sync_ips and dev.lan_ip and port.port_id in ("1", "0", "mgmt", "lan", "radio0"):
                self._sync_ip(iface, dev.lan_ip)

        # WAN IPs on uplink ports (MX appliances)
        if self.sync_ips:
            wan_map = {"wan1": dev.wan1_ip, "wan2": dev.wan2_ip}
            for port_id, ip in wan_map.items():
                if not ip:
                    continue
                iface = self._find_interface(device, port_id)
                if iface:
                    self._sync_ip(iface, ip)

    # ------------------------------------------------------------------
    # Interface sync
    # ------------------------------------------------------------------

    def _sync_interface(self, device, port, family: str):
        from dcim.models import Interface

        iface_type = self._iface_type(family, port.speed_mbps)
        iface = Interface.objects.filter(device=device, name=port.name).first()

        if iface is None:
            if not self.dry_run:
                iface = Interface.objects.create(
                    device      = device,
                    name        = port.name,
                    type        = iface_type,
                    enabled     = port.enabled,
                    description = port.description,
                )
            self.log.interfaces_synced += 1
        else:
            changed = []
            if iface.enabled != port.enabled:
                iface.enabled = port.enabled
                changed.append("enabled")
            if iface.description != port.description:
                iface.description = port.description
                changed.append("description")
            if changed and not self.dry_run:
                iface.save(update_fields=changed)
            self.log.interfaces_synced += 1

        return iface

    def _find_interface(self, device, port_id: str):
        from dcim.models import Interface
        return Interface.objects.filter(device=device, name=port_id).first()

    # ------------------------------------------------------------------
    # MAC address sync
    # ------------------------------------------------------------------

    def _sync_mac(self, iface, mac_str: str) -> None:
        """
        Create a MACAddress record linked to the given interface if it does
        not already exist.  Existing MACs are not modified (they may be
        managed by another tool or have analyst notes).
        """
        from dcim.models import MACAddress
        from django.contrib.contenttypes.models import ContentType

        if not mac_str or self.dry_run:
            return

        iface_ct = ContentType.objects.get_for_model(iface)
        exists = MACAddress.objects.filter(mac_address=mac_str).exists()
        if not exists:
            try:
                MACAddress.objects.create(
                    mac_address         = mac_str,
                    assigned_object_type = iface_ct,
                    assigned_object_id   = iface.pk,
                )
                self.log.macs_synced += 1
            except Exception as exc:
                log.debug("Syncer: could not create MAC %s: %s", mac_str, exc)

    # ------------------------------------------------------------------
    # IP address sync
    # ------------------------------------------------------------------

    def _sync_ip(self, iface, ip_str: str, prefix_length: int = 24) -> None:
        """
        Create an IPAddress record linked to the interface if it does not exist.
        Uses /24 as the default prefix length when none is available from Meraki.
        """
        from ipam.models import IPAddress as IPAddr
        from django.contrib.contenttypes.models import ContentType

        if not ip_str or self.dry_run:
            return

        address = f"{ip_str}/{prefix_length}"
        iface_ct = ContentType.objects.get_for_model(iface)

        ip_obj = IPAddr.objects.filter(address=address).first()
        if ip_obj is None:
            try:
                IPAddr.objects.create(
                    address             = address,
                    status              = "active",
                    assigned_object_type = iface_ct,
                    assigned_object_id   = iface.pk,
                )
                self.log.ips_synced += 1
            except Exception as exc:
                log.debug("Syncer: could not create IP %s: %s", address, exc)
        else:
            # Assign to this interface if currently unassigned
            if ip_obj.assigned_object_id is None and not self.dry_run:
                ip_obj.assigned_object_type = iface_ct
                ip_obj.assigned_object_id   = iface.pk
                ip_obj.save(update_fields=["assigned_object_type", "assigned_object_id"])

    # ------------------------------------------------------------------
    # Custom fields on Device
    # ------------------------------------------------------------------

    def _sync_device_custom_fields(self, device, dev: CollectedDevice) -> None:
        """
        Write Meraki-specific data into custom fields on the Device record.
        Custom fields are created on first use if absent.
        """
        if self.dry_run:
            return

        updates: dict[str, object] = {}

        if dev.firmware:
            self._ensure_device_cf("meraki_firmware", "Meraki Firmware")
            updates["meraki_firmware"] = dev.firmware

        if dev.serial:
            self._ensure_device_cf("meraki_serial", "Meraki Serial")
            updates["meraki_serial"] = dev.serial

        if dev.tags:
            self._ensure_device_cf("meraki_tags", "Meraki Tags")
            updates["meraki_tags"] = ", ".join(dev.tags)

        if updates:
            device.custom_field_data.update(updates)
            device.save(update_fields=["custom_field_data"])

    # ------------------------------------------------------------------
    # Lazy-loaded shared objects
    # ------------------------------------------------------------------

    def _get_manufacturer(self):
        if self._manufacturer is None:
            from dcim.models import Manufacturer
            self._manufacturer, _ = Manufacturer.objects.get_or_create(
                name=_MERAKI_MANUFACTURER,
                defaults={"slug": slugify(_MERAKI_MANUFACTURER)},
            )
        return self._manufacturer

    def _get_device_type(self, model: str, manufacturer):
        if model not in self._device_type_cache:
            from dcim.models import DeviceType
            dt, _ = DeviceType.objects.get_or_create(
                manufacturer=manufacturer,
                model=model,
                defaults={"slug": slugify(model)},
            )
            self._device_type_cache[model] = dt
        return self._device_type_cache[model]

    def _get_role(self, family: str):
        """
        Return a DeviceRole appropriate for the Meraki product family.
        Roles are created automatically if absent.
        """
        role_map = {
            "MS": ("switch",   "Switch",          "2196f3"),
            "MX": ("firewall", "Firewall",        "f44336"),
            "MR": ("ap",       "Access Point",    "4caf50"),
            "MG": ("router",   "Router",          "ff9800"),
            "MV": ("other",    "Other",           "9e9e9e"),
            "MT": ("other",    "Other",           "9e9e9e"),
        }
        slug, name, color = role_map.get(
            family, (self.default_role_slug, "Network", "0080ff")
        )
        if slug not in self._role_cache:
            from dcim.models import DeviceRole
            role, _ = DeviceRole.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "color": color},
            )
            self._role_cache[slug] = role
        return self._role_cache[slug]

    def _get_meraki_tag(self):
        if self._meraki_tag is None and not self.dry_run:
            from extras.models import Tag
            self._meraki_tag, _ = Tag.objects.get_or_create(
                slug=_MERAKI_TAG_SLUG,
                defaults={"name": "meraki", "color": _MERAKI_TAG_COLOR},
            )
        return self._meraki_tag

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iface_type(family: str, speed_mbps: int) -> str:
        """Derive the best NetBox interface type from family and link speed."""
        if speed_mbps and speed_mbps in _SPEED_TO_IFACE_TYPE:
            return _SPEED_TO_IFACE_TYPE[speed_mbps]
        return _FAMILY_IFACE_TYPE.get(family, "other")

    @staticmethod
    def _ensure_device_cf(name: str, label: str) -> None:
        """Create a text custom field on dcim.device if it does not exist."""
        from django.apps import apps
        from django.contrib.contenttypes.models import ContentType
        from extras.models import CustomField

        try:
            Device = apps.get_model("dcim", "Device")
            device_ct = ContentType.objects.get_for_model(Device)
            cf, _ = CustomField.objects.get_or_create(
                name=name,
                defaults={
                    "label": label,
                    "type": "text",
                    "required": False,
                },
            )
            if device_ct not in cf.object_types.all():
                cf.object_types.add(device_ct)
        except Exception as exc:
            log.debug("Syncer: could not ensure custom field %s: %s", name, exc)


# ---------------------------------------------------------------------------
# Site helpers (used by the management command)
# ---------------------------------------------------------------------------

def get_mapped_sites() -> list:
    """
    Return all NetBox Site objects that have meraki_network_id set.
    Each returned object has .custom_field_data["meraki_network_id"] populated.
    """
    from dcim.models import Site

    return [
        site for site in Site.objects.all()
        if site.custom_field_data.get("meraki_network_id")
    ]


def update_site_meraki_name(site, network_name: str) -> None:
    """Back-fill meraki_site_name on the site if it is blank."""
    if not network_name:
        return
    existing = site.custom_field_data.get("meraki_site_name") or ""
    if existing != network_name:
        site.custom_field_data["meraki_site_name"] = network_name
        site.save(update_fields=["custom_field_data"])
