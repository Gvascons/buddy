"""X11 helpers for GTK4 windows.

Mostly small wrappers around python-xlib: set a GTK window
always-on-top (via `_NET_WM_STATE_ABOVE` ClientMessage), skip taskbar,
and make a window click-through.

Ported from the screen-copilot project's overlay.py (the
`_set_always_on_top` helper) and extended.
See https://github.com/Gvascons/screen-copilot
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib


def get_xid(gtk_window) -> int | None:
    """Return the X11 window ID for a GTK4 window, or None if unavailable.

    Public wrapper for the internal `_get_xid`. Used by app.py to
    register our own windows with screenshot.py so the active-window
    crop never tries to screenshot buddy's own UI.
    """
    return _get_xid(gtk_window)


def _get_xid(gtk_window) -> int | None:
    """Return the X11 window ID for a GTK4 window, or None if unavailable."""
    try:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11  # noqa: F401
    except (ValueError, ImportError):
        return None

    native = gtk_window.get_native()
    if native is None:
        return None
    surface = native.get_surface()
    if surface is None:
        return None

    # Only X11 surfaces expose get_xid. On Wayland this will be a
    # GdkWaylandSurface and we can't do ClientMessage tricks.
    from gi.repository import GdkX11
    if not isinstance(surface, GdkX11.X11Surface):
        return None
    try:
        return surface.get_xid()
    except Exception:
        return None


def set_always_on_top(gtk_window) -> bool:
    """Send `_NET_WM_STATE_ADD _NET_WM_STATE_ABOVE` to the window manager.

    Returns True if the message was sent. Safe to call multiple times —
    some WMs drop the above-state flag when a window is re-shown after
    being hidden, so we re-apply it after every `show()`.
    """
    try:
        from Xlib import display as xdisplay, X
        from Xlib.protocol import event
    except ImportError:
        print("⚠️ python-xlib not installed; cannot set always-on-top")
        return False

    xid = _get_xid(gtk_window)
    if xid is None:
        print("⚠️ xlib_helpers: couldn't resolve X11 window id (Wayland?)")
        return False

    try:
        d = xdisplay.Display()
        root = d.screen().root
        win = d.create_resource_object("window", xid)

        net_wm_state = d.intern_atom("_NET_WM_STATE")
        net_wm_state_above = d.intern_atom("_NET_WM_STATE_ABOVE")
        _NET_WM_STATE_ADD = 1

        ev = event.ClientMessage(
            window=win,
            client_type=net_wm_state,
            data=(32, [_NET_WM_STATE_ADD, net_wm_state_above, 0, 1, 0]),
        )
        mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
        root.send_event(ev, event_mask=mask)
        d.flush()
        d.close()
        return True
    except Exception as exc:
        print(f"⚠️ always-on-top failed: {exc}")
        return False


def set_skip_taskbar(gtk_window) -> bool:
    """Ask the WM to keep this window out of the taskbar / pager."""
    try:
        from Xlib import display as xdisplay, X
        from Xlib.protocol import event
    except ImportError:
        return False

    xid = _get_xid(gtk_window)
    if xid is None:
        return False

    try:
        d = xdisplay.Display()
        root = d.screen().root
        win = d.create_resource_object("window", xid)

        net_wm_state = d.intern_atom("_NET_WM_STATE")
        skip_taskbar = d.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        skip_pager = d.intern_atom("_NET_WM_STATE_SKIP_PAGER")

        for atom in (skip_taskbar, skip_pager):
            ev = event.ClientMessage(
                window=win,
                client_type=net_wm_state,
                data=(32, [1, atom, 0, 1, 0]),  # _NET_WM_STATE_ADD
            )
            root.send_event(
                ev,
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
            )
        d.flush()
        d.close()
        return True
    except Exception as exc:
        print(f"⚠️ skip-taskbar failed: {exc}")
        return False


def make_click_through(gtk_window) -> bool:
    """Set an empty input region on the window so clicks pass through."""
    try:
        import cairo
    except ImportError:
        return False

    native = gtk_window.get_native()
    if native is None:
        return False
    surface = native.get_surface()
    if surface is None:
        return False

    try:
        empty_region = cairo.Region()
        surface.set_input_region(empty_region)
        return True
    except Exception as exc:
        print(f"⚠️ click-through failed: {exc}")
        return False


def apply_overlay_hints(gtk_window, *, click_through: bool = True) -> None:
    """Apply the full set of hints used by the cursor overlay.

    Schedules itself after a small delay so the window is fully realized
    and mapped before we talk to the X server.
    """
    def _apply() -> bool:
        set_always_on_top(gtk_window)
        set_skip_taskbar(gtk_window)
        if click_through:
            make_click_through(gtk_window)
        return False  # don't repeat

    GLib.timeout_add(250, _apply)
