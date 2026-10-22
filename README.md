# FiretvDebloater

Make a slow, ad-heavy Amazon Fire TV faster and quieter. One Python script finds your Fire TV on Wi-Fi, backs it up, asks you what to keep, and turns off Amazon ads, telemetry and preinstalled extras over ADB.

Everything is reversible. Packages are disabled with `pm disable-user`, never uninstalled. Every run writes a restore script before it changes anything.

## What it does

1. Finds ADB on your computer, or offers to download Google's official platform-tools.
2. Scans your Wi-Fi for Fire TVs with ADB debugging on. Skips anything not made by Amazon.
3. Backs up the package list, disabled packages, your installed apps, all settings and device properties.
4. Shows each group and asks before changing it. Features you might use (Amazon Photos, Kids profiles, the Appstore, system updates) get asked one by one and default to "keep".
5. Applies one group at a time. After each group it goes to the home screen, takes a screenshot from the TV, and checks the launcher is running with no error dialog.
6. If a check fails, it undoes that group, then retries the group one package at a time so only the culprit stays on.
7. Writes `restore.bat` (Windows) and `restore.sh` (macOS, Linux) for the exact changes it made.

## Supported devices

Every Fire TV running Fire OS 5, 6, 7 or 8: Fire TV Stick (all generations), Stick Lite, Stick 4K, 4K Max, Fire TV Cube and Fire TV Edition smart TVs. Packages missing on your model are skipped.

Not supported: the Fire TV Stick 4K Select and other Vega OS devices. Vega OS is Linux-based and has no Android package manager. The script detects this and stops.

### Confirmed on real hardware

| Device | Fire OS | Build | Result |
|---|---|---|---|
| Fire TV Stick 4K Max (1st gen) | 7.7.1.3 | PS7713/5443 | All apps, Alexa, Appstore and screensaver work after debloat |

Tested yours? Open an issue or pull request to add a row.

## Requirements

- A Windows, macOS or Linux computer on the same Wi-Fi as the Fire TV.
- Python 3.8 or newer. Windows: install from python.org or run `winget install Python.Python.3.12`.
- ADB. The script downloads it for you if missing.

## Step 1: turn on ADB debugging on the Fire TV

1. Settings > My Fire TV > About. Highlight the device name and press OK 7 times. "You are now a developer" appears.
2. Back to My Fire TV > Developer Options > ADB debugging: ON.
3. Note the IP address under Settings > My Fire TV > About > Network.

## Step 2: run it

```
git clone https://github.com/kunjptl/FiretvDebloater
cd FiretvDebloater
python firetv_debloat.py
```

On macOS and Linux use `python3`. The first connection shows a prompt on the TV. Select "Always allow from this computer" and press OK.

If the scan finds nothing, pass the address: `python firetv_debloat.py --ip 192.168.1.50`

## Commands

| Command | What it does |
|---|---|
| `python firetv_debloat.py` | Scan, back up, ask, debloat |
| `python firetv_debloat.py --dry-run` | Back up and show the plan. Changes nothing |
| `python firetv_debloat.py scan` | List Fire TVs on your network |
| `python firetv_debloat.py status` | Show which catalog packages are on or off |
| `python firetv_debloat.py restore` | Undo the latest session |
| `python firetv_debloat.py restore sessions/<folder>` | Undo a specific session |

Options: `--ip`, `--subnet 10.0.0.0/24`, `--defaults` (accept every default answer), `--settle 5` (seconds to wait on the home screen before each check).

## Undo

- `python firetv_debloat.py restore` finds the TV even if its IP changed, by matching its serial number.
- No Python? Double-click `sessions/<folder>/restore.bat`, or run `sh sessions/<folder>/restore.sh`. Pass a new address as the first argument if the TV's IP changed.
- A factory reset also re-enables everything.

## What gets turned off

The full list with descriptions lives in [catalog.json](catalog.json).

| Group | Default | Examples |
|---|---|---|
| Ads and sponsored content | off | Special Offers, ad ID, promoted rows, autoplay previews, Shop on TV |
| Telemetry and tracking | off | Diagnostic logger, telemetry emitter, crash reporter, Wi-Fi password cloud backup, interest-based ads setting |
| Extras | off | Kindle connect, store demo content, tutorials |
| Features you might use | kept, asked one by one | Amazon Photos, Music, Freevee, Silk, Kids profiles, Alexa notifications, smart home, sideload scanner, system updates, Appstore |
| Undocumented services | kept, asked one by one | Hedwig, audio home, Whisperplay contracts |
| Speed tweak | on | Halves menu animation time |

A protected list in `catalog.json` (launcher, settings, keyboard, setup, parental controls, voice, WebView) can never be touched. The script refuses to run if the catalog lists one of them.

## Session folder

Each run creates `sessions/<date>-<model>/`:

- `backup/`: package lists, settings and device properties before any change
- `screens/`: PNG screenshots from the TV before, after each step and after any rollback
- `state.json`: every change with its previous value
- `restore.bat`, `restore.sh`: undo scripts, updated before each change
- `log.txt`: the full run, including your answers

## Known limits

- Confirmed models are listed below. Please report results from other models.
- Some Fire OS builds refuse to disable certain packages. The script reports these and moves on.
- A major Fire OS update can re-enable some packages. Run the script again.
- Disabling system updates also stops security fixes. The script keeps updates on unless you say otherwise.

## Contributing

Found a package on your model that breaks something, or one worth adding? Open an issue with your model code (`status` prints it), Fire OS version and the package name. The script prints both when a check fails.

Use at your own risk. Not affiliated with Amazon.
