import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
from snapshot_audit import PsqlQuery, as_bool

parser = argparse.ArgumentParser()
parser.add_argument('--archive', required=True)
parser.add_argument('--database', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
listed = subprocess.run(['pg_restore', '--list', args.archive], check=True, capture_output=True, text=True).stdout
entries = [line for line in listed.splitlines() if not line.startswith(';') and ' SEQUENCE SET ' in line]
with tempfile.NamedTemporaryFile(mode='w', suffix='.list') as selected:
    selected.write('\n'.join(entries) + '\n')
    selected.flush()
    sql = subprocess.run(['pg_restore', '--file=-', '--use-list=' + selected.name, args.archive],
        check=True, capture_output=True, text=True).stdout
expected = re.findall(r"SELECT pg_catalog.setval\('((?:[^']|'')+)', (-?\d+), (true|false)\);", sql)
assert len(expected) == len(entries) and len(expected) > 0, 'Incomplete sequence archive parse'
query = PsqlQuery(args.database)
differences = []
for name, value, called in expected:
    regclass = name.replace("''", "'")
    # Sequence names come from the trusted pg_dump archive. Resolve through catalogs
    # and quote each physical identifier before querying the sequence relation.
    literal = "'" + regclass.replace("'", "''") + "'"
    table = query("SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname) AS name "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=" + literal + "::regclass AND c.relkind='S'")[0]['name']
    actual = query('SELECT last_value,is_called FROM ' + table)[0]
    if int(actual['last_value']) != int(value) or as_bool(actual['is_called']) != (called == 'true'):
        differences.append({'sequence': regclass, 'expected_value': int(value), 'actual_value': int(actual['last_value']),
            'expected_called': called == 'true', 'actual_called': as_bool(actual['is_called'])})
result = {'passed': not differences, 'sequence_values_checked': len(expected), 'differences': differences,
    'reference': 'SEQUENCE SET entries extracted from database.dump; values are not MVCC snapshot data.'}
Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
raise SystemExit(0 if result['passed'] else 1)
