#!/usr/bin/env python3
"""Run PostgreSQL regression or scale checks in a dedicated disposable database."""
import argparse
import json
import os
import subprocess
import tempfile
import uuid
from urllib.parse import urlsplit, urlunsplit

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['test', 'benchmark'])
    parser.add_argument('--image', choices=['fireisp:qa', 'fireisp:staging'], default='fireisp:qa')
    options = parser.parse_args()
    if os.geteuid() != 0: parser.error('Run as root to read installer configuration.')
    compose = ['docker', 'compose', '--project-directory', '/opt/fireisp/staging']
    configuration = json.loads(subprocess.check_output(compose + ['config', '--format', 'json'], text=True))
    values = configuration['services']['web']['environment']
    database_name = ('fireisp_bench_' if options.action == 'benchmark' else 'fireisp_test_') + uuid.uuid4().hex[:10]
    original = urlsplit(values['DATABASE_URL'])
    values['DATABASE_URL'] = urlunsplit((original.scheme, original.netloc, '/' + database_name, original.query, original.fragment))
    values['DEBUG'] = 'true'
    values['REDIS_URL'] = ''
    names = [database_name] if options.action == 'benchmark' else [database_name, 'test_' + database_name]
    created = []
    try:
        for name in names:
            subprocess.run(compose + ['exec', '-T', 'db', 'createdb', '-U', 'postgres', '-O', 'fireisp', name], check=True)
            created.append(name)
        with tempfile.NamedTemporaryFile('w', prefix='fireisp-check-', dir='/etc/fireisp') as env:
            env.write(''.join(f'{key}={value}\n' for key, value in values.items())); env.flush()
            command = ['docker', 'run', '--rm', '--network', 'fireisp-staging_default', '--env-file', env.name,
                       '--memory', '650m', '--cpus', '1.5', options.image, 'python', 'manage.py']
            if options.action == 'benchmark':
                subprocess.run(command + ['migrate', '--noinput'], check=True, stdout=subprocess.DEVNULL)
                subprocess.run(command + ['benchmark', '--customers', '20000'], check=True)
            else:
                subprocess.run(command + ['test', '--noinput', '--keepdb'], check=True)
    finally:
        for name in reversed(created):
            subprocess.run(compose + ['exec', '-T', 'db', 'dropdb', '-U', 'postgres', '--if-exists', name], check=True)

if __name__ == '__main__': main()
