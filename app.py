import base64
import hashlib
import io
import json
import logging
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections import Counter
from functools import lru_cache
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageEnhance, ImageOps
import Quartz
import AppKit
import ApplicationServices
import Vision
from Foundation import NSURL
from wordfreq import zipf_frequency

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'memory.sqlite3'
app = Flask(__name__)
lock = threading.Lock()
play_stop = threading.Event()
ocr_cache = {}
templates = [(row['letter'], int(row['bits'], 16)) for row in json.loads((ROOT / 'glyph_templates.json').read_text())]
state = {'window': None, 'crop': None, 'board': '', 'words': [], 'image': '', 'status': 'Waiting for iPhone Mirroring', 'updated': 0, 'tiles': [], 'glyphs': [], 'confidence': [], 'boxes': [], 'capture_size': None, 'window_pid': None, 'stable_board': '', 'stable_since': 0, 'after_patterns': [], 'playback': {'armed': False, 'running': False, 'played': 0, 'total': 0, 'current': '', 'error': ''}}


def db():
    con = sqlite3.connect(DB)
    con.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS tiles (signature TEXT PRIMARY KEY, letter TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS words (word TEXT PRIMARY KEY, adjustment INTEGER NOT NULL DEFAULT 0)')
    con.execute('CREATE TABLE IF NOT EXISTS glyphs (signature TEXT PRIMARY KEY, letter TEXT NOT NULL, bits TEXT NOT NULL)')
    return con


def get_setting(key, default=None):
    with db() as con:
        row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put_setting(key, value):
    with db() as con:
        con.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))


def windows():
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    entries = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    return [dict(id=int(w['kCGWindowNumber']), title=str(w.get('kCGWindowName', '')),
                 owner=str(w.get('kCGWindowOwnerName', '')),
                 pid=int(w.get('kCGWindowOwnerPID', 0)), bounds=w.get('kCGWindowBounds'))
            for w in entries if 'iPhone Mirroring' in str(w.get('kCGWindowOwnerName', ''))
            and int(w.get('kCGWindowLayer', 1)) == 0]


def capture(window_id):
    with tempfile.NamedTemporaryFile(suffix='.png') as f:
        result = subprocess.run(['/usr/sbin/screencapture', '-x', '-o', '-l', str(window_id), f.name],
                                capture_output=True, timeout=12)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors='replace').strip() or 'Screen capture failed')
        image = Image.open(f.name).convert('RGB')
        image.load()
        return image


def signature(image):
    sample = ImageOps.grayscale(image).resize((16, 16))
    return hashlib.sha256(sample.tobytes()).hexdigest()


def glyph_pattern(tile):
    w, h = tile.size
    tile = tile.crop((int(.22*w), int(.14*h), int(.78*w), int(.65*h)))
    binary = ImageOps.grayscale(tile).point(lambda p: 255 if p < 105 else 0)
    bounds = binary.getbbox()
    if not bounds:
        return None
    binary = binary.crop(bounds)
    w, h = binary.size
    scale = min(20/w, 20/h)
    binary = binary.resize((max(1, round(w*scale)), max(1, round(h*scale))))
    out = Image.new('L', (28, 28))
    out.paste(binary, ((28-binary.width)//2, (28-binary.height)//2))
    bits = 0
    for pixel in out.get_flattened_data():
        bits = (bits << 1) | int(pixel > 127)
    return bits


def fast_letter(pattern):
    if pattern is None or not templates:
        return '', 1
    distance, letter = min(((pattern ^ sample).bit_count()/784, letter)
                           for letter, sample in templates)
    return letter, distance


def confident_template_letter(pattern):
    if pattern is None or not templates:
        return ''
    by_letter = {}
    for letter, sample in templates:
        distance = (pattern ^ sample).bit_count() / 784
        by_letter[letter] = min(distance, by_letter.get(letter, 1))
    closest = sorted(by_letter.items(), key=lambda item: item[1])
    if len(closest) < 2:
        return ''
    (letter, best), (_, second) = closest[:2]
    if ((best <= .02 and second - best >= .012)
            or (best <= .09 and second - best >= .02)):
        return letter
    return ''


def detect_grid(image):
    small = image.copy()
    small.thumbnail((450, 900))
    w, h = small.size
    pixels = small.load()
    mask = bytearray(w*h)
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            if r > 150 and g > 150 and b > 140 and max(r,g,b)-min(r,g,b) < 40:
                mask[y*w+x] = 1
    components = []
    for start in range(w*h):
        if not mask[start]:
            continue
        mask[start] = 0
        queue = deque([start])
        x0 = w; y0 = h; x1 = 0; y1 = 0; area = 0
        while queue:
            pos = queue.popleft()
            x, y = pos%w, pos//w
            area += 1
            x0 = min(x0,x); x1 = max(x1,x); y0 = min(y0,y); y1 = max(y1,y)
            neighbors = ((pos-1,) if x else ()) + ((pos+1,) if x<w-1 else ()) + (pos-w,pos+w)
            for nxt in neighbors:
                if 0 <= nxt < w*h and mask[nxt]:
                    mask[nxt] = 0
                    queue.append(nxt)
        width, height = x1-x0+1, y1-y0+1
        if 25 <= width <= 100 and .8 <= width/height <= 1.25 and area > width*height*.55:
            components.append((area, (x0,y0,x1+1,y1+1)))
    if len(components) < 16:
        return None
    boxes = [box for _,box in sorted(components, reverse=True)[:16]]
    boxes.sort(key=lambda b:(b[1]+b[3])/2)
    rows = [sorted(boxes[i:i+4], key=lambda b:(b[0]+b[2])/2) for i in range(0,16,4)]
    widths = [b[2]-b[0] for b in boxes]
    median = sorted(widths)[8]
    if any(abs(width-median)>median*.25 for width in widths):
        return None
    for col in range(4):
        xs = [(row[col][0]+row[col][2])/2 for row in rows]
        if max(xs)-min(xs) > median*.3:
            return None
    ys = [sum((b[1]+b[3])/2 for b in row)/4 for row in rows]
    if not all(median*.85 < ys[i+1]-ys[i] < median*1.5 for i in range(3)):
        return None
    boxes = [b for row in rows for b in row]
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    crop = [x0/w, y0/h, (x1-x0)/w, (y1-y0)/h]
    W, H = image.size
    scaled = [(int(a*W/w),int(b*H/h),int(c*W/w),int(d*H/h)) for a,b,c,d in boxes]
    return crop, scaled


def boxes_from_crop(image, crop):
    W, H = image.size
    x, y, cw, ch = crop
    return [(int((x+col*cw/4)*W), int((y+row*ch/4)*H),
             int((x+(col+1)*cw/4)*W), int((y+(row+1)*ch/4)*H))
            for row in range(4) for col in range(4)]


def reuse_grid(image, boxes):
    if len(boxes) != 16:
        return None
    for x0, y0, x1, y1 in boxes:
        x = int(x0 + (x1-x0)*.1)
        y = int(y0 + (y1-y0)*.1)
        if not (0 <= x < image.width and 0 <= y < image.height):
            return None
        r, g, b = image.getpixel((x, y))
        if not (r > 150 and g > 150 and b > 140 and max(r,g,b)-min(r,g,b) < 40):
            return None
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    return [x0/image.width, y0/image.height,
            (x1-x0)/image.width, (y1-y0)/image.height], boxes


def recognize_variant(image, mode):
    if mode == 'full':
        image = ImageEnhance.Contrast(image).enhance(1.8)
    else:
        w, h = image.size
        if mode == 'tight':
            image = image.crop((int(.20*w),int(.10*h),int(.80*w),int(.70*h)))
        else:
            image = image.crop((int(.22*w),int(.14*h),int(.78*w),int(.65*h)))
    image = image.resize((240, 240))
    with tempfile.NamedTemporaryFile(suffix='.png') as f:
        image.save(f.name)
        url = NSURL.fileURLWithPath_(f.name)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(False)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
        ok, error = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(str(error))
        items = req.results() or []
        if not items:
            return ''
        text = str(items[0].topCandidates_(1)[0].string()).upper()
        letters = ''.join(c for c in text if c.isalpha())
        return letters[:1] if letters else ''


def recognize_tile(image):
    full = recognize_variant(image, 'full')
    tight = recognize_variant(image, 'tight')
    if full and full == tight:
        return full, 2
    glyph = recognize_variant(image, 'glyph')
    votes = Counter(x for x in (full, tight, glyph) if x)
    if not votes:
        return '', 0
    letter, count = votes.most_common(1)[0]
    if count >= 2 or len(votes) == 1:
        return letter, count
    return '', 0


def read_board(image, boxes):
    signatures, letters, patterns, confidence = [], [], [], []
    with db() as con:
        for box in boxes:
            tile = image.crop(box)
            sig = signature(tile)
            pattern = glyph_pattern(tile)
            signatures.append(sig)
            patterns.append(pattern)
            saved = con.execute('SELECT letter FROM tiles WHERE signature=?', (sig,)).fetchone()
            if saved:
                letter, strength = saved[0], 3
            elif sig in ocr_cache:
                letter, strength = ocr_cache[sig]
            else:
                letter = confident_template_letter(pattern)
                letter, strength = (letter, 2) if letter else recognize_tile(tile)
                ocr_cache[sig] = (letter, strength)
            letters.append(letter)
            confidence.append(strength)
    for i, letter in enumerate(letters):
        if not letter:
            guess, distance = fast_letter(patterns[i])
            if distance < .075:
                letters[i] = guess
                confidence[i] = 1
    for i, letter in enumerate(letters):
        if letter or patterns[i] is None:
            continue
        matches = sorted((((patterns[i]^patterns[j]).bit_count()/784, letters[j])
                          for j in range(16) if j != i and letters[j] and confidence[j] >= 1
                          and patterns[j] is not None))
        if matches and matches[0][0] < .09:
            letters[i] = matches[0][1]
            confidence[i] = 1
    return letters, signatures, patterns, confidence


WORDS = None
TRIE = None

def dictionary():
    global WORDS, TRIE
    if TRIE is not None:
        return TRIE
    path = Path('/usr/share/dict/words')
    words = set()
    for raw in path.read_text(errors='ignore').splitlines():
        word = raw.strip().lower()
        if (3 <= len(word) <= 16 and word.isascii() and word.isalpha()
                and raw == raw.lower()):
            words.add(word)
    WORDS = words
    root = {}
    for word in words:
        node = root
        for char in word:
            node = node.setdefault(char, {})
        node['$'] = True
    TRIE = root
    return root


@lru_cache(maxsize=100000)
def familiarity(word):
    return zipf_frequency(word, 'en')


def solve(board):
    board = [s.lower() for s in board]
    if len(board) != 16 or any(not s.isalpha() or len(s) > 2 for s in board):
        return []
    trie = dictionary()
    found = {}
    neighbors = [[j for j in range(16) if j != i and abs(j//4-i//4) <= 1 and abs(j%4-i%4) <= 1]
                 for i in range(16)]
    def walk(i, node, path, text):
        for char in board[i]:
            node = node.get(char)
            if node is None:
                return
        text += board[i]
        path = path + [i]
        if '$' in node and len(text) >= 3:
            found.setdefault(text, path)
        for j in neighbors[i]:
            if j not in path:
                walk(j, node, path, text)
    for i in range(16):
        walk(i, trie, [], '')
    with db() as con:
        adjustments = dict(con.execute('SELECT word, adjustment FROM words'))
    points = {3:100, 4:400, 5:800, 6:1400, 7:1800, 8:2200}
    result = []
    for word, path in found.items():
        feedback = adjustments.get(word, 0)
        if feedback < 0:
            continue
        freq = familiarity(word)
        minimum = 2.85 if len(word) >= 7 else (3.0 if len(word) >= 5 else 3.5)
        if freq < minimum and feedback == 0:
            continue
        score = points.get(len(word), 2200 + (len(word)-8)*400)
        rank = min(len(word), 8)*300 + max(0, len(word)-8)*80 + freq*180 + feedback*500
        result.append(dict(word=word.upper(), path=path, points=score, frequency=round(freq, 1), rank=rank))
    result.sort(key=lambda x: (-x['rank'], -x['frequency'], x['word']))
    return result


def scan():
    with lock:
        if state['playback']['running']:
            return
        available = windows()
        chosen = next((w for w in available if w['id'] == state['window']), None)
        if not chosen:
            chosen = available[0] if available else None
        if not chosen:
            state.update(status='Open Apple iPhone Mirroring to begin', window=None)
            return
        previous_window = state['window']
        previous_size = state['capture_size']
        previous_boxes = state['boxes']
        state['window'] = chosen['id']
        state['window_pid'] = chosen['pid']
        image = capture(chosen['id'])
        state['capture_size'] = image.size
        frame_signature = hashlib.sha256(image.tobytes()).hexdigest()
        if frame_signature != state.get('frame_signature'):
            small = image.copy()
            small.thumbnail((650, 1000))
            buffer = io.BytesIO()
            small.save(buffer, format='JPEG', quality=75)
            state['image'] = 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode()
            state['frame_signature'] = frame_signature
        manual = get_setting('manual_crop')
        if manual:
            crop, boxes = manual, boxes_from_crop(image, manual)
        else:
            detected = (reuse_grid(image, previous_boxes)
                        if previous_window == chosen['id'] and previous_size == image.size
                        else None)
            if detected is None:
                detected = detect_grid(image)
            crop, boxes = detected if detected else (None, None)
        state['crop'] = crop
        state['boxes'] = boxes or []
        if boxes:
            letters, sigs, patterns, confidence = read_board(image, boxes)
            state['tiles'] = sigs
            state['glyphs'] = patterns
            state['confidence'] = confidence
            board = ''.join(s[:1] if s else '?' for s in letters)
            if '?' not in board:
                if board != state['board']:
                    state['board'] = board
                    state['words'] = solve(letters)
                state['status'] = f'Live • {len(state["words"])} words'
            else:
                state['board'] = board
                state['words'] = []
                state['status'] = 'Some letters are unclear—correct the ? tiles below'
        else:
            state['board'] = ''
            state['words'] = []
            state['tiles'] = []
            state['glyphs'] = []
            state['confidence'] = []
            state['status'] = 'No 4×4 grid visible—show Word Hunt in iPhone Mirroring'
        state['updated'] = time.time()
        if state['board'] != state['stable_board']:
            state['stable_board'] = state['board']
            state['stable_since'] = time.time()
        if (state['playback']['armed'] and state['boxes'] and state['words']
                and state['board'] != state['playback'].get('after_board')
                and (not state['after_patterns'] or
                     matching_patterns(state['after_patterns'], state['glyphs']) <= 14)
                and time.time() - state['stable_since'] >= .5):
            launch_playback_locked()


def loop():
    while True:
        try:
            scan()
        except Exception as e:
            with lock:
                state['status'] = f'Capture error: {e}'
        time.sleep(0.1)


def post_mouse(kind, point):
    event = Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def swipe(points):
    if not points or play_stop.is_set():
        return
    post_mouse(Quartz.kCGEventMouseMoved, points[0])
    time.sleep(.03)
    post_mouse(Quartz.kCGEventLeftMouseDown, points[0])
    time.sleep(.06)
    last = points[0]
    try:
        for target in points[1:]:
            start = last
            for step in range(1, 9):
                if play_stop.is_set():
                    return
                point = (start[0] + (target[0]-start[0])*step/8,
                         start[1] + (target[1]-start[1])*step/8)
                post_mouse(Quartz.kCGEventLeftMouseDragged, point)
                last = point
                time.sleep(.025)
        time.sleep(.04)
    finally:
        post_mouse(Quartz.kCGEventLeftMouseUp, last)


def board_patterns(snapshot):
    image = capture(snapshot['window'])
    old_w, old_h = snapshot['size']
    new_w, new_h = image.size
    boxes = [(int(x0*new_w/old_w), int(y0*new_h/old_h),
              int(x1*new_w/old_w), int(y1*new_h/old_h))
             for x0,y0,x1,y1 in snapshot['boxes']]
    return [glyph_pattern(image.crop(box)) for box in boxes]


def matching_patterns(first, second):
    return sum(a is not None and b is not None and
               (a ^ b).bit_count()/784 < .10
               for a,b in zip(first, second))


def wait_for_board_refresh(snapshot, timeout=5):
    # A selected word briefly changes tile artwork. Require a different grid
    # that stays visually steady before reading or playing from it.
    deadline = time.monotonic() + timeout
    candidate = None
    candidate_since = 0
    time.sleep(.25)
    while time.monotonic() < deadline and not play_stop.is_set():
        patterns = board_patterns(snapshot)
        changed = 16 - matching_patterns(snapshot['patterns'], patterns)
        if changed >= 2:
            if candidate is not None and matching_patterns(candidate, patterns) >= 15:
                if time.monotonic() - candidate_since >= .35:
                    return True
            else:
                candidate = patterns
                candidate_since = time.monotonic()
        else:
            candidate = None
        time.sleep(.12)
    return False


def autoplay_word(item):
    # The visible list can be exploratory; auto-play uses only familiar entries.
    with db() as con:
        row = con.execute('SELECT adjustment FROM words WHERE word=?',
                          (item['word'].lower(),)).fetchone()
    if row and row[0] > 0:
        return True
    minimum = 3.8 if len(item['word']) >= 5 else 4.2
    return familiarity(item['word'].lower()) >= minimum


def focus_mirroring(pid):
    workspace = AppKit.NSWorkspace.sharedWorkspace()

    def is_front():
        front = workspace.frontmostApplication()
        return front is not None and front.processIdentifier() == pid

    if is_front():
        return
    target = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if target is None:
        raise RuntimeError('iPhone Mirroring is no longer open')
    target.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    if is_front():
        return

    # Raising the actual window handles the case where Play was clicked in a
    # browser that remains frontmost while its HTTP request is being handled.
    ax_app = ApplicationServices.AXUIElementCreateApplication(pid)
    err, ax_windows = ApplicationServices.AXUIElementCopyAttributeValue(
        ax_app, ApplicationServices.kAXWindowsAttribute, None)
    if err == 0 and ax_windows:
        ApplicationServices.AXUIElementPerformAction(
            ax_windows[0], ApplicationServices.kAXRaiseAction)
    ApplicationServices.AXUIElementSetAttributeValue(
        ax_app, ApplicationServices.kAXFrontmostAttribute, True)
    for _ in range(6):
        if is_front():
            return
        time.sleep(.1)

    subprocess.run(['/usr/bin/osascript', '-e',
                    'tell application "System Events" to tell process "Dock" '
                    'to click UI element "iPhone Mirroring" of list 1'],
                   capture_output=True, timeout=5)
    for _ in range(6):
        if is_front():
            return
        time.sleep(.1)
    raise RuntimeError('Could not activate iPhone Mirroring; bring its window forward and press Play again')


def play_words(snapshot):
    error = ''
    board_changed = False
    try:
        focus_mirroring(snapshot['pid'])
        item = next((word for word in snapshot['words']
                     if autoplay_word(word) and all(snapshot['confidence'][i] >= 2
                                                   for i in word['path'])), None)
        if item is None:
            raise RuntimeError('No familiar word has fully verified letters; correct the grid to continue')
        available = next((w for w in windows() if w['id'] == snapshot['window']), None)
        if not available:
            raise RuntimeError('iPhone Mirroring is no longer open')
        if matching_patterns(snapshot['patterns'], board_patterns(snapshot)) < 15:
            raise RuntimeError('Board changed before the swipe; press Play again')
        focus_mirroring(snapshot['pid'])
        bounds = available['bounds']
        if not bounds:
            raise RuntimeError('Could not locate the Mirroring window')
        width, height = snapshot['size']
        points = []
        for tile_index in item['path']:
            x0,y0,x1,y1 = snapshot['boxes'][tile_index]
            points.append((bounds['X'] + (x0+x1)/2 * bounds['Width']/width,
                           bounds['Y'] + (y0+y1)/2 * bounds['Height']/height))
        with lock:
            state['playback']['current'] = item['word']
        swipe(points)
        if not play_stop.is_set():
            with lock:
                state['playback']['current'] = f'Waiting for board refresh after {item["word"]}'
            board_changed = wait_for_board_refresh(snapshot)
            if board_changed:
                with lock:
                    state['playback']['played'] += 1
            else:
                raise RuntimeError(f'No new board appeared after {item["word"]}; playback paused')
    except Exception as exc:
        error = str(exc)
    finally:
        with lock:
            state['playback']['running'] = False
            state['playback']['current'] = ''
            state['playback']['error'] = error
            # Successful words consume tiles in this game. Resume on the new board.
            if board_changed and not play_stop.is_set():
                state['playback']['armed'] = True
                state['playback']['after_board'] = snapshot['board']
                state['after_patterns'] = snapshot['patterns']
                state['stable_board'] = ''


def launch_playback_locked():
    snapshot = {'window': state['window'], 'pid': state['window_pid'],
                'size': state['capture_size'], 'boxes': list(state['boxes']),
                'patterns': list(state['glyphs']), 'confidence': list(state['confidence']),
                'words': list(state['words']),
                'board': state['board']}
    play_stop.clear()
    played = state['playback']['played']
    state['playback'] = {'armed': False, 'running': True, 'played': played,
                         'total': len(snapshot['words']), 'current': '', 'error': ''}
    threading.Thread(target=play_words, args=(snapshot,), daemon=True).start()


@app.get('/')
def home():
    return render_template('index.html')


@app.get('/api/state')
def api_state():
    with lock:
        return jsonify({k:v for k,v in state.items() if k not in ('tiles','glyphs','confidence','boxes','capture_size','window_pid','after_patterns')})


@app.post('/api/play/start')
def api_play_start():
    if not Quartz.CGPreflightPostEventAccess():
        Quartz.CGRequestPostEventAccess()
        return jsonify(error='Allow Python to control your Mac in System Settings → Privacy & Security → Accessibility, then press Play again.'), 409
    with lock:
        if state['playback']['running']:
            return jsonify(ok=True)
        state['playback'] = {'armed': True, 'running': False, 'played': 0,
                             'total': 0, 'current': '', 'error': ''}
        state['after_patterns'] = []
        state['stable_board'] = ''
    return jsonify(ok=True, waiting=True)


@app.post('/api/play/stop')
def api_play_stop():
    play_stop.set()
    with lock:
        state['playback']['armed'] = False
    return jsonify(ok=True)


@app.post('/api/crop')
def api_crop():
    vals = request.json.get('crop', [])
    if len(vals) != 4 or any(not isinstance(v, (int,float)) for v in vals):
        return jsonify(error='Invalid crop'), 400
    x,y,w,h = vals
    if not (0 <= x < 1 and 0 <= y < 1 and .1 <= w <= 1-x and .1 <= h <= 1-y):
        return jsonify(error='Invalid crop'), 400
    put_setting('manual_crop', vals)
    return jsonify(ok=True)


@app.post('/api/reset-crop')
def api_reset_crop():
    with db() as con:
        con.execute('DELETE FROM settings WHERE key=?', ('manual_crop',))
    with lock:
        state['crop'] = None
    return jsonify(ok=True)


@app.post('/api/board')
def api_board():
    text = ''.join(c for c in request.json.get('board','').upper() if c.isalpha())
    if len(text) != 16:
        return jsonify(error='Enter exactly 16 letters'), 400
    with lock:
        with db() as con:
            for sig, letter, pattern in zip(state['tiles'], text, state['glyphs']):
                con.execute('INSERT OR REPLACE INTO tiles VALUES (?,?)', (sig,letter))
                ocr_cache[sig] = (letter, 3)
                if pattern is not None:
                    con.execute('INSERT OR REPLACE INTO glyphs VALUES (?,?,?)', (sig,letter,f'{pattern:0196x}'))
                    templates.append((letter, pattern))
        state['board'] = text
        state['confidence'] = [3] * 16
        state['words'] = solve(list(text))
        state['status'] = f'Corrected • {len(state["words"])} words'
    return jsonify(ok=True)


@app.post('/api/word')
def api_word():
    word = request.json.get('word','').lower()
    adjustment = request.json.get('adjustment')
    if not word.isalpha() or adjustment not in (-1,1):
        return jsonify(error='Invalid feedback'), 400
    with lock:
        with db() as con:
            con.execute('INSERT INTO words VALUES (?,?) ON CONFLICT(word) DO UPDATE SET adjustment=adjustment+excluded.adjustment', (word, adjustment))
        if len(state['board']) == 16 and '?' not in state['board']:
            state['words'] = solve(list(state['board']))
            state['status'] = f'Live • {len(state["words"])} words'
    return jsonify(ok=True)


if __name__ == '__main__':
    dictionary()
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with db() as con:
        templates.extend((letter, int(bits,16)) for letter,bits in con.execute('SELECT letter,bits FROM glyphs'))
    threading.Thread(target=loop, daemon=True).start()
    app.run(host='127.0.0.1', port=8765, debug=False, threaded=True)
