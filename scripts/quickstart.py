import getpass
import secrets
import shutil
from pathlib import Path
from textwrap import fill

import bcrypt
from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_PATH = PROJECT_ROOT / "config.toml"
BCRYPT_MAX_PASSWORD_BYTES = 72


def copy_example(destination: Path) -> None:
    if destination.exists():
        print(f"Keeping existing {destination.name}")
        return

    example = destination.with_name(f"{destination.name}.example")
    if destination.name == "config.toml":
        example = destination.with_name("config.example.toml")
    shutil.copyfile(example, destination)
    print(f"Created {destination.name} from {example.name}")


def prompt_for_admin_password() -> str:
    while True:
        password = getpass.getpass("Admin password: ")
        if not password:
            print("The admin password cannot be empty.")
            continue

        encoded_password = password.encode("utf-8")
        if len(encoded_password) > BCRYPT_MAX_PASSWORD_BYTES:
            print(
                "The admin password cannot exceed 72 bytes when UTF-8 encoded."
            )
            continue

        confirmation = getpass.getpass("Confirm admin password: ")
        if password != confirmation:
            print("The passwords do not match. Try again.")
            continue

        return bcrypt.hashpw(encoded_password, bcrypt.gensalt()).decode()


def update_env(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    remaining_updates = updates.copy()
    updated_lines = []

    for line in lines:
        key, separator, _ = line.partition("=")
        if separator and key in updates:
            newline = "\n" if line.endswith("\n") else ""
            updated_lines.append(f"{key}={updates[key]}{newline}")
            remaining_updates.pop(key, None)
        else:
            updated_lines.append(line)

    if remaining_updates:
        if updated_lines and not updated_lines[-1].endswith("\n"):
            updated_lines[-1] += "\n"
        updated_lines.extend(
            f"{key}={value}\n" for key, value in remaining_updates.items()
        )

    ENV_PATH.write_text("".join(updated_lines))


def fill_local_secrets() -> None:
    values = dotenv_values(ENV_PATH)
    updates = {}

    if not (values.get("MOGAO_SESSION_SECRET") or "").strip():
        updates["MOGAO_SESSION_SECRET"] = secrets.token_hex(32)

    if not (values.get("MOGAO_ADMIN_HASH") or "").strip():
        updates["MOGAO_ADMIN_HASH"] = prompt_for_admin_password()

    if not updates:
        print("Keeping existing local secrets in .env")
        return

    update_env(updates)
    print("Filled local secrets in .env")


def print_next_steps() -> None:
    actions = []
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        actions.append(
            "Install FFmpeg/ffprobe from https://ffmpeg.org/download.html, or "
            "disable video and audio post-processing in config.toml."
        )

    actions.extend(
        [
            "Add the API keys needed by enabled features to .env, or disable "
            "those features in config.toml.",
            "Install Anki (https://apps.ankiweb.net/) and AnkiConnect "
            "(https://ankiweb.net/shared/info/2055492159), then update the Anki "
            "deck, model, and field names in config.toml. Alternatively, set "
            "anki.enabled, postprocessing.text.enabled, and "
            "postprocessing.audio.enabled to false.",
            "Review the remaining paths and feature settings in config.toml.",
            "Start Mogao with:\nuv run server.py",
        ]
    )

    print("Initial setup is complete. Finish these steps before starting Mogao:\n")
    for number, action in enumerate(actions, start=1):
        first_line, *remaining_lines = action.splitlines()
        print(
            fill(
                first_line,
                width=80,
                initial_indent=f"{number}. ",
                subsequent_indent="   ",
            )
        )
        for line in remaining_lines:
            print(
                fill(
                    line,
                    width=80,
                    initial_indent="   ",
                    subsequent_indent="   ",
                )
            )
    print("\nSee README.md for the full configuration and dictionary instructions.")


def main() -> None:
    copy_example(ENV_PATH)
    copy_example(CONFIG_PATH)
    fill_local_secrets()
    print_next_steps()


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        raise SystemExit(1)
