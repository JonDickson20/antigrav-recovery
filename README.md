# Antigravity Conversation Recovery Tool

> *You've been deep in flow for three days straight. Dozens of conversations open across multiple workspaces — shipping features, debugging production issues, architecting systems. Antigravity has been your copilot through all of it.*
>
> *Then Windows decides it's time for updates.*
>
> *You wake up, walk to your PC, and find Antigravity has restarted. You open the sidebar and... everything from the last three days is gone. Dozens of conversations, wiped from the UI. The context, the decisions, the half-finished work — all invisible.*
>
> *Your data isn't actually gone. Every conversation is still safely stored as a `.pb` file on disk. The problem is that Antigravity's **sidebar index** — a cached protobuf inside a SQLite database — never got a chance to flush before Windows pulled the plug. When Antigravity came back up, it restored a stale copy of the index from days ago, and your recent conversations simply don't exist as far as the UI is concerned.*
>
> *This tool fixes that.*

## What Happened (Technically)

Antigravity maintains a conversation index in a key called `antigravityUnifiedStateSync.trajectorySummaries` inside a SQLite database (`state.vscdb`). This index is what populates your sidebar with conversation titles, timestamps, and workspace assignments.

The database also keeps a `.backup` copy. When Windows force-restarts, Antigravity can fail to write its latest in-memory state to disk. On next launch, it may fall back to the `.backup` file — which could be hours or days behind. The result: every conversation you had between the backup snapshot and the crash becomes invisible.

**Your actual conversation data (the `.pb` files) is completely untouched.** The sidebar just doesn't know they exist anymore.

## The Fix

This tool reverse-engineers the protobuf index format, scans your `.pb` files on disk, identifies which conversations are missing from the index, and injects new entries with:

- The correct conversation **title** (extracted from brain directory artifacts)
- The correct **workspace** assignment (detected via file path analysis and keyword matching)
- Proper **timestamps** (from the `.pb` file metadata)

Existing index entries are preserved **byte-for-byte**. The tool only appends — it never modifies what's already there.

## Quick Start

```bash
# Interactive mode — walks you through everything
python recover.py
```

That's it. The tool will scan, back up, build, and swap — with confirmation at every step.

### Manual Mode

```bash
# 1. See what's missing
python recover.py --scan

# 2. Create a safety backup
python recover.py --backup

# 3. Build the recovery index (isolated, safe)
python recover.py --build

# 4. CLOSE ANTIGRAVITY, then apply
python recover.py --swap

# 5. Open Antigravity — your conversations should be back!

# If anything went wrong:
python recover.py --rollback
```

## Safety Guarantees

| Safety Feature | Details |
|---|---|
| **`.pb` files are never modified** | The tool only reads conversation files, never writes to them |
| **Existing entries preserved byte-for-byte** | The tool appends new entries; existing ones are untouched |
| **Full backup before any change** | State DB and all conversations backed up to your Desktop |
| **One-command rollback** | `python recover.py --rollback` restores the exact previous state |
| **Isolated build step** | The recovery index is built to a temp file first, validated, then swapped |

## Requirements

- **Python 3.8+** (no external dependencies — stdlib only)
- **Antigravity must be closed** before running `--swap` or `--rollback`

## Platform Support

| Platform | State DB Location |
|----------|-------------------|
| Windows | `%APPDATA%\Antigravity\User\globalStorage\state.vscdb` |
| macOS | `~/Library/Application Support/Antigravity/User/globalStorage/state.vscdb` |
| Linux | `~/.config/Antigravity/User/globalStorage/state.vscdb` |

All paths are auto-detected. Override with `--state-db`, `--conversations-dir`, or `--brain-dir` if your setup is non-standard.

## How Workspace Detection Works

When recovering conversations, the tool needs to figure out which workspace (HSP, VeRO, engine, etc.) each conversation belongs to. It uses a three-layer strategy:

1. **File path analysis** — Scans brain directory artifacts for references like `C:\Users\...\Desktop\HSP\` or `file:///c%3A/.../Desktop/engine`
2. **Keyword matching** — Matches conversation content against workspace-specific terms (e.g., "laravel", "provider", "timesheet" → HSP)
3. **Title analysis** — Checks the conversation title itself for workspace hints

If detection fails, the tool falls back to the most common workspace (configurable with `--default-workspace`).

## CLI Reference

```
python recover.py [OPTIONS]

Options:
  --scan                Scan and report missing conversations
  --backup              Create safety backup
  --build               Build recovery index (no swap)
  --swap                Apply the recovery index
  --rollback            Undo: restore from backup
  --state-db PATH       Custom path to state.vscdb
  --conversations-dir   Custom path to conversations directory
  --brain-dir PATH      Custom path to brain directory
  --default-workspace   Default workspace name for undetected conversations
```

## FAQ

**Q: Will this delete my conversations?**
No. The tool never modifies `.pb` files. It only updates the sidebar index.

**Q: What if the swap makes things worse?**
Run `python recover.py --rollback`. It restores the exact previous state from backup.

**Q: Why can't I just reinstall Antigravity?**
Reinstalling doesn't help — the stale index persists in your user data directory. You need to repair the index itself, which is what this tool does.

**Q: Does this work with VS Code or Cursor?**
This tool is specifically for the standalone Antigravity (Agent Manager) application. VS Code and Cursor use different storage mechanisms.

**Q: I lost conversations from weeks ago, not just days. Will this work?**
Yes — the tool recovers any conversation that exists as a `.pb` file on disk but is missing from the sidebar index, regardless of age.

## The Technical Deep-Dive

<details>
<summary>Index Protobuf Schema (reverse-engineered)</summary>

The conversation index lives in `antigravityUnifiedStateSync.trajectorySummaries` as a base64-encoded protobuf:

```
Base64 → Outer Protobuf (repeated field 1):
  └─ Entry message:
       ├─ field 1 (string): Conversation UUID
       └─ field 2 (bytes): Inner protobuf wrapper
            └─ field 1 (string): Base64-encoded payload protobuf
                 ├─ field 1:  Title (string)
                 ├─ field 2:  Step count (varint)
                 ├─ field 3:  Modified timestamp {seconds, nanos}
                 ├─ field 4:  Session UUID (string)
                 ├─ field 5:  Status flag (varint, always 1)
                 ├─ field 7:  Created timestamp {seconds, nanos}
                 ├─ field 9:  Workspace info
                 │    ├─ field 1: Workspace URI (e.g., "file:///c%3A/.../HSP")
                 │    ├─ field 2: Scheme ("file:///")
                 │    └─ field 3: Empty string
                 ├─ field 10: Last active timestamp
                 ├─ field 12: Most recent task boundary (optional, complex)
                 ├─ field 14: Previous task boundary (optional, complex)
                 ├─ field 15: Final timestamp (optional)
                 └─ field 16: Final step count (optional)
```

The tool constructs minimal entries using fields 1-10, which is sufficient for sidebar display. The optional task boundary fields (12, 14) are populated by Antigravity itself when you open the conversation.

</details>

## Contributing

Found a different root cause for conversation loss? Have improvements for workspace detection? PRs and issues welcome.

## License

MIT — free to use, modify, and distribute.

---

*Built during a real incident where 50+ conversations vanished after a Windows forced restart. All data was recovered.*
