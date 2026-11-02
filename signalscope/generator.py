"""Built-in test signal: plays the partner's role and sends a configurable feed to this server's SRT
listener, optionally with SCTE-35 ad markers, encryption, a second audio language, and deliberate faults
(black video, silent audio) so every part of the inspector can be demonstrated."""

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

CUE_OUT = """<?xml version="1.0" encoding="UTF-8"?>
<tsduck><splice_information_table><splice_insert splice_event_id="{eid}" out_of_network="true" splice_immediate="true" unique_program_id="1" avail_num="1" avails_expected="1"><break_duration auto_return="true" duration="{ticks}"/></splice_insert></splice_information_table></tsduck>
"""
CUE_IN = """<?xml version="1.0" encoding="UTF-8"?>
<tsduck><splice_information_table><splice_insert splice_event_id="{eid}" out_of_network="false" splice_immediate="true" unique_program_id="1"/></splice_information_table></tsduck>
"""

SIZES = {"720p": (1280, 720), "1080p": (1920, 1080), "1080i": (1920, 1080), "480p": (720, 480)}
VIDEO = {"H264": ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high"],
         "HEVC": ["-c:v", "libx265", "-preset", "veryfast", "-x265-params", "log-level=error"],
         "MPEG2": ["-c:v", "mpeg2video"]}
AUDIO = {"AAC": ["-c:a", "aac"], "AC3": ["-c:a", "ac3"], "EAC3": ["-c:a", "eac3"]}


def build(opts: dict, seconds: int, cue_dir: Path) -> list[str]:
    """The two commands of the pipeline (ffmpeg piped into tsp), built only from validated options."""
    w, h = SIZES[opts["size"]]
    fps = {"25": "25", "29.97": "30000/1001", "50": "50", "59.94": "60000/1001"}[opts["fps"]]
    interlaced = opts["size"] == "1080i"
    vf = f"drawtext=text='SignalScope test %{{localtime}}':fontcolor=white:fontsize={h // 18}:box=1:boxcolor=black@0.5:x=20:y=20"
    if opts.get("black"):
        vf += ",drawbox=t=fill:color=black:enable='between(t,8,11)'"
    langs = ["eng", "spa"] if opts.get("second_language") else ["eng"]
    inputs = ["-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}"]
    for i, _ in enumerate(langs):
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={600 + 200 * i}:sample_rate=48000"]
    maps = ["-map", "0:v"] + sum((["-map", f"{i + 1}:a"] for i in range(len(langs))), [])
    af = ["-af", "volume=enable='between(t,12,15)':volume=0"] if opts.get("silence") else []
    meta = sum((["-metadata:s:a:%d" % i, f"language={lang}"] for i, lang in enumerate(langs)), [])
    gop = str(round(float(fps.split("/")[0]) / (float(fps.split("/")[1]) if "/" in fps else 1) * opts["keyframe_s"]))
    video = VIDEO[opts["codec"]] + ["-g", gop, "-b:v", f"{opts['bitrate_mbps']}M", "-maxrate", f"{opts['bitrate_mbps']}M",
                                    "-bufsize", f"{opts['bitrate_mbps'] * 2}M"]
    if interlaced:
        video += ["-flags", "+ilme+ildct", "-top", "1"] + (["-x264opts", "tff=1"] if opts["codec"] == "H264" else [])
    audio = AUDIO[opts["audio_codec"]] + ["-b:a", "192k", "-ac", "2"]
    mux = ["-muxrate", f"{opts['bitrate_mbps'] + 2}M", "-f", "mpegts", "-"]
    ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", *inputs, "-t", str(seconds), *maps,
              "-vf", vf, *af, *video, *audio, *meta, *mux]
    tsp = ["tsp", "-I", "file", "-"]
    if opts.get("scte35"):
        tsp += ["-P", "pmt", "--service", "1", "--add-pid", "0x1F0/0x86", "--add-registration", "0x43554549",
                "-P", "spliceinject", "--pid", "0x1F0", "--pts-pid", "0x100", "--files", str(cue_dir / "cue-*.xml"),
                "--delete-files", "--poll-interval", "200"]
    tsp += ["-O", "srt", "--caller", "127.0.0.1:9000", "--latency", str(opts["latency_ms"])]
    if opts.get("passphrase"):
        tsp += ["--passphrase", opts["passphrase"], "--pbkeylen", str(opts["key_length"])]
    return ffmpeg, tsp


def start(opts: dict, seconds: int) -> None:
    """Runs the test signal in the background for `seconds`, plus a short lead-in."""
    def work():
        cue_dir = Path(tempfile.mkdtemp(prefix="cues-"))
        ffmpeg, tsp = build(opts, seconds + 6, cue_dir)
        p1 = p2 = None
        try:
            p1 = subprocess.Popen(ffmpeg, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p2 = subprocess.Popen(tsp, stdin=p1.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p1.stdout.close()
            if opts.get("scte35"):
                time.sleep(6)
                (cue_dir / "cue-1.xml").write_text(CUE_OUT.format(eid=1001, ticks=opts["ad_seconds"] * 90000))
                time.sleep(opts["ad_seconds"])
                (cue_dir / "cue-2.xml").write_text(CUE_IN.format(eid=1001))
            p2.wait(timeout=seconds + 60)
        except subprocess.TimeoutExpired:
            pass
        finally:
            for p in (p1, p2):
                if p is not None and p.poll() is None:
                    p.kill()
            shutil.rmtree(cue_dir, ignore_errors=True)

    threading.Thread(target=work, daemon=True).start()
