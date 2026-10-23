#!/usr/bin/env python3
"""Exercise owned console-launcher stops with separate real app processes."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent
BUNDLE = "dev.callkitsim.console.fixture"
RAW_OUTPUT = b"CONSOLE raw: \r\t\x00\n"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fixtures(work):
    child = work / "console_app.py"
    child.write_text(
        "import json,os,pathlib,sys,time\n"
        "base=pathlib.Path(sys.argv[1]); mode=sys.argv[2]\n"
        "pathlib.Path(str(base)+'.app.pid').write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+20\n"
        "while not pathlib.Path(str(base)+'.ready').exists():\n"
        " if time.monotonic()>deadline: sys.exit(8)\n"
        " time.sleep(.01)\n"
        f"if mode=='buffered-header': os.write(1,{RAW_OUTPUT!r})\n"
        "if mode=='stdout-eof': os.close(1)\n"
        "rows=[{'event':'app.entry','t_ms':0},{'event':'launch','t_ms':1,'pid':os.getpid()}]\n"
        "if mode!='early-exit' and (not mode.startswith('natural-') or mode=='natural-done'): rows.append({'event':'done','t_ms':2})\n"
        "for row in rows:\n"
        " data=('PROBE '+json.dumps(row)+'\\n').encode()\n"
        " if mode=='interleaved-streams' and row['event']=='launch':\n"
        "  split=len(data)//2; os.write(2,data[:split])\n"
        "  pathlib.Path(str(base)+'.prefix').touch()\n"
        "  while not pathlib.Path(str(base)+'.header').exists():\n"
        "   if time.monotonic()>deadline: sys.exit(8)\n"
        "   time.sleep(.01)\n"
        "  os.write(2,data[split:])\n"
        " else: os.write(2 if mode in ('interleaved-streams','stdout-eof','delayed-handler') else 1,data)\n"
        "if mode=='early-exit':\n"
        " os.close(1); os.close(2)\n"
        "pathlib.Path(str(base)+'.emitted').touch()\n"
        "if mode.startswith('natural-'):\n"
        " if mode in ('natural-app-alive','natural-cleanup-death'): os.close(1); os.close(2)\n"
        " if mode in ('natural-app-alive','natural-cleanup-death','natural-timeout'): time.sleep(60)\n"
        " sys.exit(78)\n"
        "if mode in ('quiet-orphan','delayed-handler'): os.close(1); os.close(2)\n"
        "if mode=='orphan-output':\n"
        " deadline=time.monotonic()+60\n"
        " while time.monotonic()<deadline:\n"
        "  print('CONSOLE fixture app still alive',flush=True); time.sleep(.02)\n"
        "time.sleep(60)\n"
    )
    launcher = work / "console_launcher.py"
    launcher.write_text(
        "import os,pathlib,signal,subprocess,sys,time\n"
        "child,base,mode=sys.argv[1:]; base=pathlib.Path(base)\n"
        "pathlib.Path(str(base)+'.launcher.pid').write_text(str(os.getpid()))\n"
        "app=subprocess.Popen([sys.executable,child,str(base),mode])\n"
        "def stop(signum,frame):\n"
        " pathlib.Path(str(base)+'.signal').write_text(str(signum))\n"
        " if mode=='orphan-output': sys.exit(0)\n"
        " if mode=='quiet-orphan': os._exit(0)\n"
        " if mode!='ignored-signal':\n"
        "  try: app.send_signal(signum)\n"
        "  except ProcessLookupError: pass\n"
        " if mode in ('buffered-header','delayed-handler'): app.wait(); os._exit(0)\n"
        "if mode!='delayed-handler': signal.signal(signal.SIGTERM,stop)\n"
        "if mode in ('buffered-header','quiet-orphan','stdout-eof'): print('" + BUNDLE + ": '+str(app.pid),flush=mode!='buffered-header')\n"
        "if mode=='stdout-eof': os.close(1)\n"
        "pathlib.Path(str(base)+'.ready').touch()\n"
        "if mode.startswith('natural-'):\n"
        " deadline=time.monotonic()+20\n"
        " while not pathlib.Path(str(base)+'.emitted').exists():\n"
        "  if time.monotonic()>deadline: sys.exit(8)\n"
        "  time.sleep(.01)\n"
        " if mode!='natural-missing-pid':\n"
        "  header='" + BUNDLE + ": '+str(app.pid+(1 if mode=='natural-wrong-pid' else 0))\n"
        "  for _ in range(2 if mode=='natural-duplicate-pid' else 1): print(header,flush=True)\n"
        " if mode=='natural-app-alive': sys.exit(78)\n"
        " if mode=='natural-cleanup-death': os.close(1); os.close(2)\n"
        " app.wait()\n"
        " sys.exit(0 if mode=='natural-zero' else 7 if mode=='natural-bad-status' else 78)\n"
        "if mode=='delayed-handler':\n"
        " deadline=time.monotonic()+20\n"
        " while not pathlib.Path(str(base)+'.emitted').exists():\n"
        "  if time.monotonic()>deadline: sys.exit(8)\n"
        "  time.sleep(.01)\n"
        " time.sleep(.1)\n"
        " signal.signal(signal.SIGTERM,stop)\n"
        " print('" + BUNDLE + ": '+str(app.pid),flush=False)\n"
        "if mode=='interleaved-streams':\n"
        " deadline=time.monotonic()+20\n"
        " while not pathlib.Path(str(base)+'.prefix').exists():\n"
        "  if time.monotonic()>deadline: sys.exit(8)\n"
        "  time.sleep(.01)\n"
        " print('" + BUNDLE + ": '+str(app.pid),flush=True)\n"
        " pathlib.Path(str(base)+'.header').touch()\n"
        "if mode=='early-exit':\n"
        " deadline=time.monotonic()+20\n"
        " while not pathlib.Path(str(base)+'.emitted').exists():\n"
        "  if time.monotonic()>deadline: sys.exit(8)\n"
        "  time.sleep(.01)\n"
        " sys.exit(7)\n"
        "if mode in ('buffered-header','delayed-handler'):\n"
        " while True: time.sleep(.1)\n"
        "app.wait()\n"
        "if mode not in ('missing-pid','buffered-header','quiet-orphan','interleaved-streams','stdout-eof','delayed-handler'):\n"
        " header='" + BUNDLE + ": '+str(app.pid+(1 if mode=='wrong-pid' else 0))\n"
        " for _ in range(2 if mode=='duplicate-pid' else 1): print(header,flush=True)\n"
    )
    fallback = work / "console_fallback.py"
    fallback.write_text(
        "import os,pathlib,signal,sys,time\n"
        "base=pathlib.Path(sys.argv[1]); path=pathlib.Path(str(base)+'.fallback')\n"
        "with path.open('a') as output: output.write('invoked\\n')\n"
        "try: os.kill(int(pathlib.Path(str(base)+'.app.pid').read_text()),signal.SIGTERM)\n"
        "except (ProcessLookupError,FileNotFoundError): pass\n"
        "sys.exit(7)\n"
    )
    return child, launcher, fallback


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def run_case(work, sources, mode, console=True, expected_app_exit=False, timeout=20):
    child, launcher, fallback = sources
    prefix = "expected-exit-" if expected_app_exit else "console-" if console else "traditional-"
    base = work / (prefix + mode)
    receipt = Path(str(base) + ".receipt.json")
    output = Path(str(base) + ".log")
    command = [sys.executable, str(ROOT / "run_scenario.py"),
               "--output", str(output), "--receipt", str(receipt),
               "--timeout", str(timeout), "--bundle-id", BUNDLE]
    if console:
        command.append("--console-launcher-stop")
    command += ["--terminate-command", sys.executable, str(fallback), str(base),
                "--launch-command", sys.executable, str(launcher), str(child), str(base), mode]
    if expected_app_exit:
        # The opt-in is intentionally a run() API, not a normal-stop CLI flag.
        # Keep its supervisor in a separate bounded process like the default path.
        command = [sys.executable, "-c",
                   "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                   "from run_scenario import run; "
                   "raise SystemExit(run(json.loads(sys.argv[2]),json.loads(sys.argv[3]),"
                   "Path(sys.argv[4]),Path(sys.argv[5]),float(sys.argv[6]),"
                   "{'bundleID':sys.argv[7]},console_launcher_stop=True,expected_app_exit=True))",
                   str(ROOT), json.dumps([sys.executable, str(launcher), str(child), str(base), mode]),
                   json.dumps([sys.executable, str(fallback), str(base)]),
                   str(output), str(receipt), str(timeout), BUNDLE]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        require(receipt.exists(), f"{base.name}: missing receipt: {result.stderr}")
        evidence = json.loads(receipt.read_text())
        for suffix in (".app.pid", ".launcher.pid"):
            path = Path(str(base) + suffix)
            require(path.exists(), f"{base.name}: missing {suffix}: {evidence}")
            pid = int(path.read_text())
            deadline = time.monotonic() + 2
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(.02)
            require(not alive(pid), f"{base.name}: owned process {pid} survived: {evidence}")
        return result, evidence, base
    finally:
        # Keep a failing control from leaving any of its own processes alive.
        for suffix in (".app.pid", ".launcher.pid"):
            path = Path(str(base) + suffix)
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def traditional_red(work, sources):
    result, receipt, base = run_case(work, sources, "complete", console=False)
    require(result.returncode != 0 and receipt["completed"] is True and receipt["success"] is False,
            f"traditional stop unexpectedly accepted: {receipt}")
    require(receipt.get("terminateExitCode") == 7 and
            "failed to terminate completed run-owned app" in receipt.get("error", ""),
            f"traditional stop did not expose the intended failure: {receipt}")
    require(Path(str(base) + ".fallback").exists(), "traditional stop did not invoke simctl substitute")
    return "runner-console-traditional-stop-red"


def receipt_controls(receipt):
    from summarize import successful_stop

    require(successful_stop(receipt), "console receipt helper rejected the real positive")
    mutations = {
        "method": {"terminationMethod": "unknown"},
        "transport": {"consoleTransport": "pipe"},
        "absence-method": {"appAbsenceMethod": "none"},
        "live-app": {"appAbsentAfterStop": False},
        "integer-absence": {"appAbsentAfterStop": 1},
        "signal": {"terminationSignal": 9},
        "boolean-signal": {"terminationSignal": True},
        "undelivered": {"terminationSignalDelivered": False},
        "integer-delivery": {"terminationSignalDelivered": 1},
        "launcher-pid": {"launcherPID": 0},
        "boolean-launcher-pid": {"launcherPID": True},
        "app-pid": {"appPID": 0, "launcherAppPID": 0},
        "mismatched-app-pid": {"launcherAppPID": receipt["appPID"] + 1},
        "boolean-app-pid": {"appPID": True, "launcherAppPID": True},
        "exit": {"launchExitCode": 7},
        "boolean-exit": {"launchExitCode": False},
        "not-completed": {"completed": False},
        "not-terminal": {"lastProbeEvent": "launch"},
        "command-result": {"terminateExitCode": 0},
        "cleanup-result": {"cleanupExitCode": 0},
        "cleanup-error": {"cleanupError": "failed"},
    }
    checks = []
    for name, changes in mutations.items():
        require(not successful_stop({**receipt, **changes}), f"console receipt helper accepted {name}")
        checks.append(f"receipt-console-rejects-{name}")
    for field in ("consoleTransport", "appAbsenceMethod", "appAbsentAfterStop", "terminationMethod", "terminationSignal", "terminationSignalDelivered", "launcherPID",
                  "appPID", "launcherAppPID", "launchExitCode", "completed", "lastProbeEvent"):
        incomplete = dict(receipt)
        del incomplete[field]
        require(not successful_stop(incomplete), f"console receipt helper accepted missing {field}")
        checks.append(f"receipt-console-requires-{field}")
    for code in (-15, 143):
        require(successful_stop({**receipt, "launchExitCode": code}), f"console receipt helper rejected exit {code}")
        checks.append(f"receipt-console-accepts-exit-{code}")
    return checks


def failed_exec_control(work):
    from run_scenario import run

    output = work / "failed-exec.log"
    receipt = work / "failed-exec.receipt.json"
    descriptors = set(os.listdir("/dev/fd"))
    threads = {thread.ident for thread in threading.enumerate()}
    code = run([str(work / "nonexistent-launcher")], [sys.executable, "-c", "pass"],
               output, receipt, 1, {"bundleID": BUNDLE}, console_launcher_stop=True)
    evidence = json.loads(receipt.read_text())
    require(code == 1 and evidence["success"] is False and "No such file" in evidence.get("error", ""),
            f"failed exec lost its failure: {evidence}")
    require(set(os.listdir("/dev/fd")) == descriptors, "failed exec leaked a PTY descriptor")
    require({thread.ident for thread in threading.enumerate()} == threads, "failed exec leaked a reader thread")
    return "runner-console-failed-exec-closes-pty"


def expected_exit_controls(work, sources):
    from summarize import successful_stop

    checks = []
    for mode, status in (("natural-78", 78), ("natural-zero", 0)):
        result, receipt, base = run_case(work, sources, mode, expected_app_exit=True)
        require(result.returncode == 0 and receipt.get("success") is True and
                receipt.get("completed") is False and receipt.get("expectedAppExit") is True and
                receipt.get("terminationMethod") == "natural-app-exit" and
                receipt.get("launchExitCode") == receipt.get("launchExitCodeBeforeCleanup") == status and
                receipt.get("appAbsentBeforeCleanup") is True and
                receipt.get("appAbsenceMethod") == "signal-zero",
                f"runner-{mode}: natural exit lacks pre-cleanup proof: {receipt}")
        require(receipt.get("appPID") == receipt.get("launcherAppPID") ==
                int(Path(str(base) + ".app.pid").read_text()) and
                receipt.get("launcherPID") == int(Path(str(base) + ".launcher.pid").read_text()) and
                receipt["launcherPID"] != receipt["appPID"],
                f"runner-{mode}: natural exit PID identity differs: {receipt}")
        require(not any(key in receipt for key in ("terminationSignal", "terminationSignalDelivered",
                    "terminateExitCode", "cleanupExitCode", "cleanupError")) and
                not Path(str(base) + ".signal").exists() and not Path(str(base) + ".fallback").exists(),
                f"runner-{mode}: cleanup caused the accepted exit: {receipt}")
        lines = Path(str(base) + ".log").read_text().splitlines()
        require(lines.count(f"{BUNDLE}: {receipt['appPID']}") == 1 and
                [json.loads(line.split("PROBE ", 1)[1])["event"] for line in lines if "PROBE " in line] ==
                ["app.entry", "launch"] and not successful_stop(receipt),
                f"runner-{mode}: natural exit became ordinary terminal success: {receipt}")
        checks.append(f"runner-console-opt-in-{mode}")
    result, receipt, base = run_case(work, sources, "natural-78")
    require(result.returncode != 0 and receipt.get("success") is False and
            receipt.get("completed") is False and "expectedAppExit" not in receipt and
            "launcher exited before terminal done event" in receipt.get("error", "") and
            Path(str(base) + ".fallback").exists() and receipt.get("cleanupExitCode") == 7 and
            not successful_stop(receipt),
            f"default console path accepted natural exit: {receipt}")
    checks.append("runner-console-default-rejects-natural-78")

    failures = (
        ("natural-wrong-pid", "launcher/probe app PID mismatch"),
        ("natural-missing-pid", "require one launcher app PID acknowledgement"),
        ("natural-duplicate-pid", "require one launcher app PID acknowledgement"),
        ("natural-app-alive", "run-owned app remains alive before natural-exit cleanup"),
        ("natural-done", "expected app exit received terminal done event"),
        ("natural-bad-status", "unexpected natural-exit launcher status"),
        ("natural-timeout", "deadline before terminal done event"),
        ("natural-cleanup-death", "timed out"),
    )
    for mode, error in failures:
        result, receipt, base = run_case(work, sources, mode, expected_app_exit=True,
                                         timeout=2 if mode == "natural-timeout" else 20)
        require(result.returncode != 0 and receipt.get("success") is False and
                receipt.get("completed") is False and error in receipt.get("error", "") and
                receipt.get("appAbsentBeforeCleanup") is not True and not successful_stop(receipt),
                f"runner-{mode}: expected-exit refusal differs: {receipt}")
        require(Path(str(base) + ".fallback").exists() and receipt.get("cleanupExitCode") == 7,
                f"runner-{mode}: failure did not clean its own app: {receipt}")
        if mode in ("natural-timeout", "natural-cleanup-death"):
            # The launcher can return an otherwise acceptable status only AFTER
            # fallback kills its app. That must never become natural-exit proof.
            require(receipt.get("launchExitCodeBeforeCleanup") is None and
                    receipt.get("launchExitCode") == 78,
                    f"runner-{mode}: cleanup did not exercise late launcher death: {receipt}")
        checks.append(f"runner-console-opt-in-rejects-{mode}")
    return checks


def run_checks(work):
    work.mkdir(parents=True, exist_ok=True)
    sources = fixtures(work)
    checks = [failed_exec_control(work), traditional_red(work, sources)]
    for mode in ("complete", "buffered-header", "interleaved-streams", "stdout-eof", "delayed-handler", "quiet-orphan", "wrong-pid", "missing-pid", "duplicate-pid", "ignored-signal", "early-exit", "orphan-output"):
        result, receipt, base = run_case(work, sources, mode)
        expected = mode in ("complete", "buffered-header", "interleaved-streams", "stdout-eof", "delayed-handler")
        require((result.returncode == 0) is expected and receipt["success"] is expected,
                f"runner-console-{mode}: {receipt}")
        if expected:
            require(receipt.get("terminationMethod") == "console-launcher-signal" and
                    receipt.get("terminationSignal") == 15 and receipt.get("terminationSignalDelivered") is True and
                    "terminateExitCode" not in receipt and
                    receipt.get("launcherAppPID") == receipt.get("appPID") and
                    receipt.get("launcherPID") != receipt.get("appPID") and
                    receipt.get("launcherPID") == int(Path(str(base) + ".launcher.pid").read_text()),
                    f"runner-console-{mode}: unbound success: {receipt}")
            lines = Path(str(base) + ".log").read_text().splitlines()
            if mode in ("complete", "delayed-handler"):
                require(lines[-1] == f"{BUNDLE}: {receipt['appPID']}" and
                        json.loads(lines[-2].split("PROBE ", 1)[1])["event"] == "done",
                        f"runner-console-{mode}: launcher PID did not arrive after done")
            elif mode == "buffered-header":
                require(lines[0] == f"{BUNDLE}: {receipt['appPID']}" and
                        RAW_OUTPUT in Path(str(base) + ".log").read_bytes(),
                        "buffered header or raw PTY bytes were lost")
            else:
                rows = [json.loads(line.split("PROBE ", 1)[1]) for line in lines if "PROBE " in line]
                require([row["event"] for row in rows] == ["app.entry", "launch", "done"] and
                        lines.count(f"{BUNDLE}: {receipt['appPID']}") == 1,
                        "separate stdout/stderr bytes corrupted a protocol row")
            require(Path(str(base) + ".signal").read_text() == "15" and
                    not Path(str(base) + ".fallback").exists(),
                    f"runner-console-{mode}: wrong stop path")
            if mode == "complete":
                checks.extend(receipt_controls(receipt))
        elif mode in ("wrong-pid", "missing-pid", "duplicate-pid"):
            error = "launcher/probe app PID mismatch" if mode == "wrong-pid" else "require one launcher app PID acknowledgement"
            require(receipt["completed"] is True and error in receipt.get("error", ""),
                    f"runner-console-{mode}: wrong rejection: {receipt}")
            require(Path(str(base) + ".fallback").exists() and receipt.get("cleanupExitCode") == 7,
                    f"runner-console-{mode}: fallback cleanup skipped: {receipt}")
        else:
            require(Path(str(base) + ".fallback").exists() and receipt.get("cleanupExitCode") == 7,
                    f"runner-console-{mode}: fallback cleanup skipped: {receipt}")
            if mode == "ignored-signal":
                require(receipt["completed"] is True and receipt["elapsedSeconds"] >= 15 and
                        Path(str(base) + ".signal").read_text() == "15",
                        f"runner-console-{mode}: did not exercise launcher wait deadline: {receipt}")
            elif mode == "quiet-orphan":
                require(receipt["completed"] is True and receipt.get("appAbsentAfterStop") is False and
                        receipt.get("appAbsenceMethod") == "signal-zero" and
                        "run-owned app PID still exists" in receipt.get("error", ""),
                        f"runner-console-{mode}: accepted a quiet surviving app: {receipt}")
            elif mode == "orphan-output":
                require(receipt["completed"] is True and 5 <= receipt["elapsedSeconds"] < 15 and
                        "launcher output did not close after stop" in receipt.get("error", "") and
                        Path(str(base) + ".signal").read_text() == "15" and
                        "CONSOLE fixture app still alive" in Path(str(base) + ".log").read_text(),
                        f"runner-console-{mode}: did not bound orphan output draining: {receipt}")
            else:
                require(receipt["completed"] is False and
                        "launcher exited before terminal done event" in receipt.get("error", ""),
                        f"runner-console-{mode}: lost early-exit error: {receipt}")
        checks.append(f"runner-console-{mode}")
    checks.extend(expected_exit_controls(work, sources))
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traditional-red", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    def execute(work):
        work.mkdir(parents=True, exist_ok=True)
        checks = [traditional_red(work, fixtures(work))] if args.traditional_red else run_checks(work)
        print(json.dumps({"success": True, "checks": checks}, indent=2))

    if args.output:
        execute(args.output)
    else:
        with tempfile.TemporaryDirectory(prefix="callkit-console-stop-check-") as temporary:
            execute(Path(temporary))


if __name__ == "__main__":
    main()
