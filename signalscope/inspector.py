"""Records a short sample of a live feed and reports every layer of it: SRT link, transport stream,
video, audio, captions, SCTE-35 markers and picture/sound quality checks."""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(os.environ.get("SIGNALSCOPE_DATA", Path.home() / "lab" / "reports"))
KEEP_SAMPLES = 10  # recordings kept on disk; reports are kept forever
CONNECT_TIMEOUT = 180  # seconds to wait for a sender in listener mode


def new_job(source: dict) -> str:
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = DATA / job_id
    folder.mkdir(parents=True)
    _save(folder, {"id": job_id, "status": "queued", "stage": "Queued", "source": _public(source)})
    return job_id


def run(job_id: str, source: dict) -> None:
    folder = DATA / job_id
    report = json.loads((folder / "report.json").read_text())
    try:
        report["started"] = _now()
        _stage(folder, report, "Waiting for the feed" if source["kind"] == "listen" else "Connecting")
        stats = _record(folder, source, report)
        _stage(folder, report, "Analyzing")
        sample = folder / "sample.ts"
        if not sample.exists() or sample.stat().st_size < 188 * 100:
            raise RuntimeError("No usable data was received from the source.")
        report["srt"] = _srt_summary(stats, source) if stats is not None else None
        probe = _ffprobe(sample)
        report["ts"] = _transport(sample)
        report["video"] = _video(sample, probe)
        report["audio"] = _audio(probe)
        report["captions"] = _captions(probe)
        report["scte35"] = _scte35(sample, report["ts"])
        report["pcr"] = _pcr(sample)
        _stage(folder, report, "Checking picture and sound")
        report["qc"] = _quality(sample)
        _thumbnail(sample, folder / "thumb.jpg")
        report["thumbnail"] = (folder / "thumb.jpg").exists()
        report["film"] = _filmstrip(sample, folder)
        report["health"] = _health(report)
        if source.get("spec"):
            report["conformance"] = _conformance(report, source["spec"])
        report["status"], report["stage"] = "done", "Done"
    except Exception as err:  # the report always ends with a status the page can show
        report["status"], report["stage"], report["error"] = "error", "Failed", str(err)
    report["finished"] = _now()
    _save(folder, report)
    _prune()


def list_jobs() -> list[dict]:
    out = []
    for f in sorted(DATA.glob("*/report.json"), reverse=True):
        try:
            r = json.loads(f.read_text())
        except ValueError:
            continue
        v = r.get("video") or {}
        out.append({
            "id": r["id"], "status": r.get("status"), "source": r.get("source"), "started": r.get("started"),
            "summary": " · ".join(x for x in [v.get("codec"), v.get("resolution"), v.get("fps_label")] if x),
            "health": (r.get("health") or {}).get("overall"),
        })
    return out


def get_job(job_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", job_id):
        return None
    f = DATA / job_id / "report.json"
    return json.loads(f.read_text()) if f.exists() else None


# ---------- recording ----------

def srt_url(source: dict) -> str:
    params = {"latency": str(int(source.get("latency_ms", 1000)))}
    if source.get("passphrase"):
        params["passphrase"] = source["passphrase"]
        params["pbkeylen"] = str(int(source.get("key_length", 16)))
    if source.get("stream_id"):
        params["streamid"] = source["stream_id"]
    if source["kind"] == "listen":
        params["mode"] = "listener"
        return f"srt://:{int(source.get('port', 9000))}?" + urllib.parse.urlencode(params)
    params["mode"] = "caller"
    host, port = source["host"], int(source["port"])
    return f"srt://{host}:{port}?" + urllib.parse.urlencode(params)


def _record(folder: Path, source: dict, report: dict):
    """Writes sample.ts; returns the SRT stats samples, or None for non-SRT sources."""
    seconds = max(5, min(int(source.get("seconds", 20)), 120))
    sample = folder / "sample.ts"
    if source["kind"] in ("listen", "srt"):
        stats_file = folder / "srt-stats.json"
        with open(sample, "wb") as out:
            proc = subprocess.Popen(
                ["srt-live-transmit", "-s:1000", "-pf:json", f"-statsout:{stats_file}", srt_url(source), "file://con"],
                stdout=out, stderr=subprocess.DEVNULL,
            )
            started_at, connected_at = time.time(), None
            while proc.poll() is None:
                time.sleep(0.5)
                if connected_at is None:
                    if sample.stat().st_size > 0:
                        connected_at = time.time()
                        _stage(folder, report, f"Recording {seconds} s")
                    elif time.time() - started_at > CONNECT_TIMEOUT:
                        break
                elif time.time() - connected_at >= seconds:
                    break
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if connected_at is None:
            raise RuntimeError("No sender connected." if source["kind"] == "listen" else "Could not connect to the source.")
        return _parse_stats(stats_file)
    url = source["url"]
    _stage(folder, report, f"Recording {seconds} s")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-rw_timeout", "15000000",
         "-protocol_whitelist", "http,https,tcp,tls,crypto,hls,srt", "-i", url,
         "-t", str(seconds), "-map", "0", "-c", "copy", "-f", "mpegts", str(sample)],
        capture_output=True, text=True, timeout=seconds + 90,
    )
    if result.returncode != 0 and not sample.exists():
        raise RuntimeError("Could not read the source: " + (result.stderr.strip().splitlines() or ["unknown error"])[-1])
    return None


def _parse_stats(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text, out, decoder, i = path.read_text(errors="ignore"), [], json.JSONDecoder(), 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return out
        try:
            obj, i = decoder.raw_decode(text, i)
            out.append(obj)
        except ValueError:
            i += 1


def _srt_summary(samples: list[dict], source: dict) -> dict:
    recv = [s.get("recv", {}) for s in samples]
    link = [s.get("link", {}) for s in samples]
    total = lambda key: sum(r.get(key, 0) for r in recv)
    received, lost = total("packets"), total("packetsLost")
    rtts = [l["rtt"] for l in link if l.get("rtt")]
    rates = [r["mbitRate"] for r in recv if r.get("mbitRate")]
    return {
        "mode": "listener" if source["kind"] == "listen" else "caller",
        "latency_ms": int(source.get("latency_ms", 1000)),
        "encrypted": bool(source.get("passphrase")),
        "key_length_bits": int(source.get("key_length", 16)) * 8 if source.get("passphrase") else None,
        "stream_id": source.get("stream_id") or None,
        "rtt_ms_avg": round(sum(rtts) / len(rtts), 1) if rtts else None,
        "rtt_ms_max": round(max(rtts), 1) if rtts else None,
        "mbps_avg": round(sum(rates) / len(rates), 2) if rates else None,
        "bandwidth_mbps_est": round(link[-1].get("bandwidth", 0), 1) if link else None,
        "packets_received": received,
        "packets_lost": lost,
        "packets_retransmitted": total("packetsRetransmitted"),
        "packets_dropped": total("packetsDropped"),
        "packets_belated": total("packetsBelated"),
        "loss_pct": round(100 * lost / (received + lost), 3) if received + lost else 0.0,
        "timeline": [{"t": n, "mbps": r.get("mbitRate"), "rtt": l.get("rtt"), "lost": r.get("packetsLost", 0)}
                     for n, (r, l) in enumerate(zip(recv, link))],
    }


# ---------- analysis ----------

def _ffprobe(sample: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", "-show_streams", "-show_programs", "-show_format", str(sample)],
        capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out or "{}")


def _transport(sample: Path) -> dict:
    out = subprocess.run(["tsp", "-I", "file", str(sample), "-P", "analyze", "--json", "-O", "drop"],
                         capture_output=True, text=True, timeout=120).stdout
    a = json.loads(out or "{}")
    ts = a.get("ts", {})
    pids = [{
        "pid": p["id"], "hex": f"0x{p['id']:04X}", "description": p.get("description", ""),
        "bitrate": p.get("bitrate", 0), "video": p.get("video", False), "audio": p.get("audio", False),
        "packets": p.get("packets", {}).get("total", 0),
        "discontinuities": p.get("packets", {}).get("discontinuities", 0),
        "scrambled": p.get("is-scrambled", False),
    } for p in a.get("pids", [])]
    return {
        "ts_id": ts.get("id"),
        "bitrate": ts.get("bitrate"),
        "duration_s": ts.get("duration"),
        "transport_errors": ts.get("packets", {}).get("transport-errors", 0),
        "invalid_syncs": ts.get("packets", {}).get("invalid-syncs", 0),
        "continuity_errors": sum(p["discontinuities"] for p in pids),
        "services": [{"id": s.get("id"), "name": s.get("name"), "provider": s.get("provider"),
                      "type": s.get("type-name"), "pmt_pid": s.get("pmt-pid"), "pcr_pid": s.get("pcr-pid"),
                      "pids": s.get("pids", [])} for s in a.get("services", [])],
        "pids": pids,
    }


def _fps(rate: str | None) -> float | None:
    try:
        n, d = (rate or "0/0").split("/")
        return round(int(n) / int(d), 3) if int(d) else None
    except ValueError:
        return None


def _video(sample: Path, probe: dict) -> dict | None:
    v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return None
    fps = _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate"))
    keys = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
         "-of", "csv=p=0", str(sample)], capture_output=True, text=True, timeout=60).stdout.split()
    times = sorted(float(t) for t in keys if re.fullmatch(r"[0-9.]+", t))
    gaps = [b - a for a, b in zip(times, times[1:])]
    transfer = v.get("color_transfer")
    return {
        "pid": v.get("id"), "codec": v.get("codec_name", "").upper(), "codec_long": v.get("codec_long_name"),
        "profile": v.get("profile"), "level": v.get("level"),
        "width": v.get("width"), "height": v.get("height"), "resolution": f"{v.get('width')}x{v.get('height')}",
        "fps": fps, "fps_label": f"{fps:g} fps" if fps else None,
        "scan": "interlaced" if v.get("field_order") not in (None, "progressive", "unknown") else "progressive",
        "field_order": v.get("field_order"),
        "pix_fmt": v.get("pix_fmt"), "bit_depth": 10 if "10" in (v.get("pix_fmt") or "") else 8,
        "color_space": v.get("color_space"), "color_primaries": v.get("color_primaries"), "color_transfer": transfer,
        "hdr": {"smpte2084": "HDR10 (PQ)", "arib-std-b67": "HLG"}.get(transfer or "", "SDR"),
        "aspect": v.get("display_aspect_ratio"),
        "keyframe_interval_s": round(sum(gaps) / len(gaps), 2) if gaps else None,
        "keyframe_interval_regular": (max(gaps) - min(gaps) < 0.1) if len(gaps) > 1 else None,
        "has_b_frames": bool(v.get("has_b_frames")),
    }


def _audio(probe: dict) -> list[dict]:
    out = []
    for s in probe.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        out.append({
            "pid": s.get("id"), "codec": s.get("codec_name", "").upper(), "profile": s.get("profile"),
            "channels": s.get("channels"), "layout": s.get("channel_layout"),
            "sample_rate": int(s["sample_rate"]) if s.get("sample_rate") else None,
            "bitrate": int(s["bit_rate"]) if s.get("bit_rate") else None,
            "language": (s.get("tags") or {}).get("language"),
        })
    return out


def _captions(probe: dict) -> dict:
    v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    subs = [{"pid": s.get("id"), "codec": s.get("codec_name"), "language": (s.get("tags") or {}).get("language")}
            for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]
    return {"cea608_708": bool(v.get("closed_captions")), "subtitle_streams": subs}


def _scte35(sample: Path, ts: dict) -> dict:
    """Every splice command on the SCTE-35 PIDs, decoded into plain fields."""
    pids = [p["pid"] for p in ts["pids"] if "SCTE" in p["description"].upper()]
    events: list[dict] = []
    if pids:
        out = subprocess.run(
            ["tsp", "-I", "file", str(sample), "-P", "splicemonitor", "--json", "--all-commands", "-O", "drop"],
            capture_output=True, text=True, timeout=120).stdout
        start = out.find("[")
        try:
            items = json.loads(out[start:]) if start >= 0 else []
        except ValueError:
            items = []
        for item in items:
            if item.get("#name") != "splice_information_table":
                continue
            for node in item.get("#nodes", []):
                name = node.get("#name")
                if name == "metadata":
                    continue
                kids = {k.get("#name"): k for k in node.get("#nodes", [])}
                brk = kids.get("break_duration")
                seg = [k for k in node.get("#nodes", []) if k.get("#name") == "segmentation_descriptor"]
                for d in item.get("#nodes", []):
                    if d.get("#name") == "segmentation_descriptor":
                        seg.append(d)
                events.append({
                    "command": name,
                    "event_id": node.get("splice_event_id"),
                    "direction": ("OUT (ad break starts)" if node.get("out_of_network") else "IN (back to program)")
                    if name == "splice_insert" else None,
                    "cancel": node.get("splice_event_cancel", False),
                    "immediate": node.get("splice_immediate", False),
                    "pts_time": node.get("pts_time"),
                    "duration_s": round(brk["duration"] / 90000, 2) if brk and brk.get("duration") else None,
                    "auto_return": brk.get("auto_return") if brk else None,
                    "program_id": node.get("unique_program_id"),
                    "segmentation": [{"type": s.get("segmentation_type_id"), "event_id": s.get("segmentation_event_id"),
                                      "duration_s": round(s["segmentation_duration"] / 90000, 2)
                                      if s.get("segmentation_duration") else None,
                                      "upid": s.get("segmentation_upid")} for s in seg],
                })
    return {"pids": pids, "present": bool(pids), "events": events,
            "unpaired_outs": _unpaired(events)}


def _unpaired(events: list[dict]) -> list:
    """Ad breaks that went OUT without a matching IN or auto-return during the sample."""
    open_ids = {}
    for e in events:
        if e["command"] != "splice_insert" or e["cancel"]:
            continue
        if e["direction"] and e["direction"].startswith("OUT"):
            open_ids[e["event_id"]] = e
        else:
            open_ids.pop(e["event_id"], None)
    return [i for i, e in open_ids.items() if not e.get("auto_return")]


def _pcr(sample: Path) -> dict:
    """PCR (program clock) accuracy: how many clock samples drift more than 500 microseconds."""
    out = subprocess.run(["tsp", "-I", "file", str(sample), "-P", "pcrverify", "--jitter-max", "13500", "-O", "drop"],
                         capture_output=True, text=True, timeout=120)
    m = re.search(r"([\d,]+) PCR OK, ([\d,]+) with jitter > ([\d,]+) \(([\d,]+) micro-seconds\)",
                  out.stdout + out.stderr)
    if not m:
        return {"checked": False}
    n = lambda s: int(s.replace(",", ""))
    return {"checked": True, "ok": n(m.group(1)), "bad": n(m.group(2)), "threshold_us": n(m.group(4))}


def _filmstrip(sample: Path, folder: Path) -> int:
    """A frame every few seconds across the sample, for a quick visual check."""
    for old in folder.glob("film-*.jpg"):
        old.unlink()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(sample), "-vf",
                    "fps=1/3,scale=240:-2", "-frames:v", "12", str(folder / "film-%02d.jpg")],
                   capture_output=True, timeout=120)
    return len(list(folder.glob("film-*.jpg")))


def _conformance(r: dict, spec: dict) -> list[dict]:
    """Each line of the agreed spec, compared with what the feed actually carries."""
    v, a, srt, ts = r.get("video") or {}, r.get("audio") or [], r.get("srt") or {}, r["ts"]
    rows = []

    def check(name, expected, actual, ok):
        rows.append({"check": name, "expected": expected, "actual": actual, "pass": bool(ok)})

    if spec.get("video_codec"):
        check("Video codec", spec["video_codec"], v.get("codec"), (v.get("codec") or "").upper() == spec["video_codec"].upper())
    if spec.get("resolution"):
        check("Resolution", spec["resolution"], v.get("resolution"), v.get("resolution") == spec["resolution"])
    if spec.get("fps"):
        check("Frame rate", spec["fps"], v.get("fps"), v.get("fps") is not None and abs(v["fps"] - float(spec["fps"])) < 0.02)
    if spec.get("scan"):
        check("Scan", spec["scan"], v.get("scan"), v.get("scan") == spec["scan"])
    if spec.get("keyframe_max_s"):
        k = v.get("keyframe_interval_s")
        check("Keyframe interval", f"≤ {spec['keyframe_max_s']} s", k, k is not None and k <= float(spec["keyframe_max_s"]) + 0.05)
    if spec.get("max_bitrate_mbps"):
        mbps = round((ts.get("bitrate") or 0) / 1e6, 2)
        check("Total bitrate", f"≤ {spec['max_bitrate_mbps']} Mbps", f"{mbps} Mbps", mbps <= float(spec["max_bitrate_mbps"]))
    if spec.get("audio_codec"):
        codecs = sorted({t.get("codec") for t in a})
        check("Audio codec", spec["audio_codec"], ", ".join(codecs) or "none", spec["audio_codec"].upper() in codecs)
    if spec.get("languages"):
        want = [x.strip().lower() for x in spec["languages"].split(",") if x.strip()]
        have = [(t.get("language") or "").lower() for t in a]
        check("Audio languages", ", ".join(want), ", ".join(h or "?" for h in have) or "none", all(w in have for w in want))
    if spec.get("captions_required"):
        check("Captions", "required", "present" if r["captions"]["cea608_708"] else "missing", r["captions"]["cea608_708"])
    if spec.get("scte35_required"):
        check("SCTE-35 stream", "required", "present" if r["scte35"]["present"] else "missing", r["scte35"]["present"])
    if spec.get("encryption_required"):
        check("SRT encryption", "required", "on" if srt.get("encrypted") else "off", srt.get("encrypted"))
    if spec.get("max_loss_pct") not in (None, ""):
        loss = srt.get("loss_pct")
        check("Packet loss", f"≤ {spec['max_loss_pct']}%", f"{loss}%" if loss is not None else "n/a",
              loss is not None and loss <= float(spec["max_loss_pct"]))
    check("Continuity errors", "0", ts["continuity_errors"], ts["continuity_errors"] == 0)
    return rows


def delete_job(job_id: str) -> bool:
    report = get_job(job_id)
    if report is None or report.get("status") in ("running", "queued"):
        return False
    shutil.rmtree(DATA / job_id)
    return True


def _quality(sample: Path) -> dict:
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(sample), "-map", "0:v:0?", "-map", "0:a:0?",
         "-vf", "blackdetect=d=0.5:pix_th=0.10,freezedetect=n=0.003:d=2",
         "-af", "silencedetect=n=-50dB:d=1,ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300).stderr
    num = lambda pattern: [float(x) for x in re.findall(pattern, err)]
    black = list(zip(num(r"black_start:([0-9.]+)"), num(r"black_duration:([0-9.]+)")))
    freeze_starts, freeze_durs = num(r"freeze_start: ([0-9.]+)"), num(r"freeze_duration: ([0-9.]+)")
    silence = list(zip(num(r"silence_start: ([0-9.]+)"), num(r"silence_duration: ([0-9.]+)")))
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    lufs = re.search(r"I:\s+(-?[0-9.]+) LUFS", summary)
    lra = re.search(r"LRA:\s+([0-9.]+) LU", summary)
    peak = re.search(r"Peak:\s+(-?[0-9.]+) dBFS", summary)
    return {
        "black": [{"start": s, "duration": d} for s, d in black],
        "freeze": [{"start": s, "duration": d} for s, d in zip(freeze_starts, freeze_durs)],
        "silence": [{"start": s, "duration": d} for s, d in silence],
        "loudness_lufs": float(lufs.group(1)) if lufs else None,
        "loudness_range_lu": float(lra.group(1)) if lra else None,
        "true_peak_dbfs": float(peak.group(1)) if peak else None,
    }


def _thumbnail(sample: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "3", "-i", str(sample),
                    "-frames:v", "1", "-vf", "scale=640:-2", str(out)], capture_output=True, timeout=60)


def _health(r: dict) -> dict:
    """Green/yellow/red per layer, with the reasons."""
    checks: dict[str, list] = {"link": [], "transport": [], "video": [], "audio": [], "captions": [], "quality": []}
    srt = r.get("srt")
    if srt:
        if srt["loss_pct"] > 1:
            checks["link"].append(("red", f"Packet loss {srt['loss_pct']}%"))
        elif srt["loss_pct"] > 0.1:
            checks["link"].append(("yellow", f"Packet loss {srt['loss_pct']}%"))
        if srt["packets_dropped"]:
            checks["link"].append(("red", f"{srt['packets_dropped']} packets dropped (arrived too late)"))
    ts = r["ts"]
    if ts["continuity_errors"]:
        checks["transport"].append(("red", f"{ts['continuity_errors']} continuity errors"))
    if ts["transport_errors"] or ts["invalid_syncs"]:
        checks["transport"].append(("red", "Transport errors or lost sync"))
    pcr = r.get("pcr") or {}
    if pcr.get("bad"):
        level = "red" if pcr["bad"] > 0.01 * (pcr["ok"] + pcr["bad"]) else "yellow"
        checks["transport"].append((level, f"{pcr['bad']} PCR clock samples off by more than {pcr['threshold_us']} µs"))
    if r["scte35"].get("unpaired_outs"):
        checks["transport"].append(("yellow", f"Ad break(s) {r['scte35']['unpaired_outs']} went out with no return marker"))
    v = r.get("video")
    if not v:
        checks["video"].append(("red", "No video stream"))
    elif v.get("keyframe_interval_s") and v["keyframe_interval_s"] > 4:
        checks["video"].append(("yellow", f"Keyframes every {v['keyframe_interval_s']} s (slow seeking/startup)"))
    if not r.get("audio"):
        checks["audio"].append(("red", "No audio stream"))
    if not r["captions"]["cea608_708"] and not r["captions"]["subtitle_streams"]:
        checks["captions"].append(("yellow", "No captions found"))
    q = r["qc"]
    if q["black"]:
        checks["quality"].append(("red", f"Black video {len(q['black'])} time(s)"))
    if q["freeze"]:
        checks["quality"].append(("red", f"Frozen video {len(q['freeze'])} time(s)"))
    if q["silence"] and r.get("audio"):
        checks["quality"].append(("yellow", f"Silent audio {len(q['silence'])} time(s)"))
    if q["loudness_lufs"] is not None and not -30 <= q["loudness_lufs"] <= -18:
        checks["quality"].append(("yellow", f"Loudness {q['loudness_lufs']} LUFS (broadcast target about -24/-23)"))
    rank = {"green": 0, "yellow": 1, "red": 2}
    layers = {k: {"level": max((c[0] for c in v), key=rank.get, default="green"), "notes": [c[1] for c in v]}
              for k, v in checks.items()}
    return {"overall": max((l["level"] for l in layers.values()), key=rank.get), "layers": layers}


# ---------- helpers ----------

def _public(source: dict) -> dict:
    """The source as shown on the page: never includes the passphrase."""
    shown = {k: v for k, v in source.items() if k != "passphrase"}
    shown["encrypted"] = bool(source.get("passphrase"))
    return shown


def _stage(folder: Path, report: dict, stage: str) -> None:
    report["status"], report["stage"] = "running", stage
    _save(folder, report)


def _save(folder: Path, report: dict) -> None:
    tmp = folder / "report.json.tmp"
    tmp.write_text(json.dumps(report, indent=1))
    tmp.replace(folder / "report.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prune() -> None:
    samples = sorted(DATA.glob("*/sample.ts"), reverse=True)
    for old in samples[KEEP_SAMPLES:]:
        old.unlink(missing_ok=True)


def tools_available() -> dict:
    return {t: bool(shutil.which(t)) for t in ("ffmpeg", "ffprobe", "tsp", "srt-live-transmit")}
