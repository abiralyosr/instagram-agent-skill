#!/usr/bin/env python3
"""reelforge: the montage engine behind /ig-montage.

Turns a project folder (rushes, photos, voice-over, music, script) into a
1080x1920 Reel with FFmpeg. Every creative decision lives in edit.json, so a
cut can be read, argued with and re-rendered without starting over.

    python3 reelforge.py doctor                       # what this machine can do
    python3 reelforge.py new abir-atib "lancement oud" # create a project folder
    python3 reelforge.py probe  PROJECT               # inventory, contact sheets, silences, loudness
    python3 reelforge.py subs   PROJECT               # script.txt + voice -> subs.srt
    python3 reelforge.py plan   PROJECT               # first-draft edit.json from the inputs
    python3 reelforge.py check  PROJECT               # lint the cut before spending a render
    python3 reelforge.py render PROJECT --preview     # 540x960, fast, for review
    python3 reelforge.py render PROJECT --final       # 1080x1920, two-pass loudness
    python3 reelforge.py render PROJECT --dry-run     # print the ffmpeg commands only
    python3 reelforge.py selftest                     # pure-logic tests, no ffmpeg

Standard library only. Needs ffmpeg/ffprobe on PATH (libass, fribidi,
harfbuzz, zimg, vidstab for the full feature set; `doctor` says what is missing).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRANDS_DIR = HERE / "brands"
FPS = 30
SIZES = {"final": (1080, 1920), "preview": (540, 960)}

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
INPUT_DIRS = ("video", "photo", "voice", "music")

# Our transition names -> xfade names. "dissolve" is an alias of crossfade on
# purpose: xfade's own "dissolve" is a dithered noise pattern that reads cheap.
XFADE = {
    "crossfade": "fade",
    "dissolve": "fade",
    "fadeblack": "fadeblack",
    "fadewhite": "fadewhite",
    "smoothleft": "smoothleft",
    "smoothright": "smoothright",
    "smoothup": "smoothup",
    "smoothdown": "smoothdown",
}
PHOTO_MOVES = ("push_in", "pull_out", "drift_left", "drift_right", "drift_up", "drift_down", "static")

VOICE_PRESETS = {
    # warm, close, a little compressed: product voice-over
    "warm": "highpass=f=80,afftdn=nr=10:nf=-42,deesser=i=0.35,"
            "acompressor=threshold=-20dB:ratio=3:attack=6:release=140:makeup=2,"
            "equalizer=f=180:t=q:w=1:g=1.5,equalizer=f=4200:t=q:w=1.4:g=1.5",
    # soft, airy, gentle dynamics: wellness / relaxation
    "soft": "highpass=f=70,afftdn=nr=8:nf=-45,deesser=i=0.3,"
            "acompressor=threshold=-24dB:ratio=2:attack=12:release=220:makeup=1.5,"
            "equalizer=f=250:t=q:w=1.2:g=-1,equalizer=f=9000:t=h:w=0.7:g=1.5",
    # leave the recording alone apart from rumble and level
    "clean": "highpass=f=70",
    # the recording exactly as delivered: only the overall level moves, at the master
    "none": "anull",
}


# --------------------------------------------------------------------------- utils

def die(msg):
    sys.stderr.write(f"\nERREUR: {msg}\n")
    raise SystemExit(1)


def say(msg=""):
    print(msg, flush=True)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "reel"


def frames(t):
    """Round a duration to whole frames so xfade offsets land on a frame."""
    return round(t * FPS) / FPS


def seeded_rng(*parts):
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:12], 16))


def hex_to_ass(color, alpha=0.0):
    """#RRGGBB (+ alpha 0..1 transparency) -> &HAABBGGRR."""
    c = color.lstrip("#")
    r, g, b = c[0:2], c[2:4], c[4:6]
    a = int(round(max(0.0, min(1.0, alpha)) * 255))
    return f"&H{a:02X}{b}{g}{r}".upper()


def hex_to_ff(color):
    return "0x" + color.lstrip("#")


def tool(name):
    if not shutil.which(name):
        die(f"{name} introuvable. Lance `python3 reelforge.py doctor`.")
    return name


def ffrun(cmd, cwd, dry, log, capture=False):
    """Run ffmpeg/ffprobe. In dry-run, print and record the command instead."""
    line = shlex.join(cmd)
    log.append(line)
    if dry:
        say("  $ " + line)
        return ""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-4000:])
        die(f"la commande a échoué :\n  {line}")
    return p.stdout if capture else p.stderr


def font_families():
    if not shutil.which("fc-list"):
        return set()
    out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    fams = set()
    for line in out.splitlines():
        for fam in line.split(","):
            fams.add(fam.strip().replace("\\-", "-").lower())
    return fams


def system_font_file(family="DejaVu Sans"):
    if shutil.which("fc-match"):
        out = subprocess.run(["fc-match", "-f", "%{file}", family], capture_output=True, text=True).stdout
        if out and Path(out).exists():
            return out
    return None


# --------------------------------------------------------------------------- brands

def list_brands():
    return sorted(p.name for p in BRANDS_DIR.iterdir() if (p / "brand.json").exists())


def load_brand(slug):
    path = BRANDS_DIR / slug / "brand.json"
    if not path.exists():
        die(f"marque inconnue « {slug} ». Disponibles : {', '.join(list_brands())}")
    brand = load_json(path)
    brand["_dir"] = str(path.parent)
    return brand


def brand_fonts_dir(brand):
    return Path(brand["_dir"]) / "fonts"


def brand_font_available(brand, family):
    fdir = brand_fonts_dir(brand)
    if fdir.exists():
        for f in fdir.iterdir():
            if f.suffix.lower() in (".ttf", ".otf") and family.lower().replace(" ", "") in f.stem.lower().replace(" ", "").replace("-", ""):
                return True
    return family.lower() in font_families()


def resolve_font(brand, style, warn):
    family = style["font"]
    if brand_font_available(brand, family):
        return family
    fb = style.get("fallback_font", "DejaVu Sans")
    warn.append(f"police « {family} » absente : repli sur « {fb} ». "
                f"Dépose le .ttf dans {brand_fonts_dir(brand)}.")
    return fb


# --------------------------------------------------------------------------- project

class Project:
    def __init__(self, path):
        self.dir = Path(path).resolve()
        self.edit_path = self.dir / "edit.json"
        if not self.edit_path.exists():
            die(f"pas de edit.json dans {self.dir}. Crée le projet avec `new`.")
        self.edit = load_json(self.edit_path)
        self.brand = load_brand(self.edit["brand"])
        self.out = self.dir / "out"
        self.work = self.out / "work"
        self.probe_path = self.out / "probe.json"

    @property
    def probe(self):
        return load_json(self.probe_path) if self.probe_path.exists() else {"files": {}}

    def rel(self, p):
        return str(Path(p).resolve().relative_to(self.dir))

    def inputs(self, kind):
        d = self.dir / "inputs" / kind
        ext = {"video": VIDEO_EXT, "photo": PHOTO_EXT, "voice": AUDIO_EXT, "music": AUDIO_EXT}[kind]
        if not d.exists():
            return []
        return sorted(self.rel(p) for p in d.iterdir() if p.suffix.lower() in ext and not p.name.startswith("."))


def projects_root(args):
    root = getattr(args, "root", None) or os.environ.get("REELFORGE_ROOT")
    if root:
        return Path(root)
    # default: <git repo of the current directory>/reels, else ./reels
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(top.stdout.strip() if top.returncode == 0 else ".") / "reels"


def cmd_new(args):
    brand = load_brand(args.brand)
    root = projects_root(args)
    slug = datetime.date.today().isoformat() + "-" + slugify(args.title)
    pdir = root / args.brand / slug
    if pdir.exists():
        die(f"{pdir} existe déjà.")
    for k in INPUT_DIRS:
        (pdir / "inputs" / k).mkdir(parents=True)
    (pdir / "out").mkdir()
    (pdir / "script.txt").write_text(
        "# Une ligne = un sous-titre, en darija tunisienne, tel que prononcé dans la voix off.\n"
        "# Les lignes qui commencent par # sont ignorées.\n", encoding="utf-8")
    save_json(pdir / "edit.json", {
        "brand": args.brand,
        "title": args.title,
        "voice": None,
        "voice_offset": 0.3,
        "music": None,
        "subtitles": {"enabled": True, "file": "subs.srt"},
        "texts": [],
        "end_card": brand.get("end_card", {}).get("enabled", False),
        "cover_at": None,
        "shots": [],
    })
    say(f"Projet créé : {pdir}")
    say("Dépose les fichiers dans inputs/video, inputs/photo, inputs/voice, inputs/music,")
    say("écris script.txt, puis : probe → subs → plan → check → render --preview.")


# --------------------------------------------------------------------------- probe

def ffprobe(path):
    out = subprocess.run(
        [tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


def parse_rate(r):
    try:
        n, d = r.split("/")
        return float(n) / float(d) if float(d) else 0.0
    except (ValueError, AttributeError):
        return 0.0


def media_info(path):
    data = ffprobe(path)
    if not data:
        return {"error": "illisible par ffprobe"}
    v = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in data["streams"] if s.get("codec_type") == "audio"), None)
    info = {"duration": float(data.get("format", {}).get("duration", 0) or 0), "has_audio": a is not None}
    if v:
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        rot = int(v.get("tags", {}).get("rotate", rot))
        w, h = v.get("width", 0), v.get("height", 0)
        if abs(rot) in (90, 270):
            w, h = h, w
        info.update({
            "width": w, "height": h, "rotation": rot,
            "fps": round(parse_rate(v.get("avg_frame_rate")), 3),
            "codec": v.get("codec_name"),
            "hdr": v.get("color_transfer") in ("smpte2084", "arib-std-b67"),
            "color_transfer": v.get("color_transfer"),
        })
    return info


def detect_scenes(path, threshold=0.32):
    p = subprocess.run(
        [tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-an",
         "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True)
    return [round(float(m), 2) for m in re.findall(r"pts_time:([0-9.]+)", p.stderr)]


def detect_speech(path, noise_db=-35, min_silence=0.28):
    """Speech intervals = complement of silencedetect over the file."""
    p = subprocess.run(
        [tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True)
    dur = media_info(path).get("duration", 0)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", p.stderr)]
    silences = list(zip(starts, ends + [dur] * (len(starts) - len(ends))))
    return speech_from_silences(silences, dur)


def speech_from_silences(silences, dur, merge_gap=0.18, min_len=0.12):
    speech, t = [], 0.0
    for s, e in sorted(silences):
        s = max(0.0, s)
        if s - t > 0:
            speech.append([round(t, 3), round(s, 3)])
        t = max(t, e)
    if dur - t > 0:
        speech.append([round(t, 3), round(dur, 3)])
    merged = []
    for s, e in speech:
        if merged and s - merged[-1][1] < merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [iv for iv in merged if iv[1] - iv[0] >= min_len]


def measure_loudness(path, extra_af=None):
    af = (extra_af + "," if extra_af else "") + "loudnorm=print_format=json"
    p = subprocess.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-af", af, "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    return json.loads(m[-1]) if m else None


def contact_sheet(src, dst, duration, cols=4, rows=2):
    n = cols * rows
    font = system_font_file()
    stamp = (f",drawtext=fontfile={font}:text='%{{pts\\:hms}}':x=8:y=8:fontsize=20:"
             "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=4") if font else ""
    rate = max(n / max(duration, 0.1), 0.01)
    subprocess.run(
        [tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-an",
         "-vf", f"fps={rate:.5f},scale=270:-2{stamp},tile={cols}x{rows}:padding=4:color=0x111111",
         "-frames:v", "1", "-q:v", "3", str(dst)],
        capture_output=True, text=True)


def cmd_probe(args):
    pr = Project(args.project)
    pr.out.mkdir(exist_ok=True)
    (pr.out / "contact").mkdir(exist_ok=True)
    files = {}
    for kind in INPUT_DIRS:
        for rel in pr.inputs(kind):
            src = pr.dir / rel
            say(f"· {rel}")
            info = media_info(src)
            info["kind"] = kind
            if "error" in info:
                files[rel] = info
                continue
            if kind == "video":
                info["scenes"] = detect_scenes(src)
                sheet = pr.out / "contact" / (Path(rel).stem + ".jpg")
                contact_sheet(src, sheet, info["duration"])
                info["contact_sheet"] = pr.rel(sheet)
            elif kind in ("voice", "music"):
                ln = measure_loudness(src)
                info["lufs"] = float(ln["input_i"]) if ln and ln["input_i"] not in ("-inf", "inf") else None
                if kind == "voice":
                    info["speech"] = detect_speech(src)
            files[rel] = info
    ignored = [pr.rel(p) for p in (pr.dir / "inputs").glob("*") if p.is_file()]
    save_json(pr.probe_path, {"probed": datetime.datetime.now().isoformat(timespec="seconds"), "files": files})
    say()
    for rel, i in files.items():
        if "error" in i:
            say(f"  ✗ {rel}: {i['error']}")
            continue
        bits = [i["kind"], f"{i['duration']:.1f}s"]
        if "width" in i and i["kind"] != "voice":
            bits.append(f"{i['width']}x{i['height']}")
            if i["kind"] == "video":
                bits.append(f"{i['fps']:g} fps")
                if i.get("hdr"):
                    bits.append("HDR → tone-mapping SDR")
                if i["width"] > i["height"]:
                    bits.append("horizontal → recadrage 9:16 (régler focus_x)")
        if i.get("lufs") is not None:
            bits.append(f"{i['lufs']:.1f} LUFS")
        if i.get("speech"):
            bits.append(f"{len(i['speech'])} phrases détectées")
        say(f"  ✓ {rel}: " + ", ".join(bits))
    for rel in ignored:
        say(f"  ! {rel} est à la racine de inputs/ : range-le dans video/, photo/, voice/ ou music/.")
    say(f"\nInventaire : {pr.rel(pr.probe_path)}   Planches contact : out/contact/")


# --------------------------------------------------------------------------- subtitles

def srt_time(t):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt_time(s):
    h, m, rest = s.strip().replace(".", ",").split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def write_srt(path, cues):
    blocks = [f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n"
              for i, c in enumerate(cues, 1)]
    Path(path).write_text("\n".join(blocks), encoding="utf-8")


def read_srt(path):
    cues = []
    for block in re.split(r"\n\s*\n", Path(path).read_text(encoding="utf-8-sig").strip()):
        lines = block.strip().splitlines()
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")
        cues.append({"start": parse_srt_time(a), "end": parse_srt_time(b), "text": "\n".join(lines[idx + 1:]).strip()})
    return cues


def weight(text):
    """Spoken weight of a line: letters and digits, diacritics and punctuation excluded."""
    return max(1, sum(1 for ch in text if unicodedata.category(ch)[0] in "LN"))


def wrap(text, max_chars, max_lines=2):
    """Split a script line into cues of at most max_lines lines of ~max_chars, balanced."""
    words = text.split()
    if not words:
        return []
    if len(text) <= max_chars or len(words) == 1:
        return [text]
    if len(text) <= max_chars * max_lines:
        best, best_score = None, None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if len(a) > max_chars + 4 or len(b) > max_chars + 4:
                continue
            score = abs(len(a) - len(b))
            if best_score is None or score < best_score:
                best, best_score = f"{a}\n{b}", score
        if best:
            return [best]
    # too long for one cue: split in halves on the word nearest the middle
    mid, acc, cut = len(text) / 2, 0, 1
    for i, w in enumerate(words):
        acc += len(w) + 1
        if acc >= mid:
            cut = max(1, i + 1)
            break
    return wrap(" ".join(words[:cut]), max_chars, max_lines) + wrap(" ".join(words[cut:]), max_chars, max_lines)


def read_script(path):
    if not Path(path).exists():
        return []
    lines = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def time_cues(texts, speech, snap=0.35):
    """Place cue texts on the speech intervals of a voice-over.

    Equal count -> one cue per detected phrase. Otherwise the cues share the
    speech time by spoken weight, and each boundary snaps to a nearby pause so
    a subtitle never changes mid-word when a breath was available.
    """
    if not texts or not speech:
        return []
    if len(texts) == len(speech):
        return [{"start": s, "end": e, "text": t} for t, (s, e) in zip(texts, speech)]
    acc, edges = 0.0, []
    for s, e in speech:
        edges.append((acc, s, e))
        acc += e - s
    total = acc
    pauses = [c0 + (e - s) for c0, s, e in edges[:-1]]

    def to_real(p, side):
        for c0, s, e in edges:
            seg = e - s
            if p < c0 + seg - 1e-9 or (side == "end" and p <= c0 + seg + 1e-9):
                return s + max(0.0, p - c0)
        return edges[-1][2]

    ws = [weight(t) for t in texts]
    wsum, cum, bounds = sum(ws), 0, [0.0]
    for w in ws[:-1]:
        cum += w
        b = total * cum / wsum
        near = min(pauses, key=lambda x: abs(x - b), default=None)
        if near is not None and abs(near - b) <= snap and near > bounds[-1]:
            b = near
        bounds.append(b)
    bounds.append(total)
    return [{"start": round(to_real(bounds[i], "start"), 3), "end": round(to_real(bounds[i + 1], "end"), 3), "text": t}
            for i, t in enumerate(texts)]


def tidy_cues(cues, min_dur=0.7, bridge=0.25):
    for i, c in enumerate(cues):
        nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
        if nxt is not None and nxt - c["end"] < bridge:
            c["end"] = nxt - 0.04
        if c["end"] - c["start"] < min_dur:
            c["end"] = c["start"] + min_dur if nxt is None else min(c["start"] + min_dur, nxt - 0.04)
        c["start"], c["end"] = round(c["start"], 3), round(c["end"], 3)
    return cues


def cmd_subs(args):
    pr = Project(args.project)
    srt = pr.dir / pr.edit.get("subtitles", {}).get("file", "subs.srt")
    if srt.exists() and not args.force:
        die(f"{pr.rel(srt)} existe déjà (peut-être corrigé à la main). --force pour le régénérer.")
    style = pr.brand["subtitles"]
    lines = read_script(pr.dir / "script.txt")
    if not lines:
        die("script.txt est vide.")
    texts = [cue for line in lines for cue in wrap(line, style.get("max_chars", 28), style.get("max_lines", 2))]
    voice = pr.edit.get("voice") or next(iter(pr.inputs("voice")), None)
    offset = float(pr.edit.get("voice_offset", 0.0))
    if voice:
        info = pr.probe["files"].get(voice)
        speech = info["speech"] if info and "speech" in info else detect_speech(pr.dir / voice)
        cues = time_cues(texts, speech)
        for c in cues:
            c["start"] += offset
            c["end"] += offset
        how = f"calé sur les pauses de {voice} ({len(speech)} phrases détectées pour {len(texts)} sous-titres)"
    else:
        t, cues = offset, []
        for tx in texts:
            d = max(1.0, weight(tx) / 14.0)
            cues.append({"start": t, "end": t + d, "text": tx})
            t += d + 0.08
        how = "sans voix off : durée estimée à 14 caractères/seconde"
    write_srt(srt, tidy_cues(cues))
    say(f"{pr.rel(srt)} écrit : {len(cues)} sous-titres, {how}.")
    say("Relis-le : c'est un fichier texte, les horaires et le texte se corrigent à la main.")


def ass_style(name, st, font, scale=1.0):
    box = st.get("box", False)
    return ("Style: {name},{font},{size},{primary},&H000000FF,{outline},{back},{bold},0,0,0,100,100,{spacing},0,"
            "{border},{ow},{shadow},{align},{ml},{mr},{mv},1").format(
        name=name, font=font, size=int(st["size"] * scale),
        primary=hex_to_ass(st.get("color", "#FFFFFF")),
        outline=hex_to_ass(st.get("box_color", "#000000") if box else st.get("outline_color", "#000000"),
                           st.get("box_alpha", 0.45) if box else 0.0),
        back=hex_to_ass(st.get("shadow_color", "#000000"), st.get("shadow_alpha", 0.55)),
        bold=-1 if st.get("bold") else 0, spacing=st.get("spacing", 0),
        border=3 if box else 1, ow=st.get("box_pad", 14) if box else st.get("outline", 0),
        shadow=st.get("shadow", 2), align=st.get("align", 2),
        ml=st.get("margin_lr", 140), mr=st.get("margin_lr", 140), mv=st.get("margin_v", 540))


def ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_text(t):
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def build_ass(brand, cues, texts, warn):
    sub_st, title_st = brand["subtitles"], brand["titles"]
    sub_font, title_font = resolve_font(brand, sub_st, warn), resolve_font(brand, title_st, warn)
    fi, fo = sub_st.get("fade_in_ms", 90), sub_st.get("fade_out_ms", 60)
    out = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "WrapStyle: 2",
           "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
           "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
           "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
           "MarginL, MarginR, MarginV, Encoding",
           ass_style("Sub", sub_st, sub_font), ass_style("Title", title_st, title_font), "",
           "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for c in cues:
        out.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Sub,,0,0,0,,"
                   f"{{\\fad({fi},{fo})}}{ass_text(c['text'])}")
    pos_tag = {"top": "\\an8", "center": "\\an5", "bottom": "\\an2"}
    for t in texts:
        tag = pos_tag.get(t.get("pos", "top"), "\\an8")
        out.append(f"Dialogue: 1,{ass_time(t['start'])},{ass_time(t['end'])},Title,,0,0,0,,"
                   f"{{{tag}\\fad(180,140)}}{ass_text(t['text'])}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- planning

def target_length(pr):
    probe = pr.probe["files"]
    voice = pr.edit.get("voice")
    if voice and voice in probe:
        sp = probe[voice].get("speech") or [[0, probe[voice]["duration"]]]
        return frames(float(pr.edit.get("voice_offset", 0)) + sp[-1][1] + pr.brand["pacing"].get("tail", 0.5))
    return frames(pr.brand["pacing"].get("default_length", 20.0))


def cut_points(length, cue_starts, pacing, rng):
    """Shot boundaries: prefer phrase starts, keep inside [min, max], vary the rhythm."""
    lo, hi, avg = pacing["shot_min"], pacing["shot_max"], pacing["shot_avg"]
    cuts, t = [], 0.0
    while length - t > hi:
        want = t + avg * rng.uniform(0.72, 1.32)
        cands = [c for c in cue_starts if t + lo <= c <= t + hi]
        nxt = min(cands, key=lambda c: abs(c - want)) if cands else want
        if length - nxt < lo:
            break
        cuts.append(frames(nxt))
        t = nxt
    return cuts


def cmd_plan(args):
    pr = Project(args.project)
    if pr.edit.get("shots") and not args.force:
        die("edit.json contient déjà des plans. --force pour repartir d'un brouillon automatique.")
    if not pr.probe_path.exists():
        die("lance `probe` d'abord.")
    probe, pacing = pr.probe["files"], pr.brand["pacing"]
    rng = seeded_rng(pr.edit.get("title"), pr.edit.get("brand"))
    if not pr.edit.get("voice"):
        pr.edit["voice"] = next(iter(pr.inputs("voice")), None)
    if not pr.edit.get("music") and pr.inputs("music"):
        pr.edit["music"] = {"file": pr.inputs("music")[0], "start": 0.0}
    videos = [v for v in pr.inputs("video") if "error" not in probe.get(v, {"error": 1})]
    photos = [p for p in pr.inputs("photo") if "error" not in probe.get(p, {"error": 1})]
    if not videos and not photos:
        die("aucune vidéo ni photo exploitable dans inputs/.")

    length = target_length(pr)
    card = pr.brand.get("end_card", {})
    card_on = bool(pr.edit.get("end_card")) and card.get("enabled") and (Path(pr.brand["_dir"]) / card.get("logo", "logo.png")).exists()
    main_len = length
    srt = pr.dir / pr.edit.get("subtitles", {}).get("file", "subs.srt")
    cue_starts = [c["start"] for c in read_srt(srt)] if srt.exists() else []
    bounds = [0.0] + cut_points(main_len, cue_starts, pacing, rng) + [main_len]

    # media order: the first frame must move, so open on a video when there is one
    media = videos[:1] + [m for pair in zip(photos, videos[1:]) for m in pair] + photos[len(videos[1:]):] + videos[1 + len(photos):]
    if not videos:
        media = photos
    cursors = {v: 0.4 for v in videos}
    moves = [m for m in pacing.get("photo_moves", PHOTO_MOVES) if m != "static"] or ["push_in"]
    rng.shuffle(moves)
    shots, last_move = [], None
    for i in range(len(bounds) - 1):
        dur = frames(bounds[i + 1] - bounds[i])
        src = media[i % len(media)]
        shot = {"src": src, "dur": dur, "transition": "cut"}
        if src in cursors:
            info = probe[src]
            scenes = [s + 0.15 for s in info.get("scenes", []) if s + 0.15 >= cursors[src]]
            start = scenes[0] if scenes and scenes[0] + dur <= info["duration"] - 0.05 else cursors[src]
            if info["duration"] < dur + 0.1:
                # clip shorter than its slot: a slight slow-down reads as intent, a freeze frame does not
                shot["speed"] = max(0.75, round((info["duration"] - 0.1) / dur, 3))
            if start + dur * shot.get("speed", 1.0) > info["duration"] - 0.05:
                start = max(0.0, info["duration"] - dur * shot.get("speed", 1.0) - 0.05)
            shot["in"] = round(start, 2)
            cursors[src] = start + dur * shot.get("speed", 1.0)
            if info.get("width", 0) > info.get("height", 0):
                shot["focus_x"] = 0.5
        else:
            mv = next((m for m in moves[i % len(moves):] + moves if m != last_move), moves[0])
            shot["move"], last_move = mv, mv
        shots.append(shot)

    # transitions: cuts by default, one soft transition into the payoff, never mechanical
    allowed = [t for t in pacing["allowed_transitions"] if t != "cut"]
    if len(shots) >= 4 and allowed:
        shots[-1]["transition"] = allowed[0]
        shots[-1]["transition_dur"] = pacing.get("transition_dur", 0.4)
    if card_on:
        shots.append({"type": "card", "dur": frames(card.get("dur", 1.8)),
                      "transition": card.get("transition", "fadeblack"),
                      "transition_dur": card.get("transition_dur", 0.5)})
    pr.edit["shots"] = shots
    pr.edit["_plan_note"] = ("Brouillon automatique : vérifier les planches contact, choisir les vrais "
                             "points d'entrée (in), le focus_x des plans horizontaux, puis supprimer cette note.")
    save_json(pr.edit_path, pr.edit)
    say(f"edit.json : {len(shots)} plans pour {length:.1f}s"
        + (f", coupes calées sur {len(cue_starts)} débuts de phrase" if cue_starts else "") + ".")
    say("C'est un brouillon. Le montage se décide en regardant les rushes, pas ici.")


# --------------------------------------------------------------------------- check

def shot_kind(pr, shot):
    if shot.get("type") == "card":
        return "card"
    ext = Path(shot.get("src", "")).suffix.lower()
    return "photo" if ext in PHOTO_EXT else "video"


def timeline_length(shots):
    total = 0.0
    for i, s in enumerate(shots):
        total += s["dur"]
        if i and s.get("transition", "cut") != "cut":
            total -= s.get("transition_dur", 0.4)
    return frames(total)


def lint(pr, dry=False):
    """Return (errors, warnings). Errors block a render; warnings are taste."""
    E, W = [], []
    ed, pacing, probe = pr.edit, pr.brand["pacing"], pr.probe["files"]
    shots = ed.get("shots", [])
    if not shots:
        E.append("aucun plan dans edit.json (lance `plan` ou écris-les).")
        return E, W
    allowed = set(pacing["allowed_transitions"])
    used_ranges = {}
    prev_trans, prev_move, durs = None, None, []
    for i, s in enumerate(shots):
        n, kind = f"plan {i + 1}", shot_kind(pr, s)
        d = s.get("dur", 0)
        if d <= 0:
            E.append(f"{n} : dur manquante ou nulle.")
            continue
        durs.append(round(d, 2))
        if kind != "card":
            src = pr.dir / s.get("src", "")
            if not src.exists():
                (W if dry else E).append(f"{n} : fichier introuvable {s.get('src')}.")
            info = probe.get(s.get("src"), {})
            if kind == "video":
                sp = s.get("speed", 1.0)
                if not 0.25 <= sp <= 2.0:
                    E.append(f"{n} : speed {sp} hors de [0.25, 2].")
                need = s.get("in", 0) + d * sp
                if info.get("duration") and need > info["duration"] + 0.02:
                    E.append(f"{n} : demande {need:.2f}s mais {s['src']} ne dure que {info['duration']:.2f}s.")
                for a, b in used_ranges.get(s["src"], []):
                    if s.get("in", 0) < b and need > a:
                        W.append(f"{n} : réutilise un passage déjà monté de {s['src']} ({a:.1f}-{b:.1f}s).")
                used_ranges.setdefault(s["src"], []).append((s.get("in", 0), need))
                if d < pacing["shot_min"] - 0.01:
                    W.append(f"{n} : {d:.2f}s, plus court que le minimum de la marque ({pacing['shot_min']}s).")
                if d > pacing["shot_max"] + 0.01:
                    W.append(f"{n} : {d:.2f}s, plus long que le maximum de la marque ({pacing['shot_max']}s).")
                if info.get("hdr"):
                    W.append(f"{n} : source HDR, conversion SDR automatique (vérifier la couleur en preview).")
            if kind == "photo":
                mv = s.get("move", "push_in")
                if mv not in PHOTO_MOVES:
                    E.append(f"{n} : mouvement « {mv} » inconnu ({', '.join(PHOTO_MOVES)}).")
                if mv == prev_move and mv != "static":
                    W.append(f"{n} : même mouvement photo que le plan précédent ({mv}), ça se voit.")
                prev_move = mv
                if d < pacing.get("photo_min", 1.8):
                    W.append(f"{n} : photo de {d:.2f}s, trop courte pour que le mouvement se lise.")
            else:
                prev_move = None
        if i == 0:
            if kind == "photo" and s.get("move") == "static":
                W.append("plan 1 : photo fixe en ouverture. La première image doit bouger.")
            continue
        tr = s.get("transition", "cut")
        if tr not in allowed:
            E.append(f"{n} : transition « {tr} » non autorisée pour {pr.brand['name']} ({', '.join(sorted(allowed))}).")
        elif tr != "cut":
            td = s.get("transition_dur", pacing.get("transition_dur", 0.4))
            if td >= min(d, shots[i - 1]["dur"]) - 0.1:
                E.append(f"{n} : transition de {td}s trop longue pour des plans de {shots[i - 1]['dur']}s / {d}s.")
            if tr == prev_trans:
                W.append(f"{n} : deux « {tr} » de suite. Une transition répétée devient un effet.")
        prev_trans = tr
    non_cut = sum(1 for s in shots[1:] if s.get("transition", "cut") != "cut")
    if len(shots) > 1 and non_cut / (len(shots) - 1) > pacing.get("max_transition_ratio", 0.3) + 1e-9:
        W.append(f"{non_cut} transitions sur {len(shots) - 1} raccords : au-dessus du maximum de la marque "
                 f"({int(pacing.get('max_transition_ratio', 0.3) * 100)} %). Les coupes franches font le premium.")
    for i in range(len(durs) - 2):
        if abs(durs[i] - durs[i + 1]) < 0.08 and abs(durs[i + 1] - durs[i + 2]) < 0.08:
            W.append(f"plans {i + 1}-{i + 3} : trois durées identiques ({durs[i]}s). Rythme mécanique.")
            break
    total = timeline_length(shots)
    voice = ed.get("voice")
    if voice and voice in probe:
        sp = probe[voice].get("speech") or [[0, probe[voice]["duration"]]]
        vend = float(ed.get("voice_offset", 0)) + sp[-1][1]
        if total < vend + 0.2:
            E.append(f"montage de {total:.2f}s mais la voix finit à {vend:.2f}s : la fin serait coupée.")
    elif voice and not (pr.dir / voice).exists():
        (W if dry else E).append(f"voix off introuvable : {voice}.")
    if ed.get("voice_preset") and ed["voice_preset"] not in VOICE_PRESETS:
        E.append(f"voice_preset « {ed['voice_preset']} » inconnu ({', '.join(VOICE_PRESETS)}).")
    music = ed.get("music")
    if music:
        mi = probe.get(music.get("file"), {})
        if not (pr.dir / music.get("file", "")).exists():
            (W if dry else E).append(f"musique introuvable : {music.get('file')}.")
        elif mi.get("duration") and mi["duration"] - float(music.get("start", 0)) < total:
            W.append(f"musique trop courte ({mi['duration']:.1f}s depuis {music.get('start', 0)}s) pour {total:.1f}s de montage.")
    if total > 90:
        W.append(f"durée {total:.1f}s : au-delà de 90s, très peu de Reels sont regardés jusqu'au bout.")
    srt = pr.dir / ed.get("subtitles", {}).get("file", "subs.srt")
    if ed.get("subtitles", {}).get("enabled", True):
        if not srt.exists():
            (W if dry else E).append(f"sous-titres activés mais {pr.rel(srt)} absent (lance `subs`).")
        else:
            mc = pr.brand["subtitles"].get("max_chars", 28)
            for k, c in enumerate(read_srt(srt), 1):
                if any(len(l) > mc + 6 for l in c["text"].splitlines()):
                    W.append(f"sous-titre {k} : ligne de plus de {mc + 6} caractères, illisible sur téléphone.")
                if c["end"] > total:
                    W.append(f"sous-titre {k} dépasse la fin du montage.")
    return E, W


def cmd_check(args):
    pr = Project(args.project)
    E, W = lint(pr)
    for e in E:
        say(f"  ✗ {e}")
    for w in W:
        say(f"  ! {w}")
    shots = pr.edit.get("shots", [])
    if shots:
        say(f"\n{len(shots)} plans, {timeline_length(shots):.2f}s. "
            f"{len(E)} erreur(s), {len(W)} remarque(s).")
    if E:
        raise SystemExit(1)


# --------------------------------------------------------------------------- render

def smoothstep(n_frames):
    t = f"min(on/{max(n_frames - 1, 1)},1)"
    return f"(({t})*({t})*(3-2*({t})))"


def photo_motion(move, n, amount, fx=0.5, fy=0.5):
    e = smoothstep(n)
    z_in, z_out, z_hold = f"1+{amount}*{e}", f"1+{amount}*(1-{e})", f"{1 + amount}"
    cx, cy = f"(iw-iw/zoom)*{fx}", f"(ih-ih/zoom)*{fy}"
    table = {
        "push_in": (z_in, cx, cy),
        "pull_out": (z_out, cx, cy),
        "drift_left": (z_hold, f"(iw-iw/zoom)*(1-{e})", cy),
        "drift_right": (z_hold, f"(iw-iw/zoom)*{e}", cy),
        "drift_up": (z_hold, cx, f"(ih-ih/zoom)*(1-{e})"),
        "drift_down": (z_hold, cx, f"(ih-ih/zoom)*{e}"),
        "static": (f"1+{min(amount, 0.02)}*{e}", cx, cy),
    }
    return table.get(move, table["push_in"])


def look_chain(look, override, lut, w):
    g = dict(look)
    g.update(override or {})
    f = []
    dn = g.get("denoise")
    if dn == "light":
        f.append("hqdn3d=1.2:1.2:3:3")
    elif dn == "medium":
        f.append("hqdn3d=2.5:2:5:4")
    elif dn == "strong":
        f.append("nlmeans=s=2.5:p=7:r=9")
    f.append("eq=contrast={:.3f}:brightness={:.3f}:saturation={:.3f}:gamma={:.3f}".format(
        g.get("contrast", 1.0), g.get("brightness", 0.0), g.get("saturation", 1.0), g.get("gamma", 1.0)))
    wm = g.get("warmth", 0.0)
    if wm:
        f.append("colorbalance=rs={:.3f}:bs={:.3f}:rm={:.3f}:bm={:.3f}:rh={:.3f}:bh={:.3f}".format(
            wm * 0.6, -wm * 0.6, wm, -wm, wm * 0.5, -wm * 0.5))
    if lut and g.get("lut", True) is not False:
        f.append(f"lut3d=file={lut}:interp=tetrahedral")
    if g.get("sharpen"):
        amt = g["sharpen"] * (0.6 if w < 1000 else 1.0)
        f.append(f"unsharp=5:5:{amt:.2f}:5:5:0")
    if g.get("vignette"):
        f.append(f"vignette=angle={0.35 + 0.35 * g['vignette']:.3f}")
    if g.get("grain"):
        f.append(f"noise=c0s={int(g['grain'])}:c0f=t+u")
    return f


HDR_TO_SDR = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
              "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


class Renderer:
    def __init__(self, pr, mode, dry):
        self.pr, self.mode, self.dry = pr, mode, dry
        self.W, self.H = SIZES["preview" if mode == "preview" else "final"]
        self.log, self.warn = [], []
        self.brand, self.edit = pr.brand, pr.edit
        self.probe = pr.probe["files"]
        # a project may soften the brand look, e.g. footage that arrives already graded
        self.look = {**self.brand["look"], **pr.edit.get("look", {})}
        self.shots = [dict(s) for s in pr.edit["shots"]]
        for s in self.shots:
            s["dur"] = frames(s["dur"])
            if "transition_dur" in s:
                s["transition_dur"] = frames(s["transition_dur"])
            elif s.get("transition", "cut") != "cut":
                s["transition_dur"] = frames(self.brand["pacing"].get("transition_dur", 0.4))
        self.length = timeline_length(self.shots)
        self.has_voice = bool(self.edit.get("voice"))
        self.has_music = bool(self.edit.get("music"))

    # -- paths relative to the project dir: filter arguments never see a space or a colon
    def w(self, name):
        return f"out/work/{name}"

    def enc_args(self, final_output=False):
        if final_output and self.mode == "final":
            return ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-profile:v", "high", "-level:v", "4.1",
                    "-pix_fmt", "yuv420p", "-maxrate", "16M", "-bufsize", "32M", "-g", "60", "-r", str(FPS),
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
        if final_output:
            return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-r", str(FPS)]
        if self.mode == "final":
            return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p"]

    def prepare(self):
        work = self.pr.work
        work.mkdir(parents=True, exist_ok=True)
        fonts = work / "fonts"
        fonts.mkdir(exist_ok=True)
        bfonts = brand_fonts_dir(self.brand)
        if bfonts.exists():
            for f in bfonts.iterdir():
                if f.suffix.lower() in (".ttf", ".otf"):
                    shutil.copy2(f, fonts / f.name)
        self.lut = None
        lut = self.brand["look"].get("lut")
        if lut and (Path(self.brand["_dir"]) / lut).exists():
            shutil.copy2(Path(self.brand["_dir"]) / lut, work / "brand.cube")
            self.lut = self.w("brand.cube")
        elif lut:
            self.warn.append(f"LUT {lut} absente du dossier de la marque, étalonnage sans LUT.")

    def ambient_gain(self, shot):
        if "audio_gain_db" in shot:
            return shot["audio_gain_db"]
        return self.brand["audio"].get("ambient_db", -24) if (self.has_voice or self.has_music) else 0

    def shot_cmd(self, i, s):
        W, H, D = self.W, self.H, s["dur"]
        kind = shot_kind(self.pr, s)
        out = self.w(f"shot_{i:02d}.mkv")
        look = look_chain(self.look, s.get("grade"), self.lut, W)
        tail = ["tpad=stop_mode=clone:stop_duration=2", f"trim=duration={D:.4f}", "setpts=PTS-STARTPTS",
                "format=yuv420p", "setsar=1"]
        silent = ["-f", "lavfi", "-t", f"{D:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
        pre = []
        if kind == "card":
            card = self.brand.get("end_card", {})
            logo = Path(self.brand["_dir"]) / card.get("logo", "logo.png")
            inputs = ["-f", "lavfi", "-i", f"color=c={hex_to_ff(card.get('background', '#111111'))}:s={W}x{H}:r={FPS}:d={D:.4f}"]
            g = int(self.look.get("grain", 0))
            vf = ([f"noise=c0s={g}:c0f=t+u"] if g else []) + tail
            if logo.exists():
                inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{D:.4f}", "-i", str(logo)]
                lw = int(W * card.get("logo_width", 0.42))
                fc = (f"[1:v]scale={lw}:-1,format=rgba,fade=in:st=0.15:d=0.6:alpha=1[lg];"
                      f"[0:v][lg]overlay=(W-w)/2:(H-h)/2-{int(H * 0.03)},{','.join(vf)}[v];[2:a]anull[a]")
                inputs += silent
            else:
                self.warn.append(f"logo absent ({logo}) : carte de fin sans logo.")
                fc = f"[0:v]{','.join(vf)}[v];[1:a]anull[a]"
                inputs += silent
            return pre, ["-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", fc,
                         "-map", "[v]", "-map", "[a]", *self.enc_args(), "-c:a", "pcm_s16le", out]

        src = s["src"]
        fx, fy = s.get("focus_x", 0.5), s.get("focus_y", 0.5)
        if kind == "photo":
            n = int(round(D * FPS))
            amount = s.get("amount", self.brand["pacing"].get("photo_zoom", 0.07))
            z, x, y = photo_motion(s.get("move", "push_in"), n, amount, fx, fy)
            vf = [f"scale={2 * W}:{2 * H}:force_original_aspect_ratio=increase:flags=lanczos",
                  f"crop={2 * W}:{2 * H}:(iw-{2 * W})*{fx}:(ih-{2 * H})*{fy}",
                  f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{H}:fps={FPS}", *look, *tail]
            fc = f"[0:v]{','.join(vf)}[v];[1:a]anull[a]"
            return pre, ["-y", "-hide_banner", "-loglevel", "error", "-i", src, *silent, "-filter_complex", fc,
                         "-map", "[v]", "-map", "[a]", *self.enc_args(), "-c:a", "pcm_s16le", out]

        info = self.probe.get(src, {})
        sp = s.get("speed", 1.0)
        t_in, src_len = s.get("in", 0.0), D * sp + 0.1
        vf = []
        if info.get("hdr"):
            vf.append(HDR_TO_SDR)
        if s.get("stabilize"):
            trf = self.w(f"shot_{i:02d}.trf")
            pre.append(["-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t_in:.3f}", "-t", f"{src_len:.3f}",
                        "-i", src, "-an", "-vf", f"vidstabdetect=shakiness=5:accuracy=15:result={trf}", "-f", "null", "-"])
            vf.append(f"vidstabtransform=input={trf}:smoothing=14:zoom=4:optzoom=0:interpol=bicubic")
        if s.get("interpolate") and sp < 1.0:
            # motion-compensated in-between frames, so slow motion from 30 fps footage stays fluid
            vf.append(f"minterpolate=fps={math.ceil(FPS / sp)}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
        vf += [f"setpts=(PTS-STARTPTS)/{sp}", f"fps={FPS}"]
        zoom = s.get("zoom", 1.0)
        if any(k in s for k in ("zoom_to", "focus_x_to", "focus_y_to")):
            # slow eased push, pull or pan on a video shot, computed on a 2x frame to avoid stepping
            n = int(round(D * FPS))
            e = smoothstep(n)
            z = f"{zoom}+({s.get('zoom_to', zoom)}-{zoom})*{e}"
            px = f"({fx}+({s.get('focus_x_to', fx)}-{fx})*{e})"
            py = f"({fy}+({s.get('focus_y_to', fy)}-{fy})*{e})"
            vf += [f"scale={2 * W}:{2 * H}:force_original_aspect_ratio=increase:flags=lanczos",
                   f"crop={2 * W}:{2 * H}:(iw-{2 * W})*0.5:(ih-{2 * H})*0.5",
                   f"zoompan=z='{z}':x='(iw-iw/zoom)*{px}':y='(ih-ih/zoom)*{py}':d=1:s={W}x{H}:fps={FPS}"]
        else:
            vf += [f"scale={int(W * zoom)}:{int(H * zoom)}:force_original_aspect_ratio=increase:force_divisible_by=2:flags=lanczos",
                   f"crop={W}:{H}:(iw-{W})*{fx}:(ih-{H})*{fy}"]
        vf += [*look, *tail]
        gain = self.ambient_gain(s)
        use_src_audio = info.get("has_audio", True) and s.get("keep_audio", True) and gain > -60
        if use_src_audio:
            tempo = f"atempo={max(0.5, min(2.0, sp))}," if sp != 1.0 else ""
            af = (f"[0:a]asetpts=PTS-STARTPTS,{tempo}volume={gain}dB,aresample=48000,"
                  f"aformat=sample_fmts=s16:channel_layouts=stereo,apad,atrim=0:{D:.4f}[a]")
            inputs = ["-ss", f"{t_in:.3f}", "-t", f"{src_len:.3f}", "-i", src]
        else:
            af = "[1:a]anull[a]"
            inputs = ["-ss", f"{t_in:.3f}", "-t", f"{src_len:.3f}", "-i", src, *silent]
        fc = f"[0:v]{','.join(vf)}[v];{af}"
        return pre, ["-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", fc,
                     "-map", "[v]", "-map", "[a]", *self.enc_args(), "-c:a", "pcm_s16le", out]

    def fold(self, kind):
        """Chain the shots with cuts (concat) or transitions (xfade / acrossfade)."""
        parts, n = [], len(self.shots)
        for i in range(n):
            norm = f"fps={FPS},settb=AVTB" if kind == "v" else "aresample=48000"
            parts.append(f"[{i}:{kind}]{norm}[{kind}in{i}]")
        cur, length = f"[{kind}in0]", self.shots[0]["dur"]
        for i in range(1, n):
            s, nxt, out = self.shots[i], f"[{kind}in{i}]", f"[{kind}f{i}]"
            tr = s.get("transition", "cut")
            if tr == "cut":
                flags = "v=1:a=0" if kind == "v" else "v=0:a=1"
                post = ",settb=AVTB" if kind == "v" else ""
                parts.append(f"{cur}{nxt}concat=n=2:{flags}{post}{out}")
                length += s["dur"]
            else:
                d = s["transition_dur"]
                if kind == "v":
                    parts.append(f"{cur}{nxt}xfade=transition={XFADE[tr]}:duration={d:.4f}:offset={length - d:.4f}{out}")
                else:
                    parts.append(f"{cur}{nxt}acrossfade=d={d:.4f}:c1=tri:c2=tri{out}")
                length += s["dur"] - d
            cur = out
        return parts, cur

    def shot_inputs(self, audio_only=False):
        args = []
        for i in range(len(self.shots)):
            args += (["-vn"] if audio_only else []) + ["-i", self.w(f"shot_{i:02d}.mkv")]
        return args

    def mix_graph(self, ambient_idx, voice_idx, music_idx):
        L, A, parts, mix = self.length, self.brand["audio"], [], []
        parts.append(f"[{ambient_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=0:{L:.4f}[amb]")
        mix.append("[amb]")
        if voice_idx is not None:
            v = self.edit["voice"]
            lufs = self.probe.get(v, {}).get("lufs")
            gain = (A.get("voice_lufs", -16) - lufs) if lufs is not None else 0.0
            delay = int(float(self.edit.get("voice_offset", 0)) * 1000)
            chain = VOICE_PRESETS[self.edit.get("voice_preset") or A.get("voice_preset", "warm")]
            split = "asplit=2[vo][vosc]" if music_idx is not None else "anull[vo]"
            parts.append(f"[{voice_idx}:a]{chain},volume={gain:.2f}dB,adelay={delay}:all=1,"
                         f"aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=0:{L:.4f},{split}")
            mix.append("[vo]")
        if music_idx is not None:
            m = self.edit["music"]
            lufs = self.probe.get(m["file"], {}).get("lufs")
            target = A.get("music_lufs", -20) if voice_idx is not None else A.get("music_alone_lufs", -15)
            gain = m.get("gain_db", (target - lufs) if lufs is not None else -12.0)
            fo = A.get("music_fade_out", 2.0)
            parts.append(f"[{music_idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                         f"atrim=0:{L:.4f},asetpts=PTS-STARTPTS,volume={gain:.2f}dB,afade=t=in:d={m.get('fade_in', 0.6)},"
                         f"afade=t=out:st={max(0.0, L - fo):.3f}:d={fo},apad,atrim=0:{L:.4f}[mus0]")
            if voice_idx is not None:
                parts.append(f"[mus0][vosc]sidechaincompress=threshold=0.025:ratio={A.get('duck_ratio', 6)}:"
                             f"attack=30:release=450:knee=4[mus]")
            else:
                parts.append("[mus0]anull[mus]")
            mix.append("[mus]")
        parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first[pre]")
        return ";".join(parts)

    def run(self):
        pr, dry = self.pr, self.dry
        self.prepare()
        say(f"Rendu {self.mode} {self.W}x{self.H}, {len(self.shots)} plans, {self.length:.2f}s")

        say("1/5 plans (cadrage 9:16, étalonnage, mouvements photo)")
        for i, s in enumerate(self.shots):
            pre, cmd = self.shot_cmd(i, s)
            # cache: a shot whose exact command already produced its file is not re-rendered
            stamp = pr.work / f"shot_{i:02d}.cmd"
            key = "\n".join(shlex.join(c) for c in pre + [cmd])
            if not dry and (pr.dir / cmd[-1]).exists() and stamp.exists() and stamp.read_text() == key:
                say(f"  plan {i + 1} : inchangé, réutilisé")
                self.log.append("# " + shlex.join(["ffmpeg", *cmd]))
                continue
            for p in pre:
                ffrun(["ffmpeg", *p], pr.dir, dry, self.log)
            ffrun(["ffmpeg", *cmd], pr.dir, dry, self.log)
            if not dry:
                stamp.write_text(key)

        say("2/5 son direct des plans")
        parts, cur = self.fold("a")
        ffrun(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *self.shot_inputs(True),
               "-filter_complex", ";".join(parts + [f"{cur}anull[aout]"]),
               "-map", "[aout]", "-c:a", "pcm_s16le", self.w("ambient.wav")], pr.dir, dry, self.log)

        say("3/5 mixage (voix traitée, musique sous la voix, ambiance)")
        inputs, idx = ["-i", self.w("ambient.wav")], 1
        voice_idx = music_idx = None
        if self.has_voice:
            inputs += ["-i", self.edit["voice"]]
            voice_idx, idx = idx, idx + 1
        if self.has_music:
            inputs += ["-ss", f"{float(self.edit['music'].get('start', 0)):.3f}", "-i", self.edit["music"]["file"]]
            music_idx, idx = idx, idx + 1
        A = self.brand["audio"]
        target = f"I={A.get('target_lufs', -14)}:TP={A.get('true_peak', -1.5)}:LRA={A.get('lra', 11)}"
        graph = self.mix_graph(0, voice_idx, music_idx)
        ffrun(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", graph,
               "-map", "[pre]", "-c:a", "pcm_s24le", self.w("premix.wav")], pr.dir, dry, self.log)
        if self.mode == "final" and not dry:
            m = measure_loudness(pr.dir / self.w("premix.wav"))
            loud = (f"loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
                    f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
                    f"offset={m['target_offset']}:linear=true")
        else:
            loud = f"loudnorm={target}"
            if self.mode == "final":
                self.log.append("# (final) loudnorm en deux passes : mesure de premix.wav puis linear=true")
        ffrun(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", self.w("premix.wav"),
               "-af", f"{loud},aresample=48000", "-c:a", "pcm_s16le", self.w("mix.wav")], pr.dir, dry, self.log)

        say("4/5 assemblage, sous-titres, export")
        cues = []
        sub = self.edit.get("subtitles", {})
        srt = pr.dir / sub.get("file", "subs.srt")
        if sub.get("enabled", True) and srt.exists():
            cues = read_srt(srt)
        ass = build_ass(self.brand, cues, self.edit.get("texts", []), self.warn)
        if not dry:
            (pr.work / "subs.ass").write_text(ass, encoding="utf-8")
        parts, cur = self.fold("v")
        n = len(self.shots)
        vchain = f"{cur}ass=filename={self.w('subs.ass')}:fontsdir={self.w('fonts')}:shaping=complex,format=yuv420p[vout]"
        out_dir = pr.out
        stem = slugify(self.edit.get("title", "reel"))
        k = 1
        while (out_dir / f"{stem}-{self.mode}-v{k}.mp4").exists():
            k += 1
        final = f"out/{stem}-{self.mode}-v{k}.mp4"
        abitrate = "256k" if self.mode == "final" else "128k"
        ffrun(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *self.shot_inputs(), "-i", self.w("mix.wav"),
               "-filter_complex", ";".join(parts + [vchain]), "-map", "[vout]", "-map", f"{n}:a",
               *self.enc_args(final_output=True), "-c:a", "aac", "-b:a", abitrate, "-ar", "48000",
               "-t", f"{self.length:.4f}", "-movflags", "+faststart", final], pr.dir, dry, self.log)

        say("5/5 couverture")
        cover_t = self.edit.get("cover_at")
        if cover_t is None:
            cover_t = min(1.0, self.length / 3)
        ffrun(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{cover_t:.3f}", "-i", final,
               "-frames:v", "1", "-q:v", "2", f"out/{stem}-{self.mode}-v{k}-cover.jpg"], pr.dir, dry, self.log)

        script = pr.work / "commands.sh"
        if not dry:
            script.write_text("#!/bin/sh\n# commandes exactes de ce rendu, depuis le dossier du projet\nset -e\n"
                              + "\n".join(self.log) + "\n", encoding="utf-8")
        for w_ in dict.fromkeys(self.warn):
            say(f"  ! {w_}")
        if not dry:
            say(f"\nFichier : {pr.dir / final}")
        return final


def cmd_render(args):
    pr = Project(args.project)
    mode = "final" if args.final else "preview"
    E, W = lint(pr, dry=args.dry_run)
    for w in W:
        say(f"  ! {w}")
    if E:
        for e in E:
            say(f"  ✗ {e}")
        die("corrige les erreurs avant le rendu (`check`).")
    if not args.dry_run:
        tool("ffmpeg")
    Renderer(pr, mode, args.dry_run).run()


# --------------------------------------------------------------------------- doctor

REQUIRED_FILTERS = {
    "ass": "sous-titres", "xfade": "transitions", "zoompan": "mouvements photo", "loudnorm": "niveau Instagram",
    "sidechaincompress": "musique sous la voix", "afftdn": "débruitage voix", "deesser": "sifflantes",
    "acompressor": "compression voix", "silencedetect": "calage des sous-titres", "eq": "étalonnage",
    "colorbalance": "température", "unsharp": "netteté", "hqdn3d": "débruitage image", "zscale": "HDR → SDR",
    "tonemap": "HDR → SDR", "vidstabdetect": "stabilisation", "lut3d": "LUT de marque", "noise": "grain",
}


def cmd_doctor(args):
    ok = True
    lines = []
    ff = shutil.which("ffmpeg")
    if not ff or not shutil.which("ffprobe"):
        lines.append("✗ ffmpeg/ffprobe absents : rien ne peut être rendu.")
        ok = False
    else:
        ver = subprocess.run([ff, "-version"], capture_output=True, text=True).stdout.splitlines()[0]
        lines.append(f"✓ {ver.split(' Copyright')[0]}")
        filt = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        have = set(re.findall(r"^\s*[TSC.]{3}\s+(\S+)", filt, re.M))
        missing = [f"{k} ({v})" for k, v in REQUIRED_FILTERS.items() if k not in have]
        if missing:
            ok = False
            lines.append("✗ filtres manquants : " + ", ".join(missing))
        else:
            lines.append(f"✓ {len(REQUIRED_FILTERS)} filtres nécessaires présents")
        conf = subprocess.run([ff, "-hide_banner", "-buildconf"], capture_output=True, text=True).stdout
        for lib in ("libass", "libfribidi", "libharfbuzz"):
            if f"--enable-{lib}" not in conf:
                ok = False
                lines.append(f"✗ ffmpeg compilé sans {lib} : l'arabe s'afficherait mal")
    for slug in list_brands():
        b = load_brand(slug)
        for key in ("subtitles", "titles"):
            fam = b[key]["font"]
            if brand_font_available(b, fam):
                lines.append(f"✓ {b['name']} : police {fam}")
            else:
                lines.append(f"! {b['name']} : police {fam} absente (repli {b[key].get('fallback_font', 'DejaVu Sans')})")
        logo = Path(b["_dir"]) / b.get("end_card", {}).get("logo", "logo.png")
        if b.get("end_card", {}).get("enabled") and not logo.exists():
            lines.append(f"! {b['name']} : logo absent ({logo.relative_to(HERE)}) — carte de fin sans logo")
    if args.quiet:
        bad = [l for l in lines if not l.startswith("✓")]
        blocking = [l for l in bad if l.startswith("✗")]
        if blocking:
            say("reelforge : NON PRÊT — " + " | ".join(blocking))
        else:
            say("reelforge : prêt" + (f", {len(bad)} point(s) à régler (polices, logos) : "
                                      "python3 skills/ig-montage/reelforge.py doctor" if bad else ""))
    else:
        say("\n".join(lines))
    if not ok and not args.quiet:
        raise SystemExit(1)


# --------------------------------------------------------------------------- selftest

def cmd_selftest(args):
    # cue placement: equal counts map one to one
    sp = [[0.3, 1.9], [2.3, 4.0], [4.4, 6.2]]
    c = time_cues(["أ ب", "ت ث", "ج ح"], sp)
    assert [x["start"] for x in c] == [0.3, 2.3, 4.4], c
    # unequal counts: boundaries snap to the pauses, never inside a word when a pause is close
    c = time_cues(["كلمة واحدة هنا", "و هذي الجملة الثانية الطويلة شوية"], sp)
    assert c[0]["start"] == 0.3 and c[-1]["end"] == 6.2, c
    assert any(abs(x["end"] - 1.9) < 1e-6 or abs(x["end"] - 4.0) < 1e-6 for x in c[:-1]), c
    # speech from silences
    assert speech_from_silences([(1.0, 1.5), (3.0, 3.4)], 5.0) == [[0.0, 1.0], [1.5, 3.0], [3.4, 5.0]]
    # wrapping keeps lines under the limit and splits long lines into several cues
    for cue in wrap("هذا عطر يدوم معاك نهار كامل و ما يقلقكش و ريحتو فخمة برشا برشا", 28):
        assert all(len(l) <= 32 for l in cue.split("\n")), cue
    # srt round trip
    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "reelforge_selftest.srt"
    write_srt(tmp, [{"start": 1.234, "end": 2.5, "text": "سطر\nثاني"}])
    back = read_srt(tmp)
    assert back[0]["start"] == 1.234 and back[0]["text"] == "سطر\nثاني", back
    tmp.unlink()
    # colors and timeline
    assert hex_to_ass("#F4E9D8") == "&H00D8E9F4"
    assert timeline_length([{"dur": 2}, {"dur": 2, "transition": "crossfade", "transition_dur": 0.5}]) == 3.5
    assert frames(1.01) == 30 / 30
    # every brand profile loads and has what the renderer reads
    for slug in list_brands():
        b = load_brand(slug)
        for key in ("look", "pacing", "audio", "subtitles", "titles"):
            assert key in b, (slug, key)
        assert set(b["pacing"]["allowed_transitions"]) <= set(XFADE) | {"cut"}, slug
        assert b["audio"].get("voice_preset", "warm") in VOICE_PRESETS, slug
        build_ass(b, [{"start": 0, "end": 1, "text": "تجربة"}], [{"start": 0, "end": 1, "text": "عنوان"}], [])
    say("selftest : ok")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Montage automatique de Reels avec FFmpeg.")
    ap.add_argument("--root", help="dossier des projets (défaut : <repo>/reels ou $REELFORGE_ROOT)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("doctor")
    p.add_argument("--quiet", action="store_true")
    p = sub.add_parser("new")
    p.add_argument("brand")
    p.add_argument("title")
    for name in ("probe", "check"):
        sub.add_parser(name).add_argument("project")
    for name in ("subs", "plan"):
        p = sub.add_parser(name)
        p.add_argument("project")
        p.add_argument("--force", action="store_true")
    p = sub.add_parser("render")
    p.add_argument("project")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--preview", action="store_true")
    g.add_argument("--final", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("selftest")
    args = ap.parse_args()
    {"doctor": cmd_doctor, "new": cmd_new, "probe": cmd_probe, "subs": cmd_subs, "plan": cmd_plan,
     "check": cmd_check, "render": cmd_render, "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    main()
