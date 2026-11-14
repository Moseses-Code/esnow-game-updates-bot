"""Persistent incident state and private diagnostic messages."""
from datetime import datetime, timedelta, timezone
import time


def initialise(db):
    with db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(notices)')}
        for name, kind in [('detected_at', 'REAL'), ('sent_at', 'REAL'),
                           ('last_error', 'TEXT'), ('target_chat', 'INTEGER')]:
            if name not in columns:
                db.execute(f'ALTER TABLE notices ADD COLUMN {name} {kind}')
        db.execute('''CREATE TABLE IF NOT EXISTS incidents (
            key TEXT PRIMARY KEY, name TEXT NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
            warned INTEGER NOT NULL DEFAULT 0, error TEXT, changed_at REAL NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS delivery_tests (
            update_id INTEGER PRIMARY KEY, target INTEGER NOT NULL,
            outcome TEXT NOT NULL, error TEXT)''')


def observe(db, key, name, error):
    with db:
        db.execute('INSERT OR IGNORE INTO incidents(key,name,changed_at) VALUES (?,?,?)',
                   (key, name, time.time()))
        db.execute('UPDATE incidents SET name=?, failures=CASE WHEN ? IS NULL THEN 0 ELSE failures+1 END, '
                   'error=?, changed_at=? WHERE key=?', (name, error, error, time.time(), key))


def send_diagnostics(db, config, api):
    owner = config.get('owner_chat_id')
    if not owner:
        return
    rows = db.execute('SELECT key,name,failures,warned,error,changed_at FROM incidents ORDER BY key').fetchall()
    # Bound each pass so a broad source outage does not starve normal delivery.
    sent = 0
    for key, name, failures, warned, error, changed_at in rows:
        threshold = 1 if key == 'delivery' else 3
        if failures >= threshold and not warned:
            detail = 'Уведомление осталось в очереди.' if key == 'delivery' else f'Неудачных проверок подряд: {failures}.'
            message = f'⚠️ {name}\n{detail}\nОшибка: {error}'
            new_warned = 1
        elif failures == 0 and warned:
            message = f'✅ {name}: работа восстановлена.'
            new_warned = 0
        else:
            continue
        stamp = datetime.fromtimestamp(changed_at, timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')
        api(config, 'sendMessage', {'chat_id': owner, 'text': message + '\nПроверено: ' + stamp})
        with db:
            db.execute('UPDATE incidents SET warned=? WHERE key=?', (new_warned, key))
        sent += 1
        if sent >= 5:
            break


def history(db):
    rows = db.execute('SELECT name,version,detected_at,sent,sent_at,last_error,target_chat '
                      'FROM notices ORDER BY id DESC LIMIT 10').fetchall()
    if not rows:
        return 'Обнаруженных обновлений пока нет. Первое наблюдение сохраняет исходные версии.'
    def stamp(value):
        if value is None:
            return 'время не записано (старая версия бота)'
        return datetime.fromtimestamp(value, timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')
    lines = ['Последние обнаруженные обновления:']
    for name, version, detected, sent, sent_at, error, target in rows:
        state = 'Отправлено: ' + stamp(sent_at) if sent else 'В очереди'
        if not sent and error:
            state += ' · ошибка: ' + error
        if target is not None:
            state += '\nПолучатель: ' + ('рабочая группа' if target < 0 else 'личный чат')
        lines.append(f'{name[:100]} · {version[:80]}\nОбнаружено: {stamp(detected)}\n{state}')
    return '\n\n'.join(lines)


def test_delivery(db, config, update_id, target, api):
    """Never resend a completed test when retrying its private acknowledgement."""
    owner = config['owner_chat_id']
    existing = db.execute('SELECT target,outcome,error FROM delivery_tests WHERE update_id=?', (update_id,)).fetchone()
    if existing is None:
        # Persist intent before I/O; a crash or lost response has an uncertain outcome.
        with db:
            db.execute('INSERT INTO delivery_tests VALUES (?,?,?,NULL)', (update_id, target, 'unknown'))
        try:
            api(config, 'sendMessage', {'chat_id': target, 'text': '🧪 Тест ESnow. Проверка доставки уведомлений. Это не обновление игры.'})
            outcome, error = 'sent', None
        except Exception as exc:
            outcome, error = 'failed', type(exc).__name__
        with db:
            db.execute('UPDATE delivery_tests SET outcome=?,error=? WHERE update_id=?', (outcome, error, update_id))
        existing = (target, outcome, error)
        if outcome == 'sent' and target == owner:
            return
    target, outcome, error = existing
    where = 'личный чат' if target == owner else 'рабочая группа'
    if outcome == 'sent':
        text = '✅ Тестовое сообщение отправлено: ' + where + '.'
    else:
        text = '⚠️ Доставка теста не подтверждена: ' + where + '. Ошибка: ' + (error or 'прерывание отправки') + '. Повторить: /test'
    api(config, 'sendMessage', {'chat_id': owner, 'text': text})
