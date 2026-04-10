#!/usr/bin/env python3
"""
patch_hermes.py  –  Patch hermes-agent to add QQ Bot platform support.

Usage:
    python3 patch_hermes.py /path/to/hermes-agent

What it does:
  1. Copies qqbot.py into gateway/platforms/qqbot.py
  2. Adds QQBOT to the Platform enum in gateway/config.py
  3. Registers QQBotAdapter in gateway/run.py  (_create_adapter)

All changes are idempotent – running the script twice is safe.
"""

import shutil
import sys
from pathlib import Path
from typing import Optional


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def patch_file(path: Path, old: str, new: str, label: str,
               already_applied_marker: Optional[str] = None,
               optional: bool = False) -> None:
    content = path.read_text(encoding="utf-8")
    marker = already_applied_marker or new.strip()
    if marker in content:
        print(f"  [skip] {label} – already applied")
        return
    if old not in content:
        if optional:
            print(f"  [warn] {label} – anchor not found, skipping (non-critical)")
            return
        die(f"Anchor not found in {path}: {old!r}")
    patched = content.replace(old, new, 1)
    path.write_text(patched, encoding="utf-8")
    print(f"  [ok]   {label}")


def main() -> None:
    if len(sys.argv) < 2:
        die("Usage: python3 patch_hermes.py /path/to/hermes-agent")

    agent_dir = Path(sys.argv[1]).resolve()
    if not agent_dir.is_dir():
        die(f"hermes-agent directory not found: {agent_dir}")

    script_dir = Path(__file__).parent.resolve()
    qqbot_src  = script_dir / "qqbot.py"
    if not qqbot_src.exists():
        die(f"qqbot.py not found next to this script: {qqbot_src}")

    print(f"\n=== Patching hermes-agent at {agent_dir} ===\n")

    # ------------------------------------------------------------------ #
    # 1. Copy qqbot.py into gateway/platforms/
    # ------------------------------------------------------------------ #
    platforms_dir = agent_dir / "gateway" / "platforms"
    if not platforms_dir.is_dir():
        die(f"gateway/platforms/ not found: {platforms_dir}")

    dest = platforms_dir / "qqbot.py"
    shutil.copy2(qqbot_src, dest)
    print(f"  [ok]   Copied qqbot.py -> {dest}")

    # ------------------------------------------------------------------ #
    # 2. Add QQBOT to Platform enum in gateway/config.py
    # ------------------------------------------------------------------ #
    config_py = agent_dir / "gateway" / "config.py"
    if not config_py.exists():
        die(f"gateway/config.py not found: {config_py}")

    patch_file(
        config_py,
        old='    BLUEBUBBLES = "bluebubbles"',
        new='    BLUEBUBBLES = "bluebubbles"\n    QQBOT = "qqbot"',
        label="Add Platform.QQBOT to gateway/config.py",
        already_applied_marker='    QQBOT = "qqbot"',
    )

    # ------------------------------------------------------------------ #
    # 3. Register QQBotAdapter in gateway/run.py
    # ------------------------------------------------------------------ #
    run_py = agent_dir / "gateway" / "run.py"
    if not run_py.exists():
        die(f"gateway/run.py not found: {run_py}")

    patch_file(
        run_py,
        old='''\
        elif platform == Platform.BLUEBUBBLES:
            from gateway.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements
            if not check_bluebubbles_requirements():
                logger.warning("BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured")
                return None
            return BlueBubblesAdapter(config)

        return None''',
        new='''\
        elif platform == Platform.BLUEBUBBLES:
            from gateway.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements
            if not check_bluebubbles_requirements():
                logger.warning("BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured")
                return None
            return BlueBubblesAdapter(config)

        elif platform == Platform.QQBOT:
            from gateway.platforms.qqbot import QQBotAdapter, check_qqbot_requirements
            if not check_qqbot_requirements(config):
                logger.warning("QQBot: httpx/websockets not installed or QQBOT_APP_ID/QQBOT_CLIENT_SECRET not set")
                return None
            return QQBotAdapter(config)

        return None''',
        label="Register QQBotAdapter in gateway/run.py",
        already_applied_marker="platform == Platform.QQBOT:",
    )

    # ------------------------------------------------------------------ #
    # 4. Register QQBot in hermes_cli/gateway.py
    # ------------------------------------------------------------------ #
    gateway_py = agent_dir / "hermes_cli" / "gateway.py"
    if gateway_py.exists():
        patch_file(
            gateway_py,
            old='''\
_PLATFORMS = [
    {
        "key": "telegram",''',
            new='''\
_PLATFORMS = [
    {
        "key": "qqbot",
        "label": "QQ Bot",
        "emoji": "🐧",
        "token_var": "QQBOT_APP_ID",
        "setup_instructions": [
            "1. Visit https://q.qq.com",
            "2. Click '立即使用' (Use Now)",
            "3. Click '创建机器人' (Create Bot)",
            "4. Fill in basic info and get AppID + AppSecret",
        ],
        "vars": [
            {"name": "QQBOT_APP_ID", "prompt": "QQ Bot App ID", "password": False,
             "help": "Your bot's App ID from QQ Open Platform"},
            {"name": "QQBOT_CLIENT_SECRET", "prompt": "QQ Bot Client Secret", "password": True,
             "help": "Your bot's Client Secret from QQ Open Platform"},
        ],
    },
    {
        "key": "telegram",''',
            label="Add QQBot to _PLATFORMS in hermes_cli/gateway.py",
            already_applied_marker='    "key": "qqbot"',
            optional=True,
        )
    else:
        print(f"  [skip] hermes_cli/gateway.py not found (optional)")

    # ------------------------------------------------------------------ #
    # 5. Fix JSON/YAML bug in gateway/run.py
    # ------------------------------------------------------------------ #
    run_py = agent_dir / "gateway" / "run.py"
    if run_py.exists():
        patch_file(
            run_py,
            old='''\
    if args.config:
        import json
        with open(args.config, encoding="utf-8") as f:
            data = json.load(f)
            config = GatewayConfig.from_dict(data)''',
            new='''\
    if args.config:
        import yaml
        with open(args.config, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            config = GatewayConfig.from_dict(data)''',
            label="Fix JSON/YAML bug in gateway/run.py (use yaml.safe_load for YAML files)",
            already_applied_marker='        import yaml\n        with open(args.config',
            optional=True,
        )
    else:
        print(f"  [skip] gateway/run.py not found")

    # ------------------------------------------------------------------ #
    # 6. Add hermes-qqbot to toolsets.py
    # ------------------------------------------------------------------ #
    toolsets_py = agent_dir / "toolsets.py"
    if toolsets_py.exists():
        # Insert hermes-qqbot entry before hermes-gateway (which is last)
        patch_file(
            toolsets_py,
            old='''\
    "hermes-gateway": {
        "description": "Gateway toolset - union of all messaging platform tools",''',
            new='''\
    "hermes-qqbot": {
        "description": "QQ Bot toolset - QQ Open Platform messaging (full access)",
        "tools": _HERMES_CORE_TOOLS,
        "includes": []
    },

    "hermes-gateway": {
        "description": "Gateway toolset - union of all messaging platform tools",''',
            label='Add hermes-qqbot to TOOLSETS in toolsets.py',
            already_applied_marker='"hermes-qqbot"',
            optional=True,
        )
        # Also add hermes-qqbot to the hermes-gateway includes list
        patch_file(
            toolsets_py,
            old='"includes": ["hermes-telegram", "hermes-discord",',
            new='"includes": ["hermes-qqbot", "hermes-telegram", "hermes-discord",',
            label='Add hermes-qqbot to hermes-gateway includes in toolsets.py',
            already_applied_marker='"hermes-qqbot", "hermes-telegram"',
            optional=True,
        )
    else:
        print(f"  [skip] toolsets.py not found (optional)")

    # ------------------------------------------------------------------ #
    # 7. Add qqbot to config.yaml platform_toolsets
    # ------------------------------------------------------------------ #
    config_yaml = agent_dir.parent / "config.yaml"
    if config_yaml.exists():
        try:
            import yaml as yaml_module
            content = config_yaml.read_text(encoding="utf-8")
            data = yaml_module.safe_load(content) or {}
            
            # Ensure platform_toolsets exists
            if "platform_toolsets" not in data:
                data["platform_toolsets"] = {}
            
            # Add qqbot if not present
            if "qqbot" not in data["platform_toolsets"]:
                data["platform_toolsets"]["qqbot"] = ["hermes-qqbot"]
                config_yaml.write_text(
                    yaml_module.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8"
                )
                print(f"  [ok]   Added qqbot to platform_toolsets in config.yaml")
            else:
                print(f"  [skip] qqbot already in platform_toolsets (config.yaml)")
        except Exception as e:
            print(f"  [skip] Failed to update config.yaml: {e}")
    else:
        print(f"  [skip] config.yaml not found (will be created on first run)")

    print("\n=== All patches applied successfully! ===")
    print("\nNext steps:")
    print("  1. Install dependencies:  pip install httpx websockets")
    print("  2. Set env vars:          export QQBOT_APP_ID=xxx QQBOT_CLIENT_SECRET=xxx")
    print("  3. Or add to config.yaml:")
    print("       platforms:")
    print("         qqbot:")
    print("           enabled: true")
    print("           extra:")
    print("             app_id: 'xxx'")
    print("             client_secret: 'xxx'")
    print("  4. Run hermes-agent normally.\n")


if __name__ == "__main__":
    main()
