#!/usr/bin/env bash
# Open desktop Big Picture and place its window on a chosen monitor.
#
# Part of the frontend dev loop (docs/contributing/frontend-dev-loop.md);
# called by the `dev:watch` / `dev:bpm-reset` mise tasks, usable standalone.
#
# Usage:
#   dev_open_bpm.sh [target]        open BPM, place the window on <target>
#   dev_open_bpm.sh --list          print selectable targets, one per line:
#                                   the normalized (lowercase, dash-stripped)
#                                   form of each connected + enabled output,
#                                   plus the "internal" alias while the
#                                   built-in panel is enabled (drives the mise
#                                   tab completion — its prefix filter is
#                                   case-sensitive, so the lowercase forms are
#                                   what complete)
#   dev_open_bpm.sh --resolve [t]   print the output name <t> resolves to and
#                                   exit without side effects (fail-fast guard)
#
# Target resolution — always against the REAL outputs of this machine (no
# hardcoded alias tables: on some Decks the external outputs are DP-2/DP-3,
# not DP-1/DP-2):
#   - "internal" (the default) resolves to the enabled output whose name
#     starts with eDP (the built-in panel).
#   - anything else matches an enabled output name case- and
#     dash-insensitively, so `dp2`, `DP2` and `DP-2` all resolve to `DP-2`.
#   - only connected AND enabled outputs count — KWin can't place a window on
#     a disabled output, so those are neither listed nor resolvable.
#   - an explicit target that matches nothing is a hard error (exit 1, before
#     anything opens); an unresolvable DEFAULT (internal panel disabled or
#     disconnected while docked) only warns, and Big Picture opens without
#     window placement.
#
# Window placement — KWin scripting over DBus (works on X11 and Wayland, no
# extra packages): a short-lived KWin script sweeps the existing windows for
# a caption containing "Big Picture" and watches windowAdded/captionChanged
# for the window when it appears, moves the first match to the target output
# (Plasma 6 API: workspace.windowList / workspace.sendClientToScreen /
# workspace.screens[].name), then goes inert. The script is unloaded again
# after ~120 s so re-runs never accumulate. If DBus/KWin scripting is
# unavailable, the placement degrades to a warning and BPM still opens.
set -euo pipefail

SCRIPT_NAME="decky-romm-sync-bpm-place"

enabled_outputs() {
  # connected AND enabled: KWin only exposes enabled outputs as screens, so a
  # connected-but-disabled panel (e.g. internal display switched off in KDE
  # while docked) can never receive a window — don't offer it as a target.
  command -v kscreen-doctor >/dev/null 2>&1 || return 0
  kscreen-doctor --json 2>/dev/null | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for output in data.get("outputs", []):
    if output.get("connected") and output.get("enabled") and output.get("name"):
        print(output["name"])
'
}

normalize() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '-'
}

list_targets() {
  # Selectable targets: the normalized form of each enabled output — exactly
  # the spellings the resolver accepts — plus the "internal" alias, but only
  # while the built-in panel is actually enabled (offering the alias for a
  # panel KWin can't place a window on would be a lie). Raw output names
  # (e.g. DP-2) resolve too; they're just not listed.
  local name outputs
  outputs=$(enabled_outputs)
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    case "$(normalize "$name")" in
      edp*)
        echo "internal"
        break
        ;;
    esac
  done <<EOF
$outputs
EOF
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    printf '%s\n' "$(normalize "$name")"
  done <<EOF
$outputs
EOF
}

resolve_target() {
  # Prints the enabled output name $1 resolves to; status 1 if none matches.
  local requested normalized name candidate
  requested="$1"
  normalized=$(normalize "$requested")
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    candidate=$(normalize "$name")
    if [ "$normalized" = "internal" ]; then
      case "$candidate" in edp*) printf '%s\n' "$name"; return 0 ;; esac
    elif [ "$candidate" = "$normalized" ]; then
      printf '%s\n' "$name"
      return 0
    fi
  done <<EOF
$(enabled_outputs)
EOF
  return 1
}

arm_kwin_placement() {
  # Loads + runs the KWin placement script for output $1.
  # Status 1 (with nothing armed) if any step of the DBus route fails.
  local output="$1" qdbus js_file script_id
  qdbus=$(command -v qdbus6 || command -v qdbus || true)
  [ -n "$qdbus" ] || return 1
  js_file=$(mktemp --suffix=.js) || return 1
  cat > "$js_file" <<'EOF' || { rm -f "$js_file"; return 1; }
var TARGET_OUTPUT = "@TARGET@";
// The Big Picture window we manage, and a bounded re-assert budget. Both are
// set once; see track() for why the budget exists.
var tracked = null;
var reasserts = 0;
var MAX_REASSERTS = 8;

// All log lines land in the user journal (journalctl --user | grep decky-bpm),
// so a placement that misses is diagnosable without re-instrumenting.
function log(msg) {
  print("[decky-bpm] " + msg);
}

function targetOutput() {
  var screens = workspace.screens;
  for (var i = 0; i < screens.length; i++) {
    if (screens[i].name === TARGET_OUTPUT) {
      return screens[i];
    }
  }
  return null;
}

function outputName(win) {
  return win && win.output ? win.output.name : "?";
}

function onTarget(win) {
  return win && win.output && win.output.name === TARGET_OUTPUT;
}

function isBigPicture(win) {
  if (!win || !win.resourceClass ||
      win.resourceClass.toLowerCase() !== "steam") {
    return false;
  }
  // Class "steam" covers both the desktop client window and Big Picture. BPM
  // keeps the "Big Picture" brand in its title across locales (German is
  // "Big-Picture-Modus"), so match on "picture" — the English "big picture"
  // with a space misses the hyphenated forms. A fullscreen steam window is BPM
  // as well (the desktop client window isn't fullscreen), which covers a locale
  // that fully translates the title.
  var caption = (win.caption || "").toLowerCase();
  return caption.indexOf("picture") !== -1 || win.fullScreen === true;
}

function place(win) {
  var output = targetOutput();
  if (!output) {
    log("target output " + TARGET_OUTPUT + " not present; leaving window put");
    return;
  }
  // sendClientToScreen is the idiomatic move; wrap it because the accepted
  // second-arg type has varied across KWin versions (Output vs screen index).
  try {
    workspace.sendClientToScreen(win, output);
  } catch (e) {
    log("sendClientToScreen threw: " + e);
  }
  // Reinforce/fallback for a windowed (non-fullscreen) BPM: put the frame on
  // the target output. Skipped for fullscreen, which KWin re-lays out per
  // output on its own.
  if (!win.fullScreen) {
    try {
      var geo = output.geometry;
      var fg = win.frameGeometry;
      win.frameGeometry = {
        x: geo.x + Math.max(0, Math.floor((geo.width - fg.width) / 2)),
        y: geo.y + Math.max(0, Math.floor((geo.height - fg.height) / 2)),
        width: fg.width,
        height: fg.height,
      };
    } catch (e) {
      log("geometry move threw: " + e);
    }
  }
}

function track(win) {
  if (tracked) {
    return;
  }
  tracked = win;
  log("BPM opened [" + win.caption + "] on " + outputName(win) +
    " -> placing on " + TARGET_OUTPUT);
  place(win);
  // A cold-started Steam repositions Big Picture onto its remembered monitor a
  // beat AFTER the window first appears, overriding the initial move. React to
  // the real event instead of guessing a delay: outputChanged fires whenever
  // the window changes monitor. Our own move fires it too, but lands on target,
  // so onTarget() short-circuits and there is no ping-pong. The budget bounds
  // how many times we fight Steam's startup shuffle, so a later MANUAL drag to
  // another display is left alone.
  if (win.outputChanged) {
    win.outputChanged.connect(function () {
      if (!tracked || onTarget(tracked)) {
        return;
      }
      if (reasserts >= MAX_REASSERTS) {
        log("stopped re-asserting after " + reasserts +
          "; window left on " + outputName(tracked));
        return;
      }
      reasserts++;
      log("Steam moved BPM to " + outputName(tracked) +
        "; re-assert #" + reasserts + " -> " + TARGET_OUTPUT);
      place(tracked);
    });
  }
}

log("armed for " + TARGET_OUTPUT + "; screens=" +
  workspace.screens.map(function (s) { return s.name; }).join(","));

// Cover a BPM window that is already open (Steam was running) — the sweep.
var existing = workspace.windowList();
for (var i = 0; i < existing.length; i++) {
  var w = existing[i];
  log("existing win caption=[" + w.caption + "] class=[" +
    w.resourceClass + "] output=[" + outputName(w) + "]");
  if (isBigPicture(w)) {
    track(w);
  }
}

// Cover a BPM window that opens after we arm (Steam cold-started).
workspace.windowAdded.connect(function (win) {
  log("added win caption=[" + win.caption + "] class=[" +
    win.resourceClass + "] output=[" + outputName(win) + "]");
  if (isBigPicture(win)) {
    track(win);
  } else if (win.captionChanged) {
    // resourceClass is set at creation, but the title can arrive a beat later.
    win.captionChanged.connect(function () {
      log("retitled -> caption=[" + win.caption + "]");
      if (isBigPicture(win)) {
        track(win);
      }
    });
  }
});
EOF
  sed -i "s/@TARGET@/$output/" "$js_file" || { rm -f "$js_file"; return 1; }
  # Guarded unload first so a re-run never collides with a lingering script.
  "$qdbus" org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript "$SCRIPT_NAME" >/dev/null 2>&1 || true
  script_id=$("$qdbus" org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript "$js_file" "$SCRIPT_NAME" 2>/dev/null) || {
    rm -f "$js_file"
    return 1
  }
  case "$script_id" in
    '' | *[!0-9]*)
      rm -f "$js_file"
      return 1
      ;;
  esac
  "$qdbus" org.kde.KWin "/Scripting/Script$script_id" org.kde.kwin.Script.run >/dev/null 2>&1 || {
    "$qdbus" org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript "$SCRIPT_NAME" >/dev/null 2>&1 || true
    rm -f "$js_file"
    return 1
  }
  # Keep the watcher armed for the window to appear, then self-clean.
  (
    sleep 120
    "$qdbus" org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript "$SCRIPT_NAME" >/dev/null 2>&1 || true
    rm -f "$js_file"
  ) &
  return 0
}

MODE="open"
TARGET="${1:-internal}"
case "$TARGET" in
  --list)
    list_targets
    exit 0
    ;;
  --resolve)
    MODE="resolve"
    TARGET="${2:-internal}"
    ;;
esac

RESOLVED=$(resolve_target "$TARGET") || RESOLVED=""
if [ -z "$RESOLVED" ]; then
  if [ "$(normalize "$TARGET")" = "internal" ]; then
    echo "warning: no enabled internal (eDP*) panel found — window placement will be skipped" >&2
  else
    {
      echo "error: display target '$TARGET' matches no enabled output. Available targets:"
      list_targets
    } >&2
    exit 1
  fi
fi

if [ "$MODE" = "resolve" ]; then
  [ -n "$RESOLVED" ] && echo "$RESOLVED"
  exit 0
fi

# Arm the KWin watcher BEFORE opening Big Picture so the window-added signal
# is already connected when the window appears (no race). The sweep half
# covers a BPM window that is already open on the wrong monitor.
if [ -n "$RESOLVED" ]; then
  if arm_kwin_placement "$RESOLVED"; then
    echo "Big Picture window will be placed on $RESOLVED."
  else
    echo "warning: could not reach KWin scripting over DBus — opening Big Picture without window placement" >&2
  fi
fi

# Open Big Picture. Harmless no-op if it is already open; if Steam is not
# running, this cold-starts it straight into BPM.
#
# Launch through the user's systemd manager, NOT this shell: `mise run` and the
# project venv prepend their own PATH + set VIRTUAL_ENV, and steam-jupiter's
# 32-bit runtime check fails in that polluted environment on a cold start
# ("You are missing the following 32-bit libraries: libc.so.6"). systemd-run
# --user runs steam in the pristine session environment (clean PATH, but
# DISPLAY / WAYLAND_DISPLAY / DBUS intact) that Game Mode itself uses.
if command -v systemd-run >/dev/null 2>&1; then
  systemd-run --user --collect --quiet -- steam steam://open/bigpicture >/dev/null 2>&1 &
else
  # No systemd user manager: best-effort scrub of the venv marker and detach.
  nohup env -u VIRTUAL_ENV steam steam://open/bigpicture >/dev/null 2>&1 &
fi
