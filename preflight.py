"""Check a deployment without connecting to Discord.

Run this on the server when the service won't start; it reports the exact
problem instead of leaving you to read a crash loop:

    /opt/ebird-discord-bot/.venv/bin/python preflight.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
problems: list[str] = []


def check(label: str, ok: bool, detail: str = "", fatal: bool = True) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'' if ok else f': {detail}'}")
    if not ok and fatal:
        problems.append(f"{label}: {detail}")
    return ok


print(f"python {sys.version.split()[0]} at {sys.executable}")
print(f"working directory: {HERE}\n")

print("files")
for name in ("bot.py", "ebird_media.py", "alerts.py", "db.py"):
    check(name, (HERE / name).is_file(), "missing from this directory")
check(".env", (HERE / ".env").is_file(), "missing; copy .env.example and add DISCORD_TOKEN")
check(
    "directory is writable",
    os.access(HERE, os.W_OK),
    "the service user cannot write bot.db (its database) here",
)

print("\ndependencies")
try:
    import discord

    check(f"discord.py {discord.__version__}", True)
    from discord import app_commands

    modern = hasattr(app_commands, "AppCommandContext") and hasattr(
        app_commands, "AppInstallationType"
    )
    check(
        "user-install API (needs discord.py 2.4+)",
        modern,
        "too old for user install; upgrade with pip install -U -r requirements.txt",
        fatal=False,
    )
except ImportError as error:
    check("discord.py installed", False, str(error))
try:
    import aiohttp  # noqa: F401

    check("aiohttp", True)
except ImportError as error:
    check("aiohttp", False, str(error))
try:
    import dotenv  # noqa: F401

    check("python-dotenv", True)
except ImportError as error:
    check("python-dotenv", False, str(error))

print("\nproject modules (a mismatch between deployed files shows up here)")
try:
    sys.path.insert(0, str(HERE))
    import bot  # noqa: F401

    check("bot.py imports", True)
    names = sorted(c.name for c in bot.bot.tree.get_commands())
    check(f"{len(names)} commands defined: {', '.join(names)}", bool(names), "none found")
    withdetail = [
        c.name
        for c in bot.bot.tree.get_commands()
        if any(p.name == "detail" for p in c.parameters)
    ]
    check(
        f"detail option on {len(withdetail)} commands: {', '.join(withdetail) or 'nothing'}",
        len(withdetail) == 6,
        "expected 6 (checklist, checkmedia, top, recent, sp, rare); the deployed "
        "files may be from different versions",
    )
    check("database bot.db opened", (HERE / "bot.db").is_file(), "not created")
    import alerts as alerts_module
    import db as db_module

    conn = db_module.connect(HERE / "bot.db")
    for table, needed in (
        ("subscriptions", {c.strip() for c in alerts_module._SUB_COLUMNS.split(",")}),
        ("seen", {"user_id", "region", "key", "obs_dt", "status", "species", "rarity", "place"}),
    ):
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = needed - have
        check(
            f"{table} table has every column the code writes",
            not missing,
            f"missing {', '.join(sorted(missing))}; db.py is older than the other "
            "files, so its schema upgrades never ran. Deploy all *.py together.",
        )
    conn.close()
    leftovers = [
        name for name in ("aliases.json", "subscriptions.json") if (HERE / name).exists()
    ]
    check(
        "old JSON state migrated" + (f" (still present: {', '.join(leftovers)})" if leftovers else ""),
        not leftovers,
        "importing bot.py should have renamed them *.migrated; check write permissions",
        fatal=False,
    )
except Exception as error:  # noqa: BLE001 - this is the diagnostic
    check("bot.py imports", False, f"{type(error).__name__}: {error}")

print("\nconfiguration")
token = os.getenv("DISCORD_TOKEN", "")
check("DISCORD_TOKEN set", bool(token), "not found in the environment or .env")
guild = os.getenv("GUILD_ID", "")
if guild:
    check(
        f"GUILD_ID={guild}",
        all(part.strip().isdigit() for part in guild.replace(",", " ").split()),
        "must be numeric server IDs, comma-separated",
        fatal=False,
    )

print()
if problems:
    print(f"{len(problems)} problem(s) to fix:")
    for item in problems:
        print(f"  - {item}")
    raise SystemExit(1)
print("preflight passed; the service should start.")
