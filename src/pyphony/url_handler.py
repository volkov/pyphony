"""Custom URL scheme handler for pyphony:// URLs.

Supports URLs like:
    pyphony://SER-123/work?interactive=true

Parses the URL, resolves the pyphony executable, and opens a new iTerm2 tab
(or Terminal.app window as fallback) with the appropriate command.

Also provides ``install_url_scheme()`` to register a macOS .app bundle that
maps the ``pyphony://`` URL scheme to this handler.
"""

from __future__ import annotations

import os
import subprocess
import sys
import shutil
from pathlib import Path
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def parse_pyphony_url(url: str) -> dict[str, str]:
    """Parse a ``pyphony://`` URL into components.

    Examples::

        pyphony://SER-123/work?interactive=true
        → {"identifier": "SER-123", "action": "work", "interactive": "true"}

        pyphony://SER-45/work
        → {"identifier": "SER-45", "action": "work"}

    Returns a dict with keys: identifier, action, and any query params.
    """
    parsed = urlparse(url)

    # pyphony://SER-123/work  → host="SER-123", path="/work"
    # Also handle pyphony:///SER-123/work  → path="/SER-123/work"
    if parsed.hostname:
        identifier = parsed.hostname.upper()
        action = parsed.path.strip("/") or "work"
    else:
        parts = parsed.path.strip("/").split("/", 1)
        identifier = parts[0].upper() if parts else ""
        action = parts[1] if len(parts) > 1 else "work"

    result: dict[str, str] = {
        "identifier": identifier,
        "action": action,
    }

    # Add query params
    for key, values in parse_qs(parsed.query).items():
        result[key] = values[0] if values else ""

    return result


# ---------------------------------------------------------------------------
# iTerm2 / Terminal openers
# ---------------------------------------------------------------------------


def _find_pyphony_executable() -> str:
    """Return the path to the pyphony executable."""
    # 1. Check if pyphony is in PATH
    which = shutil.which("pyphony")
    if which:
        return which
    # 2. Fallback: same venv as current python
    venv_bin = Path(sys.executable).parent / "pyphony"
    if venv_bin.exists():
        return str(venv_bin)
    return "pyphony"


def _build_command(parsed: dict[str, str]) -> str:
    """Build the shell command to run based on parsed URL."""
    pyphony = _find_pyphony_executable()
    identifier = parsed["identifier"]
    action = parsed.get("action", "work")

    if action == "work":
        parts = [pyphony, "work", identifier, "--main"]
        return " ".join(parts)

    # Fallback: just open work
    return f"{pyphony} work {identifier} --main"


def _is_app_installed(app_name: str) -> bool:
    """Check if a macOS application is installed."""
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == 'com.googlecode.iterm2'"
             if app_name == "iTerm2" else
             f"kMDItemKind == 'Application' && kMDItemDisplayName == '{app_name}'"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _escape_for_applescript(s: str) -> str:
    """Escape a string for safe embedding in AppleScript double-quoted strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def open_in_iterm(command: str, title: str | None = None) -> bool:
    """Open a new iTerm2 tab and run *command*.

    Creates a new window if iTerm2 has no open windows, otherwise creates
    a new tab in the current window.

    Returns True if osascript succeeded.
    """
    if not _is_app_installed("iTerm2"):
        return False

    tab_title = _escape_for_applescript(title or "pyphony")
    safe_command = _escape_for_applescript(command)
    # Use "create window" as fallback when no window exists.
    script = f'''
tell application "iTerm2"
    activate
    if (count of windows) = 0 then
        create window with default profile
        tell current session of current window
            set name to "{tab_title}"
            write text "{safe_command}"
        end tell
    else
        tell current window
            create tab with default profile
            tell current session
                set name to "{tab_title}"
                write text "{safe_command}"
            end tell
        end tell
    end if
end tell
'''
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def open_in_terminal_app(command: str) -> bool:
    """Fallback: open a new Terminal.app window with *command*."""
    safe_command = _escape_for_applescript(command)
    script = f'''
tell application "Terminal"
    activate
    do script "{safe_command}"
end tell
'''
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def handle_url(url: str) -> None:
    """Parse a pyphony:// URL and open a terminal with the appropriate command."""
    parsed = parse_pyphony_url(url)

    if not parsed.get("identifier"):
        print(f"❌ Invalid URL: {url}", file=sys.stderr)
        sys.exit(1)

    command = _build_command(parsed)
    title = f"pyphony work {parsed['identifier']}"

    print(f"🚀 Opening: {command}")

    # Try iTerm2 first, then Terminal.app
    if open_in_iterm(command, title=title):
        print("✅ iTerm2 tab opened!")
    else:
        print("   iTerm2 not available, trying Terminal.app...")
        if open_in_terminal_app(command):
            print("✅ Terminal.app window opened!")
        else:
            print("❌ Could not open terminal", file=sys.stderr)
            print(f"   Run manually: {command}", file=sys.stderr)
            sys.exit(1)


# ---------------------------------------------------------------------------
# URL scheme installer
# ---------------------------------------------------------------------------

_APP_NAME = "PyphonyURLHandler"
_BUNDLE_ID = "com.pyphony.urlhandler"


def _app_bundle_path() -> Path:
    """Return the path where the .app bundle will be installed."""
    return Path.home() / "Applications" / f"{_APP_NAME}.app"


def install_url_scheme() -> None:
    """Create a macOS .app bundle that handles pyphony:// URLs.

    Uses ``osacompile`` to build an AppleScript applet that properly handles
    the ``open location`` Apple Event.  Falls back to a shell-based .app if
    osacompile is unavailable.

    The bundle is installed to ~/Applications/PyphonyURLHandler.app and
    registers the ``pyphony`` URL scheme via CFBundleURLTypes in Info.plist.
    """
    app_path = _app_bundle_path()

    pyphony_exe = _find_pyphony_executable()

    print(f"📦 Installing {_APP_NAME}.app to {app_path}")
    print(f"   pyphony executable: {pyphony_exe}")

    # -- Primary approach: osacompile AppleScript applet -------------------
    # The `on open location` handler is the correct way to receive URL
    # scheme events on macOS.
    applescript_source = (
        "on open location this_URL\n"
        f'    do shell script "{pyphony_exe} open-url "'
        " & quoted form of this_URL & "
        '" > /dev/null 2>&1 &"\n'
        "end open location\n"
    )

    print("   Compiling AppleScript handler...")
    compiled_ok = _compile_applescript_app(applescript_source, app_path, pyphony_exe)

    if not compiled_ok:
        # -- Fallback: shell-based .app bundle -----------------------------
        print("   ⚠️  osacompile unavailable, using shell-based handler")
        _create_shell_app(app_path, pyphony_exe)

    # Register the URL scheme with Launch Services
    _register_url_scheme(app_path)

    print(f"\n✅ Installed! The pyphony:// URL scheme is now registered.")
    print(f"   Try: open 'pyphony://SER-123/work'")


def _create_shell_app(app_path: Path, pyphony_exe: str) -> None:
    """Create a shell-script based .app bundle as fallback."""
    contents = app_path / "Contents"
    macos_dir = contents / "MacOS"

    macos_dir.mkdir(parents=True, exist_ok=True)

    # Info.plist with URL scheme registration
    info_plist = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>{_BUNDLE_ID}</string>
    <key>CFBundleName</key>
    <string>{_APP_NAME}</string>
    <key>CFBundleExecutable</key>
    <string>handler</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleURLTypes</key>
    <array>
        <dict>
            <key>CFBundleURLName</key>
            <string>Pyphony URL</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>pyphony</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
"""
    (contents / "Info.plist").write_text(info_plist, encoding="utf-8")

    handler_script = f"""\
#!/bin/bash
# PyphonyURLHandler — receives pyphony:// URL from macOS URL scheme dispatch.
for arg in "$@"; do
    case "$arg" in
        pyphony://*)
            "{pyphony_exe}" open-url "$arg"
            ;;
    esac
done
"""
    handler_path = macos_dir / "handler"
    handler_path.write_text(handler_script, encoding="utf-8")
    handler_path.chmod(0o755)


def _compile_applescript_app(source: str, app_path: Path, pyphony_exe: str) -> bool:
    """Compile an AppleScript source into a .app bundle using osacompile."""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".applescript", delete=False) as f:
        f.write(source)
        f.flush()
        src_path = f.name

    try:
        # osacompile -o App.app source.applescript
        result = subprocess.run(
            ["osacompile", "-o", str(app_path), src_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"   osacompile stderr: {result.stderr.strip()}")
            return False

        # Patch the Info.plist to add URL scheme registration
        plist_path = app_path / "Contents" / "Info.plist"
        if plist_path.exists():
            _patch_info_plist(plist_path)

        return True
    except FileNotFoundError:
        return False
    finally:
        os.unlink(src_path)


def _patch_info_plist(plist_path: Path) -> None:
    """Add CFBundleURLTypes to an existing Info.plist."""
    content = plist_path.read_text(encoding="utf-8")

    url_types_block = """\
\t<key>CFBundleURLTypes</key>
\t<array>
\t\t<dict>
\t\t\t<key>CFBundleURLName</key>
\t\t\t<string>Pyphony URL</string>
\t\t\t<key>CFBundleURLSchemes</key>
\t\t\t<array>
\t\t\t\t<string>pyphony</string>
\t\t\t</array>
\t\t</dict>
\t</array>"""

    # Insert before closing </dict>
    if "CFBundleURLTypes" not in content:
        content = content.replace(
            "</dict>\n</plist>",
            url_types_block + "\n</dict>\n</plist>",
        )
        plist_path.write_text(content, encoding="utf-8")


def _register_url_scheme(app_path: Path) -> None:
    """Tell Launch Services about the new app bundle."""
    # lsregister registers the app with Launch Services so macOS knows
    # about the URL scheme
    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework"
        "/Versions/A/Frameworks/LaunchServices.framework"
        "/Versions/A/Support/lsregister"
    )
    try:
        subprocess.run(
            [lsregister, "-R", str(app_path)],
            capture_output=True,
        )
    except FileNotFoundError:
        # lsregister not found — the app should still work when opened manually
        pass


def uninstall_url_scheme() -> None:
    """Remove the PyphonyURLHandler.app bundle."""
    app_path = _app_bundle_path()
    if app_path.exists():
        shutil.rmtree(app_path)
        print(f"✅ Removed {app_path}")
    else:
        print(f"ℹ️  {app_path} not found — nothing to remove")
