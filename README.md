# MPVloger

MPVloger is a small [mpv](https://mpv.io/) Lua script that keeps a local watch history. When a file finishes, mpv stops it, or mpv shuts down, the script appends one JSON Lines (JSONL) record for the loaded file. It does not change playback behavior and does not record files that fail to load.

## Requirements

- mpv with Lua scripting support (included in standard mpv builds)
- No additional Lua packages or external programs

## Step-by-step guide for beginners

### Linux and macOS

1. Install mpv if it is not already installed.
   - **Linux:** use your distribution's package manager, for example `sudo apt install mpv` on Debian/Ubuntu or `sudo dnf install mpv` on Fedora.
   - **macOS:** install [Homebrew](https://brew.sh/) if needed, then run `brew install mpv`.
2. Open a terminal.
3. Download the script:

```sh
git clone https://github.com/grifngfn/MPVloger.git
cd MPVloger
```

4. Create mpv's scripts folder:

```sh
mkdir -p ~/.config/mpv/scripts
```

5. Copy the script into the folder:

```sh
cp watch-history.lua ~/.config/mpv/scripts/
```

6. Completely close mpv, then open mpv again.
7. Play a video or audio file for a few seconds and close it.
8. Open `~/.config/mpv/watch-history.jsonl` in a text editor. If it contains a JSON line for the file, the installation worked.

On Linux, you may also use `${XDG_CONFIG_HOME:-$HOME/.config}` instead of `~/.config` if you configured a custom XDG config location.

### Windows

1. Download and install [mpv](https://mpv.io/installation/). Keep the installation folder location in mind.
2. Open this GitHub repository and download `watch-history.lua`:
   - Click **Code**.
   - Click **Download ZIP**.
   - Extract the ZIP to a folder such as `Downloads\MPVloger`.
3. Open File Explorer and press `Win+R`.
4. Enter `%APPDATA%\mpv\scripts` and press Enter.
5. If the `mpv` or `scripts` folder does not exist, create it.
   - You can create a folder with **Right-click → New → Folder**.
6. Copy `watch-history.lua` from the extracted folder into the `scripts` folder.
7. Close every mpv window and open mpv again.
8. Play a file briefly and close mpv or stop playback.
9. Open this path in File Explorer:

```text
%APPDATA%\mpv\watch-history.jsonl
```

If a JSON line for your file appears, the installation worked.

For a portable mpv installation, instead create and use a `scripts` folder inside the `portable_config` folder next to `mpv.exe`.

## Install

You can also install manually using the platform-specific commands below.

### Linux and BSD

```sh
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/mpv/scripts"
cp watch-history.lua "${XDG_CONFIG_HOME:-$HOME/.config}/mpv/scripts/"
```

The script uses mpv's `~~/` config-directory alias, so it also works when your configuration directory is customized.

### macOS

For standard mpv installations:

```sh
mkdir -p ~/.config/mpv/scripts
cp watch-history.lua ~/.config/mpv/scripts/
```

If your mpv build uses a different config directory, put the script in that directory's `scripts` subfolder.

### Windows

For a standard installation, copy `watch-history.lua` into:

```text
%APPDATA%\mpv\scripts\
```

You can enter `%APPDATA%\mpv\scripts` in File Explorer's address bar.

For a portable mpv installation, use the `scripts` folder under `portable_config` next to `mpv.exe`.

### Android and other packaged builds

Use the mpv application's configuration directory and place the file in its `scripts` subfolder. The exact location and whether Lua scripts are supported depend on the mpv app/build. Restart the app after copying the script.

### Get the script

Download `watch-history.lua` from this repository, or clone the repository:

```sh
git clone https://github.com/grifngfn/MPVloger.git
```

## Watch-history file

By default, the log is written to `watch-history.jsonl` in mpv's configuration directory:

- Linux: `~/.config/mpv/watch-history.jsonl`
- macOS: `~/.config/mpv/watch-history.jsonl`
- Windows: `%APPDATA%\mpv\watch-history.jsonl`

Each line is a separate JSON object, so the file can be processed incrementally.

Example record:

```json
{"filename":"Example Movie.mkv","size_bytes":123456789,"opened":"2026-11-24T20:03:11+01:00","closed":"2026-11-24T21:40:00+01:00"}
```

- `filename`: filename only, without its directory path
- `size_bytes`: file size in bytes, or `null` when unavailable
- `opened` and `closed`: local timestamps including the UTC offset

The history is local to your device. The script does not transmit or upload it.

To clear the history, close mpv and delete `watch-history.jsonl` from mpv's configuration directory.

## Troubleshooting

- Confirm the file is named `watch-history.lua` and is in the mpv config directory's `scripts` subfolder.
- Restart mpv after installing or changing the script.
- Check that your mpv build supports Lua scripts.
- If mpv cannot write the history file, check permissions on its configuration directory. mpv reports write errors in its log.
- If you do not see the file, try closing mpv completely and playing a local file—not only a live online stream—before checking again.

## License

MIT. See [LICENSE](LICENSE).
