#!/usr/bin/env python3
"""Read-only Odoo/PostgreSQL snapshot collector and restore comparator.

collect(query) accepts a callback query(sql) -> list[dict]. The source caller
must provide its already-open, exported, repeatable-read/read-only transaction.
No business rows are returned: only row counts and aggregate content hashes.
Sequence current values are deliberately excluded because they are not MVCC.
"""
import argparse
import csv
import io
import json
import subprocess
import sys
from pathlib import Path


FORMAT = "odoo-db-snapshot-audit-v1"
csv.field_size_limit(64 * 1024 * 1024)
USER_SCHEMAS = "n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'"


def ident(value):
    return '"' + str(value).replace('"', '""') + '"'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def as_bool(value):
    if isinstance(value, bool):
        return value
    if value in ("t", "true", "True", "1", 1):
        return True
    if value in ("f", "false", "False", "0", 0):
        return False
    if value is None or value == "":
        return None
    raise ValueError("Unexpected boolean representation")


def normalize(rows, ints=(), bools=(), nulls=()):
    result = []
    for original in rows:
        row = dict(original)
        for key in ints:
            row[key] = None if row.get(key) in (None, "") else int(row[key])
        for key in bools:
            row[key] = as_bool(row.get(key))
        for key in nulls:
            if row.get(key) == "":
                row[key] = None
        result.append(row)
    return result


def collect(query):
    """Collect metadata/counts/hashes through one caller-managed transaction."""
    query("SELECT set_config('timezone','UTC',false), "
          "set_config('DateStyle','ISO, YMD',false), "
          "set_config('extra_float_digits','3',false), "
          "set_config('search_path','pg_catalog,public',false)")
    result = {"format": FORMAT}
    result["database"] = normalize(query("""
        SELECT d.datname AS name, pg_encoding_to_char(d.encoding) AS encoding,
               d.datlocprovider AS locale_provider, d.datcollate AS collate,
               d.datctype AS ctype, d.daticulocale AS icu_locale,
               d.datcollversion AS recorded_collation_version,
               pg_database_collation_actual_version(d.oid) AS actual_collation_version,
               pg_get_userbyid(d.datdba) AS owner,
               current_setting('server_version') AS server_version,
               current_setting('server_version_num') AS server_version_num,
               pg_database_size(d.oid) AS size_bytes
          FROM pg_database d WHERE d.datname = current_database()
    """), ints=("server_version_num", "size_bytes"), nulls=(
        "icu_locale", "recorded_collation_version", "actual_collation_version"))[0]
    result["extensions"] = query("""
        SELECT e.extname AS name, e.extversion AS version, n.nspname AS schema
          FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
         ORDER BY e.extname COLLATE "C"
    """)
    relations = query(f"""
        SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE {USER_SCHEMAS} AND c.relkind IN ('r','p','m')
         ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
    """)
    result["tables"] = []
    for relation in relations:
        table = dict(relation)
        qualified = ident(table["schema"]) + "." + ident(table["name"])
        rows = query(f"""
            SELECT count(*) AS row_count,
                   md5(coalesce(string_agg(row_hash, '' ORDER BY row_hash COLLATE "C"), '')) AS content_md5
              FROM (SELECT md5(row_to_json(t)::text) AS row_hash FROM {qualified} AS t) AS row_hashes
        """)
        table.update(normalize(rows, ints=("row_count",))[0])
        result["tables"].append(table)
    result["columns"] = normalize(query(f"""
        SELECT n.nspname AS schema, c.relname AS table_name, a.attname AS name,
               row_number() OVER (PARTITION BY c.oid ORDER BY a.attnum) AS position,
               format_type(a.atttypid,a.atttypmod) AS type,
               a.attnotnull AS not_null, a.attidentity AS identity_kind,
               a.attgenerated AS generated_kind,
               pg_get_expr(ad.adbin,ad.adrelid) AS default_expression
          FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
         WHERE {USER_SCHEMAS} AND c.relkind IN ('r','p','m','v')
           AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C", a.attnum
    """), ints=("position",), bools=("not_null",), nulls=("default_expression",))
    result["constraints"] = normalize(query(f"""
        SELECT n.nspname AS schema, c.relname AS table_name, co.conname AS name,
               co.contype AS type, co.convalidated AS validated,
               pg_get_constraintdef(co.oid) AS definition
          FROM pg_constraint co JOIN pg_class c ON c.oid = co.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE {USER_SCHEMAS}
         ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C", co.conname COLLATE "C"
    """), bools=("validated",))
    result["indexes"] = normalize(query(f"""
        SELECT n.nspname AS schema, c.relname AS name, t.relname AS table_name,
               i.indisvalid AS valid, i.indisready AS ready,
               pg_get_indexdef(i.indexrelid) AS definition
          FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
          JOIN pg_class t ON t.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE {USER_SCHEMAS}
         ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
    """), bools=("valid", "ready"))
    result["views"] = query(f"""
        SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
               pg_get_viewdef(c.oid,false) AS definition
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE {USER_SCHEMAS} AND c.relkind IN ('v','m')
         ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
    """)
    result["sequence_definitions"] = normalize(query(f"""
        SELECT n.nspname AS schema, c.relname AS name,
               format_type(s.seqtypid,NULL) AS type,
               s.seqstart AS start, s.seqincrement AS increment,
               s.seqmax AS maximum, s.seqmin AS minimum,
               s.seqcache AS cache, s.seqcycle AS cycle
          FROM pg_sequence s JOIN pg_class c ON c.oid = s.seqrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE {USER_SCHEMAS}
         ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
    """), ints=("start", "increment", "maximum", "minimum", "cache"), bools=("cycle",))
    result["modules"] = normalize(query("""
        SELECT name, state, latest_version AS version
          FROM public.ir_module_module WHERE state <> 'uninstalled'
         ORDER BY name COLLATE "C"
    """), nulls=("version",))
    return result


def privilege_audit(query, expected_owner):
    role = literal(expected_owner)
    roles = normalize(query(f"""
        SELECT r.rolname AS name, r.rolcanlogin AS can_login,
               r.rolsuper AS superuser, r.rolcreatedb AS create_database,
               r.rolcreaterole AS create_role, r.rolreplication AS replication,
               r.rolbypassrls AS bypass_rls,
               (SELECT count(*) FROM pg_auth_members WHERE member = r.oid) AS membership_count
          FROM pg_roles r WHERE r.rolname = {role}
    """), ints=("membership_count",), bools=("can_login", "superuser", "create_database", "create_role", "replication", "bypass_rls"))
    if not roles:
        return {"expected_owner": expected_owner, "role_missing": True, "issues": []}
    issues = query(f"""
        SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
               pg_get_userbyid(c.relowner) AS owner
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE {USER_SCHEMAS} AND c.relkind IN ('r','p','v','m','S')
           AND NOT EXISTS (SELECT 1 FROM pg_depend d
                            WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')
           AND (pg_get_userbyid(c.relowner) <> {role}
                OR NOT has_schema_privilege({role},n.oid,'USAGE')
                OR CASE WHEN c.relkind = 'S' THEN
                    NOT (has_sequence_privilege({role},c.oid,'USAGE')
                         AND has_sequence_privilege({role},c.oid,'SELECT')
                         AND has_sequence_privilege({role},c.oid,'UPDATE'))
                   WHEN c.relkind IN ('r','p') THEN
                    NOT (has_table_privilege({role},c.oid,'SELECT')
                         AND has_table_privilege({role},c.oid,'INSERT')
                         AND has_table_privilege({role},c.oid,'UPDATE')
                         AND has_table_privilege({role},c.oid,'DELETE'))
                   ELSE NOT has_table_privilege({role},c.oid,'SELECT') END)
         ORDER BY n.nspname COLLATE "C", c.relname COLLATE "C"
    """)
    return {"expected_owner": expected_owner, "role_missing": False, "role": roles[0], "issues": issues}


class PsqlQuery:
    """Target collector: no passwords or application data in process arguments."""
    def __init__(self, database, port=5432, psql="psql"):
        self.command = [psql, "-X", "--csv", "--quiet", "--no-password", "--host=/var/run/postgresql",
                        "--port=" + str(port), "--dbname=" + database, "--set=ON_ERROR_STOP=1"]

    def __call__(self, sql):
        # Each target query uses its own read-only transaction. The restored test
        # database must have no writers. Source queries use caller's shared txn.
        prefix = ("BEGIN READ ONLY; SET LOCAL timezone='UTC'; SET LOCAL DateStyle='ISO, YMD'; "
                  "SET LOCAL extra_float_digits=3; SET LOCAL search_path=pg_catalog,public; "
                  "SET LOCAL statement_timeout='10min'; SET LOCAL lock_timeout='10s'; ")
        completed = subprocess.run(self.command, input=prefix + sql.rstrip().rstrip(";") + "; COMMIT;\n",
                                   text=True, encoding="utf-8", capture_output=True, check=True)
        return list(csv.DictReader(io.StringIO(completed.stdout)))


def compare(source, target):
    if source.get("format") != FORMAT or target.get("format") != FORMAT:
        raise ValueError("Unsupported snapshot format")
    differences = []
    warnings = []
    for key in ("encoding", "locale_provider", "collate", "ctype", "icu_locale"):
        if source["database"].get(key) != target["database"].get(key):
            differences.append({"section": "database", "field": key,
                                "source": source["database"].get(key), "target": target["database"].get(key)})
    if source["database"]["server_version_num"] // 10000 != target["database"]["server_version_num"] // 10000:
        differences.append({"section": "database", "field": "server_major_version"})
    for key in ("recorded_collation_version", "actual_collation_version"):
        if source["database"].get(key) != target["database"].get(key):
            warnings.append({"section": "database", "field": key,
                             "source": source["database"].get(key), "target": target["database"].get(key),
                             "note": "Cross-OS libc collation versions differ; indexes were freshly built by restore. Application sorting still needs acceptance."})
    key_fields = {
        "extensions": ("name",), "tables": ("schema", "name"),
        "columns": ("schema", "table_name", "name"),
        "constraints": ("schema", "table_name", "name"), "indexes": ("schema", "name"),
        "views": ("schema", "name"), "sequence_definitions": ("schema", "name"), "modules": ("name",),
    }
    for section, keys in key_fields.items():
        left = {tuple(row[k] for k in keys): row for row in source[section]}
        right = {tuple(row[k] for k in keys): row for row in target[section]}
        for identity in sorted(left.keys() | right.keys()):
            if identity not in left or identity not in right:
                differences.append({"section": section, "object": list(identity),
                                    "status": "missing_from_source" if identity not in left else "missing_from_target"})
            elif left[identity] != right[identity]:
                fields = [k for k in sorted(left[identity].keys() | right[identity].keys())
                          if left[identity].get(k) != right[identity].get(k)]
                differences.append({"section": section, "object": list(identity), "fields": fields,
                                    "source": {k: left[identity].get(k) for k in fields},
                                    "target": {k: right[identity].get(k) for k in fields}})
    audit = target.get("privilege_audit")
    if audit:
        if audit.get("role_missing") or audit.get("issues"):
            differences.append({"section": "privilege_audit", "details": audit})
        elif (not audit["role"]["can_login"] or audit["role"]["membership_count"] != 0
              or any(audit["role"][key] for key in ("superuser", "create_database", "create_role", "replication", "bypass_rls"))):
            differences.append({"section": "privilege_audit", "role": audit["role"]})
    invalid = [row for row in target["indexes"] if not row["valid"] or not row["ready"]]
    if invalid:
        differences.append({"section": "invalid_indexes", "objects": invalid})
    return {"passed": not differences, "tables_checked": len(source["tables"]),
            "total_table_rows": sum(row["row_count"] for row in source["tables"]),
            "columns_checked": len(source["columns"]), "constraints_checked": len(source["constraints"]),
            "indexes_checked": len(source["indexes"]), "views_checked": len(source["views"]),
            "sequence_definitions_checked": len(source["sequence_definitions"]),
            "modules_checked": len(source["modules"]), "differences": differences, "warnings": warnings,
            "limits": ["Hashes compare logical row contents without exposing business rows; MD5 is an integrity check, not a cryptographic proof.",
                       "Sequence current values require a separate comparison to archive SEQUENCE SET entries.",
                       "Target collection assumes no writers; source callback must share pg_dump exported snapshot.",
                       "Odoo startup, app-host connectivity, filestore bytes and systemd reboot persistence are separate acceptance checks."]}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("collect")
    snapshot.add_argument("--database", required=True)
    snapshot.add_argument("--port", type=int, default=5432)
    snapshot.add_argument("--psql", default="psql")
    snapshot.add_argument("--expected-owner")
    snapshot.add_argument("--output", required=True)
    diff = sub.add_parser("compare")
    diff.add_argument("--source", required=True)
    diff.add_argument("--target", required=True)
    diff.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "collect":
        query = PsqlQuery(args.database, args.port, args.psql)
        value = collect(query)
        if args.expected_owner:
            value["privilege_audit"] = privilege_audit(query, args.expected_owner)
        write_json(args.output, value)
        print(json.dumps({"tables": len(value["tables"]), "rows": sum(t["row_count"] for t in value["tables"]), "output": args.output}))
        return 0
    result = compare(json.loads(Path(args.source).read_text(encoding="utf-8-sig")),
                     json.loads(Path(args.target).read_text(encoding="utf-8-sig")))
    write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
