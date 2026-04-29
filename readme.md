# YouTubeBot — Flask + Web GUI

A YouTube automation bot with a real-time web dashboard.

## Features
- 🎮 Live dashboard — start, stop, pause, skip from your browser
- 👻 Stealth mode via `undetected-chromedriver`
- ⏭  Auto ad-skipping
- 🔀 Randomised playback order
- 📊 Real-time progress bars, stats, and live log
- ⚙️  All settings editable from the UI (no code changes needed)

## Setup

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Run the server
python app.py

# 3. Open in browser
http://localhost:5000
```

Requires **Google Chrome** to be installed.

## Project structure

```
youtubebot/
├── app.py              ← Flask backend + bot engine
├── requirements.txt
├── templates/
│   └── index.html      ← Web dashboard
└── README.md
```

## Dashboard controls

| Button  | Action                              |
|---------|-------------------------------------|
| Start   | Launch Chrome and begin playback    |
| Pause   | Freeze the current watch timer      |
| Skip    | Jump to the next video immediately  |
| Stop    | Kill the browser and end the session|

## Config options (all editable in-dashboard)

| Option         | Description                                   |
|----------------|-----------------------------------------------|
| Video URLs     | One YouTube URL per line                      |
| Loops          | How many full passes (0 = infinite)           |
| Watch ratio    | % of each video to watch (10–100%)            |
| Delay (min/max)| Random pause between videos (seconds)         |
| Shuffle        | Randomise order each loop                     |
| Headless       | Run Chrome invisibly                          |
| Mute           | Silence audio                                 |