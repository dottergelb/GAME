from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import dotenv_values


REQUIRED_BY_MODE = {
    "backend": ("BOT_TOKEN", "DATABASE_URL", "CORS_ORIGINS"),
    "bot": ("BOT_TOKEN", "LEADERBOARD_URL"),
    "ocr": ("OPENAI_API_KEY",),
}

PLACEHOLDER_PREFIXES = (
    "PASTE_",
    "https://your-",
)


def is_placeholder(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate required values in .env")
    parser.add_argument(
        "--mode",
        action="append",
        choices=sorted(REQUIRED_BY_MODE.keys()),
        help="Validation mode. Can be provided multiple times.",
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"ERROR: {env_path} not found")
        return 1

    values = dotenv_values(env_path)
    modes = args.mode or ["backend", "bot"]

    missing: list[str] = []
    placeholders: list[str] = []
    for mode in modes:
        for key in REQUIRED_BY_MODE[mode]:
            value = (values.get(key) or "").strip()
            if not value:
                missing.append(key)
            elif is_placeholder(value):
                placeholders.append(key)

    if missing:
        print("ERROR: Missing env values:")
        for key in sorted(set(missing)):
            print(f"  - {key}")
        return 1

    if placeholders:
        print("ERROR: Placeholder env values detected:")
        for key in sorted(set(placeholders)):
            print(f"  - {key}")
        return 1

    print(f"OK: {env_path} is valid for modes: {', '.join(modes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
