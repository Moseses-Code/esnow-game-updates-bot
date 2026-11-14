"""ESnow update monitor. Python 3.12+, standard library only."""
import hashlib
import html
import json
import re
import time
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
TLS = ssl.create_default_context()
if (ROOT / 'cacert.pem').exists():
    TLS.load_verify_locations(str(ROOT / 'cacert.pem'))


class SourceUnavailable(ValueError):
    """No verified source for this edition/launcher."""


def source_quality(game):
    kind = game.get('source')
    if game.get('signal_only'):
        return 'indirect'
    if kind == 'unsupported':
        return 'unavailable'
    if kind == 'steam' and game.get('launcher') == 'Steam':
        return 'build'
    if kind in ('battle', 'minecraft', 'albion', 'roblox', 'osu', 'fortnite_build'):
        return 'build'
    return 'indirect'


def released_patch(title):
    """Conservative title filter; news remains an indirect release signal."""
    if re.search(r'(?i)\b(preview|upcoming|coming|planned|schedule|roadmap|test|playtest|beta|ptr|pts)\b|'
                 r'new features|sneak peek|\bПТС\b|скоро|анонс|планиру|предзагруз|тестов', title):
        return False
    return bool(re.search(r'(?i)\b(patch|hotfix)\b|\bupdate\s*[-:#v\d]|'
                          r'\bupdate\s+(?:is\s+)?(?:out now|live)|'
                          r'\b(?:small|minor|major|gameplay) update\b|'
                          r'обновление\s*[\d\[]|обновление в игре|обновление.*установлен|исправления\s*[\d\[]', title))

def fetch(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        'User-Agent': 'ESnowUpdateMonitor/1.0', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=25, context=TLS) as r:
        return r.read().decode('utf-8-sig')

def get_json(url):
    return json.loads(fetch(url))

def head_etag(url):
    with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=25, context=TLS) as response:
        etag = response.headers.get('ETag')
        if not etag:
            raise ValueError('Источник не вернул ETag')
        return etag

def fetch_public_page(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=25, context=TLS) as response:
        return response.read().decode('utf-8-sig')

def read_version(g):
    kind = g.get('source')
    if kind == 'steam':
        app = str(g['appid'])
        d = get_json('https://api.steamcmd.net/v1/info/' + app)['data'][app]
        if d.get('common', {}).get('releasestate', 'released') != 'released':
            raise ValueError('Игра не отмечена как выпущенная в Steam')
        branch = d['depots']['branches']['public']
        if str(branch.get('pwdrequired', '0')) != '0':
            raise ValueError('Публичная ветка требует пароль')
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
        if not versions or not isinstance(versions[0], str) or not re.fullmatch(r'\d+\.\d+\.\d+', versions[0]):
            raise ValueError('Некорректная версия Riot Data Dragon')
        return versions[0]
    if kind == 'riot_valorant':
        data = get_json('https://valorant-api.com/v1/version')['data']
        return data['version']
    if kind == 'steam_patch_news':
        app = str(g['appid'])
        items = get_json('https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=' + app + '&count=100&maxlength=0')['appnews']['newsitems']
        for item in sorted(items, key=lambda x: int(x.get('date', 0)), reverse=True):
            title = item.get('title', '')
            if (item.get('feedlabel') == 'Community Announcements'
                    and 0 < int(item.get('date', 0)) <= time.time()
                    and released_patch(title)):
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
    if kind == 'fortnite_build':
        # Third-party mirror: retain CL so hotfixes within one release are visible.
        build = get_json('https://fortnite-api.com/v2/aes')['data']['build']
        match = re.fullmatch(r'\+\+Fortnite\+Release-(\d+\.\d+(?:\.\d+)?)-CL-(\d+)', build)
        if not match:
            raise ValueError('Не найдена текущая сборка Fortnite')
        return match.group(1) + '-CL-' + match.group(2)
    if kind == 'warface_news':
        # Official Russian edition only. Test-server posts and previews are excluded.
        candidates = []
        for page_number in range(3):
            page = fetch_public_page('https://ru.warface.com/news/?page=' + str(page_number))
            for post_id, title in re.findall(r'<a[^>]+href="/news/(\d+)\.html"[^>]*>(.*?)</a>', page, re.S):
                title = html.unescape(re.sub(r'<[^>]+>', '', title)).strip()
                if released_patch(title):
                    candidates.append(int(post_id))
        if not candidates:
            raise SourceUnavailable('Нет подтверждённой публикации об обновлении Warface RU в последних новостях')
        return str(max(candidates))
    if kind == 'genshin_news':
        query = urllib.parse.urlencode({'gids': 2, 'type': 1, 'page_size': 50})
        posts = get_json('https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList?' + query)['data']['list']
        versions = []
        for item in posts:
            subject = item['post']['subject']
            match = re.search(r'(?i)Version\s+([0-9]+(?:\.[0-9]+)+)\s+Update Details', subject)
            if match:
                versions.append(match.group(1))
        if not versions:
            raise ValueError('Не найдены опубликованные HoYoLAB Update Details')
        return max(versions, key=lambda s: tuple(map(int, s.split('.'))))
    if kind == 'lor_assets':
        # Riot says LoR Data Dragon assets are updated together with game releases.
        parts = ['core', 'adventure'] + ['set' + str(i) for i in range(1, 10)]
        tags = [head_etag('https://dd.b.pvp.net/latest/' + part + '-en_us.zip') for part in parts]
        return hashlib.sha256('|'.join(tags).encode()).hexdigest()[:24]
    if kind == 'battle':
        raw = fetch('https://eu.version.battle.net/v2/products/' + g['product'] + '/versions')
        rows = [x.split('|') for x in raw.splitlines() if x and not x.startswith('#')]
        columns = [x.split('!')[0] for x in rows[0]]
        for row in rows[1:]:
            d = dict(zip(columns, row))
            if d.get('Region') == g.get('region', 'eu'):
                if not d.get('VersionsName', '').strip() or not d.get('BuildId', '').isdigit():
                    raise ValueError('Неполная версия Battle.net')
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
        if not isinstance(d, list) or not d:
            raise SourceUnavailable('osu! не вернул манифест Stable')
        if any(not isinstance(x, dict) or not isinstance(x.get('filename'), str)
               or not isinstance(x.get('file_hash'), str) or not x['file_hash'] for x in d):
            raise ValueError('Неполный манифест osu!')
        files = sorted((x['filename'], x['file_hash']) for x in d)
        if not any(x[0].lower() == 'osu!.exe' for x in files):
            raise ValueError('Нет osu!.exe в манифесте Stable')
        return hashlib.sha256(json.dumps(files).encode()).hexdigest()[:16]
    raise ValueError(g.get('reason', 'Источник ещё не подключён'))
