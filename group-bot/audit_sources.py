"""Read-only source check. Does not read tokens, edit state or send messages."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from sources import read_version, source_quality

ROOT = Path(__file__).resolve().parent


def probe(game):
    result = {key: game[key] for key in ('id', 'name', 'launcher', 'source')}
    result['quality'] = source_quality(game)
    result['limitation'] = game.get('source_note')
    try:
        version = read_version(game)
        if not isinstance(version, str) or not version.strip():
            raise ValueError('Empty version')
        result.update(ok=True, version=version)
    except Exception as error:
        result.update(ok=False, error=type(error).__name__)
    result['checked_at'] = datetime.now(timezone.utc).isoformat()
    return result


def main():
    games = [g for g in json.loads((ROOT / 'games.json').read_text(encoding='utf-8'))
             if g.get('enabled', True)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(probe, games))
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'games': results}
    (ROOT / 'source-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Sources answered: {sum(r['ok'] for r in results)}/{len(results)}")
    print(f"Indirect signals: {sum(r['quality'] == 'indirect' for r in results)}")
    for result in results:
        if not result['ok']:
            print(result['name'] + ': ' + result['error'])
    print('Details: source-check.json. Successful response does not prove complete launcher coverage.')


if __name__ == '__main__':
    main()
