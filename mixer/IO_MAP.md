# `/mixer` console I/O map — what the Pi reads, writes and expects

As-built for Stage Messenger **v4.1.1** (Oct 2026). Everything here is **fixed in code** today
(`mixer/__init__.py` `feed_table` / `ensure_patch` / `_ensure_x32_patch`, `mixer/spotify.py`,
`mixer/asound.conf`). Making the patch user-adjustable is a later project; until then this file is
the reference for what plugging the Pi into a console will change.

Vocabulary: **console → Pi** = the console's USB/card *outputs* (what the Pi records: listen-back, video
sound, meters are OSC not audio). **Pi → console** = the console's USB/card *inputs* (Spotify).

---

## 1. WING (WING Rack verified; USB 48 in / 48 out, S24_3LE, 48 kHz)

### 1.1 Console → Pi: USB outs the Pi patches (`/io/out/USB/<n>/grp` + `/in`)

Written on load / reconnect and again whenever the console reports a change to any `/io/out/USB/*` or
to the ambient channel's input (throttled to one pass per 10 s; `usb_patch: true`). Anything you change
by hand in this range is put back within seconds.

| USB out | Source written | Feed id on `/mixer` |
|---|---|---|
| 1–32 | **Bus 1–16 L/R** (BUS 1/2 = Bus 1 L/R … BUS 31/32 = Bus 16 L/R) | `bus1` … `bus16` |
| 33–40 | **Mtx 1–4 L/R** (MTX 1/2 … 7/8) | `mtx1` … `mtx4` |
| 41 | *not written* — free | — |
| 42 | **Ambient**: the physical input of the ambient channel (default **Ch 10**'s `in/conn`; page can pick another). Falls back to `ambient.grp/in` = **AES50 B 4** if that channel has no source | `ambient` |
| 43 / 44 | **Main 1 L/R** (MAIN 1/2) | `main1` |
| 45 / 46 | **Main 2 L/R** (MAIN 3/4) — Joseph's sub send | `main2` |
| 47 / 48 | **Monitor 1 L/R** (MON 1/2) — phones / solo | `mon1` |

Assumption baked in: stereo sources occupy consecutive `in` indices (BUS 1/2 = Bus 1 L/R, MAIN 3/4 =
Main 2 L/R). Verified by ear on the WING Rack.

### 1.2 Pi → console: Spotify

| Pi | Console |
|---|---|
| go-librespot → ALSA `spotify_out` → `wing_pi` → **USB in 1/2** | arrives as **USB 1/2**. The Pi does **not** patch any strip to it; on Joseph's WING **AUX 1** is sourced from USB 1/2 by hand. `/mixer` pins the **AUX 1** strip as the level control (`spotify.strip.wing`, default `aux/1`). |

### 1.3 Not touched on a WING
USB outs 41 (and nothing above 48); all `/io/in/*`; strip input sources (`/<kind>/<n>/in/conn`) unless
someone re-patches a strip on the channel page; `/io/altsw` (MAIN/ALT) only when the page button is
pressed; faders, mutes, sends, EQ etc. only on user action; WING-LIVE recorder only on user action.

### 1.4 Network
OSC/UDP **2223** to the console (discovery broadcast + subnet sweep on eth0; no fixed IP), plus the
meter subscription. The console address is never pinned unless `mixer_type`/`mixer_ip` are set.

---

## 2. X32 / M32 family (X32 Rack fw 4.15 + X-LIVE and M32C fw 4.06 verified; X-USB expected)

USB card: 32 in / 32 out, S24_3LE, **48 kHz assumed** (console clock must be 48 k; 44.1 k breaks both
capture and `xlive_dmix`). ALSA card id auto-detected: `XLIVE` (X-LIVE) or `XUSB` (X-USB) —
`capture.x32.cards`; exported as `STAGE_RIG_X32_CARD`.

### 2.1 Console → Pi: the 7 routing writes

Written on load and re-checked every ~15 s (`usb_patch: true`). **Card blocks 1-8 / 9-16 / 17-24 are
never written** — they stay whatever the console has (normally the multitrack inputs for SD/USB
recording).

| Console address | Value | Meaning |
|---|---|---|
| `/config/routing/CARD/25-32` | `26` (**UOUT1-8**) | card out block 25–32 = User Out 1–8 |
| `/config/userrout/out/03` | input code of the **ambient channel**'s source (default Ch 10; `0` = off if none) | User Out 3 |
| `/config/userrout/out/04` | `182` = **Local Out 14** tap | User Out 4 — sub (mono) |
| `/config/userrout/out/05` | `207` = **Monitor L** | User Out 5 |
| `/config/userrout/out/06` | `208` = **Monitor R** | User Out 6 |
| `/config/userrout/out/07` | `183` = **Local Out 15** tap | User Out 7 — Main L |
| `/config/userrout/out/08` | `184` = **Local Out 16** tap | User Out 8 — Main R |

Resulting card outs 25–32 (= USB channels the Pi records = **SD tracks 25–32** on an X-LIVE):

| Card out | Carries | Feed id |
|---|---|---|
| 25 | User Out 1 — *not written* | — |
| 26 | User Out 2 — *not written* | — |
| 27 | ambient (ambient channel's physical input) | `ambient` |
| 28 | sub — Local Out 14 | `main2` (mono, both sides) |
| 29 / 30 | Monitor L/R | `mon1` |
| 31 / 32 | Main L/R — Local Out 15/16 | `main1` |

User Out source codes (verified with the oscillator, X32 Rack fw 4.15): inputs 1–32 local, 33–80
AES50 A, 81–128 AES50 B, 129–160 card, 161–166 aux in; 169–184 = Local Out 1–16; 207/208 = Monitor L/R.

**"Main" and "sub" are Local-Out taps, not bus taps.** The code assumes Main L/R live on XLR Out 15/16
(stock X32 default) and the sub send on Out 14 (Joseph's rack). On a console with other output routing,
`main1` is whatever is on Out 15/16 and the sub blend is whatever is on Out 14.

### 2.2 Pi → console: Spotify

| Pi | Console |
|---|---|
| go-librespot → `spotify_out` → `xlive_pi` → USB playback **1/2** | arrives as **Card in 1/2**. No console write. Audible only where something is sourced from Card 1/2: *Routing › Aux In Remap = Card 1-4* puts it on **Aux In 1/2** (matches the default pinned strip `spotify.strip.x32 = aux/1`), or route an input block / user-in (`/config/userrout/in/NN` = 129/130) to Card 1-2 for a channel and set `spotify.strip.x32` to that channel (`ch/31` …). |

Watch-outs: a console whose **PLAY** input blocks are `CARD1-8…` (stock) will put Stage Rig on Ch 1/2
whenever `routswitch` = PLAY (virtual soundcheck, or an X-LIVE on `URECrout AUTO` going into SD
playback). go-librespot only opens the USB device while a track plays.

### 2.3 Not touched on an X32
Card blocks 1–24; User Out 1–2; all input blocks (`IN/1-8 … IN/AUX`, `PLAY/*`); User In 1–32;
`/config/routing/routswitch` (X32 caps have `alt: False` — no page button); Out 1–16 / AES50 / Ultranet
/ P16 blocks; channel sources, headamps, faders, mutes, sends, processing — all only on user action;
X-LIVE transport/markers only on user action.

Collision checklist for a house console: (1) nothing in *Routing › Out 1-16 / AES50 A-B / Ultranet* is
set to a `User Out` block containing UOUT 3–8; (2) SD/USB tracks 25–32 are expendable (they become the
listen block); (3) 48 kHz; (4) Ch 10 (or the chosen ambient channel) is the input you want as "ambient".
`usb_patch: false` in `mixer_config.json` freezes all 7 writes (listen feeds then have no source;
Spotify and remote mixing still work).

### 2.4 Network
OSC/UDP **10023** (`/xremote` renewed every 8 s; meters a separate subscription). Same discovery as the
WING; never pinned unless configured.

---

## 3. Pi side (both consoles)

| | WING | X32 |
|---|---|---|
| ALSA card | `hw:WING` | `hw:XLIVE` / `hw:XUSB` (auto) |
| Capture (listen + video sound) | `arecord -D hw:WING -c 48 -f S24_3LE -r 48000` → `picker.py` picks the feed's two channels | same, `-c 32`, device from `capture.x32` |
| Playback dmix | `wing_dmix` (ipc 4815) → `wing_pi` = stereo → USB 1/2 | `xlive_dmix` (ipc 4816, card from `STAGE_RIG_X32_CARD`) → `xlive_pi` = stereo → Card 1/2 |
| go-librespot output | `spotify_out` → `STAGE_RIG_PCM=wing_pi` (default) | `spotify_out` → `STAGE_RIG_PCM=xlive_pi` |
| Who sets `console.env` | `mixer/spotify.py set_console()` on attach / hot-swap / card appearing; restarts go-librespot only when it changes | same |
| Desktop PipeWire | WirePlumber rule disables any `BEHRINGER_WING*`, `*X?LIVE*`, `*X?USB*` card so the kiosk session never grabs it | same |
| Spotify control | go-librespot API 127.0.0.1:3678, proxied under `/mixer` | same |
| MediaMTX | `listen` (WebRTC listen-back), `cam` (video), `live/gopro` (RTMP in :1935) — console-independent | same |

Capture and playback are separate PCM streams and run side by side (verified on the WING 2026-10-05).

---

## 4. Future: adjustable patching (not built)

Everything above is a table in code: `feed_table()`, `AMBIENT_USB`, `X32_UOUT`, `X32_UOUT_SLOTS`,
`X32_CARD_BLOCK`, `spotify.strip`, `spotify.pcm`. The intended next step is to move these into
`mixer_config.json` (per console) with a page to edit them, and a "don't touch the console" mode that
only *reads* an existing patch the user made. Until then, change the constants and re-run the tests
(`mixer/tests/README.md`).
