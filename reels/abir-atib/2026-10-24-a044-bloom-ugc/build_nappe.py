#!/usr/bin/env python3
"""Rebuild inputs/music/nappe_A044.wav from the original reel's soundtrack.

The source audio is a mono ambient bed with an impact on each of its five
cuts. The bed (8.6-12.2 s) is looped with 0.8 s crossfades to 23.6 s, the
opening impact (0-1.65 s) is kept, and one impact is placed on the CTA
crossfade (16.33 s in v1). Run: python3 build_nappe.py [impact_time]
"""
import subprocess
import sys

N, SRC = 8, "inputs/video/A044.mp4"
HIT = float(sys.argv[1]) if len(sys.argv) > 1 else 16.33  # impact time, on the CTA arrival
inp = ["-t", "1.65", "-i", SRC]
for _ in range(N):
    inp += ["-ss", "8.6", "-t", "3.6", "-i", SRC]
inp += ["-ss", "1.68", "-t", "1.0", "-i", SRC]
norm = lambda k: f"[{k}:a]pan=mono|c0=c0,aformat=sample_rates=48000,asetpts=PTS-STARTPTS"
parts = [norm(0) + "[head]"] + [norm(i) + f"[l{i}]" for i in range(1, N + 1)]
parts.append(norm(N + 1) + f",afade=t=out:st=0.6:d=0.4,adelay={int(HIT * 1000)},apad[hit]")
cur = "[head]"
for i in range(1, N + 1):
    parts.append(f"{cur}[l{i}]acrossfade=d={0.6 if i == 1 else 0.8}:c1=qsin:c2=qsin[c{i}]")
    cur = f"[c{i}]"
parts.append(f"{cur}[hit]amix=inputs=2:normalize=0:duration=first,atrim=0:23.6,pan=stereo|c0=c0|c1=c0[out]")
subprocess.run(["ffmpeg", "-v", "error", "-y", *inp, "-filter_complex", ";".join(parts),
                "-map", "[out]", "-c:a", "pcm_s16le", "inputs/music/nappe_A044.wav"], check=True)
