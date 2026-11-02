# SignalScope

**See every layer of a live video feed.**

SignalScope is a live feed inspector for video and broadcast engineers. Point it at a live feed (SRT, HLS
or UDP) and within about half a minute it tells you everything about that feed: how healthy the network link
is, how the stream is packed, the exact video and audio settings, whether captions and ad markers are present,
and whether the picture or sound has problems. It can also check the feed against a partner's agreed
delivery spec and give a pass/fail report.

It was built for one of the most common jobs in streaming and partner engineering: **onboarding a new
channel and answering "is this feed right, and if not, whose side is the problem on?"**

---

## Why it exists

When a media partner sends a live channel to a streaming platform, a lot has to be right at once: the SRT
connection and its encryption, the transport stream structure, the codec, resolution and frame rate, keyframe
spacing, audio tracks and languages, captions, and the SCTE-35 markers that drive ad breaks and blackouts.
Checking all of that usually means several command-line tools and a lot of reading. SignalScope runs them for
you and puts the answers on one page, in plain language, with a green/yellow/red rating per layer.

## What it checks

| Layer | What SignalScope reports |
|---|---|
| **SRT link** | Mode (listener or caller), latency, encryption and key length, stream ID, round-trip time, bitrate, estimated link capacity, packets received, lost, retransmitted and dropped, with live charts |
| **Transport stream (MPEG-TS)** | Total bitrate, services and providers, every PID with its contents and bitrate, PMT/PCR PIDs, continuity errors, transport errors, lost sync, and PCR clock accuracy |
| **Video** | Codec, profile and level, resolution and aspect ratio, frame rate, progressive or interlaced, keyframe interval and regularity, bit depth, HDR or SDR, color, B-frames |
| **Audio** | Every track: codec, channels, sample rate, bitrate and language, plus integrated loudness (LUFS), loudness range and true peak |
| **Captions** | CEA-608/708 captions and subtitle streams |
| **SCTE-35** | The marker PID and every splice command decoded: event ID, ad break OUT or back IN, duration, auto-return, timing, segmentation descriptors, and ad breaks that never returned |
| **Picture and sound** | Black video, frozen video and silent audio, with timestamps; a thumbnail and a filmstrip (one frame every 3 seconds) |
| **Spec conformance** | Optional pass/fail table against an expected spec: codec, resolution, frame rate, scan, keyframe gap, max bitrate, audio codec, languages, captions, SCTE-35, encryption and max packet loss |

Every inspection is saved in a history, and you can download the full JSON report and the recorded sample.

## How it works

```
 partner encoder ── SRT / HLS / UDP ──►  SignalScope
                                            │  1. records a short sample (5–120 s)
                                            │  2. analyzes it with proven open-source tools:
                                            │       libsrt stats   → link health
                                            │       TSDuck         → transport stream, PCR, SCTE-35
                                            │       ffprobe/ffmpeg → codecs, loudness, black/freeze/silence
                                            ▼  3. combines everything into one report + health rating
                                      web page · JSON report · sample.ts
```

SignalScope is a thin, readable layer on top of [ffmpeg](https://ffmpeg.org/),
[SRT](https://github.com/Haivision/srt) and [TSDuck](https://tsduck.io/). Those tools do the deep analysis;
SignalScope runs them, interprets their output and makes it easy to understand.

## Getting started

### Requirements

- Linux (tested on **Ubuntu 24.04**) with Python 3.10 or newer
- **ffmpeg** with SRT support, **srt-tools** (for `srt-live-transmit`), and **TSDuck**

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg srt-tools python3-venv jq
# TSDuck is not in Ubuntu's package list; install the .deb for your Ubuntu version from
# https://github.com/tsduck/tsduck/releases, for example:
#   sudo apt-get install -y ./tsduck_<version>.ubuntu24_amd64.deb
```

### Run it

```bash
git clone https://github.com/slmnkhn/signalscope.git
cd signalscope
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn signalscope.web:app --host 0.0.0.0 --port 8080
```

Open `http://<your-server>:8080`. Reports are stored in `~/lab/reports` (set `SIGNALSCOPE_DATA` to change it).

To run it as a service, see [`deploy/signalscope.service`](deploy/signalscope.service) and
[`deploy/deploy.sh`](deploy/deploy.sh) (set `SIGNALSCOPE_HOST` and `SIGNALSCOPE_KEY`, or put them in `deploy/.env.local`, which git ignores).

### Network ports

| Port | Protocol | Used for |
|---|---|---|
| 8080 | TCP | The web interface |
| 9000 | UDP | Receiving SRT feeds in listener mode |

## Using it

### Inspect a feed

1. Choose the source:
   - **Listen for a sender:** the partner's encoder sends to `srt://<your-server>:9000` in caller mode.
   - **Pull from an SRT address:** SignalScope connects out to an encoder running as an SRT listener.
   - **Other URL:** an HLS playlist (`.m3u8`), UDP/RTP multicast, or a full `srt://` URL.
2. Set the sample length and SRT latency. If the feed is encrypted, enter the **passphrase** and **key length**
   (AES-128/192/256), and the **stream ID** if the sender needs one.
3. Optionally, give the source a name and notes (partner, encoder, contact), and fill in the **expected spec**.
4. Click **Inspect**. The page shows each stage, then the full report.

### Try it with no partner: the test signal

The **Test signal** panel plays the partner's role. It sends a feed to SignalScope's own listener and inspects
it, end to end. You can choose:

- codec (H.264, HEVC, MPEG-2), size (480p, 720p, 1080p, 1080i), frame rate, bitrate and keyframe interval
- audio codec (AAC, AC-3, E-AC-3) and a second audio language
- a real **SCTE-35 ad break** (OUT, then back IN) of any length
- **encryption** with a passphrase
- deliberate faults: **black video** and **silent audio**, to see the checks catch them

It's the quickest way to see every part of the report working.

### Send a test feed from your own machine

```bash
ffmpeg -re -f lavfi -i testsrc2=size=1920x1080:rate=30000/1001 -f lavfi -i sine=frequency=1000 \
  -c:v libx264 -profile:v high -g 60 -b:v 8M -c:a aac -b:a 192k -f mpegts \
  "srt://<your-server>:9000?mode=caller&latency=1000000"
```

(ffmpeg's SRT `latency` is in microseconds.) Start an inspection in **Listen** mode first, then run this.

## API

Everything on the page is also available as JSON.

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/inspect` | Start an inspection. Body: `kind` (`listen`, `srt`, `url`), `host`, `port`, `url`, `seconds`, `latency_ms`, `passphrase`, `key_length`, `stream_id`, `label`, `notes`, `spec` |
| `POST` | `/api/test-signal` | Send the built-in test signal and inspect it |
| `GET` | `/api/jobs` | List past inspections |
| `GET` | `/api/jobs/{id}` | The full report, including status while it runs |
| `DELETE` | `/api/jobs/{id}` | Delete an inspection and its files |
| `GET` | `/api/jobs/{id}/report.json` | Download the report |
| `GET` | `/api/jobs/{id}/sample.ts` | Download the recorded sample |
| `GET` | `/api/jobs/{id}/thumb.jpg`, `/film/{n}.jpg` | Thumbnail and filmstrip frames |

Example:

```bash
curl -X POST http://<your-server>:8080/api/inspect -H 'Content-Type: application/json' \
  -d '{"kind":"listen","seconds":20,"label":"Partner Sports 1, primary",
       "spec":{"video_codec":"H264","resolution":"1920x1080","fps":29.97,"scte35_required":true}}'
```

## Security notes

- **There is no login yet.** Restrict the web port and the SRT port to trusted IP addresses with your firewall,
  or put SignalScope behind a VPN or an authenticating proxy.
- SRT passphrases are used only for the connection and are **never written to the report**; the report records
  only whether the feed was encrypted and the key length.
- Only analyze feeds you own or are authorized to monitor.

## Roadmap

- Live monitoring mode with alerts (packet loss, missing captions or markers, black, freeze, silence)
- Primary vs backup comparison
- Full ETSI TR 101 290 priority 1–3 checks
- Captions in the test signal
- Login and HTTPS out of the box
- Plain-English incident summaries for partners

## Project layout

```
signalscope/
  inspector.py   records a sample and analyzes every layer
  generator.py   the built-in test signal (ffmpeg + TSDuck, SCTE-35 injection, SRT out)
  web.py         FastAPI app: API + the web page
  static/        the single-page interface
deploy/          systemd service and deploy script
```

## Author

Built by **Sal Khan**, engineering leader in live video streaming.
