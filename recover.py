#!/usr/bin/env python3
"""
Antigravity Conversation Recovery Tool
=======================================

Recovers missing conversations in Google's Antigravity (Agent Manager) IDE
after Windows restarts, crashes, or updates cause the conversation index to
become stale.

The tool works by:
1. Reading the current trajectory summaries index from the state database
2. Scanning .pb conversation files on disk
3. Identifying conversations that exist on disk but are missing from the index
4. Building new index entries with correct workspace assignments
5. Writing the repaired index back to the database

Usage:
    python recover.py                  # Interactive mode (recommended)
    python recover.py --scan           # Just scan and report missing conversations
    python recover.py --backup         # Create backup only
    python recover.py --build          # Build experimental index (no swap)
    python recover.py --swap           # Swap in the experimental index
    python recover.py --rollback       # Rollback to backup

Safety:
    - All operations create backups before modifying anything
    - The tool never touches .pb conversation files (read-only)
    - Existing index entries are preserved byte-for-byte
    - Rollback script restores the exact previous state

Requirements:
    - Python 3.8+
    - No external dependencies (stdlib only)
    - Antigravity must be CLOSED before --swap or --rollback

Author: JonDickson20 (with AI assistance)
License: MIT
"""

import sqlite3
import base64
import struct
import os
import sys
import re
import shutil
import uuid as uuid_mod
import argparse
import json
from datetime import datetime
from collections import Counter
from pathlib import Path


# ============================================================
# CONFIGURATION - Auto-detect paths
# ============================================================

def get_default_paths():
    """Auto-detect Antigravity paths based on OS."""
    home = Path.home()
    
    if sys.platform == 'win32':
        appdata = Path(os.environ.get('APPDATA', home / 'AppData' / 'Roaming'))
        state_db = appdata / 'Antigravity' / 'User' / 'globalStorage' / 'state.vscdb'
        gemini_dir = home / '.gemini' / 'antigravity'
    elif sys.platform == 'darwin':
        state_db = home / 'Library' / 'Application Support' / 'Antigravity' / 'User' / 'globalStorage' / 'state.vscdb'
        gemini_dir = home / '.gemini' / 'antigravity'
    else:  # Linux
        config_dir = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
        state_db = config_dir / 'Antigravity' / 'User' / 'globalStorage' / 'state.vscdb'
        gemini_dir = home / '.gemini' / 'antigravity'
    
    return {
        'state_db': state_db,
        'conversations_dir': gemini_dir / 'conversations',
        'brain_dir': gemini_dir / 'brain',
        'backup_dir': home / 'Desktop' / f'antigravity-recovery-backup-{datetime.now().strftime("%Y%m%d")}',
        'experimental_index': Path(os.environ.get('TEMP', '/tmp')) / 'experimental_index.bin',
    }


# ============================================================
# PROTOBUF HELPERS
# ============================================================

def encode_varint(value):
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def encode_field(field_number, wire_type, data):
    """Encode a protobuf field with tag."""
    tag = encode_varint((field_number << 3) | wire_type)
    return tag + data


def encode_string(field_number, value):
    """Encode a string as a length-delimited protobuf field."""
    encoded = value.encode('utf-8')
    return encode_field(field_number, 2, encode_varint(len(encoded)) + encoded)


def encode_bytes_field(field_number, value):
    """Encode raw bytes as a length-delimited protobuf field."""
    return encode_field(field_number, 2, encode_varint(len(value)) + value)


def encode_varint_field(field_number, value):
    """Encode a varint protobuf field."""
    return encode_field(field_number, 0, encode_varint(value))


def encode_message(field_number, data):
    """Encode a nested message as a length-delimited protobuf field."""
    return encode_field(field_number, 2, encode_varint(len(data)) + data)


def decode_varint(data, pos):
    """Decode a varint from data at the given position."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def decode_protobuf_fields(data):
    """Decode all fields from a protobuf message."""
    pos = 0
    fields = []
    while pos < len(data):
        try:
            tag_wire, new_pos = decode_varint(data, pos)
            if new_pos == pos:
                break
            field_number = tag_wire >> 3
            wire_type = tag_wire & 0x07
            pos = new_pos

            if wire_type == 0:  # Varint
                value, pos = decode_varint(data, pos)
                fields.append((field_number, 'varint', value))
            elif wire_type == 1:  # 64-bit fixed
                value = struct.unpack('<Q', data[pos:pos+8])[0]
                pos += 8
                fields.append((field_number, '64bit', value))
            elif wire_type == 2:  # Length-delimited
                length, pos = decode_varint(data, pos)
                if length > len(data) - pos or length < 0:
                    break
                value = data[pos:pos+length]
                pos += length
                fields.append((field_number, 'bytes', value))
            elif wire_type == 5:  # 32-bit fixed
                value = struct.unpack('<I', data[pos:pos+4])[0]
                pos += 4
                fields.append((field_number, '32bit', value))
            else:
                break
        except (IndexError, struct.error):
            break
    return fields


def unix_to_proto_timestamp(unix_ts):
    """Convert a Unix timestamp to protobuf Timestamp format."""
    seconds = int(unix_ts)
    nanos = int((unix_ts - seconds) * 1e9)
    return encode_varint_field(1, seconds) + encode_varint_field(2, nanos)


# ============================================================
# INDEX READER
# ============================================================

def read_current_index(state_db_path):
    """Read and parse the current trajectory summaries index."""
    conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT value FROM ItemTable WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'"
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return None, []

    raw_value = row[0]
    outer = base64.b64decode(raw_value)
    fields = decode_protobuf_fields(outer)

    # Extract UUIDs from entries
    entries = []
    for fn, ft, val in fields:
        entry_fields = decode_protobuf_fields(val)
        uuid_str = None
        for efn, eft, eval_data in entry_fields:
            if efn == 1 and eft == 'bytes':
                try:
                    candidate = eval_data.decode('utf-8')
                    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-', candidate):
                        uuid_str = candidate
                except (UnicodeDecodeError, ValueError):
                    pass
        if uuid_str:
            entries.append({'uuid': uuid_str, 'raw': val})

    return raw_value, entries


# ============================================================
# WORKSPACE DETECTION
# ============================================================

# Keywords that suggest a workspace based on conversation content
WORKSPACE_KEYWORDS = {
    'HSP': ['hospital', 'staffing', 'provider', 'facility', 'credentialing',
            'timesheet', 'impersonat', 'onboarding', 'hsp', 'portal',
            'contract letter', 'fcl', 'pcl', 'passkey', 'webauthn',
            'laravel', 'php artisan', 'livewire', 'seeders', 'migration'],
    'VeRO': ['vero', 'ebay', 'enforcement', 'listing', 'veroscout'],
    'Xero': ['xero', 'accounting', 'invoice'],
    'Botpyle': ['botpyle', 'kicad', 'pcb', 'circuit'],
    'Financial': ['financial', 'investment', 'streamlit', 'portfolio'],
}


def scan_workspaces_on_disk(home_dir=None):
    """Scan the Desktop for directories that could be workspaces."""
    if home_dir is None:
        home_dir = Path.home()
    
    workspaces = {}
    desktop = home_dir / 'Desktop'
    
    if desktop.exists():
        for item in desktop.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                # URL-encode the path for the workspace URI
                uri = f"file:///c%3A/Users/{home_dir.name}/Desktop/{item.name}"
                workspaces[item.name] = uri
    
    # Also check OneDrive Desktop
    onedrive_desktop = home_dir / 'OneDrive' / 'Desktop'
    if onedrive_desktop.exists():
        for item in onedrive_desktop.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                uri = f"file:///c%3A/Users/{home_dir.name}/OneDrive/Desktop/{item.name}"
                if item.name not in workspaces:
                    workspaces[item.name] = uri
    
    return workspaces


def detect_workspace(conv_id, brain_dir, known_workspaces):
    """Detect which workspace a conversation belongs to."""
    brain_path = brain_dir / conv_id
    all_text = ""

    if brain_path.is_dir():
        for fpath in brain_path.rglob('*.md'):
            try:
                all_text += fpath.read_text(encoding='utf-8', errors='ignore')[:10000] + "\n"
            except OSError:
                pass
        for fpath in brain_path.rglob('*.txt'):
            try:
                all_text += fpath.read_text(encoding='utf-8', errors='ignore')[:10000] + "\n"
            except OSError:
                pass

    # Method 1: Direct path references in content
    path_matches = Counter()
    
    # Windows backslash paths
    for m in re.finditer(r'[Cc]:\\Users\\[^\\]+\\Desktop\\([A-Za-z0-9_-]+)\\', all_text):
        ws = m.group(1)
        if ws in known_workspaces:
            path_matches[ws] += 1

    # URI-encoded paths
    for m in re.finditer(r'file:///c%3A/Users/[^/]+/Desktop/([A-Za-z0-9_-]+)', all_text):
        ws = m.group(1)
        if ws in known_workspaces:
            path_matches[ws] += 1

    # OneDrive paths
    for m in re.finditer(r'OneDrive[/\\]Desktop[/\\]([A-Za-z0-9_-]+)', all_text):
        ws = m.group(1)
        if ws in known_workspaces:
            path_matches[ws] += 1

    if path_matches:
        return path_matches.most_common(1)[0][0]

    # Method 2: Keyword matching
    keyword_scores = Counter()
    text_lower = all_text.lower()
    for ws, keywords in WORKSPACE_KEYWORDS.items():
        if ws in known_workspaces:
            for kw in keywords:
                count = text_lower.count(kw.lower())
                if count > 0:
                    keyword_scores[ws] += count

    if keyword_scores:
        best = keyword_scores.most_common(1)[0]
        if best[1] >= 2:
            return best[0]

    # Method 3: Title-based hints
    title = extract_title(conv_id, brain_dir)
    if title:
        title_lower = title.lower()
        for ws, keywords in WORKSPACE_KEYWORDS.items():
            if ws in known_workspaces:
                for kw in keywords:
                    if kw.lower() in title_lower:
                        return ws

    return None


def extract_title(conv_id, brain_dir):
    """Extract conversation title from brain directory artifacts."""
    brain_path = brain_dir / conv_id
    if not brain_path.is_dir():
        return None

    # Check common artifact files in priority order
    for fname in ['task.md', 'implementation_plan.md', 'walkthrough.md']:
        fpath = brain_path / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding='utf-8', errors='ignore')[:500]
                for line in content.split('\n'):
                    line = line.strip()
                    if line.startswith('# '):
                        return line[2:].strip()
            except OSError:
                pass

    # Try any .md file
    for fpath in sorted(brain_path.glob('*.md')):
        try:
            content = fpath.read_text(encoding='utf-8', errors='ignore')[:500]
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('# '):
                    return line[2:].strip()
        except OSError:
            pass

    return None


# ============================================================
# ENTRY BUILDER
# ============================================================

def build_entry(conv_id, title, created_ts, modified_ts, workspace_uri):
    """Build a minimal trajectory summary entry for the index.
    
    Schema (reverse-engineered from existing entries):
        Outer: field 1 (string: UUID), field 2 (bytes: inner protobuf)
        Inner field 2 contains: field 1 (base64-encoded protobuf)
        Base64 payload:
            field 1: title (string)
            field 2: step count (varint)
            field 3: modified timestamp (message)
            field 4: session UUID (string)
            field 5: status flag (varint, always 1)
            field 7: created timestamp (message)
            field 9: workspace info (message {field 1: URI, field 2: scheme, field 3: empty})
            field 10: last active timestamp (message)
    """
    session_id = str(uuid_mod.uuid4())

    inner_proto = b''
    inner_proto += encode_string(1, title)
    inner_proto += encode_varint_field(2, 1)  # step count
    inner_proto += encode_message(3, unix_to_proto_timestamp(modified_ts))
    inner_proto += encode_string(4, session_id)
    inner_proto += encode_varint_field(5, 1)  # status
    inner_proto += encode_message(7, unix_to_proto_timestamp(created_ts))

    workspace_msg = (
        encode_string(1, workspace_uri)
        + encode_string(2, "file:///")
        + encode_string(3, "")
    )
    inner_proto += encode_message(9, workspace_msg)
    inner_proto += encode_message(10, unix_to_proto_timestamp(modified_ts))

    # Base64 encode the inner protobuf
    inner_b64 = base64.b64encode(inner_proto).decode('ascii')

    # Wrap in field-2 protobuf
    field2_proto = encode_string(1, inner_b64)

    # Build the entry: field 1 = UUID, field 2 = wrapped protobuf
    entry_proto = encode_string(1, conv_id) + encode_bytes_field(2, field2_proto)

    return entry_proto


# ============================================================
# MAIN OPERATIONS
# ============================================================

def scan(paths):
    """Scan and report the current state."""
    print("\n=== Antigravity Conversation Recovery Tool ===\n")

    # Check paths exist
    if not paths['state_db'].exists():
        print(f"ERROR: State database not found at {paths['state_db']}")
        print("Is Antigravity installed? Check the path and try again.")
        return False

    if not paths['conversations_dir'].exists():
        print(f"ERROR: Conversations directory not found at {paths['conversations_dir']}")
        return False

    # Read current index
    raw_value, entries = read_current_index(paths['state_db'])
    if raw_value is None:
        print("ERROR: No trajectory summaries found in the database.")
        print("The index key may have been deleted. Try --rollback if you have a backup.")
        return False

    index_uuids = {e['uuid'] for e in entries}

    # Scan .pb files
    pb_files = {f.stem for f in paths['conversations_dir'].glob('*.pb')}

    missing = sorted(pb_files - index_uuids)
    orphaned = sorted(index_uuids - pb_files)

    print(f"State database:     {paths['state_db']}")
    print(f"Conversations dir:  {paths['conversations_dir']}")
    print(f"Brain directory:    {paths['brain_dir']}")
    print()
    print(f"Entries in index:   {len(entries)}")
    print(f".pb files on disk:  {len(pb_files)}")
    print(f"Missing from index: {len(missing)}")
    print(f"Orphaned in index:  {len(orphaned)}")

    if not missing:
        print("\n[OK] All conversations are indexed! Nothing to recover.")
        return True

    print(f"\n--- Missing Conversations ({len(missing)}) ---\n")

    known_workspaces = scan_workspaces_on_disk()
    
    for conv_id in missing:
        pb_path = paths['conversations_dir'] / f"{conv_id}.pb"
        mtime = datetime.fromtimestamp(pb_path.stat().st_mtime)
        title = extract_title(conv_id, paths['brain_dir']) or f"(no title)"
        ws = detect_workspace(conv_id, paths['brain_dir'], known_workspaces) or "unknown"
        print(f"  {conv_id[:12]}  {mtime.strftime('%Y-%m-%d %H:%M')}  [{ws}]  {title[:60]}")

    return True


def backup(paths):
    """Create a backup of the state database and conversations."""
    backup_dir = paths['backup_dir']
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nBacking up to: {backup_dir}\n")

    # Back up state DB
    src = paths['state_db']
    dst = backup_dir / 'state.vscdb'
    shutil.copy2(src, dst)
    print(f"  [OK] {src.name} ({src.stat().st_size:,} bytes)")

    # Back up state DB backup if it exists
    src_backup = src.with_suffix('.vscdb.backup')
    if src_backup.exists():
        shutil.copy2(src_backup, backup_dir / 'state.vscdb.backup')
        print(f"  [OK] {src_backup.name}")

    # Back up conversations
    conv_backup = backup_dir / 'conversations'
    if not conv_backup.exists():
        shutil.copytree(paths['conversations_dir'], conv_backup)
        pb_count = len(list(conv_backup.glob('*.pb')))
        print(f"  [OK] conversations/ ({pb_count} .pb files)")
    else:
        print(f"  [SKIP]  conversations/ already backed up")

    print(f"\nBackup complete!")
    return True


def build(paths, default_workspace=None):
    """Build the experimental index with missing conversations added."""
    print("\n=== Building Experimental Index ===\n")

    # Read current index
    raw_value, entries = read_current_index(paths['state_db'])
    if raw_value is None:
        print("ERROR: No trajectory summaries found in the database.")
        return False

    existing_outer = base64.b64decode(raw_value)
    existing_fields = decode_protobuf_fields(existing_outer)
    index_uuids = {e['uuid'] for e in entries}

    print(f"Current index: {len(entries)} entries")

    # Find missing conversations
    pb_files = {f.stem for f in paths['conversations_dir'].glob('*.pb')}
    missing = sorted(pb_files - index_uuids)

    if not missing:
        print("No missing conversations found!")
        return True

    print(f"Missing conversations: {len(missing)}")

    # Detect workspaces
    known_workspaces = scan_workspaces_on_disk()
    
    if default_workspace and default_workspace in known_workspaces:
        default_uri = known_workspaces[default_workspace]
    else:
        # Use the most common workspace from existing entries as default
        default_uri = list(known_workspaces.values())[0] if known_workspaces else "file:///unknown"

    # Build new entries
    new_entries = []
    ws_counts = Counter()

    for conv_id in missing:
        pb_path = paths['conversations_dir'] / f"{conv_id}.pb"
        stat = pb_path.stat()
        created_ts = stat.st_ctime
        modified_ts = stat.st_mtime

        title = extract_title(conv_id, paths['brain_dir']) or f"Conversation {conv_id[:8]}"
        workspace_name = detect_workspace(conv_id, paths['brain_dir'], known_workspaces)

        if workspace_name and workspace_name in known_workspaces:
            workspace_uri = known_workspaces[workspace_name]
        else:
            workspace_name = "(default)"
            workspace_uri = default_uri

        entry = build_entry(conv_id, title, created_ts, modified_ts, workspace_uri)
        new_entries.append(entry)
        ws_counts[workspace_name] += 1
        print(f"  + {conv_id[:12]}  [{workspace_name:20s}]  {title[:60]}")

    # Combine existing + new
    combined = bytearray()
    for fn, ft, val in existing_fields:
        combined += encode_message(1, val)
    for entry in new_entries:
        combined += encode_message(1, entry)
    combined = bytes(combined)

    new_index_b64 = base64.b64encode(combined).decode('ascii')

    # Validate
    print(f"\n--- Validation ---")
    decoded = base64.b64decode(new_index_b64)
    validated_fields = decode_protobuf_fields(decoded)

    validated_uuids = set()
    for fn, ft, val in validated_fields:
        entry_fields = decode_protobuf_fields(val)
        for efn, eft, eval_data in entry_fields:
            if efn == 1 and eft == 'bytes':
                try:
                    uid = eval_data.decode('utf-8')
                    if re.match(r'^[0-9a-f]{8}-', uid):
                        validated_uuids.add(uid)
                except (UnicodeDecodeError, ValueError):
                    pass

    errors = []

    # Check all existing entries preserved
    if not index_uuids.issubset(validated_uuids):
        lost = index_uuids - validated_uuids
        errors.append(f"LOST {len(lost)} existing entries!")

    # Check byte-for-byte preservation
    orig_entries = [val for fn, ft, val in existing_fields]
    new_raw = [val for fn, ft, val in validated_fields]
    for i, orig in enumerate(orig_entries):
        if i >= len(new_raw) or new_raw[i] != orig:
            errors.append(f"Entry {i} modified from original!")

    if errors:
        print(f"\n[FAIL] VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        print("\nExperimental index NOT saved.")
        return False

    print(f"  [OK] Total entries: {len(validated_fields)}")
    print(f"  [OK] Existing entries preserved: {len(index_uuids)}")
    print(f"  [OK] New entries added: {len(missing)}")
    print(f"  [OK] Byte-for-byte integrity verified")

    # Save
    output_path = paths['experimental_index']
    output_path.write_text(new_index_b64)

    print(f"\n  Workspace distribution of new entries:")
    for ws, count in ws_counts.most_common():
        print(f"    {ws}: {count}")

    print(f"\n[OK] Experimental index saved to: {output_path}")
    print(f"   Size: {len(new_index_b64):,} chars")
    return True


def swap(paths):
    """Swap the experimental index into the state database."""
    experimental = paths['experimental_index']
    if not experimental.exists():
        print(f"ERROR: Experimental index not found at {experimental}")
        print("Run with --build first.")
        return False

    print("\n=== Swapping Index ===\n")

    # Create pre-swap backup
    backup_dir = paths['backup_dir']
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    pre_swap = backup_dir / f"state.vscdb.pre-swap-{ts}"
    shutil.copy2(paths['state_db'], pre_swap)
    print(f"Pre-swap backup: {pre_swap}")

    # Read experimental index
    new_index = experimental.read_text()
    print(f"Experimental index: {len(new_index):,} chars")

    # Write to DB
    conn = sqlite3.connect(str(paths['state_db']))
    cur = conn.cursor()
    cur.execute(
        "UPDATE ItemTable SET value = ? WHERE key = 'antigravityUnifiedStateSync.trajectorySummaries'",
        (new_index,)
    )
    rows = cur.rowcount
    conn.commit()
    conn.close()

    if rows == 0:
        print("WARNING: No rows updated. The key may not exist.")
        return False

    print(f"Updated: {rows} row(s)")
    print(f"\n[OK] Swap complete! Open Antigravity to verify.")
    print(f"\nIf something went wrong, run:")
    print(f"  python recover.py --rollback")
    return True


def rollback(paths):
    """Rollback to the most recent backup."""
    backup_dir = paths['backup_dir']

    # Find the most recent backup
    candidates = []
    if backup_dir.exists():
        for f in backup_dir.glob('state.vscdb*'):
            candidates.append(f)

    if not candidates:
        print(f"ERROR: No backups found in {backup_dir}")
        return False

    # Sort by modification time, most recent first
    candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    print("\nAvailable backups:")
    for i, f in enumerate(candidates):
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        print(f"  [{i}] {f.name} ({mtime.strftime('%Y-%m-%d %H:%M:%S')}, {f.stat().st_size:,} bytes)")

    # Use the original backup by default
    original = backup_dir / 'state.vscdb'
    if original.exists():
        restore_from = original
    else:
        restore_from = candidates[0]

    print(f"\nRestoring from: {restore_from}")
    shutil.copy2(restore_from, paths['state_db'])
    print(f"[OK] Restored state.vscdb from backup")
    print(f"\nOpen Antigravity - your conversations should be back to their previous state.")
    return True


# ============================================================
# INTERACTIVE MODE
# ============================================================

def interactive(paths):
    """Run in interactive mode with guided steps."""
    print("""
============================================================
     Antigravity Conversation Recovery Tool
                                                         
  Recovers conversations lost after Windows restarts,
  crashes, or forced updates.
============================================================
""")

    # Step 1: Scan
    print("Step 1: Scanning for missing conversations...\n")
    if not scan(paths):
        return

    raw_value, entries = read_current_index(paths['state_db'])
    pb_files = {f.stem for f in paths['conversations_dir'].glob('*.pb')}
    missing = pb_files - {e['uuid'] for e in entries}

    if not missing:
        return

    # Step 2: Confirm
    print(f"\nFound {len(missing)} missing conversation(s)!")
    answer = input("\nProceed with recovery? [Y/n]: ").strip().lower()
    if answer and answer != 'y':
        print("Aborted.")
        return

    # Step 3: Backup
    print("\n" + "="*50)
    print("Step 2: Creating backups...\n")
    if not backup(paths):
        return

    # Step 4: Build
    print("\n" + "="*50)
    print("Step 3: Building recovery index...\n")
    if not build(paths):
        return

    # Step 5: Swap
    print("\n" + "="*50)
    print("Step 4: Ready to apply!")
    print("""
[!]  IMPORTANT: Close Antigravity completely before proceeding!

The state database is locked while Antigravity is running.
Close the application, then press Enter to continue.
""")
    input("Press Enter when Antigravity is closed (or Ctrl+C to abort)... ")

    if not swap(paths):
        return

    print("""
[OK] Recovery complete!

1. Open Antigravity
2. Check your sidebar — missing conversations should be restored
3. If something looks wrong, close Antigravity and run:
   python recover.py --rollback
""")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Recover missing conversations in Antigravity (Agent Manager)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python recover.py               Interactive mode (recommended)
  python recover.py --scan        Report missing conversations  
  python recover.py --backup      Create safety backup
  python recover.py --build       Build recovery index (no swap)
  python recover.py --swap        Apply the recovery index (close Antigravity first!)
  python recover.py --rollback    Undo: restore from backup
  
Safety:
  This tool never modifies .pb conversation files.
  All changes are to the sidebar index only.
  Backups are created before any modifications.
        """
    )
    parser.add_argument('--scan', action='store_true', help='Scan and report missing conversations')
    parser.add_argument('--backup', action='store_true', help='Create backup of state DB and conversations')
    parser.add_argument('--build', action='store_true', help='Build experimental recovery index')
    parser.add_argument('--swap', action='store_true', help='Swap in the recovery index (close Antigravity first!)')
    parser.add_argument('--rollback', action='store_true', help='Rollback to backup')
    parser.add_argument('--state-db', type=str, help='Path to state.vscdb (auto-detected by default)')
    parser.add_argument('--conversations-dir', type=str, help='Path to conversations directory')
    parser.add_argument('--brain-dir', type=str, help='Path to brain directory')
    parser.add_argument('--default-workspace', type=str, help='Default workspace for undetected conversations')

    args = parser.parse_args()

    # Get paths
    paths = get_default_paths()
    if args.state_db:
        paths['state_db'] = Path(args.state_db)
    if args.conversations_dir:
        paths['conversations_dir'] = Path(args.conversations_dir)
    if args.brain_dir:
        paths['brain_dir'] = Path(args.brain_dir)

    # Route to action
    if args.scan:
        scan(paths)
    elif args.backup:
        backup(paths)
    elif args.build:
        build(paths, args.default_workspace)
    elif args.swap:
        swap(paths)
    elif args.rollback:
        rollback(paths)
    else:
        interactive(paths)


if __name__ == '__main__':
    main()
