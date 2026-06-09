#!/bin/bash
# Attach-only wrapper for ttyd. Does NOT create the base session — the
# dashboard owns that creation flow.
#
# Each ttyd viewer attaches to its OWN per-viewer session in the same
# session group as the base. That gives every viewer independent
# window sizing: the base session's windows are shared, but each
# grouped session has its own "current window" and sizing policy
# (see `new-session -t` in tmux(1)), so one narrow client can't pin
# the whole group to 80×24.
#
# Exits cleanly when the WebSocket drops (tty gone) so stale wrappers
# don't accumulate as orphaned tmux clients. Cleans up the grouped
# view session on exit so idle viewers don't leak.
#
# Usage: ttyd_wrap.sh <session_name>

SESSION="${1:?Usage: ttyd_wrap.sh <session_name>}"

# Per-viewer grouped session name. PID + a short random suffix so two
# ttyd processes for the same base started in the same millisecond
# don't collide.
VIEW="${SESSION}-v$$-${RANDOM}"

tty_alive() {
    [ -t 0 ] && [ -t 1 ]
}

# Best-effort cleanup on exit.
WATCHER_PID=""
cleanup() {
    [ -n "$WATCHER_PID" ] && kill "$WATCHER_PID" 2>/dev/null
    tmux kill-session -t "=$VIEW" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Background watcher: while the wrapper is blocked inside `attach-session`
# we can't re-check whether the base session still exists. If the base is
# killed mid-attach (the common cause of orphan viewer sessions like
# `<base>-v<pid>-<rand>` outliving their parent), the viewer stays alive
# because it still has an attached client (ttyd). The watcher polls the
# base every 2s and, on disappearance, proactively kills the viewer so the
# attach unblocks and the wrapper exits. Belt-and-braces with the trap.
(
    while sleep 2; do
        if ! tmux has-session -t "=$SESSION" 2>/dev/null; then
            tmux kill-session -t "=$VIEW" 2>/dev/null
            exit 0
        fi
        # Stop if the parent wrapper has already exited.
        kill -0 "$$" 2>/dev/null || exit 0
    done
) &
WATCHER_PID=$!

while tty_alive; do
    # "=NAME" forces exact-match — tmux-browse's python side uses = everywhere.
    if ! tmux has-session -t "=$SESSION" 2>/dev/null; then
        echo "[tmux-browse] session '$SESSION' is gone" >&2
        exit 0
    fi

    # Create/refresh the per-viewer grouped session if it's gone. The
    # group membership (`-t =SESSION`) links its windows to the base;
    # `destroy-unattached on` makes it self-clean when the ttyd client
    # detaches; `window-size latest` resizes the session's windows to
    # the current (and only) attaching client rather than the
    # min-of-all-clients default.
    if ! tmux has-session -t "=$VIEW" 2>/dev/null; then
        tmux new-session -d -t "=$SESSION" -s "$VIEW" 2>/dev/null \
            && tmux set-option -t "=$VIEW" destroy-unattached on  >/dev/null 2>&1 \
            && tmux set-option -t "=$VIEW" window-size latest     >/dev/null 2>&1
        # If the grouped-session create failed (older tmux or transient
        # error), fall back to attaching directly to the base so the
        # viewer still gets something — the old behavior.
        if ! tmux has-session -t "=$VIEW" 2>/dev/null; then
            tmux attach-session -t "=$SESSION"
            sleep 1
            continue
        fi
    fi
    tmux attach-session -t "=$VIEW"
    sleep 1
done
