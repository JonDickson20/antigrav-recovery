#!/usr/bin/env python3
"""
Antigravity State Watchdog
==========================

Prevents conversation loss by periodically snapshotting Antigravity's state
database. Runs silently in the background.

The Problem:
  Antigravity's state.vscdb.backup can fall days behind the live database.
  If Windows force-restarts, the app restores from this stale backup and
  your recent conversations vanish from the sidebar.

The Fix:
  This watchdog copies state.vscdb -> state.vscdb.backup every hour using
  SQLite's online backup API (crash-safe, even while Antigravity is running).
  It also keeps rotating timestamped snapshots for extra safety.

Usage:
  python watchdog.py              # Run in foreground (for testing)
  python watchdog.py --install    # Install as a Windows Scheduled Task
  python watchdog.py --uninstall  # Remove the scheduled task
  python watchdog.py --once       # Run a single snapshot and exit
  python watchdog.py --interval 30  # Custom interval in minutes (default: 60)

Requirements:
  Python 3.8+, no external dependencies.
"""

import sqlite3
import shutil
import os
import sys
import time
import argparse
import subprocess
import logging
from datetime import datetime
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

TASK_NAME = "AntigravityStateWatchdog"
DEFAULT_INTERVAL_MINUTES = 60
MAX_SNAPSHOTS = 24  # Keep last 24 hourly snapshots


def get_paths():
    """Auto-detect paths."""
    home = Path.home()
    if sys.platform == 'win32':
        appdata = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming'))
        state_dir = appdata / 'Antigravity' / 'User' / 'globalStorage'
    elif sys.platform == 'darwin':
        state_dir = home / 'Library' / 'Application Support' / 'Antigravity' / 'User' / 'globalStorage'
    else:
        config_dir = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
        state_dir = config_dir / 'Antigravity' / 'User' / 'globalStorage'

    snapshot_dir = state_dir / 'snapshots'
    return {
        'state_db': state_dir / 'state.vscdb',
        'backup_db': state_dir / 'state.vscdb.backup',
        'snapshot_dir': snapshot_dir,
    }


# ============================================================
# CORE: SAFE SNAPSHOT
# ============================================================

def safe_snapshot(paths, logger):
    """
    Create a safe snapshot of state.vscdb using SQLite's online backup API.
    This is safe to run while Antigravity is actively using the database.
    """
    state_db = paths['state_db']
    backup_db = paths['backup_db']
    snapshot_dir = paths['snapshot_dir']

    if not state_db.exists():
        logger.warning(f"State database not found: {state_db}")
        return False

    try:
        # Step 1: Use SQLite's online backup API for a crash-consistent copy
        # This is the gold standard - it handles WAL mode, locks, everything.
        src_conn = sqlite3.connect(str(state_db))
        
        # Create a temporary backup first
        temp_backup = state_db.with_suffix('.vscdb.watchdog-temp')
        dst_conn = sqlite3.connect(str(temp_backup))
        
        src_conn.backup(dst_conn)
        
        dst_conn.close()
        src_conn.close()

        # Step 2: Overwrite the .backup file with our fresh copy
        # This is the key fix - the app's own fallback mechanism now has fresh data
        shutil.copy2(str(temp_backup), str(backup_db))
        logger.info(f"Updated state.vscdb.backup ({backup_db.stat().st_size:,} bytes)")

        # Step 3: Keep a timestamped snapshot for extra safety
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = snapshot_dir / f"state_{timestamp}.vscdb"
        shutil.move(str(temp_backup), str(snapshot_path))
        logger.info(f"Snapshot saved: {snapshot_path.name}")

        # Step 4: Prune old snapshots (keep last MAX_SNAPSHOTS)
        snapshots = sorted(snapshot_dir.glob("state_*.vscdb"), key=lambda f: f.stat().st_mtime)
        while len(snapshots) > MAX_SNAPSHOTS:
            oldest = snapshots.pop(0)
            oldest.unlink()
            logger.debug(f"Pruned old snapshot: {oldest.name}")

        return True

    except sqlite3.OperationalError as e:
        logger.error(f"SQLite error (database may be locked): {e}")
        # Clean up temp file if it exists
        temp_backup = state_db.with_suffix('.vscdb.watchdog-temp')
        if temp_backup.exists():
            temp_backup.unlink()
        return False

    except Exception as e:
        logger.error(f"Snapshot failed: {e}")
        temp_backup = state_db.with_suffix('.vscdb.watchdog-temp')
        if temp_backup.exists():
            temp_backup.unlink()
        return False


# ============================================================
# WATCHDOG LOOP
# ============================================================

def run_watchdog(paths, interval_minutes, logger):
    """Run the watchdog loop."""
    interval_seconds = interval_minutes * 60
    
    logger.info(f"Antigravity State Watchdog started")
    logger.info(f"  Monitoring: {paths['state_db']}")
    logger.info(f"  Interval:  {interval_minutes} minutes")
    logger.info(f"  Snapshots: {paths['snapshot_dir']}")
    logger.info("")

    # Immediate first snapshot
    safe_snapshot(paths, logger)

    while True:
        try:
            time.sleep(interval_seconds)
            safe_snapshot(paths, logger)
        except KeyboardInterrupt:
            logger.info("Watchdog stopped by user.")
            break


# ============================================================
# WINDOWS SCHEDULED TASK
# ============================================================

def install_scheduled_task(interval_minutes):
    """Install as a Windows Scheduled Task that runs at logon."""
    if sys.platform != 'win32':
        print("Scheduled task installation is only supported on Windows.")
        print("On Linux/macOS, use cron or launchd instead.")
        return False

    script_path = os.path.abspath(__file__)
    python_path = sys.executable

    # Create the task XML
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Periodically snapshots Antigravity state database to prevent conversation loss after crashes.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python_path}</Command>
      <Arguments>"{script_path}" --interval {interval_minutes}</Arguments>
    </Exec>
  </Actions>
</Task>'''

    # Write XML to temp file
    xml_path = os.path.join(os.environ.get('TEMP', '/tmp'), 'antigrav_watchdog_task.xml')
    with open(xml_path, 'w', encoding='utf-16') as f:
        f.write(xml)

    # Create the task
    result = subprocess.run(
        ['schtasks', '/Create', '/TN', TASK_NAME, '/XML', xml_path, '/F'],
        capture_output=True, text=True
    )

    os.unlink(xml_path)

    if result.returncode == 0:
        print(f"[OK] Scheduled task '{TASK_NAME}' installed!")
        print(f"     Runs at logon, snapshots every {interval_minutes} minutes.")
        print(f"     Script: {script_path}")
        
        # Start it immediately too
        subprocess.run(
            ['schtasks', '/Run', '/TN', TASK_NAME],
            capture_output=True, text=True
        )
        print(f"     Started immediately.")
        return True
    else:
        print(f"[FAIL] Could not create scheduled task:")
        print(f"       {result.stderr.strip()}")
        print(f"\nTry running as Administrator, or run the watchdog manually:")
        print(f"  python {script_path} --interval {interval_minutes}")
        return False


def uninstall_scheduled_task():
    """Remove the Windows Scheduled Task."""
    if sys.platform != 'win32':
        print("Only supported on Windows.")
        return False

    result = subprocess.run(
        ['schtasks', '/Delete', '/TN', TASK_NAME, '/F'],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        print(f"[OK] Scheduled task '{TASK_NAME}' removed.")
        return True
    else:
        print(f"[FAIL] Could not remove task: {result.stderr.strip()}")
        return False


def check_status():
    """Check if the watchdog is running."""
    paths = get_paths()
    
    print("\n=== Antigravity State Watchdog Status ===\n")
    print(f"State database: {paths['state_db']}")
    
    if paths['state_db'].exists():
        mtime = datetime.fromtimestamp(paths['state_db'].stat().st_mtime)
        print(f"  Last modified: {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    
    if paths['backup_db'].exists():
        mtime = datetime.fromtimestamp(paths['backup_db'].stat().st_mtime)
        age_hours = (time.time() - paths['backup_db'].stat().st_mtime) / 3600
        freshness = "FRESH" if age_hours < 2 else "STALE" if age_hours > 24 else "OK"
        print(f"  Backup age: {age_hours:.1f} hours [{freshness}]")
    
    snapshot_dir = paths['snapshot_dir']
    if snapshot_dir.exists():
        snapshots = sorted(snapshot_dir.glob("state_*.vscdb"), key=lambda f: f.stat().st_mtime)
        print(f"\nSnapshots: {len(snapshots)} stored")
        if snapshots:
            latest = snapshots[-1]
            mtime = datetime.fromtimestamp(latest.stat().st_mtime)
            print(f"  Latest: {latest.name} ({mtime.strftime('%Y-%m-%d %H:%M:%S')})")
            oldest = snapshots[0]
            mtime = datetime.fromtimestamp(oldest.stat().st_mtime)
            print(f"  Oldest: {oldest.name} ({mtime.strftime('%Y-%m-%d %H:%M:%S')})")
    else:
        print("\nNo snapshots yet (watchdog hasn't run)")

    # Check scheduled task
    if sys.platform == 'win32':
        result = subprocess.run(
            ['schtasks', '/Query', '/TN', TASK_NAME],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"\nScheduled task: INSTALLED")
        else:
            print(f"\nScheduled task: NOT INSTALLED")
            print(f"  Install with: python watchdog.py --install")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Antigravity State Watchdog - prevents conversation loss',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python watchdog.py               Run watchdog in foreground
  python watchdog.py --once        Single snapshot and exit
  python watchdog.py --install     Install as Windows Scheduled Task
  python watchdog.py --uninstall   Remove scheduled task
  python watchdog.py --status      Check watchdog status
  python watchdog.py --interval 30 Snapshot every 30 minutes
        """
    )
    parser.add_argument('--once', action='store_true', help='Run a single snapshot and exit')
    parser.add_argument('--install', action='store_true', help='Install as Windows Scheduled Task')
    parser.add_argument('--uninstall', action='store_true', help='Remove the scheduled task')
    parser.add_argument('--status', action='store_true', help='Check watchdog status')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL_MINUTES,
                        help=f'Snapshot interval in minutes (default: {DEFAULT_INTERVAL_MINUTES})')

    args = parser.parse_args()

    # Set up logging
    logger = logging.getLogger('watchdog')
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
    logger.addHandler(handler)

    # Also log to file
    paths = get_paths()
    log_dir = paths['snapshot_dir']
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / 'watchdog.log', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(file_handler)

    if args.status:
        check_status()
    elif args.install:
        install_scheduled_task(args.interval)
    elif args.uninstall:
        uninstall_scheduled_task()
    elif args.once:
        paths = get_paths()
        success = safe_snapshot(paths, logger)
        sys.exit(0 if success else 1)
    else:
        paths = get_paths()
        run_watchdog(paths, args.interval, logger)


if __name__ == '__main__':
    main()
