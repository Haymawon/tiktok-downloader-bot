# Before you start

- **Python 3.10 or later.** The code uses `X | None` type hints and `asyncio` features that are not available in older versions.
- **ffmpeg on your PATH.** Without it, videos will have no audio and the mp3 step is skipped. This is the most common reason a fresh install fails.
- **A Telegram bot token.** Get one from [@BotFather](https://t.me/BotFather): send `/newbot`, follow the prompts, and copy the token.

## Install

### Arch and Arch-based

This includes Manjaro, EndeavourOS, CachyOS, Garuda, and other Arch-based distributions.

```bash
sudo pacman -S python python-pip ffmpeg
```

Then set up the project. I recommend a virtualenv, mostly so `pacman -Syu` doesn't accidentally break your bot's dependencies when Python gets bumped:

```bash
git clone https://github.com/Haymawon/tiktok-downloader-bot
cd tiktok-downloader-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
### If you are using Fish shell
```bash
source .venv/bin/activate.fish
```
### Alternative 1
```bash
. .venv/bin/activate.fish
```
### Alternative 2 (if you just want python/pip from the venv without sourcing script)
```bash
fish_add_path .venv/bin
```
### Debian / Ubuntu / Mint / Pop!_OS

```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv ffmpeg
```

### Fedora / RHEL / Rocky

```bash
sudo dnf install python3 python3-pip ffmpeg
```

On stock Fedora you may need RPM Fusion for ffmpeg. If `dnf install ffmpeg` fails, enable it first:

```bash
sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf install ffmpeg
```

### openSUSE

```bash
sudo zypper install python3 python3-pip ffmpeg
```

Then, on any of these:

```bash
git clone https://github.com/Haymawon/tiktok-downloader-bot
cd tiktok-downloader-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## macOS

You need Homebrew. If you don't have it, install it first from [brew.sh](https://brew.sh/) — it's a one-liner on that page.

```bash
brew install python ffmpeg
```

macOS ships a Python 3, but it's the system one and you shouldn't install packages into it. Homebrew's Python formula gives you a clean `python3` to work with.

```bash
git clone https://github.com/Haymawon/tiktok-downloader-bot
cd tiktok-downloader-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Windows

Two ways, pick one.

### Option A: winget

Windows 10/11, easiest:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

Close and reopen your terminal afterwards so PATH updates. Then, in the project folder:

```powershell
git clone https://github.com/Haymawon/tiktok-downloader-bot
cd tiktok-downloader-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Option B: manual

Install Python from [python.org](https://www.python.org/). During setup, tick **"Add Python to PATH"** — this trips people up more than anything else.

Install ffmpeg: download a build from [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/), extract it somewhere like `C:\ffmpeg`, and add `C:\ffmpeg\bin` to your system PATH.

Open PowerShell in the project folder and run the git clone / venv / pip install block from Option A.

Verify ffmpeg is reachable before you run the bot:

```powershell
ffmpeg -version
```

If that errors, the bot won't be able to extract audio.

## Configure

Create a file called `.env` in the project root:

```env
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
```

Paste your token from BotFather.

If you forget this step, the bot exits immediately with `BOT_TOKEN is missing from .env / environment`. That's intentional; a bot without a token has nothing to do.
