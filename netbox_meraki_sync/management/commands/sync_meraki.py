"""
Management command: sync_meraki

Syncs Cisco Meraki network devices into NetBox by reading every NetBox Site
that has the meraki_network_id custom field populated, collecting device data
from the corresponding Meraki network via the official Meraki Python SDK, and
writing it to NetBox via the Django ORM.

Usage
-----
  # Sync all mapped sites
  python manage.py sync_meraki

  # Sync a single Meraki network (by network ID)
  python manage.py sync_meraki --network N_xxxxxxxxxxxx

  # Dry run — show what would change without writing anything
  python manage.py sync_meraki --dry-run

  # Also create IPAM IP address records for device LAN/WAN IPs
  python manage.py sync_meraki --sync-ips

  # List all Meraki networks accessible to the API key
  python manage.py sync_meraki --list-networks

Configuration
-------------
Set meraki_api_key in NetBox's PLUGINS_CONFIG (or the MERAKI_DASHBOARD_API_KEY
environment variable):

  PLUGINS_CONFIG = {
      "netbox_meraki_sync": {
          "meraki_api_key": "your-api-key",
          "sync_ip_addresses": False,
          "http_proxy": None,
      }
  }

Scheduling
----------
Add a cron entry to run the sync automatically:

  # /etc/cron.d/netbox-meraki-sync
  0 */4 * * * netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py \\
      sync_meraki >> /var/log/netbox/meraki_sync.log 2>&1
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from ...choices import SyncStatusChoices
from ...collector import MerakiCollector
from ...models import SyncLog
from ...signals import _ensure_site_custom_fields
from ...syncer import MerakiSyncer, get_mapped_sites, update_site_meraki_name

logger = logging.getLogger(__name__)


def _plugin_setting(key: str, default=None):
    try:
        from netbox.plugins import get_plugin_config
        return get_plugin_config("netbox_meraki_sync", key) or default
    except Exception:
        return default


def _get_api_key() -> str:
    key = (
        _plugin_setting("meraki_api_key", "")
        or os.environ.get("MERAKI_DASHBOARD_API_KEY", "")
    )
    return key


class Command(BaseCommand):
    help = "Sync Cisco Meraki network devices into NetBox."

    def add_arguments(self, parser):
        parser.add_argument(
            "--network",
            metavar="NETWORK_ID",
            default=None,
            help=(
                "Sync only this Meraki network ID (e.g. N_xxxxxxxxxxxx). "
                "The corresponding NetBox site must have meraki_network_id set "
                "to this value.  Default: sync all mapped sites."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Collect data but do not write anything to NetBox.",
        )
        parser.add_argument(
            "--sync-ips",
            action="store_true",
            help="Also create IPAM IPAddress records for device LAN/WAN IPs.",
        )
        parser.add_argument(
            "--client-timespan",
            type=int,
            default=86400,
            metavar="SECONDS",
            help=(
                "How far back (in seconds) to look for Meraki network clients "
                "when building MAC tables.  Default: 86400 (24 hours)."
            ),
        )
        parser.add_argument(
            "--list-networks",
            action="store_true",
            help=(
                "List all Meraki networks accessible to the configured API key "
                "and exit.  Useful for finding network IDs to map to sites."
            ),
        )

    def handle(self, *args, **options):
        api_key = _get_api_key()
        if not api_key:
            self.stderr.write(self.style.ERROR(
                "No Meraki API key configured.\n"
                "Set meraki_api_key in PLUGINS_CONFIG or the "
                "MERAKI_DASHBOARD_API_KEY environment variable."
            ))
            sys.exit(1)

        dry_run         = options["dry_run"]
        sync_ips        = options["sync_ips"] or bool(_plugin_setting("sync_ip_addresses"))
        network_filter  = options["network"]
        client_timespan = options["client_timespan"]
        list_networks   = options["list_networks"]
        proxy           = _plugin_setting("http_proxy", "") or ""
        timeout         = _plugin_setting("request_timeout", 30) or 30
        role_slug       = _plugin_setting("default_device_role", "network") or "network"

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes"))

        collector = MerakiCollector(api_key=api_key, timeout=timeout, proxy=proxy)

        # ------------------------------------------------------------------
        # --list-networks mode
        # ------------------------------------------------------------------
        if list_networks:
            self._list_networks(collector)
            return

        # ------------------------------------------------------------------
        # Ensure custom fields exist before any ORM reads
        # ------------------------------------------------------------------
        _ensure_site_custom_fields()

        # ------------------------------------------------------------------
        # Build the list of (site, network_id) pairs to sync
        # ------------------------------------------------------------------
        mapped_sites = get_mapped_sites()
        if not mapped_sites:
            self.stdout.write(self.style.WARNING(
                "No NetBox sites have meraki_network_id set.\n"
                "Populate the custom field on a Site to map it to a Meraki network.\n"
                "Use --list-networks to see available network IDs."
            ))
            return

        if network_filter:
            mapped_sites = [
                s for s in mapped_sites
                if s.custom_field_data.get("meraki_network_id") == network_filter
            ]
            if not mapped_sites:
                self.stderr.write(self.style.ERROR(
                    f"No NetBox site has meraki_network_id = {network_filter!r}"
                ))
                sys.exit(1)

        self.stdout.write(
            f"Syncing {len(mapped_sites)} Meraki network(s) into NetBox…"
        )

        # ------------------------------------------------------------------
        # Sync each network
        # ------------------------------------------------------------------
        total_devices   = 0
        total_created   = 0
        total_updated   = 0
        total_interfaces = 0
        total_macs      = 0
        total_ips       = 0
        failed_networks = 0

        for site in mapped_sites:
            network_id = site.custom_field_data["meraki_network_id"]
            site_name  = site.name

            self.stdout.write(f"\n[{network_id}] → site: {site_name}")

            # Create a SyncLog entry for this network
            sync_log = SyncLog(
                network_id   = network_id,
                site_name    = site_name,
                status       = SyncStatusChoices.RUNNING,
            )
            if not dry_run:
                sync_log.save()

            syncer = MerakiSyncer(
                sync_log          = sync_log,
                dry_run           = dry_run,
                sync_ips          = sync_ips,
                default_role_slug = role_slug,
            )

            try:
                devices = collector.collect_network(
                    network_id,
                    client_timespan=client_timespan,
                )

                # Back-fill the Meraki network name onto the site
                # (we don't get the name from getNetworkDevices, so we store
                # whatever the user populated — or leave it to be set manually)

                self.stdout.write(
                    f"  Collected {len(devices)} device(s) from Meraki"
                )

                syncer.sync_devices(devices, site=site)
                syncer.close(success=True)

                total_devices    += sync_log.devices_seen
                total_created    += sync_log.devices_created
                total_updated    += sync_log.devices_updated
                total_interfaces += sync_log.interfaces_synced
                total_macs       += sync_log.macs_synced
                total_ips        += sync_log.ips_synced

                self.stdout.write(self.style.SUCCESS(
                    f"  Done — "
                    f"{sync_log.devices_created} created, "
                    f"{sync_log.devices_updated} updated, "
                    f"{sync_log.interfaces_synced} interfaces, "
                    f"{sync_log.macs_synced} MACs"
                    + (f", {sync_log.ips_synced} IPs" if sync_ips else "")
                ))

            except Exception as exc:
                msg = f"Sync failed for network {network_id}: {exc}"
                logger.exception(msg)
                syncer.close(success=False, message=str(exc))
                self.stderr.write(self.style.ERROR(f"  ERROR: {exc}"))
                failed_networks += 1

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry-run complete. Would have synced {total_devices} device(s) "
                f"across {len(mapped_sites)} network(s)."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Sync complete — "
                f"{total_devices} devices seen, "
                f"{total_created} created, "
                f"{total_updated} updated, "
                f"{total_interfaces} interfaces, "
                f"{total_macs} MACs"
                + (f", {total_ips} IPs" if sync_ips else "")
                + (f"  [{failed_networks} network(s) failed]" if failed_networks else "")
            ))

    # ------------------------------------------------------------------
    # List networks helper
    # ------------------------------------------------------------------

    def _list_networks(self, collector: MerakiCollector) -> None:
        orgs = collector.get_organizations()
        if not orgs:
            self.stderr.write(self.style.ERROR(
                "No organisations returned.  Check the API key has access."
            ))
            return

        for org in orgs:
            org_id   = org.get("id", "?")
            org_name = org.get("name", "?")
            self.stdout.write(f"\nOrganisation: {org_name}  (id: {org_id})")
            self.stdout.write("-" * 60)

            networks = collector.get_organization_networks(org_id)
            if not networks:
                self.stdout.write("  (no networks)")
                continue

            for net in networks:
                net_id   = net.get("id", "?")
                net_name = net.get("name", "?")
                tags     = ", ".join(net.get("tags") or []) or "—"
                self.stdout.write(f"  {net_id:<30}  {net_name}  [tags: {tags}]")
