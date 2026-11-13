"""Group-only game release notifications. Python 3.12+, standard library."""
import concurrent.futures
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import time
from sources import fetch, read_version

ROOT = Path(__file__).resolve().parent


def api(config, method, payload):
    response = json.loads(fetch('https://api.telegram.org/bot' + config['token'] + '/' + method, payload))
    if not response.get('ok'):
        raise RuntimeError('Telegram rejected request')
    return response['result']


def database(path):
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS versions (game TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY, game TEXT NOT NULL, version TEXT NOT NULL,
            name TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
            UNIQUE(game, version));
    ''')
    return db


def remember(db, game, version):
    """Persist the version and notification atomically; first reading is a baseline."""
    with db:
        previous = db.execute('SELECT value FROM versions WHERE game=?', (game['id'],)).fetchone()
        if previous and previous[0] != version:
            db.execute('INSERT OR IGNORE INTO notices(game,version,name) VALUES (?,?,?)',
                       (game['id'], version, game['name']))
        db.execute('INSERT INTO versions VALUES (?,?) ON CONFLICT(game) DO UPDATE SET value=excluded.value',
                   (game['id'], version))


def deliver(db, config):
    for row in db.execute('SELECT id,name FROM notices WHERE sent=0 ORDER BY id').fetchall():
        # Only the game name goes to the working chat; no markup, buttons or status messages.
        api(config, 'sendMessage', {'chat_id': config['chat_id'], 'text': row[1]})
        with db:
            db.execute('UPDATE notices SET sent=1 WHERE id=?', (row[0],))


def check(game):
    try:
        version = read_version(game)
        if not isinstance(version, str) or not version.strip():
            raise ValueError('Empty version')
        return game, version, None
    except Exception as error:
        # Never log URLs or credentials from HTTP exception messages.
        return game, None, type(error).__name__


def run():
    config = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    if config.get('chat_type') not in ('group', 'supergroup') or int(config['chat_id']) >= 0:
        raise ValueError('Use SETUP.cmd to connect a working group')
    interval = int(config.get('check_interval_seconds', 300))
    if interval < 60:
        raise ValueError('Check interval must be at least 60 seconds')
    games = [g for g in json.loads((ROOT / 'games.json').read_text(encoding='utf-8'))
             if g.get('enabled', True)]
    db = database(ROOT / 'state.sqlite3')
    # Same port as the test bot: they must not run together on this PC.
    lock = socket.socket()
    lock.bind(('127.0.0.1', 47831))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    pending, next_scan, next_delivery = {}, 0, 0
    try:
        while True:
            now = time.monotonic()
            if not pending and now >= next_scan:
                pending = {executor.submit(check, game): game for game in games}
                next_scan = now + interval
            for future in list(pending):
                if not future.done():
                    continue
                game, version, error = future.result()
                del pending[future]
                if error:
                    logging.warning('%s: %s', game['name'], error)
                else:
                    remember(db, game, version)
            if now >= next_delivery:
                try:
                    deliver(db, config)
                    next_delivery = now + 2
                except Exception as error:
                    logging.warning('Delivery: %s', type(error).__name__)
                    next_delivery = now + 30
            time.sleep(1)
    finally:
        executor.shutdown(wait=True)
        db.close()
        lock.close()


def setup():
    token = getpass.getpass('Telegram token (hidden): ').strip()
    config = {'token': token, 'check_interval_seconds': 300}
    me = api(config, 'getMe', {})
    code = secrets.token_hex(8)
    print('Add @' + me['username'] + ' to the working group. Allow sending messages.')
    print('Send this command IN THAT GROUP: /connect@' + me['username'] + ' ' + code)
    offset, deadline = 0, time.monotonic() + 600
    while time.monotonic() < deadline:
        updates = api(config, 'getUpdates', {'offset': offset, 'timeout': 15, 'allowed_updates': ['message']})
        for update in updates:
            offset = update['update_id'] + 1
            message = update.get('message', {})
            chat = message.get('chat', {})
            if chat.get('type') not in ('group', 'supergroup'):
                continue
            command = message.get('text', '').strip()
            if command not in ('/connect ' + code, '/connect@' + me['username'] + ' ' + code):
                continue
            if message.get('sender_chat') or message.get('from', {}).get('is_bot'):
                continue
            member = api(config, 'getChatMember', {'chat_id': chat['id'], 'user_id': message['from']['id']})
            if member['status'] not in ('creator', 'administrator'):
                print('Connection command must be sent by a group administrator.')
                continue
            config.update(chat_id=chat['id'], chat_type=chat['type'])
            (ROOT / 'settings.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
            api(config, 'getUpdates', {'offset': offset, 'timeout': 0, 'allowed_updates': ['message']})
            print('Connected to: ' + chat.get('title', str(chat['id'])))
            print('Run AUTOSTART.cmd. First scan establishes baseline without sending old updates.')
            return
    raise RuntimeError('Connection timed out; run SETUP.cmd again')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        ROOT / 'bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')])
    try:
        setup() if sys.argv[1:] == ['setup'] else run()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logging.error('Stopped: %s', type(error).__name__)
        print('Stopped:', type(error).__name__, 'Check bot.log and settings.')
        sys.exit(1)
