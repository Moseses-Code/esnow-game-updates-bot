"""Game release notifications to the owner or working group. Python 3.12+."""
import concurrent.futures
from datetime import datetime, timedelta, timezone
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
import monitoring
from sources import fetch, read_version, source_quality

ROOT = Path(__file__).resolve().parent
MOSCOW = timezone(timedelta(hours=3))
VERSION = '2026.11.14-keyboard'
BUTTON_COMMANDS = {
    '📊 Статус': '/status',
    '🕘 Последние обновления': '/last',
    '🧪 Проверить доставку': '/test',
    '🔎 Источники': '/sources',
}


def command_keyboard():
    labels = list(BUTTON_COMMANDS)
    return {'keyboard': [[{'text': label}] for label in labels],
            'resize_keyboard': True, 'is_persistent': True, 'one_time_keyboard': False}


def api(config, method, payload):
    if (method == 'sendMessage' and config.get('owner_chat_id')
            and payload.get('chat_id') == config['owner_chat_id']):
        payload = {**payload, 'reply_markup': command_keyboard()}
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
            name TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_revisions (game TEXT PRIMARY KEY, revision TEXT NOT NULL);
    ''')
    # Old uniqueness by (game, version) suppressed a real A -> B -> A -> B transition.
    schema = db.execute("SELECT sql FROM sqlite_master WHERE name='notices'").fetchone()[0]
    if 'UNIQUE' in schema.upper():
        db.execute('BEGIN IMMEDIATE')
        with db:
            db.execute('CREATE TABLE notices_new (id INTEGER PRIMARY KEY, game TEXT NOT NULL, '
                       'version TEXT NOT NULL, name TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0)')
            db.execute('INSERT INTO notices_new SELECT id,game,version,name,sent FROM notices')
            db.execute('DROP TABLE notices')
            db.execute('ALTER TABLE notices_new RENAME TO notices')
    monitoring.initialise(db)
    return db


def remember(db, game, version):
    """Persist the version and notification atomically; first reading is a baseline."""
    with db:
        previous = db.execute('SELECT value FROM versions WHERE game=?', (game['id'],)).fetchone()
        revision = str(game.get('source_revision', 1))
        saved = db.execute('SELECT revision FROM source_revisions WHERE game=?', (game['id'],)).fetchone()
        old_revision = saved[0] if saved else '1'
        if previous and previous[0] != version and revision == old_revision:
            db.execute('INSERT INTO notices(game,version,name,detected_at) VALUES (?,?,?,?)',
                       (game['id'], version, game['name'], time.time()))
        db.execute('INSERT INTO versions VALUES (?,?) ON CONFLICT(game) DO UPDATE SET value=excluded.value',
                   (game['id'], version))
        db.execute('INSERT INTO source_revisions VALUES (?,?) ON CONFLICT(game) DO UPDATE SET revision=excluded.revision',
                   (game['id'], revision))


def destination(config):
    """A bound group takes precedence; delivery failures never change the recipient."""
    if config.get('chat_id') is not None:
        chat_id = int(config['chat_id'])
        if chat_id >= 0:
            raise ValueError('Working group ID must be negative')
        return chat_id
    owner = int(config.get('owner_chat_id', 0))
    if owner <= 0:
        raise ValueError('Use SETUP.cmd to connect your private chat')
    return owner


def deliver(db, config):
    chat_id = destination(config)
    for row in db.execute('SELECT id,name FROM notices WHERE sent=0 ORDER BY id').fetchall():
        # Only the game name goes to the working chat; no markup, buttons or status messages.
        try:
            api(config, 'sendMessage', {'chat_id': chat_id, 'text': row[1]})
        except Exception as error:
            with db:
                db.execute('UPDATE notices SET last_error=?,target_chat=? WHERE id=?',
                           (type(error).__name__, chat_id, row[0]))
            monitoring.observe(db, 'delivery', 'Доставка обновлений', type(error).__name__)
            raise
        with db:
            db.execute('UPDATE notices SET sent=1,sent_at=?,last_error=NULL,target_chat=? WHERE id=?',
                       (time.time(), chat_id, row[0]))
        monitoring.observe(db, 'delivery', 'Доставка обновлений', None)


def check(game):
    try:
        version = read_version(game)
        if not isinstance(version, str) or not version.strip():
            raise ValueError('Empty version')
        return game, version, None
    except Exception as error:
        # Never log URLs or credentials from HTTP exception messages.
        return game, None, type(error).__name__


def status_text(db, config, health):
    def stamp(value):
        return datetime.fromtimestamp(value, MOSCOW).strftime('%d.%m %H:%M:%S МСК')
    target = 'рабочая группа' if config.get('chat_id') is not None else 'твой личный чат'
    lines = ['🟢 ESnow работает', 'Версия: ' + VERSION,
             'Запущен: ' + stamp(health['started'])]
    if health['last_scan'] is None:
        lines.append('Первая проверка ещё не завершена.')
    else:
        last = health['last_scan']
        lines.extend(['Последняя проверка: ' + stamp(last['finished']),
                      f"Источники ответили: {last['ok']}/{health['total']}",
                      f"Ошибок источников: {last['errors']}"])
    if health['scanning']:
        lines.append(f"Сейчас проверено: {health['checked']}/{health['total']}")
    lines.extend([f"Интервал: {config.get('check_interval_seconds', 300)} сек.",
                  'Уведомления: ' + target])
    queued = db.execute('SELECT COUNT(*) FROM notices WHERE sent=0').fetchone()[0]
    lines.append(f'В очереди отправки: {queued}')
    if health['delivery_error']:
        lines.append('Ошибка отправки: ' + health['delivery_error'])
    if health.get('indirect'):
        lines.append(f"Косвенных источников: {health['indirect']}. Ограничения: /sources")
    if health.get('unavailable'):
        lines.append(f"Без подтверждённого источника: {health['unavailable']}. Подробнее: /sources")
    lines.append('История: /last · Проверка доставки: /test')
    return '\n'.join(lines)


def source_report(games):
    lines = ['Источники ESnow',
             'Успешный ответ источника не гарантирует обновление нужного лаунчера.',
             'Steam: публичная ветка через SteamCMD API; зеркало может запаздывать.',
             'Fortnite: полная сборка через сторонний Fortnite-API.',
             'Battle.net: EU; Minecraft, osu!, Roblox, Albion: рабочие каналы издателей.',
             '', 'Ограничения по играм:']
    for game in games:
        if source_quality(game) != 'build':
            lines.append(game['name'] + ': ' + game.get('source_note', 'косвенный источник, не манифест установленного клиента'))
    return '\n'.join(lines)


def handle_commands(db, config, health, updates, offset):
    for update in updates:
        message = update.get('message', {})
        chat = message.get('chat', {})
        owner = config.get('owner_chat_id')
        if (owner and chat.get('type') == 'private' and chat.get('id') == owner
                and message.get('from', {}).get('id') == owner):
            text = message.get('text', '').strip()
            words = text.split()
            command = BUTTON_COMMANDS.get(text, words[0].split('@')[0] if words else '')
            if command in ('/status', '/start'):
                api(config, 'sendMessage', {'chat_id': owner, 'text': status_text(db, config, health)})
            elif command == '/sources':
                report = source_report(health.get('games', []))
                if health.get('source_errors'):
                    report += '\n\nПоследние ошибки:\n' + '\n'.join(
                        name + ': ' + error for name, error in sorted(health['source_errors'].items()))
                for start in range(0, len(report), 3500):
                    api(config, 'sendMessage', {'chat_id': owner, 'text': report[start:start + 3500]})
            elif command == '/last':
                report = monitoring.history(db)
                for start in range(0, len(report), 3500):
                    api(config, 'sendMessage', {'chat_id': owner, 'text': report[start:start + 3500]})
            elif command == '/test':
                monitoring.test_delivery(db, config, update['update_id'], destination(config), api)
            elif 'Проверить обновления' in text or 'Сообщить об обновлении' in text or 'Опубликовать сводку' in text:
                api(config, 'sendMessage', {'chat_id': owner, 'text':
                    'Клавиатура обновлена. Выбери команду ниже. Игры проверяются автоматически.'})
        offset = max(offset, update['update_id'] + 1)
        with db:
            db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('telegram_offset', str(offset)))
    return offset


def run():
    config = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    destination(config)
    interval = int(config.get('check_interval_seconds', 300))
    if interval < 60:
        raise ValueError('Check interval must be at least 60 seconds')
    games = [g for g in json.loads((ROOT / 'games.json').read_text(encoding='utf-8'))
             if g.get('enabled', True)]
    if len({g['id'] for g in games}) != len(games):
        raise ValueError('Duplicate game IDs in games.json')
    db = database(ROOT / 'state.sqlite3')
    # Same port as the test bot: they must not run together on this PC.
    lock = socket.socket()
    lock.bind(('127.0.0.1', 47831))
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    commands = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    poll, next_poll = None, 0
    saved = db.execute("SELECT value FROM metadata WHERE key='telegram_offset'").fetchone()
    offset = int(saved[0]) if saved else 0
    health = dict(started=time.time(), last_scan=None, scanning=False,
                  checked=0, ok=0, errors=0, total=len(games), delivery_error=None)
    health.update(games=games, source_errors={}, indirect=sum(source_quality(g) == 'indirect' for g in games),
                  unavailable=sum(source_quality(g) == 'unavailable' for g in games))
    pending, next_scan, next_delivery, next_diagnostics = {}, 0, 0, 0
    try:
        while True:
            now = time.monotonic()
            if not pending and now >= next_scan:
                health.update(scanning=True, checked=0, ok=0, errors=0)
                pending = {executor.submit(check, game): game for game in games}
                next_scan = now + interval
            for future in list(pending):
                if not future.done():
                    continue
                game, version, error = future.result()
                del pending[future]
                health['checked'] += 1
                monitoring.observe(db, 'source:' + game['id'], game['name'], error)
                if error:
                    health['source_errors'][game['name']] = error
                    health['errors'] += 1
                    logging.warning('%s: %s', game['name'], error)
                else:
                    health['source_errors'].pop(game['name'], None)
                    health['ok'] += 1
                    remember(db, game, version)
            if health['scanning'] and not pending:
                health['last_scan'] = dict(finished=time.time(), ok=health['ok'], errors=health['errors'])
                health['scanning'] = False
            if poll is not None and poll.done():
                try:
                    offset = handle_commands(db, config, health, poll.result(), offset)
                    next_poll = now + 1
                except Exception as error:
                    logging.warning('Commands: %s', type(error).__name__)
                    saved = db.execute("SELECT value FROM metadata WHERE key='telegram_offset'").fetchone()
                    offset = int(saved[0]) if saved else offset
                    next_poll = now + 10
                poll = None
            if config.get('owner_chat_id') and poll is None and now >= next_poll:
                poll = commands.submit(api, config, 'getUpdates', {
                    'offset': offset, 'timeout': 0, 'allowed_updates': ['message']})
            if now >= next_delivery:
                try:
                    deliver(db, config)
                    health['delivery_error'] = None
                    next_delivery = now + 2
                except Exception as error:
                    health['delivery_error'] = type(error).__name__
                    logging.warning('Delivery: %s', type(error).__name__)
                    next_delivery = now + 30
            if now >= next_diagnostics:
                try:
                    monitoring.send_diagnostics(db, config, api)
                    next_diagnostics = now + 10
                except Exception as error:
                    logging.warning('Diagnostics: %s', type(error).__name__)
                    next_diagnostics = now + 60
            time.sleep(1)
    finally:
        commands.shutdown(wait=True)
        executor.shutdown(wait=True)
        db.close()
        lock.close()


def setup(group=False):
    settings = ROOT / 'settings.json'
    if group:
        config = json.loads(settings.read_text(encoding='utf-8'))
        if not config.get('owner_chat_id'):
            raise ValueError('Run SETUP.cmd to connect your private chat first')
    else:
        token = getpass.getpass('Telegram token (hidden): ').strip()
        config = {'token': token, 'check_interval_seconds': 300}
        if settings.exists():
            previous = json.loads(settings.read_text(encoding='utf-8'))
            if previous.get('token') == token:
                config = previous
    me = api(config, 'getMe', {})
    code = secrets.token_hex(8)
    if group:
        print('Add @' + me['username'] + ' to the working group. Allow sending messages.')
        print('Send as group administrator from your linked account: /connect@' + me['username'] + ' ' + code)
    else:
        print('Open your private chat: https://t.me/' + me['username'] + '?start=' + code)
        print('Press START, or send this command privately: /start ' + code)
    offset, deadline = 0, time.monotonic() + 600
    while time.monotonic() < deadline:
        updates = api(config, 'getUpdates', {'offset': offset, 'timeout': 15, 'allowed_updates': ['message']})
        for update in updates:
            offset = update['update_id'] + 1
            message = update.get('message', {})
            chat = message.get('chat', {})
            allowed_types = ('group', 'supergroup') if group else ('private',)
            if chat.get('type') not in allowed_types:
                continue
            command = message.get('text', '').strip()
            verb = '/connect' if group else '/start'
            if command not in (verb + ' ' + code, verb + '@' + me['username'] + ' ' + code):
                continue
            if message.get('sender_chat') or message.get('from', {}).get('is_bot'):
                continue
            sender = message.get('from', {}).get('id')
            if group:
                if sender != config['owner_chat_id']:
                    continue
                member = api(config, 'getChatMember', {'chat_id': chat['id'], 'user_id': sender})
                if member['status'] not in ('creator', 'administrator'):
                    print('Connection command must be sent by a group administrator.')
                    continue
                config.update(chat_id=chat['id'], chat_type=chat['type'])
            else:
                if sender != chat['id'] or sender <= 0:
                    continue
                config['owner_chat_id'] = chat['id']
            destination(config)
            settings.write_text(json.dumps(config, indent=2), encoding='utf-8')
            api(config, 'getUpdates', {'offset': offset, 'timeout': 0, 'allowed_updates': ['message']})
            print('Connected to: ' + chat.get('title', str(chat['id'])))
            print('Run AUTOSTART.cmd. First scan establishes baseline without sending old updates.')
            return
    raise RuntimeError('Connection timed out; run the connection script again')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        ROOT / 'bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')])
    try:
        if sys.argv[1:] == ['setup']:
            setup()
        elif sys.argv[1:] == ['connect-group']:
            setup(group=True)
        else:
            run()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        logging.error('Stopped: %s', type(error).__name__)
        print('Stopped:', type(error).__name__, 'Check bot.log and settings.')
        sys.exit(1)
