"""Web interface: start an inspection or a test signal, watch it run, read and download the report."""

import ipaddress
import re
import socket
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import generator, inspector

app = FastAPI(title="SignalScope", docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"
busy = threading.Lock()  # one inspection at a time: they share the SRT port and the CPU


class Spec(BaseModel):
    """The partner's agreed delivery spec; every field is optional."""
    video_codec: str | None = Field(None, max_length=10)
    resolution: str | None = Field(None, pattern=r"^\d{3,4}x\d{3,4}$")
    fps: float | None = Field(None, gt=0, le=120)
    scan: str | None = Field(None, pattern="^(progressive|interlaced)$")
    keyframe_max_s: float | None = Field(None, gt=0, le=20)
    max_bitrate_mbps: float | None = Field(None, gt=0, le=200)
    audio_codec: str | None = Field(None, max_length=10)
    languages: str | None = Field(None, max_length=60)
    captions_required: bool = False
    scte35_required: bool = False
    encryption_required: bool = False
    max_loss_pct: float | None = Field(None, ge=0, le=100)


class Source(BaseModel):
    kind: str = Field(pattern="^(listen|srt|url)$")
    host: str | None = None
    port: int = Field(9000, ge=1, le=65535)
    url: str | None = None
    seconds: int = Field(20, ge=5, le=120)
    latency_ms: int = Field(1000, ge=20, le=8000)
    passphrase: str | None = Field(None, max_length=79)
    key_length: int = 16
    stream_id: str | None = Field(None, max_length=512)
    label: str | None = Field(None, max_length=120)
    notes: str | None = Field(None, max_length=2000)
    spec: Spec | None = None


class TestSignal(BaseModel):
    size: str = Field("1080p", pattern="^(480p|720p|1080p|1080i)$")
    fps: str = Field("29.97", pattern=r"^(25|29\.97|50|59\.94)$")
    codec: str = Field("H264", pattern="^(H264|HEVC|MPEG2)$")
    bitrate_mbps: int = Field(6, ge=1, le=40)
    keyframe_s: float = Field(2, ge=0.5, le=10)
    audio_codec: str = Field("AAC", pattern="^(AAC|AC3|EAC3)$")
    second_language: bool = False
    scte35: bool = True
    ad_seconds: int = Field(8, ge=2, le=30)
    black: bool = False
    silence: bool = False
    passphrase: str | None = Field(None, max_length=79)
    key_length: int = 16
    latency_ms: int = Field(1000, ge=20, le=8000)
    seconds: int = Field(25, ge=10, le=90)
    spec: Spec | None = None


def _check_srt(passphrase: str | None, key_length: int) -> None:
    if passphrase and len(passphrase) < 10:
        raise HTTPException(400, "An SRT passphrase must be 10 to 79 characters.")
    if key_length not in (16, 24, 32):
        raise HTTPException(400, "Key length must be 16, 24 or 32 bytes.")


def _public_host(host: str) -> None:
    """Refuses hosts that resolve to this server, private networks or cloud metadata, so a public page
    can't be used to reach anything internal."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"Can't find the host {host}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise HTTPException(400, "That address isn't allowed: only public internet addresses can be inspected.")


def _check(src: Source) -> dict:
    _check_srt(src.passphrase, src.key_length)
    if src.kind == "srt":
        if not src.host or not re.fullmatch(r"[A-Za-z0-9.\-:]+", src.host):
            raise HTTPException(400, "Enter the SRT host as a name or IP address.")
        _public_host(src.host)
    if src.kind == "url":
        if not src.url or not re.match(r"^(https?|srt)://", src.url):
            raise HTTPException(400, "The URL must start with http://, https:// or srt://.")
        host = urllib.parse.urlsplit(src.url).hostname
        if not host:
            raise HTTPException(400, "That URL has no host.")
        _public_host(host)
    if src.kind == "listen" and src.port != 9000:
        raise HTTPException(400, "Listening is only open on port 9000 (the firewall allows nothing else).")
    out = src.model_dump(exclude_none=True)
    if out.get("spec"):
        out["spec"] = {k: v for k, v in out["spec"].items() if v not in (None, "", False)}
    return out


def _start(source: dict, before_run=None) -> str:
    if not busy.acquire(blocking=False):
        raise HTTPException(409, "Another inspection is running. Try again when it finishes.")
    job_id = inspector.new_job(source)

    def work():
        try:
            if before_run:
                before_run()
            inspector.run(job_id, source)
        finally:
            busy.release()

    threading.Thread(target=work, daemon=True).start()
    return job_id


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {"busy": busy.locked(), "tools": inspector.tools_available()}


@app.post("/api/inspect")
def inspect(src: Source):
    return {"id": _start(_check(src))}


@app.post("/api/test-signal")
def test_signal(sig: TestSignal):
    """Listens on :9000 and sends the test feed to it, so the whole pipeline runs end to end."""
    _check_srt(sig.passphrase, sig.key_length)
    opts = sig.model_dump()
    parts = [sig.codec, sig.size, f"{sig.fps} fps", f"{sig.bitrate_mbps} Mbps", sig.audio_codec]
    if sig.second_language:
        parts.append("eng+spa")
    if sig.scte35:
        parts.append(f"SCTE-35 {sig.ad_seconds}s break")
    if sig.passphrase:
        parts.append(f"AES-{sig.key_length * 8}")
    faults = [x for x, on in (("black 8-11s", sig.black), ("silence 12-15s", sig.silence)) if on]
    source = {
        "kind": "listen", "port": 9000, "seconds": sig.seconds, "latency_ms": sig.latency_ms,
        "key_length": sig.key_length, "label": "Test signal · " + " · ".join(parts),
        "notes": "Built-in generator." + (f" Deliberate faults: {', '.join(faults)}." if faults else ""),
    }
    if sig.passphrase:
        source["passphrase"] = sig.passphrase
    if sig.spec:
        spec = {k: v for k, v in sig.spec.model_dump().items() if v not in (None, "", False)}
        if spec:
            source["spec"] = spec

    def launch():
        time.sleep(1.5)  # let the listener open before the sender calls
        generator.start(opts, sig.seconds)

    threading.Thread(target=launch, daemon=True).start()
    return {"id": _start(source)}


@app.get("/api/jobs")
def jobs():
    return inspector.list_jobs()


def _job_or_404(job_id: str) -> dict:
    report = inspector.get_job(job_id)
    if report is None:
        raise HTTPException(404, "No such inspection.")
    return report


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    return _job_or_404(job_id)


@app.delete("/api/jobs/{job_id}")
def delete(job_id: str):
    _job_or_404(job_id)
    if not inspector.delete_job(job_id):
        raise HTTPException(409, "It's still running.")
    return {"deleted": job_id}


def _file(job_id: str, name: str, media: str, download: str | None = None):
    _job_or_404(job_id)
    path = inspector.DATA / job_id / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=media, filename=download)


@app.get("/api/jobs/{job_id}/thumb.jpg")
def thumb(job_id: str):
    return _file(job_id, "thumb.jpg", "image/jpeg")


@app.get("/api/jobs/{job_id}/film/{n}.jpg")
def film(job_id: str, n: int):
    if not 1 <= n <= 12:
        raise HTTPException(404)
    return _file(job_id, f"film-{n:02d}.jpg", "image/jpeg")


@app.get("/api/jobs/{job_id}/report.json")
def download_report(job_id: str):
    return _file(job_id, "report.json", "application/json", f"signalscope-{job_id}.json")


@app.get("/api/jobs/{job_id}/sample.ts")
def download_sample(job_id: str):
    return _file(job_id, "sample.ts", "video/mp2t", f"signalscope-{job_id}.ts")
