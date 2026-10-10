"""client-check.ps1: static checks always, and runs under pwsh when it is on PATH.

The static checks need no PowerShell. A small tokenizer separates code, strings
and comments, so that the checks look at code only: the script must run in
Windows PowerShell 5.1 and in PowerShell 7, so it starts with
"#Requires -Version 5.1" and uses no operator or parameter of PowerShell 7 only
(?:, ??, ?., &&, ||, -SkipCertificateCheck, ForEach-Object -Parallel, and the
cmdlets, parameters and .NET members that PowerShell 7 added). It declares every
parameter of the agreed interface, has no comment but #Requires and no <# #>
help block, is ASCII (Windows PowerShell 5.1 reads a script without a byte
order mark in the ANSI code page), calls curl.exe through one native call with
an argument array and --max-time 10, never follows redirects, never uses
Invoke-WebRequest, and gives both setups in the reason of a LOOP (HTTPS to
port 443 on the front, or the http option on the route).

With pwsh (PowerShell 7) on PATH on Linux or macOS, the PowerShell parser reads
the script, and the script runs with a fake curl.exe first on PATH. The fake
answers from a scenario: per host name, the answer of the normal path, of the
certificate check (the normal path without -k; the normal answer unless the
scenario gives one), of the direct path (--resolve) and of the hand-over mode
request (-w) through the normal and the direct path. The host names end in
.invalid and do not resolve, so the verdicts depend on the curl answers only. The hosts file functions run alone (taken from the script's syntax tree)
on temporary files, because the hosts switches need administrator rights on
Windows and refuse elsewhere. Without pwsh, these tests are skipped.
"""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "client-check.ps1"
PWSH = shutil.which("pwsh")
INTERFACE = {
    "AppServer": "string",
    "Names": "string[]",
    "Path": "string",
    "ModePath": "string",
    "HubName": "string",
    "AddHostsBypass": "switch",
    "RemoveHostsBypass": "switch",
    "HostsFile": "string",
}
PS7_ONLY = [
    (r"\?\?", "the ?? operator"),
    (r"\?\.", "the ?. operator"),
    (r"\?\[", "the ?[ ] operator"),
    (r"(^|[\s(=,;{])\?($|\s)", "the ternary ?: operator"),
    (r"&&", "an && pipeline chain"),
    (r"\|\|", "an || pipeline chain"),
    (r"-SkipCertificateCheck\b", "-SkipCertificateCheck"),
    (r"-Parallel\b", "ForEach-Object -Parallel"),
    (r"-AsByteStream\b", "-AsByteStream"),
    (r"-AsHashtable\b", "ConvertFrom-Json -AsHashtable"),
    (r"\$Is(Windows|Linux|MacOS)\b", "$IsWindows, $IsLinux or $IsMacOS"),
    (r"-Encoding\s+Byte\b", "-Encoding Byte (Windows PowerShell only)"),
    (r"\bGet-WmiObject\b", "Get-WmiObject (Windows PowerShell only)"),
    (r"\bJoin-String\b", "Join-String"),
    (r"\bGet-Error\b", "Get-Error"),
    (r"\bTest-Json\b", "Test-Json"),
    (r"\bRemove-Alias\b", "Remove-Alias"),
    (r"(?<!\w)-TcpPort\b", "Test-Connection -TcpPort"),
    (r"\bConvertFrom-Json\b[^\n|;]*-Depth\b", "ConvertFrom-Json -Depth"),
    (r"-AsArray\b", "ConvertTo-Json -AsArray"),
    (r"\butf8NoBOM\b", "the utf8NoBOM encoding"),
    (r"::Latin1\b", "[Text.Encoding]::Latin1"),
    (r"-replace\s+[^,\n]+,\s*\{", "-replace with a script block"),
    (r"\$PSStyle\b", "$PSStyle"),
    (r"^\s*clean\s*\{", "a clean block"),
]
NO_HTTP_CMDLETS = [
    (r"\bInvoke-WebRequest\b", "Invoke-WebRequest"),
    (r"\bInvoke-RestMethod\b", "Invoke-RestMethod"),
    (r"(?<![\w$-])(iwr|irm)(?![\w.-])", "an alias of Invoke-WebRequest or Invoke-RestMethod"),
    (r"(?<![\w$.-])curl(?![\w.-])", "curl without .exe (Invoke-WebRequest in Windows PowerShell 5.1)"),
    (r"ServerCertificateValidationCallback", "a certificate validation bypass"),
    (r"CertificatePolicy", "a certificate policy bypass"),
]
SCOPES = {"env", "script", "global", "local", "private", "using", "variable", "function", "alias"}


class PowerShellSource:
    """PowerShell source split into code, strings and comments.

    It knows enough of the PowerShell tokenizer for static checks: # and <# #>
    comments, single- and double-quoted strings, here-strings, backtick
    escapes, and $( ) inside double-quoted strings, whose content is code
    again. Each string is replaced by '' in the code.
    """

    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.code = []
        self.strings = []
        self.comments = []
        self.errors = []
        self._scan_code(nested=False)
        self.code_text = "".join(self.code)

    def _line(self, pos):
        return self.text.count("\n", 0, pos) + 1

    def _at(self, offset=0):
        index = self.pos + offset
        return self.text[index] if index < len(self.text) else ""

    def _comment_may_start(self):
        if self.pos == 0:
            return True
        before = self.text[self.pos - 1]
        return before.isspace() or before in "(){}[];,|&="

    def _scan_code(self, nested):
        depth = 0
        while self.pos < len(self.text):
            char = self._at()
            following = self._at(1)
            if char == "`":
                self.code.append(self.text[self.pos:self.pos + 2])
                self.pos += 2
            elif char == "<" and following == "#":
                end = self.text.find("#>", self.pos + 2)
                if end < 0:
                    self.errors.append("line %d: <# without #>" % self._line(self.pos))
                    end = len(self.text)
                self.comments.append((self._line(self.pos), self.text[self.pos:end + 2]))
                self.pos = end + 2
            elif char == "#" and self._comment_may_start():
                end = self.text.find("\n", self.pos)
                if end < 0:
                    end = len(self.text)
                self.comments.append((self._line(self.pos), self.text[self.pos:end].rstrip("\r")))
                self.pos = end
            elif char == "@" and following in ("'", '"') and self._here_string():
                continue
            elif char == "'":
                self._single_quoted()
            elif char == '"':
                self._double_quoted()
            else:
                if nested and char == "(":
                    depth += 1
                elif nested and char == ")":
                    if depth == 0:
                        self.code.append(")")
                        self.pos += 1
                        return
                    depth -= 1
                self.code.append(char)
                self.pos += 1
        if nested:
            self.errors.append("a $( in a string has no closing )")

    def _here_string(self):
        quote = self._at(1)
        opening = re.compile(r"[ \t]*\r?\n").match(self.text, self.pos + 2)
        if not opening:
            return False
        newline = opening.end() - 1
        end = self.text.find("\n" + quote + "@", newline)
        if end < 0:
            self.errors.append("line %d: a here-string is not closed" % self._line(self.pos))
            end = len(self.text)
        kind = "here-single" if quote == "'" else "here-double"
        self.strings.append((kind, self._line(self.pos), self.text[newline + 1:end]))
        self.code.append("''")
        self.pos = end + 3
        return True

    def _single_quoted(self):
        start = self.pos
        index = self.pos + 1
        while True:
            end = self.text.find("'", index)
            if end < 0:
                self.errors.append("line %d: a ' string is not closed" % self._line(start))
                self.pos = len(self.text)
                return
            if self.text[end + 1:end + 2] == "'":
                index = end + 2
                continue
            break
        self.strings.append(("single", self._line(start), self.text[start + 1:end]))
        self.code.append("''")
        self.pos = end + 1

    def _double_quoted(self):
        start = self.pos
        self.pos += 1
        content = []
        while self.pos < len(self.text):
            char = self._at()
            if char == "`":
                content.append(self.text[self.pos:self.pos + 2])
                self.pos += 2
            elif char == '"' and self._at(1) == '"':
                content.append('""')
                self.pos += 2
            elif char == '"':
                self.pos += 1
                self.strings.append(("double", self._line(start), "".join(content)))
                self.code.append("''")
                return
            elif char == "$" and self._at(1) == "(":
                content.append("$( )")
                self.code.append("(")
                self.pos += 2
                self._scan_code(nested=True)
            else:
                content.append(char)
                self.pos += 1
        self.errors.append('line %d: a " string is not closed' % self._line(start))


def unbalanced(code):
    """The first bracket of code that has no partner, or None."""
    partners = {")": "(", "]": "[", "}": "{"}
    stack = []
    for index, char in enumerate(code):
        if char in "([{":
            stack.append((char, index))
        elif char in partners:
            if not stack or stack[-1][0] != partners[char]:
                return "%r at code offset %d" % (char, index)
            stack.pop()
    if stack:
        return "%r at code offset %d is not closed" % stack[-1]
    return None


def forbidden(code, patterns):
    """The descriptions of the patterns found in code."""
    found = []
    without_status = code.replace("$?", "")
    for pattern, description in patterns:
        if re.search(pattern, without_status, re.IGNORECASE | re.MULTILINE):
            found.append(description)
    return found


def bad_scope_references(source):
    """Double-quoted "$name:" references that PowerShell reads as a scope or drive."""
    found = []
    for kind, line, content in source.strings:
        if kind not in ("double", "here-double"):
            continue
        for match in re.finditer(r"(?<!`)\$([A-Za-z_]\w*):(?!:)", content):
            if match.group(1).lower() not in SCOPES:
                found.append("line %d: $%s:" % (line, match.group(1)))
    return found


def param_block(code):
    """The code of the script's param( ) block."""
    start = code.index("[CmdletBinding()]")
    opening = code.index("param(", start) + len("param")
    depth = 0
    for index in range(opening, len(code)):
        if code[index] == "(":
            depth += 1
        elif code[index] == ")":
            depth -= 1
            if depth == 0:
                return code[opening + 1:index]
    raise AssertionError("the param( ) block is not closed")


class TokenizerTest(unittest.TestCase):
    """The tokenizer the static checks rely on finds what it must find."""

    def test_strings_and_comments_are_not_code(self):
        source = PowerShellSource(
            "$a = 'x ?? y && z' # 1 ?? 2\n"
            "$b = $c ?? 'it''s'\n"
            "$e = \"$(if ($f) { 'g' }) and `\" and $name: x\"\n"
            "$h = @'\n?? here\n'@\n"
            "<# help ?? #>\n"
        )
        self.assertEqual(source.errors, [])
        self.assertEqual(source.code_text.count("??"), 1)
        self.assertEqual([text for _, text in source.comments], ["# 1 ?? 2", "<# help ?? #>"])
        self.assertIn("if (", source.code_text)
        self.assertIsNone(unbalanced(source.code_text))
        self.assertEqual(forbidden(source.code_text, PS7_ONLY), ["the ?? operator"])
        self.assertEqual(bad_scope_references(source), ["line 3: $name:"])

    def test_forbidden_patterns_find_samples(self):
        samples = {
            "$a = $b ? 1 : 2": "the ternary ?: operator",
            "$a = $b?.c": "the ?. operator",
            "git status && echo": "an && pipeline chain",
            "Invoke-WebRequest -SkipCertificateCheck $u": "-SkipCertificateCheck",
            "1..3 | ForEach-Object -Parallel { $_ }": "ForEach-Object -Parallel",
            "if ($IsWindows) { }": "$IsWindows, $IsLinux or $IsMacOS",
            "$x = @('a', 'b') | Join-String -Separator ','": "Join-String",
            "Test-Connection -TargetName 'a' -TcpPort 443 -Quiet": "Test-Connection -TcpPort",
            "$e = [System.Text.Encoding]::Latin1": "[Text.Encoding]::Latin1",
            "$y = 'ab' -replace 'a', { $_.Value.ToUpper() }": "-replace with a script block",
            "$j = ConvertFrom-Json -InputObject $t -Depth 5": "ConvertFrom-Json -Depth",
            "Get-Error": "Get-Error",
            "Set-Content -Path $f -Encoding utf8NoBOM -Value 1": "the utf8NoBOM encoding",
            "$s = ConvertTo-Json -InputObject $o -AsArray": "ConvertTo-Json -AsArray",
            "$PSStyle.Reset": "$PSStyle",
        }
        for code, description in samples.items():
            with self.subTest(code=code):
                self.assertIn(description, forbidden(PowerShellSource(code).code_text, PS7_ONLY))
        self.assertEqual(forbidden(PowerShellSource("if ($?) { $x = $curl }").code_text, PS7_ONLY + NO_HTTP_CMDLETS), [])
        self.assertIn("curl without .exe (Invoke-WebRequest in Windows PowerShell 5.1)",
                      forbidden(PowerShellSource("curl https://x").code_text, NO_HTTP_CMDLETS))


class StaticTest(unittest.TestCase):
    """Checks of client-check.ps1 that need no PowerShell."""

    @classmethod
    def setUpClass(cls):
        cls.raw = SCRIPT.read_bytes()
        cls.text = cls.raw.decode("ascii", errors="replace")
        cls.source = PowerShellSource(cls.text)

    def test_starts_with_requires_5_1(self):
        self.assertEqual(self.text.splitlines()[0], "#Requires -Version 5.1")

    def test_is_ascii_without_byte_order_mark(self):
        bad = [index for index, byte in enumerate(self.raw) if byte > 0x7F]
        self.assertEqual(bad, [], "non-ASCII bytes at these offsets")

    def test_tokenizes_and_brackets_balance(self):
        self.assertEqual(self.source.errors, [])
        self.assertIsNone(unbalanced(self.source.code_text))

    def test_no_comments_and_no_help_block(self):
        self.assertNotIn("<#", self.text)
        self.assertEqual(self.source.comments, [(1, "#Requires -Version 5.1")])

    def test_no_powershell_7_only_syntax(self):
        self.assertEqual(forbidden(self.source.code_text, PS7_ONLY), [])
        without_status = self.source.code_text.replace("$?", "")
        self.assertNotIn("?", without_status)

    def test_http_only_through_curl_exe(self):
        self.assertEqual(forbidden(self.source.code_text, NO_HTTP_CMDLETS), [])
        self.assertRegex(self.source.code_text, r"&\s*\$Curl\s+@Arguments\s+2>\$null")
        self.assertIn("Get-Command -Name curl.exe -CommandType Application", self.source.code_text)
        self.assertIn("$ErrorActionPreference = 'Continue'", self.text)
        argument_lists = re.findall(r"\$curlArgs = @\(([^)]*)\)", self.text)
        self.assertEqual(len(argument_lists), 2)
        for arguments in argument_lists:
            self.assertIn("'--max-time', '10'", arguments)
        self.assertIn("'-o', $nullDevice, '-D', '-'", self.text)
        self.assertIn("'--resolve', ('{0}:443:{1}' -f $Name, $Direct)", self.text)
        self.assertIn("$curlArgs += '--ssl-no-revoke'", self.text)

    def test_one_native_call_for_curl_and_no_redirect_following(self):
        self.assertEqual(sorted(re.findall(r"&\s*\$([\w.]+)", self.source.code_text)), ["Curl", "ipconfig.Path"])
        self.assertNotRegex(self.source.code_text, r"(?i)\b(Start-Process|Invoke-Expression|iex)\b")
        literals = {content for _, _, content in self.source.strings}
        self.assertFalse(literals & {"-L", "--location", "--location-trusted"}, "curl must not follow redirects")

    def test_no_scope_references_in_double_quoted_strings(self):
        self.assertEqual(bad_scope_references(self.source), [])

    def test_interface_parameters(self):
        self.assertRegex(self.source.code_text, r"^\n\[CmdletBinding\(\)\]\s*param\(")
        block = param_block(self.source.code_text)
        declared = {name: kind for kind, name in re.findall(r"\[(string\[\]|string|switch)\]\s*\$(\w+)", block)}
        self.assertEqual(declared, INTERFACE)
        defaults = [
            "[string[]]$Names = @('stgiss.perodua.com.my', 'stgissrp.perodua.com.my', "
            "'stgisssp.perodua.com.my', 'stgisscp.perodua.com.my')",
            "[string]$Path = '/dev/app/'",
            "[string]$ModePath = '/dev/uiux/api/handover/mode'",
            '[string]$HostsFile = "$env:SystemRoot\\System32\\drivers\\etc\\hosts"',
        ]
        for default in defaults:
            self.assertIn(default, self.text)

    def test_the_loop_reason_gives_both_setups(self):
        lines = [line for line in self.text.splitlines() if "New-Verdict -Verdict 'LOOP'" in line]
        self.assertEqual(len(lines), 1)
        for needed in ("forwards HTTPS requests to port 80 of the App server, and port 80 redirects them back to HTTPS.",
                       "Ask the front''s owner to forward HTTPS to port 443 with the original Host header, or,",
                       "add the http option to the route of this name in the routes table of https.sh, then run: "
                       "sudo bash https.sh apply (README: Behind a TLS front on port 80).')"):
            self.assertIn(needed, lines[0])

    def test_hosts_switches_and_tcp(self):
        for needed in ("Security.Principal.WindowsPrincipal", "Clear-DnsClientCache", "/flushdns",
                       "# perodua-bypass", "System.Net.Sockets.TcpClient", "[System.IO.File]::WriteAllBytes"):
            self.assertIn(needed, self.text)
        for status in ("exit 0", "exit 2", "exit 4"):
            self.assertIn(status, self.source.code_text)


FAKE_CURL = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CURL_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("curl 8.13.0 (fake)")
    sys.exit(0)
with open(os.environ["FAKE_CURL_SCENARIO"]) as handle:
    scenario = json.load(handle)
url = [arg for arg in args if arg.startswith("https://")][-1]
name = url.split("/")[2]
if "-w" in args:
    kind = "mode_direct" if "--resolve" in args else "mode"
elif "--resolve" in args:
    kind = "direct"
elif "-k" not in args:
    kind = "certificate"
else:
    kind = "normal"
answer = scenario.get(name, {}).get(kind)
if answer is None and kind == "certificate":
    answer = scenario.get(name, {}).get("normal")
if answer is None:
    sys.exit(7)
status = answer.get("exit", 0)
if kind in ("mode", "mode_direct"):
    if status == 0:
        sys.stdout.write(answer.get("body", ""))
    code = answer.get("code", 0) if status == 0 else 0
    form = args[args.index("-w") + 1]
    sys.stdout.write(form.replace("\\n", "\n").replace("%{http_code}", "%03d" % code))
elif status == 0:
    sys.stdout.write(answer.get("headers", "").replace("\n", "\r\n"))
sys.exit(status)
'''
LOAD_FUNCTIONS = r'''
$ErrorActionPreference = 'Stop'
$parseTokens = $null
$parseErrors = $null
$tree = [System.Management.Automation.Language.Parser]::ParseFile($env:CLIENT_CHECK, [ref]$parseTokens, [ref]$parseErrors)
foreach ($definition in $tree.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
'''
HUB = "hub.example.invalid"
MODULE = "module.example.invalid"
NGINX_301 = "<html>\r\n<head><title>301 Moved Permanently</title></head>\r\n</html>\r\n"


def head(code, reason, server=None, location=None):
    lines = ["HTTP/1.1 %d %s" % (code, reason)]
    if server:
        lines.append("Server: " + server)
    if location:
        lines.append("Location: " + location)
    return "\n".join(lines) + "\n\n"


def mode_body(mode):
    return json.dumps({"code": 0, "data": {"mode": mode, "enabled": True, "modules": []}})


def block(output, name):
    """The lines that client-check.ps1 printed for one host name."""
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(name + " (expected mode: "):
            found = []
            for following in lines[index:]:
                if not following.strip():
                    break
                found.append(following)
            return "\n".join(found)
    raise AssertionError("no block for %s in:\n%s" % (name, output))


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


needs_pwsh = unittest.skipUnless(
    PWSH and os.name == "posix",
    "pwsh (PowerShell 7) is not on PATH of this Linux or macOS host: the parser check, the runs "
    "with a fake curl.exe and the hosts file checks of client-check.ps1 need it")


@needs_pwsh
class PowerShellTest(unittest.TestCase):
    """client-check.ps1 parsed and run by pwsh."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="client-check-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "curl.exe"
        fake.write_text(FAKE_CURL.replace("PYTHON", sys.executable, 1))
        fake.chmod(0o755)
        self.log = self.tmp / "curl.log"
        self.scenario = self.tmp / "scenario.json"

    def pwsh(self, command, **extra):
        env = dict(os.environ, CLIENT_CHECK=str(SCRIPT), **extra)
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
                              env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=300)

    def run_check(self, scenario, *args, with_curl=True):
        self.scenario.write_text(json.dumps(scenario))
        env = dict(os.environ, FAKE_CURL_LOG=str(self.log), FAKE_CURL_SCENARIO=str(self.scenario))
        if with_curl:
            env["PATH"] = str(self.bin) + os.pathsep + env.get("PATH", "")
        else:
            env["PATH"] = os.pathsep.join(folder for folder in env.get("PATH", "").split(os.pathsep)
                                          if folder and not os.path.exists(os.path.join(folder, "curl.exe")))
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)] + list(args),
                              env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=300)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_the_powershell_parser_finds_no_error(self):
        result = self.pwsh(
            "$parseTokens = $null\n"
            "$parseErrors = $null\n"
            "$null = [System.Management.Automation.Language.Parser]::ParseFile("
            "$env:CLIENT_CHECK, [ref]$parseTokens, [ref]$parseErrors)\n"
            "foreach ($parseError in $parseErrors) { 'line {0}: {1}' -f "
            "$parseError.Extent.StartLineNumber, $parseError.Message }\n"
            "'PARSED ' + @($parseErrors).Count\n")
        self.assertEqual(result.stdout.strip().splitlines()[-1:], ["PARSED 0"], result.stdout + result.stderr)

    def test_a_redirect_to_the_same_url_is_a_loop(self):
        scenario = {
            HUB: {"normal": {"headers": head(301, "Moved Permanently", "CloudWAF", "https://%s/dev/app/" % HUB)},
                  "direct": {"headers": head(200, "OK", "nginx/1.24.0 (Ubuntu)")},
                  "mode": {"code": 301, "body": NGINX_301}},
            MODULE: {"normal": {"headers": head(301, "Moved Permanently", "CloudWAF",
                                                "HTTPS://%s/dev/app/" % MODULE.upper())},
                     "direct": {"headers": head(200, "OK", "nginx/1.24.0 (Ubuntu)")},
                     "mode": {"code": 301, "body": NGINX_301}},
        }
        result = self.run_check(scenario, "-AppServer", "127.0.0.1", "-Names", "%s,%s" % (HUB, MODULE))
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        for name in (HUB, MODULE):
            lines = block(result.stdout, name)
            self.assertIn("Server: CloudWAF", lines)
            self.assertIn("-> LOOP: a redirect to the same URL", lines)
            self.assertIn("  Direct:       HTTP/1.1 200 OK; Server: nginx/1.24.0 (Ubuntu)", lines)
            self.assertIn("  TCP 443:      127.0.0.1 ", lines)
            self.assertIn("  Mode:         NOTE not read: HTTP 301, no mode in the answer", lines)
            self.assertRegex(lines, r"Verdict:\s+LOOP: the front \(Server: CloudWAF\) forwards HTTPS requests "
                                    r"to port 80 of the App server")
        self.assertIn("-AppServer 127.0.0.1 -AddHostsBypass", result.stdout)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "Result: 2 of 2 names not OK")
        calls = [call for call in self.calls() if call != ["--version"]]
        self.assertEqual(len(calls), 10)
        for call in calls:
            self.assertEqual(call[call.index("--max-time") + 1], "10")
            self.assertNotIn("-L", call)
        resolved = sorted(call[call.index("--resolve") + 1] for call in calls if "--resolve" in call)
        self.assertEqual(resolved, sorted(["%s:443:127.0.0.1" % HUB, "%s:443:127.0.0.1" % MODULE] * 2))
        verified = [call for call in calls if "-k" not in call]
        self.assertEqual(len(verified), 2)
        for call in verified:
            self.assertIn("--ssl-no-revoke", call)
            self.assertNotIn("--resolve", call)
        self.assertIn("  Certificate:  OK trusted on this PC for this name", block(result.stdout, HUB))

    def test_a_loop_on_the_direct_path_too_is_a_server_problem(self):
        location = "https://%s/dev/app/" % HUB
        scenario = {HUB: {"normal": {"headers": head(301, "Moved Permanently", "CloudWAF", location)},
                          "direct": {"headers": head(301, "Moved Permanently", "nginx/1.24.0 (Ubuntu)", location)},
                          "mode": {"exit": 28}}}
        result = self.run_check(scenario, "-AppServer", "127.0.0.1", "-Names", HUB)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertRegex(block(result.stdout, HUB), r"Verdict:\s+SERVER problem: the direct path to the App server also "
                                                    r"redirects to the same URL")
        self.assertNotIn("Workaround", result.stdout)

    def test_every_name_ok_gives_exit_status_0(self):
        scenario = {
            HUB: {"normal": {"headers": head(200, "OK", "CloudWAF")},
                  "mode": {"code": 200, "body": mode_body("hub")}},
            MODULE: {"normal": {"headers": head(200, "OK", "CloudWAF")},
                     "mode": {"code": 200, "body": mode_body("module")}},
        }
        result = self.run_check(scenario, "-Names", "%s,%s" % (HUB, MODULE))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  Mode:         OK hub", block(result.stdout, HUB))
        self.assertIn("  Mode:         OK module", block(result.stdout, MODULE))
        self.assertRegex(block(result.stdout, MODULE), r"Verdict:\s+OK: ")
        self.assertNotIn("TCP 443", result.stdout)
        self.assertNotIn("Workaround", result.stdout)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "Result: all 2 names OK")
        self.assertFalse([call for call in self.calls() if "--resolve" in call])

    def test_a_changed_host_header_is_a_front_problem(self):
        scenario = {
            HUB: {"normal": {"headers": head(200, "OK", "CloudWAF")},
                  "mode": {"code": 200, "body": mode_body("hub")}},
            MODULE: {"normal": {"headers": head(200, "OK", "CloudWAF")},
                     "mode": {"code": 200, "body": mode_body("local")}},
        }
        result = self.run_check(scenario, "-Names", "%s,%s" % (HUB, MODULE), "-HubName", HUB)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        lines = block(result.stdout, MODULE)
        self.assertIn("  Mode:         PROBLEM local, expected module: the front changes the Host header", lines)
        self.assertRegex(lines, r"Verdict:\s+FRONT problem: the hand-over mode is local, expected module")
        self.assertRegex(block(result.stdout, HUB), r"Verdict:\s+OK: ")

    def test_front_server_and_unreachable_verdicts(self):
        front = "front.example.invalid"
        server = "server.example.invalid"
        scenario = {
            front: {"normal": {"exit": 28},
                    "direct": {"headers": head(200, "OK", "nginx/1.24.0 (Ubuntu)")},
                    "mode": {"exit": 28}},
            server: {"normal": {"headers": head(502, "Bad Gateway", "CloudWAF")},
                     "direct": {"headers": head(502, "Bad Gateway", "nginx/1.24.0 (Ubuntu)")},
                     "mode": {"code": 502, "body": "<html>bad gateway</html>"}},
        }
        result = self.run_check(scenario, "-AppServer", "127.0.0.1", "-Names", "%s,%s" % (front, server))
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        front_lines = block(result.stdout, front)
        self.assertIn("  Normal path:  no answer (curl exit 28: no answer within 10 s)", front_lines)
        self.assertRegex(front_lines, r"Verdict:\s+FRONT problem: the direct path to the App server works")
        self.assertRegex(block(result.stdout, server), r"Verdict:\s+SERVER problem: the direct path fails too")
        alone = self.run_check(scenario, "-Names", front)
        self.assertEqual(alone.returncode, 4, alone.stdout + alone.stderr)
        self.assertRegex(block(alone.stdout, front), r"Verdict:\s+UNREACHABLE: the normal path fails")
        self.assertIn("Workaround until the front is repaired", result.stdout)

    def test_no_dns_and_no_direct_answer_are_not_blamed_on_the_front_or_the_server(self):
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", 443)) == 0:
                self.skipTest("something listens on port 443 of this host: the TCP test of -AppServer 127.0.0.1 succeeds")
        dns = "dns.example.invalid"
        blocked = "blocked.example.invalid"
        scenario = {
            dns: {"normal": {"exit": 6}, "direct": {"headers": head(200, "OK", "nginx/1.24.0 (Ubuntu)")},
                  "mode": {"exit": 6}},
            blocked: {"normal": {"headers": head(403, "Forbidden", "CloudWAF")}, "direct": {"exit": 28},
                      "mode": {"code": 403, "body": "<html>blocked</html>"}},
        }
        result = self.run_check(scenario, "-AppServer", "127.0.0.1", "-Names", "%s,%s" % (dns, blocked))
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertRegex(block(result.stdout, dns), r"Verdict:\s+UNREACHABLE: this PC cannot resolve the name")
        self.assertRegex(block(result.stdout, blocked), r"Verdict:\s+UNREACHABLE: the normal path fails \(403 CloudWAF\), "
                                                        r"and this PC gets no answer from port 443 of the App server")

    def test_a_certificate_that_this_pc_does_not_trust_is_a_front_problem(self):
        scenario = {HUB: {"normal": {"headers": head(200, "OK", "CloudWAF")}, "certificate": {"exit": 60},
                          "mode": {"code": 200, "body": mode_body("hub")}}}
        result = self.run_check(scenario, "-Names", HUB)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        lines = block(result.stdout, HUB)
        self.assertIn("  Certificate:  PROBLEM curl exit 60: the certificate is not trusted on this PC, or it does not "
                      "cover the name. Browsers on this PC show a certificate error for this name.", lines)
        self.assertRegex(lines, r"Verdict:\s+FRONT problem: this PC does not trust the certificate of the normal path")
        scenario[HUB]["certificate"] = {"exit": 28}
        result = self.run_check(scenario, "-Names", HUB)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("  Certificate:  NOTE not checked: no answer without -k (curl exit 28", block(result.stdout, HUB))

    def test_a_blocked_api_path_is_a_front_problem(self):
        challenge = {"code": 200, "body": "<html><script>challenge()</script></html>"}
        scenario = {MODULE: {"normal": {"headers": head(200, "OK", "CloudWAF")},
                             "direct": {"headers": head(200, "OK", "nginx/1.24.0 (Ubuntu)")},
                             "mode": challenge, "mode_direct": {"code": 200, "body": mode_body("module")}}}
        result = self.run_check(scenario, "-AppServer", "127.0.0.1", "-Names", MODULE, "-HubName", HUB)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        lines = block(result.stdout, MODULE)
        self.assertIn("  Mode:         PROBLEM not read through the normal path (HTTP 200, no mode in the answer), but the "
                      "direct path gives module: the front blocks or changes /dev/uiux/api/handover/mode", lines)
        self.assertRegex(lines, r"Verdict:\s+FRONT problem: the hand-over API /dev/uiux/api/handover/mode answers on the "
                                r"direct path \(mode module\)")
        alone = self.run_check(scenario, "-Names", MODULE, "-HubName", HUB)
        self.assertEqual(alone.returncode, 0, alone.stdout + alone.stderr)
        self.assertIn("  Mode:         NOTE not read: HTTP 200, but not the App's answer: a JavaScript challenge", block(alone.stdout, MODULE))

    def test_a_proxy_reply_and_http2_headers(self):
        location = "https://%s/dev/app/" % HUB
        headers = ("HTTP/1.1 200 Connection established\n\n"
                   "HTTP/2 301\nserver: CloudWAF\nlocation: %s\n\n" % location)
        scenario = {HUB: {"normal": {"headers": headers}, "mode": {"exit": 7}}}
        result = self.run_check(scenario, "-Names", HUB)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        lines = block(result.stdout, HUB)
        self.assertIn("Normal path:  HTTP/2 301; Server: CloudWAF; Location: %s -> LOOP" % location, lines)
        self.assertRegex(lines, r"Verdict:\s+LOOP: ")

    def test_without_curl_exe(self):
        result = self.run_check({}, "-Names", HUB, with_curl=False)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("NOTE curl.exe was not found", result.stdout)
        self.assertRegex(block(result.stdout, HUB), r"Verdict:\s+NOT CHECKED: curl.exe is missing")
        self.assertEqual(self.calls(), [])

    def test_refusals_change_nothing(self):
        hosts = self.tmp / "hosts"
        original = b"127.0.0.1 localhost\r\n"
        hosts.write_bytes(original)
        cases = [
            (["-AppServer", "127.0.0.1", "-AddHostsBypass", "-RemoveHostsBypass"], "not both"),
            (["-AddHostsBypass"], "-AddHostsBypass needs -AppServer"),
            (["-AppServer", "192.168.23.256"], "is not an IPv4 address"),
            (["-AppServer", "app.example.invalid"], "is not an IPv4 address"),
            (["-Names", "bad_name!"], "is not a host name"),
            (["-Path", "dev/app/"], "must start with /"),
            (["-AppServer", "127.0.0.1", "-AddHostsBypass"], "needs administrator rights"),
            (["-RemoveHostsBypass"], "needs administrator rights"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                result = self.run_check({}, "-HostsFile", str(hosts), *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertEqual(hosts.read_bytes(), original)
        self.assertEqual(self.calls(), [])

    def hosts_change(self, hosts, action, names, address="192.0.2.10"):
        result = self.pwsh(
            LOAD_FUNCTIONS
            + "try {\n"
              "    $result = Update-HostsFile -File $env:TEST_HOSTS -Names ($env:TEST_NAMES -split ',') "
              "-Address $env:TEST_ADDRESS -Action $env:TEST_ACTION\n"
              "    $result | ConvertTo-Json -Depth 3 -Compress\n"
              "} catch {\n"
              "    'THROWN ' + $_.Exception.Message\n"
              "}\n",
            TEST_HOSTS=str(hosts), TEST_NAMES=",".join(names), TEST_ADDRESS=address, TEST_ACTION=action)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = result.stdout.strip()
        if output.startswith("THROWN "):
            return output
        change = json.loads(output)
        summary = {key: as_list(change[key]) for key in ("Added", "Removed", "Rewritten", "Notes")}
        summary["Changed"] = change["Changed"]
        return summary

    def test_hosts_lines_added_and_removed_byte_for_byte(self):
        hosts = self.tmp / "hosts"
        original = (b"\xef\xbb\xbf# Copyright (c) Microsoft Corp.\r\n"
                    b"# 192.168.23.13 hub.example.test\r\n"
                    b"127.0.0.1 localhost\r\n"
                    b"10.0.0.5 hub.example.test other.example.test   # caf\xe9 by hand\r\n"
                    b"192.0.2.99 keep.example.test")
        hosts.write_bytes(original)
        names = ["hub.example.test", "module.example.test"]
        added = self.hosts_change(hosts, "Add", names)
        self.assertEqual(added["Added"], ["192.0.2.10 module.example.test # perodua-bypass"])
        self.assertEqual(added["Notes"], ["hub.example.test is already mapped to 10.0.0.5 in the hosts file: not changed"])
        self.assertTrue(added["Changed"])
        after_add = original + b"\r\n192.0.2.10 module.example.test # perodua-bypass\r\n"
        self.assertEqual(hosts.read_bytes(), after_add)

        again = self.hosts_change(hosts, "Add", names)
        self.assertEqual(again["Added"], [])
        self.assertFalse(again["Changed"])
        self.assertEqual(hosts.read_bytes(), after_add)

        removed = self.hosts_change(hosts, "Remove", names, address="")
        self.assertEqual(removed["Removed"], ["192.0.2.10 module.example.test # perodua-bypass"])
        self.assertEqual(removed["Rewritten"], ["10.0.0.5 hub.example.test other.example.test   # café by hand -> "
                                                "10.0.0.5 other.example.test   # café by hand"])
        self.assertEqual(hosts.read_bytes(),
                         b"\xef\xbb\xbf# Copyright (c) Microsoft Corp.\r\n"
                         b"# 192.168.23.13 hub.example.test\r\n"
                         b"127.0.0.1 localhost\r\n"
                         b"10.0.0.5 other.example.test   # caf\xe9 by hand\r\n"
                         b"192.0.2.99 keep.example.test\r\n")

    def test_hosts_newlines_empty_file_and_utf16(self):
        hosts = self.tmp / "hosts"
        hosts.write_bytes(b"127.0.0.1 localhost\n")
        self.hosts_change(hosts, "Add", ["hub.example.test"])
        self.assertEqual(hosts.read_bytes(), b"127.0.0.1 localhost\n192.0.2.10 hub.example.test # perodua-bypass\n")

        hosts.write_bytes(b"127.0.0.1 localhost\r\n")
        self.hosts_change(hosts, "Add", ["hub.example.test"])
        self.assertEqual(hosts.read_bytes(), b"127.0.0.1 localhost\r\n192.0.2.10 hub.example.test # perodua-bypass\r\n")

        hosts.write_bytes(b"")
        self.hosts_change(hosts, "Add", ["hub.example.test"])
        self.assertEqual(hosts.read_bytes(), b"192.0.2.10 hub.example.test # perodua-bypass\r\n")

        utf16 = "﻿127.0.0.1 localhost\r\n".encode("utf-16-le")
        hosts.write_bytes(utf16)
        outcome = self.hosts_change(hosts, "Add", ["hub.example.test"])
        self.assertIsInstance(outcome, str)
        self.assertIn("UTF-16", outcome)
        self.assertEqual(hosts.read_bytes(), utf16)


if __name__ == "__main__":
    unittest.main()
