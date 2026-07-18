#!/usr/bin/env python3
"""
hacker.py — Termux edition. Cyberpunk terminal dashboard with REAL telemetry.

Ye version pichle wale se alag hai: numbers fake nahi hain.
Sab kuch tumhare apne device se aata hai:

    CPU        -> /proc/stat  (jiffy deltas)
    RAM        -> /proc/meminfo
    STORAGE    -> os.statvfs()
    BATTERY    -> termux-battery-status  ya  /sys/class/power_supply/
    TEMP       -> /sys/class/thermal/thermal_zone*/temp
    NETWORK    -> /proc/net/dev  (asli rx/tx bytes aur packets)
    IP / IFACE -> `ip -o addr`  (local command, koi traffic nahi)
    PROCESSES  -> /proc/<pid>/
    UPTIME     -> /proc/uptime
    LOAD       -> os.getloadavg()
    EVENT LOG  -> real events: process start/exit, battery change,
                  thermal shift, network spikes

Scope: sirf ye device. Koi remote scanning, koi exploitation, koi
internet call nahi. Ye ek system monitor hai jo Hollywood jaisa dikhta hai.

Install (Termux):
    pkg install python
    pkg install iproute2        # `ip` command ke liye (interfaces panel)
    pkg install termux-api      # optional, battery ke liye behtar
    # aur F-Droid se "Termux:API" app bhi chahiye battery ke liye

Run:
    python hacker.py

Controls (Termux extra-keys row use karo):
    q       quit
    space   pause
    c       cipher-break page (fullscreen toggle on wide screens)
    x       crack cycle ko turant recycle karo
    n / p   next / previous page  (chhoti screen pe)
    r       refresh interface list
    + / -   fps up / down
"""

import curses
import os
import re
import json
import time
import random
import socket
import datetime
import subprocess

# --------------------------------------------------------------------------- #
#  SECTION 1 — Real telemetry readers
#
#  Har reader defensive hai. Android /proc ke kuch hisson ko restrict karta
#  hai (khaas kar /proc/net Android 10+ pe), isliye har jagah fallback aur
#  "N/A" handling hai. Kabhi crash nahi hoga.
# --------------------------------------------------------------------------- #


def _read(path):
    """Read a file, return None on any failure (permission, missing, etc)."""
    try:
        with open(path, "r") as f:
            return f.read()
    except (OSError, IOError):
        return None


def _run(cmd, timeout=2):
    """Run a local command, return stdout or None. No network involved."""
    try:
        out = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


class CPUReader:
    """
    Real CPU usage from /proc/stat.

    /proc/stat ki pehli line cumulative jiffies deti hai. Usage nikalne ke
    liye do samples ka delta chahiye:  busy_delta / total_delta * 100
    """

    def __init__(self):
        self.prev = None
        self.cores = os.cpu_count() or 1

    def _snapshot(self):
        data = _read("/proc/stat")
        if not data:
            return None
        first = data.split("\n", 1)[0]
        if not first.startswith("cpu "):
            return None
        try:
            parts = [int(x) for x in first.split()[1:]]
        except ValueError:
            return None
        if len(parts) < 4:
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
        total = sum(parts)
        return total, idle

    def percent(self):
        snap = self._snapshot()
        if snap is None:
            return None
        if self.prev is None:
            self.prev = snap
            return None
        total_d = snap[0] - self.prev[0]
        idle_d = snap[1] - self.prev[1]
        self.prev = snap
        if total_d <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (total_d - idle_d) / total_d))

    def freq_mhz(self):
        """Current frequency of core 0, if the kernel exposes it."""
        raw = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
        if raw:
            try:
                return int(raw.strip()) / 1000.0
            except ValueError:
                pass
        return None


def read_memory():
    """
    Real RAM from /proc/meminfo.
    MemAvailable kernel ka apna estimate hai — MemFree se kahin accurate.
    """
    data = _read("/proc/meminfo")
    if not data:
        return None
    vals = {}
    for line in data.split("\n"):
        m = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if m:
            vals[m.group(1)] = int(m.group(2)) * 1024
    total = vals.get("MemTotal")
    if not total:
        return None
    avail = vals.get("MemAvailable", vals.get("MemFree", 0))
    used = total - avail
    return {
        "total": total,
        "used": used,
        "avail": avail,
        "percent": 100.0 * used / total,
        "swap_total": vals.get("SwapTotal", 0),
        "swap_used": vals.get("SwapTotal", 0) - vals.get("SwapFree", 0),
    }


def read_storage(path=None):
    """Real disk usage via statvfs. Termux home default."""
    if path is None:
        path = os.path.expanduser("~")
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    if total == 0:
        return None
    return {
        "path": path,
        "total": total,
        "used": used,
        "free": free,
        "percent": 100.0 * used / total,
    }


def read_battery():
    """
    Real battery. Do raste:
      1. termux-battery-status  (Termux:API app chahiye) -> best data
      2. /sys/class/power_supply/*/capacity -> fallback, aksar readable
    """
    out = _run("termux-battery-status")
    if out:
        try:
            d = json.loads(out)
            return {
                "percent": float(d.get("percentage", 0)),
                "status": str(d.get("status", "?")).upper(),
                "temp": d.get("temperature"),
                "source": "termux-api",
            }
        except (ValueError, TypeError):
            pass

    base = "/sys/class/power_supply"
    try:
        for name in sorted(os.listdir(base)):
            cap = _read(f"{base}/{name}/capacity")
            if cap:
                status = _read(f"{base}/{name}/status") or "?"
                try:
                    pct = float(cap.strip())
                except ValueError:
                    continue
                return {
                    "percent": pct,
                    "status": status.strip().upper(),
                    "temp": None,
                    "source": "sysfs",
                }
    except OSError:
        pass
    return None


def read_thermal():
    """Real temperatures from every readable thermal zone."""
    zones = []
    base = "/sys/class/thermal"
    try:
        names = sorted(n for n in os.listdir(base) if n.startswith("thermal_zone"))
    except OSError:
        return zones
    for n in names:
        raw = _read(f"{base}/{n}/temp")
        typ = _read(f"{base}/{n}/type")
        if raw:
            try:
                v = float(raw.strip())
            except ValueError:
                continue
            # Kernel milli-degrees ya degrees dono deta hai, normalize.
            if v > 1000:
                v /= 1000.0
            if 0 < v < 150:
                zones.append((typ.strip() if typ else n, v))
    return zones


class NetReader:
    """
    Real network counters from /proc/net/dev.

    Ye tumhare device ke asli rx/tx bytes aur packets hain. YouTube kholo
    to ye panel actually spike karega — koi random number nahi.

    Android 10+ kabhi kabhi /proc/net ko restrict karta hai; us case me
    panel honestly "restricted" bolega.
    """

    def __init__(self):
        self.prev = {}
        self.prev_t = None

    def _parse_proc(self):
        data = _read("/proc/net/dev")
        if not data:
            return None
        stats = {}
        for line in data.split("\n")[2:]:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            f = rest.split()
            if len(f) < 16:
                continue
            try:
                stats[name] = {
                    "rx_bytes": int(f[0]), "rx_packets": int(f[1]),
                    "tx_bytes": int(f[8]), "tx_packets": int(f[9]),
                }
            except ValueError:
                continue
        return stats or None

    def sample(self):
        """Return per-interface totals + rates (bytes/sec, packets/sec)."""
        stats = self._parse_proc()
        now = time.time()
        if stats is None:
            return None

        result = {}
        dt = (now - self.prev_t) if self.prev_t else 0
        for name, cur in stats.items():
            entry = dict(cur)
            old = self.prev.get(name)
            if old and dt > 0:
                entry["rx_bps"] = max(0, (cur["rx_bytes"] - old["rx_bytes"]) / dt)
                entry["tx_bps"] = max(0, (cur["tx_bytes"] - old["tx_bytes"]) / dt)
                entry["rx_pps"] = max(0, (cur["rx_packets"] - old["rx_packets"]) / dt)
                entry["tx_pps"] = max(0, (cur["tx_packets"] - old["tx_packets"]) / dt)
            else:
                entry.update(rx_bps=0, tx_bps=0, rx_pps=0, tx_pps=0)
            result[name] = entry

        self.prev = stats
        self.prev_t = now
        return result


def read_interfaces():
    """
    Real local IP addresses via `ip -o addr`. Ye sirf kernel se apne
    interfaces poochta hai — koi packet nahi jaata.
    """
    ifaces = []
    out = _run("ip -o addr show")
    if out:
        for line in out.strip().split("\n"):
            m = re.search(r"^\d+:\s+(\S+)\s+inet6?\s+(\S+)", line)
            if m:
                name, addr = m.group(1), m.group(2)
                if name != "lo":
                    ifaces.append((name, addr))
    if not ifaces:
        # Fallback: apne hostname ka local resolve (koi DNS traffic nahi)
        try:
            ifaces.append(("host", socket.gethostbyname(socket.gethostname())))
        except Exception:
            pass
    return ifaces


def read_uptime():
    """Real uptime seconds from /proc/uptime."""
    data = _read("/proc/uptime")
    if data:
        try:
            return float(data.split()[0])
        except (ValueError, IndexError):
            pass
    return None


def read_loadavg():
    """Real 1/5/15 min load average."""
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        data = _read("/proc/loadavg")
        if data:
            try:
                p = data.split()
                return float(p[0]), float(p[1]), float(p[2])
            except (ValueError, IndexError):
                pass
    return None


def read_processes(limit=12):
    """
    Real processes visible to us. Android dusre apps ke /proc entries
    chhupa deta hai, to yahan mostly Termux ke apne processes dikhenge —
    jo abhi bhi bilkul real hain.
    """
    procs = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return procs
    try:
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    except (OSError, ValueError):
        page_kb = 4
    for pid in pids:
        comm = _read(f"/proc/{pid}/comm")
        if not comm:
            continue
        statm = _read(f"/proc/{pid}/statm")
        rss_kb = 0
        if statm:
            try:
                # statm field index 1 = resident pages
                rss_kb = int(statm.split()[1]) * page_kb
            except (ValueError, IndexError):
                rss_kb = 0
        procs.append((int(pid), comm.strip(), rss_kb))
    procs.sort(key=lambda x: -x[2])
    return procs[:limit]


def fmt_bytes(n):
    """Human readable byte formatting."""
    if n is None:
        return "N/A"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}P"


def fmt_uptime(sec):
    if sec is None:
        return "N/A"
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}:{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
#  SECTION 2 — Real event detection
#
#  Fake random log lines ki jagah, hum actual changes detect karte hain
#  aur unhe log karte hain. Ye sach me ho rahi cheezein hain.
# --------------------------------------------------------------------------- #

class EventLog:
    def __init__(self, maxlen=200):
        self.lines = []
        self.maxlen = maxlen
        self._prev_pids = set()
        self._prev_batt = None
        self._prev_temp = None
        self._first = True

    def add(self, level, text):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.lines.append((level, f"[{ts}] {level:<4} {text}"))
        self.lines[:] = self.lines[-self.maxlen:]

    def detect(self, procs, batt, temps, net):
        """Compare current state to last state, log the real differences."""
        # --- process start / exit (asli events) ---
        cur = {p[0]: p[1] for p in procs}
        cur_pids = set(cur)
        if not self._first:
            for pid in sorted(cur_pids - self._prev_pids)[:3]:
                self.add("PROC", f"spawn {pid} {cur[pid]}")
            for pid in sorted(self._prev_pids - cur_pids)[:3]:
                self.add("PROC", f"exit  {pid}")
        self._prev_pids = cur_pids

        # --- battery change ---
        if batt:
            b = round(batt["percent"])
            if self._prev_batt is not None and b != self._prev_batt:
                arrow = "up" if b > self._prev_batt else "down"
                self.add("PWR", f"batt {arrow} {self._prev_batt}->{b}%")
            self._prev_batt = b

        # --- thermal shift (0.5C se zyada) ---
        if temps:
            t = temps[0][1]
            if self._prev_temp is not None and abs(t - self._prev_temp) >= 0.5:
                self.add("THRM", f"{temps[0][0][:10]} {t:.1f}C")
            self._prev_temp = t

        # --- network spike (asli traffic) ---
        if net:
            for name, s in net.items():
                if name == "lo":
                    continue
                total_bps = s["rx_bps"] + s["tx_bps"]
                if total_bps > 200_000:  # ~200 KB/s
                    self.add("NET", f"{name} {fmt_bytes(total_bps)}/s")
                    break

        self._first = False


# --------------------------------------------------------------------------- #
#  SECTION 3 — Cosmetic layer (typing effect, cipher-break animation)
#
#  Background matrix rain hata diya gaya hai — background ab saaf black.
#  Ye hissa decorative hai aur honestly decorative hi label kiya gaya hai.
#  Data real, chrome cinematic.
# --------------------------------------------------------------------------- #

class Typer:
    """Character-by-character reveal."""

    def __init__(self, text, cps=45):
        self.text = text
        self.cps = cps
        self.start = time.time()

    @property
    def done(self):
        return (time.time() - self.start) * self.cps >= len(self.text)

    def render(self):
        n = min(len(self.text), int((time.time() - self.start) * self.cps))
        cur = "" if self.done else "_"
        return self.text[:n] + cur


class BootSequence:
    """
    Boot overlay — yahan twist hai: har line ek ASLI operation hai jo hum
    sach me perform karte hain, aur uska asli result dikhate hain. Isliye
    "Access Granted" jaisa jhooth nahi — real probe results.
    """

    def __init__(self):
        self.steps = [
            ("Reading /proc/stat", lambda: f"{os.cpu_count() or '?'} cores"),
            ("Mapping /proc/meminfo", self._mem),
            ("Probing thermal zones", self._thermal),
            ("Querying power supply", self._batt),
            ("Enumerating interfaces", self._ifaces),
            ("Stat'ing filesystem", self._fs),
            ("Telemetry link ready", lambda: "readers nominal"),
        ]
        self.idx = 0
        self.results = []
        self.typer = Typer(self.steps[0][0])
        self.hold = 0
        self.finished = False

    def _mem(self):
        m = read_memory()
        return f"{fmt_bytes(m['total'])} total" if m else "unavailable"

    def _thermal(self):
        z = read_thermal()
        return f"{len(z)} zones" if z else "restricted"

    def _batt(self):
        b = read_battery()
        return f"{b['percent']:.0f}% ({b['source']})" if b else "no access"

    def _ifaces(self):
        i = read_interfaces()
        return f"{len(i)} iface" if i else "none"

    def _fs(self):
        s = read_storage()
        return f"{fmt_bytes(s['free'])} free" if s else "unavailable"

    def update_draw(self, win, h, w, cp, cp_ok, cp_dim):
        if self.finished:
            return
        top = max(0, h // 2 - len(self.steps) // 2 - 1)
        for i in range(self.idx):
            label = self.steps[i][0]
            res = self.results[i] if i < len(self.results) else ""
            line = f"[OK] {label} .. {res}"
            safe_addstr(win, top + i, max(0, (w - len(line)) // 2),
                        line[: max(0, w - 1)], cp_ok | curses.A_BOLD)
        if self.idx < len(self.steps):
            txt = f"[..] {self.typer.render()}"
            safe_addstr(win, top + self.idx, max(0, (w - len(txt) - 8) // 2),
                        txt[: max(0, w - 1)], cp | curses.A_BOLD)
            if self.typer.done:
                self.hold += 1
                if self.hold > 6:
                    self.hold = 0
                    # Ab actually operation run karo, real result store karo.
                    try:
                        self.results.append(self.steps[self.idx][1]())
                    except Exception:
                        self.results.append("error")
                    self.idx += 1
                    if self.idx >= len(self.steps):
                        self.finished = True
                    else:
                        self.typer = Typer(self.steps[self.idx][0])


class CipherBreaker:
    """
    Decorative "cipher break" animation. PURE THEATRE — 100% fake.

    Yahan koi asli cryptography nahi hoti, koi asli file/hash/password
    kahin se nahi aata. Bas random target strings hain jinke characters
    ek-ek karke "lock" hote hain, jabki baaki positions random glyphs
    cycle karte rehte hain.

    CONTINUOUS DESIGN (ye important hai):
      Ye kabhi restart nahi hota. Jab ek key resolve ho jaati hai, wo
      wipe nahi hoti — upar `resolved` history me chali jaati hai aur
      permanently resolved dikhti rehti hai. Neeche turant nayi target
      shuru ho jaati hai. Attempts counter bhi kabhi reset nahi hota,
      bas badhta rehta hai. Isliye screen pe ek lagataar chalti hui
      operation dikhti hai — 100% pe pahunch ke zero pe girna nahi.
    """

    # Ye sirf labels hain — asli algorithms ke naam, par yahan inka koi
    # implementation nahi hai. Sirf screen pe likhne ke liye.
    ALGOS = [
        "AES-256-GCM", "RSA-4096", "ChaCha20-Poly1305", "Blowfish-448",
        "Twofish-256", "Serpent-256", "Camellia-256", "ECC-P521",
        "3DES-168", "Salsa20-256",
    ]
    STAGES = [
        "locating header block",
        "extracting salt",
        "recovering IV",
        "rebuilding key schedule",
        "resolving key material",
        "verifying MAC",
    ]
    GLYPHS = "0123456789ABCDEF"

    def __init__(self, keylen=32):
        self.base_keylen = keylen
        # --- ye saari state PERSISTENT hai, kabhi reset nahi hoti ---
        self.attempts = random.randint(50_000, 400_000)   # cumulative
        self.rate = random.uniform(2e5, 8e6)              # fake keys/sec
        self.resolved = []        # [(key, algo)] — history, kabhi wipe nahi
        self.feed = []
        self.session = "".join(random.choice(self.GLYPHS) for _ in range(8))
        # --- sirf active row ki state naye target pe badalti hai ---
        self._new_target(first=True)

    def _new_target(self, first=False):
        """
        Sirf ACTIVE row reset hoti hai. History, attempts, session — sab
        waise ke waise rehte hain. Isliye ye 'restart' nahi lagta.
        """
        self.keylen = self.base_keylen
        self.target = "".join(random.choice(self.GLYPHS)
                              for _ in range(self.keylen))
        # Lock order shuffle karo taaki left-to-right predictable na lage.
        self.order = list(range(self.keylen))
        random.shuffle(self.order)
        self.locked = [False] * self.keylen
        self.nlocked = 0
        self.algo = random.choice(self.ALGOS)
        self.stage = 0
        self.next_lock = time.time() + random.uniform(0.04, 0.16)
        if not first:
            self._feed(f"next target :: {self.algo}")
        else:
            self._feed(f"session {self.session} :: {self.algo}")

    def _feed(self, text):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.feed.append(f"[{ts}] {text}")
        self.feed[:] = self.feed[-40:]

    def set_keylen(self, n):
        """Screen width ke hisaab se key size adjust karo."""
        n = max(8, min(64, (n // 4) * 4))
        if n != self.base_keylen:
            self.base_keylen = n
            self._new_target()      # sirf active row, history safe

    def update(self, paused=False):
        if paused:
            return
        now = time.time()

        # Attempts hamesha climb karta hai — kabhi rukta nahi, kabhi
        # reset nahi hota. Ye hi "continuous" feel ka core hai.
        self.attempts += int(self.rate * 0.05 * random.uniform(0.5, 1.5))
        # Rate ko bounded random walk rakho. Bina clamp ke ye compound
        # hoke exponentially phat jaata hai (uniform ka mean 1 se upar hai).
        self.rate = max(2e5, min(9e6, self.rate * random.uniform(0.96, 1.05)))

        if now < self.next_lock:
            return
        # Tight timing — animation lagataar move karti dikhe.
        self.next_lock = now + random.uniform(0.04, 0.16)

        # --- drama: kabhi kabhi backtrack (4% chance) ---
        # Ye animation ko rokta nahi, sirf 2 chars wapas unlock karta hai
        # taaki mechanical na lage. Bilkul nahi chahiye? chance 0.0 kar do.
        if self.nlocked > 3 and random.random() < 0.04:
            for _ in range(min(2, self.nlocked)):
                self.nlocked -= 1
                self.locked[self.order[self.nlocked]] = False
            self._feed("checksum mismatch - backtracking 2 blocks")
            return

        # --- ek aur character lock karo ---
        if self.nlocked < self.keylen:
            self.locked[self.order[self.nlocked]] = True
            self.nlocked += 1

            if random.random() < 0.3:
                cand = "".join(random.choice(self.GLYPHS) for _ in range(8))
                self._feed(f"blk {random.randint(0, 999):03d} candidate {cand}")

            # stage progress key completion ke saath tied hai
            st = min(len(self.STAGES) - 1,
                     int(self.nlocked / self.keylen * len(self.STAGES)))
            if st != self.stage:
                self.stage = st
                self._feed(f">> {self.STAGES[st]}")

            # --- KEY RESOLVED ---
            # Koi hold nahi, koi flash-freeze nahi, koi wipe nahi.
            # Key history me push hoti hai aur usi frame me agli shuru.
            if self.nlocked >= self.keylen:
                self.resolved.append((self.target, self.algo))
                self.resolved[:] = self.resolved[-60:]   # memory cap
                self._feed(f"RESOLVED {self.algo}")
                self._new_target()

    def char_at(self, i):
        """Locked position -> target char. Unlocked -> random cycling glyph."""
        return self.target[i] if self.locked[i] else random.choice(self.GLYPHS)

    @property
    def progress(self):
        return 100.0 * self.nlocked / self.keylen

    @property
    def count(self):
        return len(self.resolved)



# --------------------------------------------------------------------------- #
#  SECTION 4 — Drawing primitives
# --------------------------------------------------------------------------- #

def safe_addstr(win, y, x, text, attr=0):
    """addstr jo kabhi crash nahi karta (edge cell / off-screen safe)."""
    if y < 0 or x < 0 or not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_box(win, y, x, h, w, title, cp):
    """Compact ASCII border — phone fonts pe ACS_ se behtar render hota hai."""
    if h < 2 or w < 4:
        return
    try:
        win.addstr(y, x, "+" + "-" * (w - 2) + "+", cp)
        win.addstr(y + h - 1, x, "+" + "-" * (w - 2) + "+", cp)
    except curses.error:
        pass
    for i in range(1, h - 1):
        safe_addstr(win, y + i, x, "|", cp)
        safe_addstr(win, y + i, x + w - 1, "|", cp)
    if title:
        t = f" {title} "[: max(0, w - 4)]
        safe_addstr(win, y, x + 2, t, cp | curses.A_BOLD)


def bar(pct, width):
    """ASCII progress bar. pct None ho to question marks."""
    width = max(3, width)
    if pct is None:
        return "[" + "?" * width + "]"
    filled = int(round(width * max(0, min(100, pct)) / 100.0))
    return "[" + "#" * filled + "." * (width - filled) + "]"


# --------------------------------------------------------------------------- #
#  SECTION 5 — Panels (har panel real data leta hai)
# --------------------------------------------------------------------------- #

def panel_system(win, y, x, h, w, st, cp, cp_warn, cp_dim):
    draw_box(win, y, x, h, w, "SYSTEM", cp)
    iw = w - 4
    bw = max(4, iw - 14)
    rows = []

    cpu = st["cpu"]
    rows.append(("CPU", cpu, f"{cpu:5.1f}%" if cpu is not None else "  N/A"))

    mem = st["mem"]
    rows.append(("RAM", mem["percent"] if mem else None,
                 f"{mem['percent']:5.1f}%" if mem else "  N/A"))

    sto = st["storage"]
    rows.append(("DISK", sto["percent"] if sto else None,
                 f"{sto['percent']:5.1f}%" if sto else "  N/A"))

    batt = st["batt"]
    rows.append(("BATT", batt["percent"] if batt else None,
                 f"{batt['percent']:5.0f}%" if batt else "  N/A"))

    line = y + 1
    for name, val, label in rows:
        if line >= y + h - 1:
            break
        attr = cp_warn if (val is not None and val > 85) else cp
        safe_addstr(win, line, x + 2, f"{name:<5}{bar(val, bw)}{label}"[:iw], attr)
        line += 1

    # Extra real details agar jagah bache.
    if line < y + h - 1 and mem:
        safe_addstr(win, line, x + 2,
                    f"     {fmt_bytes(mem['used'])}/{fmt_bytes(mem['total'])}"[:iw],
                    cp_dim)
        line += 1
    if line < y + h - 1 and sto:
        safe_addstr(win, line, x + 2,
                    f"     {fmt_bytes(sto['free'])} free"[:iw], cp_dim)
        line += 1
    if line < y + h - 1 and batt:
        safe_addstr(win, line, x + 2, f"     {batt['status']}"[:iw], cp_dim)


def panel_network(win, y, x, h, w, st, cp, cp_dim):
    draw_box(win, y, x, h, w, "NET (REAL COUNTERS)", cp)
    iw = w - 4
    line = y + 1
    net = st["net"]

    if not net:
        safe_addstr(win, line, x + 2, "/proc/net restricted"[:iw], cp_dim)
        return

    # Sabse busy interface pehle.
    active = sorted(
        ((n, s) for n, s in net.items() if n != "lo"),
        key=lambda kv: -(kv[1]["rx_bytes"] + kv[1]["tx_bytes"]),
    )
    for name, s in active:
        if line >= y + h - 1:
            break
        safe_addstr(win, line, x + 2, f"{name}"[:iw], cp | curses.A_BOLD)
        line += 1
        if line < y + h - 1:
            safe_addstr(win, line, x + 2,
                        f" dn {fmt_bytes(s['rx_bps']):>6}/s "
                        f"up {fmt_bytes(s['tx_bps']):>6}/s"[:iw], cp)
            line += 1
        if line < y + h - 1:
            safe_addstr(win, line, x + 2,
                        f" pkt {s['rx_packets']:,} / {s['tx_packets']:,}"[:iw],
                        cp_dim)
            line += 1


def panel_iface(win, y, x, h, w, st, cp, cp_dim):
    draw_box(win, y, x, h, w, "INTERFACES", cp)
    iw = w - 4
    line = y + 1
    for name, addr in st["ifaces"]:
        if line >= y + h - 1:
            break
        safe_addstr(win, line, x + 2, f"{name:<7}{addr}"[:iw], cp)
        line += 1
    if not st["ifaces"] and line < y + h - 1:
        safe_addstr(win, line, x + 2, "none visible"[:iw], cp_dim)


def panel_thermal(win, y, x, h, w, st, cp, cp_warn, cp_dim):
    draw_box(win, y, x, h, w, "THERMAL / LOAD", cp)
    iw = w - 4
    line = y + 1

    for typ, val in st["temps"]:
        if line >= y + h - 3:
            break
        attr = cp_warn if val > 55 else cp
        safe_addstr(win, line, x + 2, f"{typ[:13]:<14}{val:5.1f}C"[:iw], attr)
        line += 1

    if not st["temps"] and line < y + h - 1:
        safe_addstr(win, line, x + 2, "zones restricted"[:iw], cp_dim)
        line += 1

    load = st["load"]
    if load and line < y + h - 1:
        safe_addstr(win, line, x + 2,
                    f"load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"[:iw], cp_dim)
        line += 1
    if line < y + h - 1:
        safe_addstr(win, line, x + 2, f"up   {fmt_uptime(st['uptime'])}"[:iw],
                    cp_dim)


def panel_procs(win, y, x, h, w, st, cp, cp_dim):
    draw_box(win, y, x, h, w, "PROCESSES (RSS)", cp)
    iw = w - 4
    line = y + 1
    namew = max(4, iw - 15)
    for pid, name, rss in st["procs"]:
        if line >= y + h - 1:
            break
        safe_addstr(win, line, x + 2,
                    f"{pid:>6} {name[:namew]:<{namew}}"
                    f"{fmt_bytes(rss * 1024):>7}"[:iw], cp)
        line += 1
    if not st["procs"] and line < y + h - 1:
        safe_addstr(win, line, x + 2, "no /proc access"[:iw], cp_dim)


def panel_log(win, y, x, h, w, elog, cp, cp_warn, cp_dim):
    draw_box(win, y, x, h, w, "EVENT LOG (REAL)", cp)
    iw = w - 4
    visible = elog.lines[-(h - 2):] if h > 2 else []
    colors = {"NET": cp | curses.A_BOLD, "PWR": cp_warn, "THRM": cp_warn}
    for i, (level, text) in enumerate(visible):
        safe_addstr(win, y + 1 + i, x + 2, text[:iw], colors.get(level, cp_dim))


def panel_hex(win, y, x, h, w, st, cp, cp_dim):
    """
    Hex feed — ye panel honestly DECOR label kiya gaya hai. Baaki sab panels
    real hain; ye sirf visual texture ke liye hai. Chhoti screens pe skip.
    """
    draw_box(win, y, x, h, w, "HEX FEED (DECOR)", cp)
    iw = w - 4
    for i in range(1, h - 1):
        s = " ".join(f"{random.randint(0, 255):02X}" for _ in range(max(1, iw // 3)))
        safe_addstr(win, y + i, x + 2, s[:iw], cp_dim)


def panel_crack(win, y, x, h, w, cb, cp, cp_warn, cp_ok, cp_dim):
    """
    Cipher-break page. Poora panel DECORATIVE hai — title me [SIM] likha
    hai taaki confusion na ho. Baaki saare panels real telemetry dikhate
    hain, ye sirf eye-candy hai.

    Layout (upar se niche):
        active algo + stage + session
        RESOLVED history  <- ye upar scroll karti hai, kabhi wipe nahi hoti
        >> active key     <- yahi ek row flicker karti hai
        progress bar (sirf active row ka)
        cumulative counters
    """
    draw_box(win, y, x, h, w, "CIPHER BREAK [SIM]", cp)
    iw = w - 4
    if iw < 12 or h < 8:
        return

    line = y + 1
    bottom = y + h - 1

    # ---- header: active algo + session ---- #
    safe_addstr(win, line, x + 2, cb.algo[:iw], cp | curses.A_BOLD)
    rid = f"#{cb.session}"
    if iw > len(cb.algo) + len(rid) + 2:
        safe_addstr(win, line, x + w - 2 - len(rid), rid, cp_dim)
    line += 1
    safe_addstr(win, line, x + 2, cb.STAGES[cb.stage][:iw], cp_dim)
    line += 2

    # ---- footer block ki jagah reserve karo (progress + 2 counters) ---- #
    foot_rows = 3
    list_bottom = bottom - foot_rows - 1
    if list_bottom <= line:
        return

    # ---- active row hamesha sabse niche, uske upar resolved history ---- #
    # Jitni resolved keys fit hoti hain utni dikhao — sabse nayi sabse niche,
    # active row ke bilkul upar. History wipe nahi hoti, bas scroll hoti hai.
    room = list_bottom - line
    show = cb.resolved[-max(0, room - 1):] if room > 1 else []

    def fmt_key(chars, group=4):
        """Key ko 4-4 ke groups me todo, width ke hisaab se truncate."""
        out = []
        for i in range(0, len(chars), group):
            out.append("".join(chars[i:i + group]))
        return " ".join(out)

    ly = list_bottom - len(show)      # bottom-align: sabse nayi key active
    for key, algo in show:            # row ke bilkul upar chipki rahe
        if ly >= list_bottom:
            break
        if ly >= line:
            txt = f"[OK] {fmt_key(list(key))}"
            if iw > len(txt) + len(algo) + 3:
                txt = f"{txt}  {algo}"
            safe_addstr(win, ly, x + 2, txt[:iw], cp_ok)
        ly += 1

    # ---- ACTIVE row: locked chars solid, unlocked flickering ---- #
    ay = list_bottom
    safe_addstr(win, ay, x + 2, ">>", cp | curses.A_BOLD)
    col = x + 5
    for i in range(cb.keylen):
        if col >= x + w - 2:
            break
        attr = (cp | curses.A_BOLD) if cb.locked[i] else cp_dim
        safe_addstr(win, ay, col, cb.char_at(i), attr)
        col += 1
        if (i + 1) % 4 == 0:
            col += 1      # group gap

    # ---- progress bar (active row ka) ---- #
    py = bottom - 3
    bw = max(4, iw - 8)
    safe_addstr(win, py, x + 2,
                f"{bar(cb.progress, bw)}{cb.progress:4.0f}%"[:iw], cp)

    # ---- cumulative counters — ye kabhi reset nahi hote ---- #
    safe_addstr(win, bottom - 2, x + 2,
                f"tried    {cb.attempts:,}"[:iw], cp_dim)
    safe_addstr(win, bottom - 1, x + 2,
                f"resolved {cb.count} keys @ {cb.rate / 1e6:.2f}M k/s"[:iw],
                cp_dim)



# --------------------------------------------------------------------------- #
#  SECTION 6 — Layout engine
#
#  Termux screens chhoti hoti hain (aksar 40-60 cols x 20-30 rows, keyboard
#  khulne pe aur kam). Layout teen mode me kaam karta hai:
#     wide   (>=100 cols) -> 3 columns
#     medium (>=60 cols)  -> 2 columns
#     narrow (<60 cols)   -> single column, pages me (n/p se switch)
# --------------------------------------------------------------------------- #

PAGES = ["SYS", "NET", "PROC", "LOG", "CRK"]


def draw_narrow(win, top, h, w, st, elog, page, cb, cp, cp_warn, cp_ok, cp_dim):
    """Phone portrait: ek column, page-based."""
    avail = h - top - 1
    name = PAGES[page]
    if name == "CRK":
        panel_crack(win, top, 0, avail, w, cb, cp, cp_warn, cp_ok, cp_dim)
    elif name == "SYS":
        half = max(7, avail // 2)
        panel_system(win, top, 0, half, w, st, cp, cp_warn, cp_dim)
        if avail - half >= 3:
            panel_thermal(win, top + half, 0, avail - half, w,
                          st, cp, cp_warn, cp_dim)
    elif name == "NET":
        half = (avail * 2) // 3
        panel_network(win, top, 0, half, w, st, cp, cp_dim)
        if avail - half >= 3:
            panel_iface(win, top + half, 0, avail - half, w, st, cp, cp_dim)
    elif name == "PROC":
        panel_procs(win, top, 0, avail, w, st, cp, cp_dim)
    elif name == "LOG":
        panel_log(win, top, 0, avail, w, elog, cp, cp_warn, cp_dim)


def draw_medium(win, top, h, w, st, elog, cp, cp_warn, cp_dim):
    """Landscape phone / small tablet: 2 columns."""
    avail = h - top - 1
    cw = w // 2
    rh = avail // 2
    panel_system(win, top, 0, rh, cw, st, cp, cp_warn, cp_dim)
    panel_network(win, top, cw, rh, w - cw, st, cp, cp_dim)
    panel_thermal(win, top + rh, 0, avail - rh, cw, st, cp, cp_warn, cp_dim)
    panel_log(win, top + rh, cw, avail - rh, w - cw, elog, cp, cp_warn, cp_dim)


def draw_wide(win, top, h, w, st, elog, cp, cp_warn, cp_dim):
    """Desktop / big terminal: 3 columns."""
    avail = h - top - 1
    cw = w // 3
    rh = avail // 2
    bh = avail - rh
    panel_system(win, top, 0, rh, cw, st, cp, cp_warn, cp_dim)
    panel_network(win, top, cw, rh, cw, st, cp, cp_dim)
    panel_thermal(win, top, 2 * cw, rh, w - 2 * cw, st, cp, cp_warn, cp_dim)
    panel_procs(win, top + rh, 0, bh, cw, st, cp, cp_dim)
    ih = bh // 2
    panel_iface(win, top + rh, cw, ih, cw, st, cp, cp_dim)
    panel_hex(win, top + rh + ih, cw, bh - ih, cw, st, cp, cp_dim)
    panel_log(win, top + rh, 2 * cw, bh, w - 2 * cw, elog, cp, cp_warn, cp_dim)


# --------------------------------------------------------------------------- #
#  SECTION 7 — Main loop
# --------------------------------------------------------------------------- #

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(0)

    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_GREEN, bg)
    curses.init_pair(2, curses.COLOR_CYAN, bg)
    curses.init_pair(3, curses.COLOR_YELLOW, bg)
    curses.init_pair(4, curses.COLOR_WHITE, bg)
    CP = curses.color_pair(1)
    CP_CY = curses.color_pair(2)
    CP_WARN = curses.color_pair(3)
    CP_OK = curses.color_pair(4)
    CP_DIM = curses.color_pair(1) | curses.A_DIM

    h, w = stdscr.getmaxyx()
    cpu = CPUReader()
    net = NetReader()
    elog = EventLog()
    boot = BootSequence()
    crack = CipherBreaker()      # decorative only — dekho SECTION 3

    # Phone pe har frame /proc scan karna battery kha jayega — isliye cache.
    cache = {"cpu": None, "mem": None, "storage": None, "batt": None,
             "temps": [], "net": None, "procs": [], "uptime": None,
             "load": None, "ifaces": read_interfaces()}
    last_fast = 0.0    # cpu, mem, net      -> har 1s
    last_slow = 0.0    # batt, temp, procs  -> har 3s

    paused = False
    crack_full = False   # medium/wide pe 'c' se fullscreen crack view
    page = 0
    fps = 30           # phone pe 30 realistic hai; 60 sirf battery jalata hai
    frame_delay = 1.0 / fps

    header = Typer("LOCAL TELEMETRY // THIS DEVICE ONLY", cps=35)

    while True:
        t0 = time.time()

        # ---------------- input ---------------- #
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        elif key == ord(" "):
            paused = not paused
        elif key in (ord("c"), ord("C")):
            # Narrow pe seedha CRK page pe jump, warna fullscreen toggle.
            if w < 60:
                page = PAGES.index("CRK")
            else:
                crack_full = not crack_full
        elif key in (ord("x"), ord("X")):
            crack._new_target()    # sirf active row skip, history safe rehti hai
        elif key in (ord("n"), ord("N"), curses.KEY_RIGHT):
            page = (page + 1) % len(PAGES)
        elif key in (ord("p"), ord("P"), curses.KEY_LEFT):
            page = (page - 1) % len(PAGES)
        elif key in (ord("r"), ord("R")):
            cache["ifaces"] = read_interfaces()
            elog.add("USER", "iface refresh")
        elif key in (ord("+"), ord("=")):
            fps = min(60, fps + 10)
            frame_delay = 1.0 / fps
        elif key in (ord("-"), ord("_")):
            fps = max(5, fps - 10)
            frame_delay = 1.0 / fps
        elif key == curses.KEY_RESIZE:
            h, w = stdscr.getmaxyx()

        # Resize jo KEY_RESIZE ke bina aa jaye (Termux rotate) usko bhi pakdo.
        nh, nw = stdscr.getmaxyx()
        if (nh, nw) != (h, w):
            h, w = nh, nw

        # ---------------- telemetry sampling ---------------- #
        now = time.time()
        if not paused and now - last_fast > 1.0:
            c = cpu.percent()
            if c is not None:
                cache["cpu"] = c
            cache["mem"] = read_memory()
            cache["net"] = net.sample()
            cache["uptime"] = read_uptime()
            cache["load"] = read_loadavg()
            last_fast = now

        if not paused and now - last_slow > 3.0:
            cache["batt"] = read_battery()
            cache["temps"] = read_thermal()
            cache["storage"] = read_storage()
            cache["procs"] = read_processes(14)
            elog.detect(cache["procs"], cache["batt"], cache["temps"], cache["net"])
            last_slow = now

        # ---------------- render ---------------- #
        stdscr.erase()

        # Header
        host = socket.gethostname()[:18]
        safe_addstr(stdscr, 0, 1, f"== {host} =="[: max(0, w - 1)],
                    CP | curses.A_BOLD)
        clock = datetime.datetime.now().strftime("%H:%M:%S")
        safe_addstr(stdscr, 0, max(0, w - len(clock) - 1), clock, CP_CY)
        safe_addstr(stdscr, 1, 1, header.render()[: max(0, w - 2)], CP_CY)

        # Crack animation har frame advance hoti hai (chahe page dikhe ya na).
        # Key length screen width ke hisaab se — chhoti screen pe chhoti key.
        crack.set_keylen(32 if w >= 60 else 16)
        crack.update(paused)

        top = 3
        if h - top > 6 and w >= 24:
            if w >= 60 and crack_full:
                panel_crack(stdscr, top, 0, h - top - 1, w, crack,
                            CP, CP_WARN, CP_OK, CP_DIM)
            elif w >= 100:
                draw_wide(stdscr, top, h, w, cache, elog, CP, CP_WARN, CP_DIM)
            elif w >= 60:
                draw_medium(stdscr, top, h, w, cache, elog, CP, CP_WARN, CP_DIM)
            else:
                draw_narrow(stdscr, top, h, w, cache, elog, page, crack,
                            CP, CP_WARN, CP_OK, CP_DIM)
        else:
            safe_addstr(stdscr, top, 1, "screen too small", CP_WARN)

        if not boot.finished:
            boot.update_draw(stdscr, h, w, CP, CP_CY, CP_DIM)

        # Footer: narrow mode me page tabs, warna shortcuts.
        if w < 60:
            foot = " ".join(f"[{p}]" if i == page else f" {p} "
                            for i, p in enumerate(PAGES))
            if paused:
                foot += " ||"
        else:
            foot = (f"q:quit space:pause c:crack x:recycle "
                    f"r:refresh  {fps}fps")
            if crack_full:
                foot += "  [SIM VIEW]"
            if paused:
                foot += "  || PAUSED"
        safe_addstr(stdscr, h - 1, 0, foot[: max(0, w - 1)],
                    CP_CY | curses.A_DIM)

        stdscr.refresh()

        # ---------------- frame pacing ---------------- #
        rest = frame_delay - (time.time() - t0)
        if rest > 0:
            time.sleep(rest)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    print("closed — all readings were live from this device (/proc, sysfs).")
