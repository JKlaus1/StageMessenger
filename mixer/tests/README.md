# mixer tests (off-hardware)

Run from the repo root (`~/stage-messenger` on the Pi, or any clone). Nothing here touches a real
console: the suites start their own fake console on 127.0.0.1 and run the real blueprint from a temp
copy of the package (no `mixer_config.json` / `mixer_state.json` is written into the repo).

| Suite | What | Command |
|---|---|---|
| X32 API | fake M32C (`fake_x32.py`) on udp 10023 + Flask test client: detection, caps, canonical values, writes, M32 mute-group semantics, console pushes, meters, channel sheet (input stage, headamps appearing / going with a stagebox, EQ / gate / dyn scalings both ways, source picker), X-LIVE recorder (record, markers, sessions, playback, seek, SD / error states), X32-off features, page CAPS injection, per-console channel order | `python3 -m mixer.tests.test_x32_api` |
| Auto-detect / hot swap | no IP configured: fake M32C + fake WING on loopback aliases 127.0.0.2/.3; finds whichever answers, swaps X32 -> WING -> WING at a new address without a restart (old driver stopped, socket freed, per-console order, pages told), never second-guesses a connected console, discovery parsing / type filter / subnet sweep | `python3 -m mixer.tests.test_autoswitch` |
| WING regression | minimal fake WING on udp 2223: WING driver, 40/8/16 strips, USB patch, own mute, float fader writes, WING order key | `python3 -m mixer.tests.test_wing_regression` |
| Cam feed | `picker.py` cam output (second pair, sample-accurate delay line, bounded writer that can't hold up listen), `cam.py` against a fake 48-channel capture and a lavfi "camera" (audio delay really shifts the encoded stream ~500 ms, video-only mode, idle stop, give-up on a dead camera), picture tiers really change size / fps / preset (v3.5), video sound + Listen Opus bitrates, `/mixer/api/cam/*` + `/mixer/api/listen/set`, saved settings | `python3 -m mixer.tests.test_cam` |
| Page (jsdom) | the page as served (CAPS injected) + a live snapshot -> DOM; clicks -> requests; X32 channel sheet driven by real driver answers (`x32_api.json`); WING defaults | `python3 -m mixer.tests.gen_page_fixtures /tmp/fx && node mixer/tests/test_page.js /tmp/fx` |

Ports 10023 / 2222 / 2223 on 127.0.0.1-3 must be free (stop `stage-messenger` first if it runs on the same
machine with a console on localhost -- normally it talks to the console's IP, so no clash).
The cam suite needs `ffmpeg` / `ffprobe` (libx264 + libopus) and takes ~25 s.
The page suite needs jsdom (`npm i jsdom`, put its `node_modules` on `NODE_PATH`); it also covers the Video card (sound hand-over between Listen and the video, bitrate pickers, Picture-in-Picture + the Chrome pop-out window).

`fake_x32.py` can also run standalone (`python3 -m mixer.tests.fake_x32 [port]`) to point a dev
server at it.
