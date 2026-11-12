"""ESnow update monitor. Python 3.12+, standard library only."""
import concurrent.futures
import datetime as dt
import getpass
import html
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
MSK = dt.timezone(dt.timedelta(hours=3), 'MSK')
INTERVAL = 900

def fetch(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        'User-Agent': 'ESnowUpdateMonitor/1.0', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode('utf-8-sig')

def get_json(url):
    return json.loads(fetch(url))

def fetch_public_page(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=25) as response:
        return response.read().decode('utf-8-sig')

def read_version(g):
    kind = g.get('source')
    if kind == 'steam':
        app = str(g['appid'])
        d = get_json('https://api.steamcmd.net/v1/info/' + app)['data'][app]
        if d.get('common', {}).get('releasestate', 'released') != 'released':
            raise ValueError('Игра не отмечена как выпущенная в Steam')
        branch = d['depots']['branches']['public']
        v = str(branch['buildid'])
        if not v.isdigit() or v == '0':
            raise ValueError('Нет публичной сборки')
        return v
    if kind == 'minecraft':
        d = get_json('https://piston-meta.mojang.com/mc/game/version_manifest_v2.json')
        v = d['latest']['release']
        if not any(x['id'] == v and x['type'] == 'release' for x in d['versions']):
            raise ValueError('Нет подтверждённого стабильного релиза')
        return v
    if kind == 'riot_lol':
        versions = get_json('https://ddragon.leagueoflegends.com/api/versions.json')
        if not versions or not isinstance(versions[0], str):
            raise ValueError('Riot не вернул версию League of Legends')
        return versions[0]
    if kind == 'riot_valorant':
        data = get_json('https://valorant-api.com/v1/version')['data']
        return data['version']
    if kind == 'steam_patch_news':
        app = str(g['appid'])
        items = get_json('https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=' + app + '&count=100&maxlength=0')['appnews']['newsitems']
        for item in items:
            title = item.get('title', '')
            if item.get('feedlabel') == 'Community Announcements' and re.search(r'(?i)\b(update|patch|hotfix|обновлени|патч|исправлени)', title):
                return str(item['gid'])
        raise ValueError('Нет подтверждённых патч-нотов среди последних новостей Steam')
    if kind == 'lesta_wiki':
        query = urllib.parse.urlencode({'action': 'parse', 'page': g['page'], 'prop': 'wikitext', 'format': 'json'})
        content = get_json('https://wiki.lesta.ru/api.php?' + query)['parse']['wikitext']['*']
        found = re.search(r'(?i)(?:Обновление\s*[= ]\s*|Версия\s*)(\d+(?:\.\d+){1,3})', content)
        if not found:
            raise ValueError('Версия не найдена на официальной Lesta Wiki')
        return found.group(1)
    if kind == 'tarkov_arena_news':
        page = fetch_public_page('https://arena.tarkov.com/news')
        versions = re.findall(r'Escape from Tarkov: Arena\s+(\d+(?:\.\d+){2,4})\s+patch notes', page, re.I)
        if not versions:
            raise ValueError('Не найдены официальные патч-ноты Arena')
        return max(versions, key=lambda s: tuple(map(int, s.split('.'))))
    if kind == 'majestic_news':
        page = fetch_public_page('https://t.me/s/majestic_gta5')
        posts = re.findall(r'data-post="majestic/(\d+)"([\s\S]*?)(?=data-post="majestic/|$)', page)
        matches = [int(post_id) for post_id, body in posts if re.search(r'(?i)Обновление:', body)]
        if not matches:
            raise ValueError('Не найдена официальная публикация Majestic об обновлении')
        return str(max(matches))
    if kind == 'battle':
        raw = fetch('https://eu.version.battle.net/v2/products/' + g['product'] + '/versions')
        rows = [x.split('|') for x in raw.splitlines() if x and not x.startswith('#')]
        columns = [x.split('!')[0] for x in rows[0]]
        for row in rows[1:]:
            d = dict(zip(columns, row))
            if d.get('Region') == g.get('region', 'eu'):
                return d['VersionsName'] + ' / ' + d['BuildId']
        raise ValueError('Регион Battle.net не найден')
    if kind == 'albion':
        d = ET.fromstring(fetch('https://live.albiononline.com/autoupdate/manifest.xml'))
        return d.find('./albiononline/win32/fullinstall').attrib['version']
    if kind == 'roblox':
        d = get_json('https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer/channel/LIVE')
        return d['version'] + ' / ' + d['clientVersionUpload']
    if kind == 'osu':
        d = get_json('https://osu.ppy.sh/web/check-updates.php?action=check&stream=stable40')
        files = sorted((x['filename'], x['file_hash']) for x in d)
        if not any(x[0].lower() == 'osu!.exe' for x in files):
            raise ValueError('Нет osu!.exe в манифесте Stable')
        return hashlib.sha256(json.dumps(files).encode()).hexdigest()[:16]
    raise ValueError(g.get('reason', 'Источник ещё не подключён'))

def safe_check(g):
    try:
        v = read_version(g)
        if not isinstance(v, str) or not v.strip():
            raise ValueError('Пустая версия')
        return g, v, None
    except Exception as e:
        # Do not print HTTP URLs: Telegram URLs contain a token.
        return g, None, type(e).__name__

def connect(path=None):
    c = sqlite3.connect(path or ROOT / 'state.sqlite3')
    c.row_factory = sqlite3.Row
    c.executescript('''
        CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, version TEXT, checked REAL,
            error TEXT, baseline REAL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, game TEXT, seen REAL,
            old TEXT, new TEXT, uncertain INTEGER);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS gaps(start REAL, end REAL);
        CREATE TABLE IF NOT EXISTS outbox(slot REAL PRIMARY KEY, parts TEXT, next INTEGER DEFAULT 0,
            sent INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS subscribers(chat_id INTEGER PRIMARY KEY, started REAL,
            username TEXT, first_name TEXT);
        CREATE TABLE IF NOT EXISTS deliveries(slot REAL, chat_id INTEGER, next INTEGER DEFAULT 0,
            sent INTEGER DEFAULT 0, PRIMARY KEY(slot, chat_id));
    ''')
    return c

def meta(c, key, default=None):
    r = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return r[0] if r else default

def put(c, key, value):
    c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))

def record(c, g, version, error, now):
    old = c.execute('SELECT * FROM games WHERE id=?', (g['id'],)).fetchone()
    if error:
        c.execute('INSERT INTO games(id,error) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET error=excluded.error', (g['id'], error))
    else:
        if old and old['version'] and old['version'] != version:
            uncertain = not old['checked'] or now - old['checked'] > INTERVAL * 2
            c.execute('INSERT INTO events(game,seen,old,new,uncertain) VALUES (?,?,?,?,?)',
                      (g['id'], now, old['version'], version, int(uncertain)))
        c.execute('INSERT INTO games VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                  'version=excluded.version, checked=excluded.checked, error=NULL, '
                  'baseline=COALESCE(games.baseline,excluded.baseline)',
                  (g['id'], version, now, None, now))


def scan(c, games):
    enabled = [g for g in games if g.get('enabled', True) and g.get('source')]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for g, v, e in pool.map(safe_check, enabled):
            with c:
                record(c, g, v, e, time.time())
    with c:
        put(c, 'last_scan', time.time())


def boundary(now):
    local = dt.datetime.fromtimestamp(now, MSK)
    slots = [local.replace(hour=h, minute=30, second=0, microsecond=0) for h in (8, 20)]
    slots += [slots[1] - dt.timedelta(days=1)]
    return max(x.timestamp() for x in slots if x.timestamp() <= now)


def split_text(text, limit=3500):
    chunks, current = [], ''
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = ''
        current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


def report(c, games, start, end):
    night = dt.datetime.fromtimestamp(end, MSK).hour == 8
    period = 'За ночь' if night else 'За день'
    events = c.execute('SELECT * FROM events WHERE seen>? AND seen<=? ORDER BY seen', (start, end)).fetchall()
    changed = {}
    for e in events:
        changed[e['game']] = e
    index = {g['id']: g for g in games}
    icon = '🌙' if night else '☀️'
    if changed:
        lines = ['<b>' + icon + ' ' + period + ' вышли обновления</b>']
        for key, e in changed.items():
            g = index.get(key, {'name': key, 'launcher': '?'})
            size = g.get('update_size')
            lines.append('• ' + html.escape(g['name']) + (' — ' + html.escape(size) if size else ''))
    else:
        lines = ['<b>' + icon + ' ' + period + ' обновлений не вышло</b>']
    states = {r['id']: r for r in c.execute('SELECT * FROM games')}
    manual = []
    for g in games:
        if not g.get('enabled', True):
            continue
        s = states.get(g['id'])
        if not g.get('source'):
            manual.append(g['name'])
        elif not s or s['error'] or not s['checked'] or end - s['checked'] > INTERVAL * 2:
            manual.append(g['name'])
    if manual:
        lines += ['', '<b>❗ Проверь вручную</b>'] + ['• ' + html.escape(name) for name in manual]
    return '\n'.join(lines)


def telegram(config, method, payload):
    d = json.loads(fetch('https://api.telegram.org/bot' + config['token'] + '/' + method, payload))
    if not d.get('ok'):
        raise RuntimeError('Telegram API rejected request')
    return d['result']


MENU = {
    'keyboard': [
        [{'text': 'Проверить обновления', 'icon_custom_emoji_id': '5429571366384842791', 'style': 'success'}],
        [{'text': 'Сообщить об обновлении ❗'}],
    ],
    'resize_keyboard': True,
    'is_persistent': True,
}


def send(config, chat_id, text):
    telegram(config, 'sendMessage', {
        'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'reply_markup': MENU})


def queue_reports(c, games, now):
    latest = boundary(now)
    last = float(meta(c, 'last_slot', latest))
    while last < latest:
        slot = last + 43200
        parts = split_text(report(c, games, last, slot))
        with c:
            c.execute('INSERT OR IGNORE INTO outbox(slot,parts) VALUES (?,?)', (slot, json.dumps(parts, ensure_ascii=False)))
            put(c, 'last_slot', slot)
        last = slot


def deliver(c, config):
    for r in c.execute('SELECT * FROM outbox ORDER BY slot').fetchall():
        parts = json.loads(r['parts'])
        subscribers = c.execute('SELECT chat_id FROM subscribers WHERE started<=?', (r['slot'],)).fetchall()
        for user in subscribers:
            with c:
                c.execute('INSERT OR IGNORE INTO deliveries(slot,chat_id) VALUES (?,?)',
                          (r['slot'], user['chat_id']))
        pending = c.execute('SELECT * FROM deliveries WHERE slot=? AND sent=0', (r['slot'],)).fetchall()
        for delivery in pending:
            try:
                for i in range(delivery['next'], len(parts)):
                    send(config, delivery['chat_id'], parts[i])
                    with c:
                        c.execute('UPDATE deliveries SET next=? WHERE slot=? AND chat_id=?',
                                  (i + 1, r['slot'], delivery['chat_id']))
                with c:
                    c.execute('UPDATE deliveries SET sent=1 WHERE slot=? AND chat_id=?',
                              (r['slot'], delivery['chat_id']))
            except Exception as e:
                logging.warning('Delivery to subscriber failed: %s', type(e).__name__)


def setup():
    print('ESnow: открытый бот. Токен хранится только на этом компьютере.')
    token = getpass.getpass('Введите токен от BotFather (ввод скрыт): ').strip()
    config = {'token': token}
    me = telegram(config, 'getMe', {})
    print('Откройте https://t.me/' + me['username'])
    print('Установка завершена. Любой пользователь может открыть эту ссылку и нажать /start.')
    p = ROOT / 'settings.json'
    p.write_text(json.dumps(config, indent=2), encoding='utf-8')
    if os.name != 'nt':
        p.chmod(0o600)
    c = connect()
    updates = telegram(config, 'getUpdates', {'timeout': 0, 'allowed_updates': ['message']})
    with c:
        if updates:
            put(c, 'offset', max(x['update_id'] for x in updates) + 1)
    c.close()


def main():
    # A process-held socket prevents duplicate instances, including scheduled/manual starts.
    lock = socket.socket()
    try:
        lock.bind(('127.0.0.1', 47831))
    except OSError:
        print('ESnow уже запущен или порт 47831 занят.')
        return
    config = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    games = json.loads((ROOT / 'games.json').read_text(encoding='utf-8'))
    # Migrate installations where only bot.py was replaced but games.json stayed old.
    source_overrides = {
        'game-002': ('steam', 291480), 'game-007': ('steam', 3932890),
        'game-009': ('steam', 1172470), 'game-011': ('steam', 252950),
        'game-012': ('steam', 289070), 'game-013': ('steam', 699130),
        'game-031': ('steam_patch_news', 386180),
        'game-032': ('steam_patch_news', 1422450),
    }
    for g in games:
        if g['id'] == 'game-113':
            g['enabled'] = False
        elif g['id'] in source_overrides:
            g['source'], g['appid'] = source_overrides[g['id']]
            g.pop('reason', None)
        elif g['id'] == 'game-021':
            g['source'] = 'riot_lol'
            g.pop('reason', None)
        elif g['id'] == 'game-023':
            g['source'] = 'riot_valorant'
            g.pop('reason', None)
        elif g['id'] in ('game-016', 'game-017', 'game-018'):
            g['source'] = 'lesta_wiki'
            g['page'] = {'game-016': 'Мир_кораблей:Список_обновлений',
                         'game-017': 'Мир_танков:Список_обновлений',
                         'game-018': 'Tanks_Blitz:Список_обновлений'}[g['id']]
            g.pop('reason', None)
        elif g['id'] == 'game-008':
            g['source'] = 'tarkov_arena_news'
            g.pop('reason', None)
        elif g['id'] == 'game-019':
            g['source'] = 'majestic_news'
            g.pop('reason', None)
    c = connect()
    now = time.time()
    with c:
        if meta(c, 'created') is None:
            put(c, 'created', now)
            put(c, 'last_slot', boundary(now))
        heartbeat = float(meta(c, 'heartbeat', now))
        if now - heartbeat > INTERVAL * 2:
            c.execute('INSERT INTO gaps VALUES (?,?)', (heartbeat, now))
        put(c, 'heartbeat', now)
    last_poll = 0
    last_scan = float(meta(c, 'last_scan', 0))
    last_manual = {}
    manual_report_after_scan = set()
    # Scan in a worker so reporting and Telegram stay responsive during network failures.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = None
    def background_scan():
        worker_db = connect()
        try:
            scan(worker_db, games)
        finally:
            worker_db.close()
    while True:
        now = time.time()
        with c:
            put(c, 'heartbeat', now)
        if future and future.done():
            try:
                future.result()
                last_scan = time.time()
                logging.info('Scan finished')
                if manual_report_after_scan:
                    for chat_id in manual_report_after_scan:
                        for part in split_text(report(c, games, boundary(last_scan), last_scan)):
                            send(config, chat_id, part)
                    manual_report_after_scan.clear()
            except Exception as e:
                logging.error('Scan failed: %s', type(e).__name__)
                last_scan = time.time() - INTERVAL + 60
                for chat_id in manual_report_after_scan:
                    send(config, chat_id, 'Внеочередная проверка не завершилась. Повторите кнопку через минуту.')
                manual_report_after_scan.clear()
            future = None
        if future is None and now - last_scan >= INTERVAL:
            future = pool.submit(background_scan)
        try:
            queue_reports(c, games, now)
            deliver(c, config)
            if now - last_poll >= 5:
                last_poll = now
                offset = int(meta(c, 'offset', 0))
                updates = telegram(config, 'getUpdates', {'offset': offset, 'timeout': 0, 'allowed_updates': ['message', 'callback_query']})
                for u in updates:
                    callback = u.get('callback_query')
                    m = callback.get('message', {}) if callback else u.get('message', {})
                    if m.get('chat', {}).get('type') == 'private':
                        chat_id = m['chat']['id']
                        cmd = callback.get('data', '') if callback else m.get('text', '').split('@')[0].strip()
                        if callback:
                            telegram(config, 'answerCallbackQuery', {'callback_query_id': callback['id']})
                        if cmd == '/start':
                            already = c.execute('SELECT 1 FROM subscribers WHERE chat_id=?', (chat_id,)).fetchone()
                            with c:
                                c.execute('INSERT INTO subscribers VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name',
                                          (chat_id, now, m.get('from', {}).get('username'), m.get('from', {}).get('first_name')))
                            if not already:
                                send(config, chat_id, '<b><tg-emoji emoji-id="5785358395531859004">🧊</tg-emoji> ESnow Updates</b>\n\nОтчёт: <b>08:30 и 20:30 МСК</b>.\nПроверка и сводка — кнопками ниже.')
                            elif now - last_manual.get(chat_id, 0) > 10:
                                send(config, chat_id, '<b><tg-emoji emoji-id="5785358395531859004">🧊</tg-emoji> ESnow Updates</b>\n\nОтчёт: <b>08:30 и 20:30 МСК</b>.')
                            last_manual[chat_id] = now
                        elif not c.execute('SELECT 1 FROM subscribers WHERE chat_id=?', (chat_id,)).fetchone():
                            send(config, chat_id, '<b><tg-emoji emoji-id="5785358395531859004">🧊</tg-emoji> ESnow Updates</b>\n\nНажмите /start, чтобы получать отчёты.')
                        elif cmd in ('/status',) and now - last_manual.get(chat_id, 0) > 5:
                            last_manual[chat_id] = now
                            rows = c.execute('SELECT COUNT(*) FROM games WHERE checked IS NOT NULL AND error IS NULL AND checked>?', (now - 1800,)).fetchone()[0]
                            send(config, chat_id, f'<b>🟢 ESnow Updates работает</b>\n\nВремя: <b>{dt.datetime.now(MSK):%d.%m %H:%M} МСК</b>\nСвежие проверки: <b>{rows}/{len(games)}</b>\nПодписчиков: <b>{c.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]}</b>\n\nОтчёты: <b>08:30</b> и <b>20:30</b>.')
                        elif cmd in ('/report', 'publish_report', 'Сообщить об обновлении ❗') and now - last_manual.get(chat_id, 0) > 10:
                            last_manual[chat_id] = now
                            for part in split_text(report(c, games, boundary(now), now)):
                                send(config, chat_id, part)
                        elif cmd in ('check_updates', 'Проверить обновления') and now - last_manual.get(chat_id, 0) > 5:
                            last_manual[chat_id] = now
                            manual_report_after_scan.add(chat_id)
                            if future is None:
                                future = pool.submit(background_scan)
                                send(config, chat_id, '<b><tg-emoji emoji-id="5256172434154866918">⏳</tg-emoji> Проверяю обновления</b>\n\nСводка придёт после завершения.')
                            else:
                                send(config, chat_id, '<b><tg-emoji emoji-id="5256172434154866918">⏳</tg-emoji> Проверка уже идёт</b>\n\nСводка придёт после её завершения.')
                    with c:
                        put(c, 'offset', u['update_id'] + 1)
        except Exception as e:
            logging.warning('Telegram/report operation failed: %s', type(e).__name__)
            time.sleep(10)
        time.sleep(2)


if __name__ == '__main__':
    handler = RotatingFileHandler(ROOT / 'bot.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(handlers=[handler], level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if len(sys.argv) > 1 and sys.argv[1] == 'setup':
            setup()
        else:
            main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.error('Fatal: %s', type(e).__name__)
        print('Ошибка:', type(e).__name__, '— проверьте настройки и интернет. Подробности без токена в bot.log.')
        sys.exit(1)
