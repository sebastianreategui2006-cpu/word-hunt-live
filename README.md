# Word Hunt Live for Mac

Word Hunt Live reads a 4×4 GamePigeon Word Hunt board from **Apple iPhone Mirroring**, suggests words with their tile paths, and can swipe words automatically. It rescans after tiles fall. There are no accounts, subscriptions, or usage limits in this app.

## Install

1. Use a Mac that supports Apple iPhone Mirroring and pair it with your own iPhone.
2. Install [Python 3.11 or newer](https://www.python.org/downloads/macos/) if it is not already installed.
3. [Download Word Hunt Live for Mac](https://github.com/sebastianreategui2006-cpu/word-hunt-live/releases/latest/download/WordHunt-Live-Mac.zip) and unzip it. Double-click `run.command`. On first launch, it installs its Python dependencies; this requires an internet connection.
4. If macOS blocks the downloaded script, Control-click `run.command`, choose **Open**, and confirm.
5. Open iPhone Mirroring and show a Word Hunt board. The local page opens at `http://127.0.0.1:8765/`.

The app may need **Screen & System Audio Recording** permission for the terminal or Python process to see the mirror. For **Play board**, allow the same process under **Accessibility** so it can send mouse gestures. After granting a permission, restart `run.command`.

## Use

The 4×4 grid is found automatically. If detection misses it, drag a box around the tiles in the preview. Correct any wrong letters in the grid and click **Correct letters**. Clear letter shapes use learned templates for fast updates; uncertain shapes are checked with OCR. The word list favors familiar long words and filters capitalized names and rare entries. Each suggested word includes a numbered tile path. Click **Play board** to start automatic swipes; click **Stop** to end them. The player watches for falling tiles and solves the new board.

The app remembers corrected tile readings and word feedback in a local `memory.sqlite3` file created beside the app. Each downloaded copy starts with its own empty memory. No mirror images or memory data are sent to a server. The web page is served only on the Mac at `127.0.0.1`.

## Requirements and limits

- macOS, Apple iPhone Mirroring, a paired iPhone, and Python 3.11–3.14 are required for automatic screen reading and swiping.
- macOS permissions must be granted on each person's own Mac. A public website alone cannot access or control their iPhone Mirroring window.
- The word list and OCR can occasionally be wrong, and the game may reject a suggested word.

The solver is an independent project and is not affiliated with Apple, GamePigeon, or The Word Finder.
