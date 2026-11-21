# flyscribe

Convert DJI flight records, the `FlightRecord_*.txt` files that DJI Fly writes on the controller or
phone, to CSV, JSON, KML, GPX or GeoJSON.

Flight records in log format 13 and 14 are encrypted, and their keys can only be unlocked through DJI's
key service. flyscribe asks that service once per log, keeps the unlocked keys on your disk and works
offline from then on. It can also write an export without your serial numbers, or an anonymized one
that leaves out who flew and where.

Not affiliated with or endorsed by DJI.

## Status

- Developed with, and checked against, logs written by DJI Fly 1.21 (log format 14) for a DJI Mini 5 Pro
  flown with a DJI RC 2.
- Other aircraft, other app versions and the older "13.1" key grouping follow DJI's published parsing
  library but have not been tried on real logs.
- Log formats 12 and older are not supported;
  [dji-log-parser](https://github.com/lvauvillier/dji-log-parser) reads those.
- Developed and run on Linux. There is no automated test suite yet.

## Requirements

- [uv](https://docs.astral.sh/uv/). It fetches what else is needed: the
  [cryptography](https://pypi.org/project/cryptography/) package and, if yours is older than 3.10, a
  Python. Without uv, Python 3.10 or newer with pip will do; see
  [Other ways to run it](#other-ways-to-run-it).
- A DJI developer API key (free), used the first time each log is converted

## Quick start

In a copy of this repository:

```sh
cp /path/to/FlightRecord_*.txt logs/
export DJI_API_KEY=your-app-key        # only for logs that were not converted before
uv run flyscribe logs/*.txt            # writes exports/<log>.csv
```

The first `uv run` creates a `.venv/` folder here, with the package versions recorded in `uv.lock`.

### Other ways to run it

- `uv tool install .` installs a `flyscribe` command for use in any folder. Leave out `uv run` in the
  examples below.
- `pip install .` in a virtual environment does the same without uv.
- `uvx --from git+https://github.com/gibbsoft/flyscribe flyscribe`, in place of `uv run flyscribe`, runs
  it without a copy of the repository.

### Where the flight records are

DJI Fly keeps them on the device it runs on. On a DJI RC or RC 2 they are in
`Android/data/dji.go.v5/files/FlightRecord/` on the controller's internal storage, which a computer
can reach over USB
([AirData's guide](https://app.airdata.com/wiki/Help/DJI+RC%2C+RC+2%3A+Uploads+for+US+Users) has
pictures). The files are named `FlightRecord_<date>_[<time>].txt` or
`DJIFlightRecord_<date>_[<time>].txt`.

### Getting a DJI API key

1. Sign in at <https://developer.dji.com/user> and choose **CREATE APP**.
2. Select the app type **Open API**, fill in a name, category and description, and create the app.
3. Activate it from the e-mail DJI sends you.
4. Open the app on your developer page and copy its **App Key**.

Give the key to flyscribe with `--api-key` or in the `DJI_API_KEY` environment variable. It is sent to
DJI once per log, and flyscribe never writes it to disk.

## Usage

```sh
uv run flyscribe logs/*.txt             # exports/<log>.csv for every log
uv run flyscribe -f gpx logs/*.txt      # GPX tracks
uv run flyscribe --anon logs/*.txt      # exports/<log>.anon.csv, which can be passed on
uv run flyscribe --no-ids logs/*.txt    # exports/<log>.no-ids.csv: the real track, no serial numbers
uv run flyscribe --info logs/*.txt      # flight summaries as JSON; needs no key
uv run flyscribe -f kml -o exports/track.kml 'logs/FlightRecord_2025-06-01_[10-30-00].txt'
```

| Option | Meaning |
|---|---|
| `-f`, `--format` | `csv` (default), `json`, `kml`, `gpx` or `geojson` |
| `-o`, `--output FILE` | output file for a single log; `-` writes to standard output |
| `--exports DIR` | folder the output files go to (default `exports`) |
| `--keychains DIR` | folder the unlocked keys are kept in (default `keychains`) |
| `--api-key KEY` | DJI API key (default `$DJI_API_KEY`) |
| `--info` | print the unencrypted flight summary as JSON and stop |
| `--anon` | leave out what says whose flight it was or where |
| `--no-ids` | leave out what says whose aircraft it was; positions stay as recorded |

- Quote a file name that you type out: its square brackets are special to the shell.
- `logs/*.txt` is expanded by the shell. Windows' `cmd.exe` and PowerShell do not do that, so name the
  files there, or use WSL or Git Bash.

### Folders

| Folder | Holds |
|---|---|
| `logs/` | flight records; only a convention, any path can be given |
| `keychains/` | unlocked keys, one `<log>.keychains.json` per log |
| `exports/` | output: `<log>.<format>`; `<log>.anon.<format>` with `--anon`, `<log>.no-ids.<format>` with `--no-ids` |

`keychains/` and `exports/` are looked for in the folder flyscribe is run from and are created when
needed. All three folders hold private data; see [Privacy](#privacy).

## Output

### CSV

One row per sample the controller received, ten a second, in about 120 columns. A cell stays empty until
the log has reported that value. Units are metres, m/s, degrees, volts, amperes, mAh and °C.

| Columns | Content |
|---|---|
| `CUSTOM.dateTime` | UTC time on the controller's clock when the row was logged |
| `OSD.*` | flight controller: timer, position, height, speeds (x north, y east, z down), attitude, flight mode, GPS and status flags |
| `GIMBAL.*` | gimbal attitude, mode and end stops |
| `CAMERA.*` | photo and recording flags, SD-card state |
| `RC.*` | stick positions, uplink and downlink signal |
| `BATTERY.*` | charge, voltage, current, capacity, cell voltages, temperature |
| `HOME.*` | home point, return-to-home settings, height limit |
| `RECOVER.*` | app version, aircraft name, serial numbers |
| `APP.tip`, `APP.warn` | messages from the app, flight mode changes, gimbal end stops |
| `DETAILS.*` | the app's own summary of the flight, the same on every row |

Before analysing it:

- `CUSTOM.dateTime` comes from the controller's or phone's clock, not from GPS, and it is the time the
  row was logged. When the link or the app stalls, that is up to a few seconds after the sample.
- `OSD.flyTime` is the flight controller's own timer in steps of 0.1 s. A jump means samples were lost on
  the radio link; the log is written on the controller, so they are gone. The timer can carry on from
  the previous flight instead of starting at 0.
- `OSD.height` is the barometric height above the take-off point. `OSD.altitude` adds the home point's
  barometric altitude and is not a height above sea level.
- `DETAILS.*` is the log header as the app wrote it. DJI Fly 1.21 gives `totalDistance` in kilometres and
  leaves `photoNum` at 0; `CAMERA.isPhoto` and `CAMERA.isVideo` are the reliable source.

### JSON

The header summary under `details` and every record of the log, in file order, under `records`. Each
record has its `type` and its decoded fields; types flyscribe does not decode carry their payload as hex
under `raw`.

### KML, GPX and GeoJSON

The flight track. Heights are barometric heights above the take-off point. GPX and GeoJSON also carry
the time of each point.

## Anonymized exports

`--anon` is for an export you want to pass on, for example to ask for help with a flight.

- Serial numbers (aircraft, camera, controller, battery), the aircraft's name, the flight's ID and the
  address fields are left out.
- Positions are moved as if the globe had been turned to put the first GPS fix at 0°N 0°E. The track
  keeps its shape, size and orientation, so distances and bearings still work.
- In the JSON export, payloads that flyscribe cannot decode are withheld, since nobody knows what is in
  them.
- Dates and times, heights, speeds, battery and signal data and the app's messages are kept.

What it does not do:

- It does not hide a flight from someone who has the original log, or who tracked the flight some other
  way: the times and the shape of the track still match.
- Exports from the same aircraft can be matched to each other, for example by the battery's cycle count
  or the flight counter.
- App messages are kept word for word. None seen so far names a person or a place, but flyscribe cannot
  vouch for every message the app can produce.
- Distances are kept on a sphere. Software that measures on the WGS84 ellipsoid will see them change by
  up to 1%.

### Leaving out the serial numbers only

`--no-ids` is for an export that has to keep the real track, for example to set a flight against
measurements made on the ground.

- Serial numbers (aircraft, camera, controller, battery), the aircraft's name and the flight's ID are
  left out.
- Positions, the address fields and all other flight data stay as recorded, so the export says where
  and when the flight took place.
- In the JSON export, a payload that flyscribe cannot decode is withheld only if one of the left-out
  values occurs in it as text. What else such a payload holds is not known.

## Privacy

Flight records are personal data: they say where and when you fly and carry the serial numbers of your
equipment.

- **What DJI receives.** To unlock a log, flyscribe sends DJI the wrapped keys stored in that log, about
  4 kB, together with your API key. The flight data is not sent, and decryption happens on your machine.
  DJI can see that your developer account unlocked a log, and when.
- **Key files are secrets.** A file from `keychains/` together with its log can be read by anyone,
  without DJI.
- **Raw logs are not safe to share.** DJI's key service does not appear to check who is asking, so
  treat a raw log as readable by whoever gets hold of it. Pass on `--anon` exports instead, or
  `--no-ids` exports where the real position is needed.
- **Git ignores the data.** `.gitignore` keeps the contents of `logs/`, `keychains/` and `exports/`, and
  any `FlightRecord_*` file, out of the repository. An export that you name yourself and write somewhere
  else is not covered.

## How it works

A flight record is a 100-byte prefix, a scrambled summary of the flight and a stream of records. Each
record is XOR-scrambled and, underneath, AES-256-CBC encrypted. Record types are grouped into "feature
points", each with its own key, and every record continues the cipher stream of the previous record in
its group. The keys are in the log, but wrapped with a key only DJI holds, which is why DJI's service
is needed once per log.

All of it is in one module, `flyscribe.py`; its docstring and comments describe the layout in detail.

## Differences from dji-log-parser

The CSV uses the column layout of [dji-log-parser](https://github.com/lvauvillier/dji-log-parser), so
analysis scripts written for it carry over. Where the two disagree (checked against its version 0.5.7),
it is on purpose:

- Record types 57, 253 and 254 are kept on their key chains. Leaving them out corrupts the record that
  follows, which shows as `RC.downlinkSignal` reading 0 and as wrong timestamps.
- The battery's cell count and cell voltages come from the log, not from a table of known aircraft.
- A cell is empty, not 0, until its value has been reported, and an invalid position is empty.
- `CUSTOM.dateTime`, the sticks and the link signal take the first value logged after each sample, so a
  radio dropout does not stamp the last row before it with the time the link came back.
- `APP.warn` includes DJI Fly's own warnings with their codes.
- `CAMERA.isPhoto` is set once per shot.

dji-log-parser reads older log formats and extracts the snapshot images, which flyscribe does not.

## Limitations

- Only the flight records written by the app are read, not the aircraft's own `.DAT` logs.
- New logs need DJI's key service. Logs whose keys are already in `keychains/` keep working without it.
- Some record types are not decoded and appear only as hex in the JSON export.
- Snapshot images stored in a log are ignored.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please never attach a raw flight record or a key file to a
public issue.

## Credits

flyscribe builds on the published work of others: DJI's
[FlightRecordParsingLib](https://github.com/dji-sdk/FlightRecordParsingLib),
[dji-log-parser](https://github.com/lvauvillier/dji-log-parser) and
[pydjirecord](https://github.com/rembish/pydjirecord). Record layouts, names and the CSV column layout
follow them; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

[MIT](LICENSE). DJI is a trademark of SZ DJI Technology Co., Ltd.
