#!/usr/bin/env python3
"""
cosmic_ask_claude.py

Press the Copilot key -> takes a screenshot with COSMIC's native
cosmic-screenshot, asks what you want to know via a small popup, then
opens a terminal running Claude Code (claude) with that question
already seeded as the first prompt, telling Claude exactly where the
screenshot file is so it can read it itself.

Screenshots are written to /dev/shm (RAM-backed tmpfs), never the actual
disk, and are wiped automatically on reboot. The directory is also
chmod 700'd so other local users on the machine can't read them, and
anything older than 10 minutes is cleaned up on every run.

(Terminal paste only carries text, never image data -- so instead of
copying the screenshot to the clipboard, we just tell Claude its file path.)

Reading the screenshot is auto-approved (--allowedTools=Read) so Claude
doesn't stop to ask permission just to open the file. No other tools are
pre-approved, so edits/commands still prompt as normal. Note the "=" form
of the flag -- "--allowedTools" "Read" as two separate argv entries lets
Claude Code's parser greedily consume the next argument (the seeded
question) as a second tool name, silently dropping the prompt.

cosmic-term is intentionally NOT supported here: it has no CLI flag to
receive a seeded initial command (confirmed via `cosmic-term --help`,
which only exposes --working-directory). alacritty/gnome-terminal are used
instead, both of which support a real "-e"/"--" seeded-command flag.

There's no cosmic-screenshot flag to force an exact output filename
(confirmed via `cosmic-screenshot --help`: only --interactive, --modal,
--notify, -s/--save-dir exist) -- so detecting the new file via a
before/after directory listing is the correct approach, not a workaround.

Requires (install with apt):
    cosmic-screenshot          - COSMIC's native screenshot utility
    xdg-desktop-portal-cosmic  - COSMIC's native screenshot portal backend
    zenity                      - small popup dialog for typing your question
    libnotify-bin                - desktop notifications (notify-send)
    alacritty (or gnome-terminal) - to launch claude in
    Claude Code                  - the CLI itself (see install steps)

    sudo apt install cosmic-screenshot xdg-desktop-portal-cosmic \
        zenity libnotify-bin alacritty
    npm install -g @anthropic-ai/claude-code   # requires Node.js 18+
"""

import fcntl
import os
import shutil
import subprocess
import sys
import time

# When launched via a desktop-shortcut Spawn() (as opposed to an
# interactive shell), PATH additions from ~/.bashrc/~/.zshrc are never
# sourced, so tools like `claude` installed under ~/.npm-global/bin won't
# be found even though they work fine in a normal terminal. Prepend the
# common user-local bin dirs here so this script finds them regardless of
# how it was launched.
for _extra_bin in ("~/.npm-global/bin", "~/.local/bin"):
    _extra_bin = os.path.expanduser(_extra_bin)
    if _extra_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _extra_bin + os.pathsep + os.environ.get("PATH", "")

CLAUDE_COMMAND = "claude"

# /dev/shm is a RAM-backed tmpfs on virtually all Linux systems: screenshots
# written here never touch the physical disk, and everything in it is wiped
# automatically on reboot. Falls back to ~/.cache only if /dev/shm isn't
# available (rare, but possible on some minimal/containerized systems).
SCREENSHOT_DIR = "/dev/shm/copilot-screenshots" if os.path.isdir("/dev/shm") \
    else os.path.expanduser("~/.cache/copilot-screenshots")
MAX_AGE_SECONDS = 60 * 10  # aggressively clean up -- these are screen contents

# Set to True to have cosmic-screenshot prompt for a click-drag region
# selection instead of capturing the full screen automatically. Off by
# default to preserve the original one-key, zero-extra-clicks behavior.
REGION_SELECT = False

# Prevents two overlapping runs if the Copilot key is pressed twice quickly.
LOCK_FILE = "/tmp/copilot-antigravity.lock"

# Small persistent (non-tmpfs) history of recent questions, used to
# populate the zenity dropdown so you don't have to retype similar
# questions. This is just text you typed, not screenshots -- kept in
# ~/.local/share rather than /dev/shm since there's no privacy reason to
# wipe it on reboot the way there is for the images themselves.
HISTORY_FILE = os.path.expanduser("~/.local/share/copilot-antigravity/history.txt")
HISTORY_MAX_ENTRIES = 10

# How long the "what do you want to know?" popup waits before auto-closing
# (treated the same as pressing Cancel) if you walk away.
ZENITY_TIMEOUT_SECONDS = 60


def find_tool(*names):
    for name in names:
        if shutil.which(name):
            return name
    return None


def notify(title: str, message: str, urgency: str = "normal") -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", f"--urgency={urgency}", title, message])
    else:
        print(f"{title}: {message}")


def notify_failure(title: str, message: str) -> None:
    """Like notify(), but also tries to play an audible cue so a failure
    isn't easy to miss if you've already looked away from the screen."""
    notify(title, message, urgency="critical")
    for player, args in (
        ("canberra-gtk-play", ["-i", "dialog-error"]),
        ("paplay", ["/usr/share/sounds/freedesktop/stereo/dialog-error.oga"]),
    ):
        if shutil.which(player):
            subprocess.run([player] + args, capture_output=True)
            break


def acquire_lock():
    """Returns an open file handle holding an exclusive lock, or None if
    another instance is already running (e.g. Copilot key double-pressed)."""
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fp.close()
        return None
    return lock_fp


def cleanup_old_screenshots(directory: str) -> None:
    """Housekeeping: remove screenshots older than MAX_AGE_SECONDS. Never touches today's files."""
    if not os.path.isdir(directory):
        return
    now = time.time()
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and (now - os.path.getmtime(path)) > MAX_AGE_SECONDS:
                os.remove(path)
        except OSError:
            pass


def _run_cosmic_screenshot(out_dir: str) -> set:
    """Runs cosmic-screenshot once and returns the set of newly-created files."""
    before = set(os.listdir(out_dir))
    interactive_flag = "--interactive=true" if REGION_SELECT else "--interactive=false"
    subprocess.run(
        ["cosmic-screenshot", interactive_flag, "--notify=false", "-s", out_dir],
        check=True,
    )
    time.sleep(0.3)  # give the portal a moment to finish writing the file
    after = set(os.listdir(out_dir))
    return after - before


def take_screenshot(out_dir: str) -> str:
    """Runs cosmic-screenshot and returns the path of the saved file.
    Retries once on failure to smooth over transient portal hiccups."""
    if not shutil.which("cosmic-screenshot"):
        notify_failure("Screenshot failed", "cosmic-screenshot not found. Install it with: sudo apt install cosmic-screenshot")
        sys.exit(1)

    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o700)  # /dev/shm is shared across all local users --
                               # keep screenshots readable by you only

    last_error = None
    for attempt in range(2):
        try:
            new_files = _run_cosmic_screenshot(out_dir)
            if new_files:
                return os.path.join(out_dir, new_files.pop())
            last_error = "cosmic-screenshot produced no new file."
        except subprocess.CalledProcessError as e:
            last_error = f"cosmic-screenshot exited with an error: {e}"
        if attempt == 0:
            time.sleep(0.5)  # brief pause before retrying

    notify_failure("Screenshot failed", last_error or "Unknown error after retry.")
    sys.exit(1)


def _load_history() -> list:
    if not os.path.isfile(HISTORY_FILE):
        return []
    with open(HISTORY_FILE) as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def _save_history(question: str) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    history = [question] + [q for q in _load_history() if q != question]
    history = history[:HISTORY_MAX_ENTRIES]
    with open(HISTORY_FILE, "w") as f:
        f.write("\n".join(history) + "\n")


def ask_question() -> str:
    if not shutil.which("zenity"):
        notify_failure("Missing tool", "zenity not found. Install it with: sudo apt install zenity")
        sys.exit(1)

    history = _load_history()

    if history:
        # --list --editable gives a combo box: pick a recent question, or
        # type a brand new one in the same field.
        cmd = [
            "zenity", "--list", "--editable",
            "--title=Ask Claude about your screen",
            "--text=What do you want to know? (pick a recent one or type new)",
            "--width=480", "--height=320",
            f"--timeout={ZENITY_TIMEOUT_SECONDS}",
            "--column=Question",
        ] + history
    else:
        cmd = [
            "zenity", "--entry",
            "--title=Ask Claude about your screen",
            "--text=What do you want to know?",
            "--width=420",
            f"--timeout={ZENITY_TIMEOUT_SECONDS}",
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(0)  # user hit Cancel, or the popup timed out

    question = result.stdout.strip()
    if question:
        _save_history(question)
    return question


def open_terminal_with_claude(seed_prompt: str) -> None:
    if not shutil.which(CLAUDE_COMMAND):
        notify_failure("Claude Code not found", "Install it first: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    terminal = find_tool("alacritty", "gnome-terminal", "x-terminal-emulator", "konsole", "xterm")

    # --add-dir grants Claude's file tools access to the screenshot folder --
    # without this, it may not be able to open the file even though the
    # prompt text mentions its path.
    # --allowedTools=Read auto-approves file reads (so it doesn't stop to
    # ask permission just to open the screenshot) without pre-approving
    # edits or shell commands, which still prompt normally. Must be a
    # single "--allowedTools=Read" token (not two separate argv entries) --
    # otherwise the parser can swallow the next argument (the seeded
    # question) as a second tool name and silently drop the prompt.
    claude_cmd = [
        CLAUDE_COMMAND,
        "--add-dir", SCREENSHOT_DIR,
        "--allowedTools=Read",
        seed_prompt,
    ]

    if terminal == "alacritty":
        subprocess.Popen(["alacritty", "-e"] + claude_cmd)
    elif terminal == "gnome-terminal":
        subprocess.Popen(["gnome-terminal", "--"] + claude_cmd)
    elif terminal == "x-terminal-emulator":
        subprocess.Popen(["x-terminal-emulator", "-e"] + claude_cmd)
    elif terminal == "konsole":
        subprocess.Popen(["konsole", "-e"] + claude_cmd)
    elif terminal == "xterm":
        subprocess.Popen(["xterm", "-e"] + claude_cmd)
    else:
        notify_failure("No terminal emulator found", "Install alacritty: sudo apt install alacritty")
        sys.exit(1)


def main() -> None:
    lock_fp = acquire_lock()
    if lock_fp is None:
        notify("Already running", "A copilot-antigravity request is already in progress.")
        sys.exit(0)

    try:
        cleanup_old_screenshots(SCREENSHOT_DIR)

        screenshot_path = take_screenshot(SCREENSHOT_DIR)
        question = ask_question()

        if not question:
            sys.exit(0)

        seed_prompt = (
            f"I've taken a screenshot of my screen, saved at {screenshot_path}. "
            f"Please look at it and then help with this: {question}"
        )

        open_terminal_with_claude(seed_prompt)
    finally:
        lock_fp.close()  # releases the flock automatically


if __name__ == "__main__":
    main()
