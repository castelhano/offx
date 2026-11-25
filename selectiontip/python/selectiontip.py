# -*- coding: utf-8 -*-
"""Selection Tip for LibreOffice Calc.

Shows a small tooltip next to the selected range with the number of
selected cells and, when the range contains numbers, their sum.

Design notes (performance):
  * Work happens only on selection-change events coming from Calc.
  * Count is derived from range addresses (no cell iteration).
  * SUM / COUNT use XSheetOperation.computeFunction, which runs natively
    in Calc's C++ core (same code path as the status bar), so even whole
    columns are cheap.
  * The tooltip window is created once per view and only moved/re-labelled.
  * The sum's format comes from a single cell, never from scanning the range.
  * Auto-hide uses at most one (sleeping) thread per view.
"""

import os
import threading
import time
import traceback
from decimal import Decimal, ROUND_HALF_UP

import uno
import unohelper

from com.sun.star.task import XJob
from com.sun.star.document import XDocumentEventListener
from com.sun.star.view import XSelectionChangeListener
from com.sun.star.awt import (XEnhancedMouseClickHandler, XTopWindowListener,
                              XCallback, WindowDescriptor, Rectangle)
from com.sun.star.awt.WindowClass import TOP, SIMPLE
from com.sun.star.awt.PosSize import POSSIZE
from com.sun.star.sheet.GeneralFunction import SUM, COUNTNUMS
from com.sun.star.table.CellContentType import EMPTY, TEXT
from com.sun.star.util.NumberFormat import DATE, TIME, CURRENCY, PERCENT

IMPL_NAME = "org.offx.selectiontip.Job"

# --- settings ---------------------------------------------------------------
AUTO_HIDE_SECONDS = 4.0   # 0 = keep visible while the range stays selected
OFFSET_X = 6              # px to the right of the selection's last cell
OFFSET_Y = 6              # px below the selection's last cell
PAD_X, PAD_Y = 6, 3       # inner padding of the tooltip
# -----------------------------------------------------------------------------

_BORDER = uno.getConstantByName("com.sun.star.awt.WindowAttribute.BORDER")


_CENTS = Decimal("0.01")
_LOG = os.environ.get("SELECTIONTIP_LOG")


def _log(msg):
    """Debug log, enabled with SELECTIONTIP_LOG=/path/to/file."""
    if _LOG:
        try:
            with open(_LOG, "a") as f:
                f.write(msg.rstrip() + "\n")
        except Exception:
            pass


def _int(n):
    return "{:,}".format(n).replace(",", ".")


def _is_calc(model):
    try:
        return model is not None and model.supportsService(
            "com.sun.star.sheet.SpreadsheetDocument")
    except Exception:
        return False


class _Hider(unohelper.Base, XCallback):
    """Runs on the main thread (via AsyncCallback) to hide the tooltip."""

    def __init__(self, watcher):
        self.watcher = watcher

    def notify(self, data):
        self.watcher.auto_hide()


class ViewWatcher(unohelper.Base, XSelectionChangeListener,
                  XEnhancedMouseClickHandler, XTopWindowListener):

    def __init__(self, ctx, controller, on_dispose=None):
        self.ctx = ctx
        self.ctrl = controller
        self.on_dispose = on_dispose
        self.tip = None
        self.label = None
        self.last_key = None
        self.formatter = None       # created only if a currency sum shows up
        self.async_cb = None
        self.hider = None
        self.deadline = None        # monotonic time to hide; None = hidden
        self.timer = None           # the single auto-hide thread, if running
        self.cond = threading.Condition()
        self.alive = True
        controller.addSelectionChangeListener(self)
        controller.addEnhancedMouseClickHandler(self)
        try:
            controller.Frame.ContainerWindow.addTopWindowListener(self)
        except Exception:
            pass

    # ---------------------------------------------------------------- tooltip
    def _ensure_tip(self):
        if self.tip is not None:
            return
        smgr = self.ctx.ServiceManager
        tk = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        parent = self.ctrl.Frame.ComponentWindow

        d = WindowDescriptor()
        d.Type = TOP
        d.WindowServiceName = "floatingwindow"
        d.Parent = parent
        d.Bounds = Rectangle(0, 0, 10, 10)
        d.WindowAttributes = _BORDER
        self.tip = tk.createWindow(d)

        d2 = WindowDescriptor()
        d2.Type = SIMPLE
        d2.WindowServiceName = "fixedtext"
        d2.Parent = self.tip
        d2.Bounds = Rectangle(PAD_X, PAD_Y, 10, 10)
        d2.WindowAttributes = 0
        self.label = tk.createWindow(d2)

        try:
            st = parent.StyleSettings
            bg, fg = st.HelpColor, st.HelpTextColor
        except Exception:
            bg, fg = 0xFFFFE1, 0x000000
        self.tip.setBackground(bg)
        self.label.setBackground(bg)
        try:
            self.label.setProperty("TextColor", fg)
        except Exception:
            pass
        self.label.setVisible(True)

    def hide(self):
        self.last_key = None
        self.deadline = None
        if self.tip is not None:
            try:
                self.tip.setVisible(False)
            except Exception:
                pass

    def auto_hide(self):
        # main thread: the deadline may have moved since the timer fired
        if self.alive and self.deadline is not None \
                and time.monotonic() >= self.deadline:
            self.hide()

    def _schedule_hide(self):
        if AUTO_HIDE_SECONDS <= 0:
            return
        if self.async_cb is None:
            self.async_cb = self.ctx.ServiceManager.createInstanceWithContext(
                "com.sun.star.awt.AsyncCallback", self.ctx)
            self.hider = _Hider(self)
        with self.cond:
            self.deadline = time.monotonic() + AUTO_HIDE_SECONDS
            if self.timer is None:      # otherwise the running one picks it up
                self.timer = threading.Thread(target=self._wait_and_hide,
                                              daemon=True)
                self.timer.start()

    def _wait_and_hide(self):
        """Timer thread: sleeps until the (possibly extended) deadline."""
        with self.cond:
            while self.alive and self.deadline is not None:
                left = self.deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            due = self.alive and self.deadline is not None
            self.timer = None
        if due:     # outside the lock: never call UNO while holding it
            try:
                self.async_cb.addCallback(self.hider, None)
            except Exception:
                _log(traceback.format_exc())

    # ------------------------------------------------------------- formatting
    @staticmethod
    def _format(value):
        """1234567.891 -> '1.234.567,89' (2 decimals, pt-BR separators)."""
        d = Decimal(repr(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)
        if d == 0:
            d = abs(d)                     # avoid "-0,00"
        s = "{:,.2f}".format(d)            # '1,234,567.89'
        return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")

    @staticmethod
    def _format_time(days):
        """1.0625 -> '25:30'; does not wrap at 24h, seconds only if any."""
        secs = int(abs(days) * 86400 + 0.5)
        m, sec = divmod(secs, 60)
        h, m = divmod(m, 60)
        text = "{}{}:{:02d}".format("-" if days < 0 and secs else "", _int(h), m)
        return text + ":{:02d}".format(sec) if sec else text

    @staticmethod
    def _reference_cell(sheet, a):
        """Cell that defines the sum's format: the first one or, when that is
        a header/empty, the next one (below; to the right in a single row)."""
        cell = sheet.getCellByPosition(a.StartColumn, a.StartRow)
        if cell.Type in (EMPTY, TEXT):
            if a.EndRow > a.StartRow:
                return sheet.getCellByPosition(a.StartColumn, a.StartRow + 1)
            if a.EndColumn > a.StartColumn:
                return sheet.getCellByPosition(a.StartColumn + 1, a.StartRow)
        return cell

    def _sum_text(self, sel, sheet, addr):
        """Formatted sum, or None when it is meaningless (dates)."""
        model = self.ctrl.Model
        key = self._reference_cell(sheet, addr).NumberFormat
        kind = model.NumberFormats.getByKey(key).Type
        if kind & DATE:
            return None
        total = sel.computeFunction(SUM)
        if kind & TIME:
            return self._format_time(total)
        if kind & PERCENT:
            return self._format(total * 100) + "%"
        if kind & CURRENCY:
            if self.formatter is None:
                self.formatter = self.ctx.ServiceManager.createInstanceWithContext(
                    "com.sun.star.util.NumberFormatter", self.ctx)
                self.formatter.attachNumberFormatsSupplier(model)
            return self.formatter.convertNumberToString(key, total)
        return self._format(total)

    # ---------------------------------------------------------------- geometry
    def _anchor_px(self, sheet, addr):
        """Bottom-right corner of the range's last cell, in coordinates
        relative to the top-level frame (what a child float expects)."""
        va = self.ctrl.VisibleArea           # 1/100 mm
        vs = self.ctrl.VisibleAreaOnScreen   # pixels
        if va.Width <= 0 or va.Height <= 0:
            return None
        cell = sheet.getCellByPosition(addr.EndColumn, addr.EndRow)
        p, s = cell.Position, cell.Size
        x_mm = p.X + s.Width
        y_mm = p.Y + s.Height
        sx = vs.Width / va.Width
        sy = vs.Height / va.Height
        x = vs.X + int((x_mm - va.X) * sx) + OFFSET_X
        y = vs.Y + int((y_mm - va.Y) * sy) + OFFSET_Y
        top = self.ctrl.Frame.ContainerWindow.PosSize
        return x, y, vs, top

    # ------------------------------------------------------------------ update
    def update(self):
        sel = self.ctrl.getSelection()
        if sel is None:
            return self.hide()
        if sel.supportsService("com.sun.star.sheet.SheetCellRange"):
            addrs = (sel.RangeAddress,)
        elif sel.supportsService("com.sun.star.sheet.SheetCellRanges"):
            addrs = tuple(sel.RangeAddresses)
        else:
            return self.hide()          # shapes, charts, etc.
        if not addrs:
            return self.hide()

        key = tuple((a.Sheet, a.StartColumn, a.StartRow, a.EndColumn, a.EndRow)
                    for a in addrs)
        if key == self.last_key:
            return
        cells = 0
        for a in addrs:
            cells += (a.EndRow - a.StartRow + 1) * (a.EndColumn - a.StartColumn + 1)
        if cells <= 1:
            return self.hide()

        rows = addrs[0].EndRow - addrs[0].StartRow + 1
        cols = addrs[0].EndColumn - addrs[0].StartColumn + 1
        text = _int(cells) + " células"
        if len(addrs) == 1 and rows > 1 and cols > 1:
            text += "  ({}L × {}C)".format(_int(rows), _int(cols))

        sheet = self.ctrl.ActiveSheet
        numbers = sel.computeFunction(COUNTNUMS)
        if numbers > 0:
            total = self._sum_text(sel, sheet, addrs[0])
            if total is not None:
                text += "   Soma: " + total

        anchor = self._anchor_px(sheet, addrs[-1])
        if anchor is None:
            return self.hide()
        x, y, vs, top = anchor

        self._ensure_tip()
        self.label.setProperty("Text", text)
        pref = self.label.getPreferredSize()
        w = pref.Width + 2 * PAD_X
        h = pref.Height + 2 * PAD_Y
        self.label.setPosSize(PAD_X, PAD_Y, pref.Width, pref.Height, POSSIZE)

        # keep it inside the visible grid
        x = max(vs.X, min(x, vs.X + vs.Width - w))
        y = max(vs.Y, min(y, vs.Y + vs.Height - h))
        # floating windows are positioned relative to the parent frame
        x -= top.X
        y -= top.Y

        self.tip.setPosSize(x, y, w, h, POSSIZE)
        if not self.tip.isVisible():
            self.tip.setVisible(True)
        self.last_key = key
        self._schedule_hide()

    # ------------------------------------------------- XSelectionChangeListener
    def selectionChanged(self, ev):
        try:
            self.update()
            if _LOG:
                _log("update: %r" % (self.label.getProperty("Text")
                     if self.tip and self.tip.isVisible() else None,))
        except Exception:
            _log(traceback.format_exc())
            self.hide()

    def disposing(self, ev):
        self.detach()

    # ---------------------------------------------- XEnhancedMouseClickHandler
    def mousePressed(self, ev):
        self.hide()
        return True     # never consume the event

    def mouseReleased(self, ev):
        return True

    # ------------------------------------------------------ XTopWindowListener
    def windowDeactivated(self, ev):
        self.hide()

    def windowOpened(self, ev): pass
    def windowClosing(self, ev): pass
    def windowClosed(self, ev): pass
    def windowMinimized(self, ev): self.hide()
    def windowNormalized(self, ev): pass
    def windowActivated(self, ev): pass

    # ------------------------------------------------------------------ detach
    def detach(self):
        if not self.alive:
            return
        with self.cond:
            self.alive = False
            self.cond.notify_all()      # let the timer thread end now
        for fn in (lambda: self.ctrl.removeSelectionChangeListener(self),
                   lambda: self.ctrl.removeEnhancedMouseClickHandler(self),
                   lambda: self.ctrl.Frame.ContainerWindow.removeTopWindowListener(self),
                   lambda: self.tip and self.tip.dispose()):
            try:
                fn()
            except Exception:
                pass
        self.tip = self.label = None
        if self.on_dispose:
            self.on_dispose(self)


class Manager(unohelper.Base, XDocumentEventListener):
    """Attaches a ViewWatcher to every Calc view."""

    _instance = None

    @classmethod
    def get(cls, ctx):
        if cls._instance is None:
            cls._instance = cls(ctx)
        return cls._instance

    def __init__(self, ctx):
        self.ctx = ctx
        self.watchers = []
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        geb = self.ctx.getValueByName(
            "/singletons/com.sun.star.frame.theGlobalEventBroadcaster")
        geb.addDocumentEventListener(self)
        desktop = self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", self.ctx)
        comps = desktop.Components.createEnumeration()
        while comps.hasMoreElements():
            m = comps.nextElement()
            if _is_calc(m):
                try:
                    ctrls = m.getControllers()
                    while ctrls.hasMoreElements():
                        self.attach(ctrls.nextElement())
                except Exception:
                    pass

    def attach(self, controller):
        if controller is None:
            return
        for w in self.watchers:
            if w.ctrl == controller:
                return
        try:
            self.watchers.append(ViewWatcher(self.ctx, controller, self._forget))
            _log("attached to view (%d active)" % len(self.watchers))
        except Exception:
            _log(traceback.format_exc())

    def _forget(self, watcher):
        if watcher in self.watchers:
            self.watchers.remove(watcher)

    def documentEventOccured(self, ev):
        name = ev.EventName
        if name == "OnViewCreated" and _is_calc(ev.Source):
            ctrl = ev.ViewController or ev.Source.CurrentController
            self.attach(ctrl)
        # closing views is handled by the controller's disposing() callback

    def disposing(self, ev):
        pass


class Job(unohelper.Base, XJob):
    def __init__(self, ctx):
        self.ctx = ctx

    def execute(self, args):
        _log("job started")
        try:
            Manager.get(self.ctx).start()
        except Exception:
            _log(traceback.format_exc())
        return None


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(Job, IMPL_NAME, (IMPL_NAME,))
