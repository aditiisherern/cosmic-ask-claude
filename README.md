# cosmic-ask-claude

Press a key that will screenshot your screen and allow you to ask Claude
Code about it, instantly, from anywhere on COSMIC/Linux.

Repurpose your keyboard's Copilot key (or any key combo) to take a
screenshot, ask a question about it via a small popup, and hand both
straight to [Claude Code](https://github.com/anthropics/claude-code) —
no manual screenshotting, no clipboard juggling.

Press the key → screenshot is captured → a popup asks what you want to know
→ a terminal opens with Claude Code already reading the screenshot and
answering your question.

## How it works

1. `cosmic-screenshot` captures your screen to a RAM-backed directory
   (`/dev/shm`) — screenshots never touch your physical disk and are wiped
   automatically on reboot.
2. `zenity` pops up a small text box asking what you want to know (with a
   dropdown of your last 10 questions after the first run).
3. A terminal (Alacritty, falling back to gnome-terminal/xterm/konsole)
   opens running `claude`, seeded with your question and the screenshot's
   file path so it can read the image itself.

Terminal paste only carries text, never image data — so instead of copying
the screenshot to the clipboard, the script just tells Claude where to find
it on disk (`--add-dir` + `--allowedTools=Read` so it can open the file
without an extra permission prompt).

## Requirements

- A COSMIC desktop session (for `cosmic-screenshot`) — this script is
  COSMIC-specific; adapting it to GNOME/KDE would mean swapping the
  screenshot command for `gnome-screenshot` or `spectacle`.
- [Claude Code](https://github.com/anthropics/claude-code), installed
  **without sudo** so it isn't root-owned:
  ```bash
  mkdir -p ~/.npm-global
  npm config set prefix ~/.npm-global
  echo 'export PATH=~/.npm-global/bin:$PATH' >> ~/.bashrc
  source ~/.bashrc
  npm install -g @anthropic-ai/claude-code
  claude auth login
  ```
  A Claude Pro/Max/Team/Enterprise plan or a Console (API) account both
  work — check with `claude auth status`.
- System packages:
  ```bash
  sudo apt install cosmic-screenshot xdg-desktop-portal-cosmic \
      zenity libnotify-bin alacritty
  ```

## Setup

1. Clone this repo (or just download `cosmic_ask_claude.py`) somewhere
   convenient, e.g. `~/scripts/cosmic_ask_claude.py`.
2. Bind it to a key in COSMIC. If your key doesn't register a shortcut
   normally (e.g. a laptop's dedicated Copilot key), remap it first with
   [`keyd`](https://github.com/rvaiya/keyd) to something COSMIC does
   recognize, like `Super+Alt+C`.
3. Add a custom shortcut in COSMIC Settings → Keyboard → Custom Shortcuts,
   or edit
   `~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom`
   directly:
   ```ron
   {
       (
           modifiers: [Super, Alt],
           key: "c",
       ): Spawn("python3 /home/YOUR_USERNAME/scripts/cosmic_ask_claude.py"),
   }
   ```
4. Press the key and try it out.

## Configuration

A few constants near the top of the script:

| Constant | Default | What it does |
|---|---|---|
| `REGION_SELECT` | `False` | Set `True` for a click-drag region capture instead of full-screen |
| `MAX_AGE_SECONDS` | 10 minutes | How long screenshots are kept before cleanup |
| `HISTORY_MAX_ENTRIES` | 10 | How many past questions are remembered |
| `ZENITY_TIMEOUT_SECONDS` | 60 | How long the question popup waits before auto-cancelling |

## Privacy notes

- Screenshots live in `/dev/shm` (RAM only), `chmod 700`'d, and are deleted
  after 10 minutes or on reboot — whichever comes first.
- Your typed questions are kept in a small plain-text history file at
  `~/.local/share/copilot-antigravity/history.txt` so the popup can offer
  recent questions as a dropdown. Delete that file anytime to clear it.
- Claude Code only gets `Read` access pre-approved for the screenshot
  directory — it will still prompt you before editing files or running
  commands.

## Known limitations

- `cosmic-term` isn't supported as a launch target — as of writing it has
  no CLI flag to receive a seeded initial command (only
  `--working-directory`). The script uses Alacritty or gnome-terminal
  instead, both of which support `-e`/`--` for this.
- The very first time Claude Code runs in a given working directory, it
  shows a one-time "do you trust this folder?" prompt. Accept it once and
  it's remembered for future runs.
