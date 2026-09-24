import getpass
import secrets
import shutil
import tomllib
from pathlib import Path
from textwrap import fill

import bcrypt
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
CONFIG_PATH = PROJECT_ROOT / "config.toml"
BCRYPT_MAX_PASSWORD_BYTES = 72
AI_FEATURE_SECTIONS = (
    "postprocessing.text",
    "postprocessing.audio",
    "dictionary_generation",
)


def copy_example(destination: Path) -> None:
    if destination.exists():
        print(f"Keeping existing {destination.name}")
        return

    example = destination.with_name(f"{destination.name}.example")
    shutil.copyfile(example, destination)
    print(f"Created {destination.name} from {example.name}")


def prompt_for_ai_features() -> bool:
    while True:
        answer = input(
            "Enable AI features (dictionary generation, text and audio "
            "postprocessing)? [Y/n] "
        ).strip().lower()
        if answer in {"", "y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def create_config() -> None:
    if CONFIG_PATH.exists():
        print(f"Keeping existing {CONFIG_PATH.name}")
        return

    enable_ai = prompt_for_ai_features()
    example = CONFIG_PATH.with_name("config.example.toml")
    content = example.read_text()
    if not enable_ai:
        for section in AI_FEATURE_SECTIONS:
            enabled_setting = f"[{section}]\nenabled = true"
            if content.count(enabled_setting) != 1:
                raise RuntimeError(f"Could not find enabled setting for {section}")
            content = content.replace(
                enabled_setting, f"[{section}]\nenabled = false", 1
            )

    CONFIG_PATH.write_text(content)
    print(f"Created {CONFIG_PATH.name} from {example.name}")


def ai_features_enabled() -> bool:
    with CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)
    return any(
        config["postprocessing"][feature]["enabled"]
        for feature in ("text", "audio")
    ) or config["dictionary_generation"]["enabled"]


def prompt_for_admin_password() -> str:
    while True:
        password = getpass.getpass("Admin password: ")
        if not password:
            print("The admin password cannot be empty.")
            continue

        encoded_password = password.encode("utf-8")
        if len(encoded_password) > BCRYPT_MAX_PASSWORD_BYTES:
            print("The admin password cannot exceed 72 bytes when UTF-8 encoded.")
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

    if ai_features_enabled():
        actions.append(
            "Add the API keys needed by enabled features to .env, or disable "
            "those features in config.toml."
        )

    actions.extend(
        [
            "Install Anki (https://apps.ankiweb.net/) and AnkiConnect "
            "(https://ankiweb.net/shared/info/2055492159), "
            "configure your preferred deck settings in config.toml "
            "and then keep Anki open "
            "when starting Mogao. If the configured deck or note type is "
            "missing, Mogao will offer to create it. Alternatively, set "
            "anki.enabled to false and disable any enabled postprocessing "
            "features in config.toml.",
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
    create_config()
    fill_local_secrets()
    print_next_steps()


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        raise SystemExit(1)
