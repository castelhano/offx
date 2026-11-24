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
"""

import os
import threading
import traceback

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

IMPL_NAME = "org.offx.selectiontip.Job"

# --- settings ---------------------------------------------------------------
AUTO_HIDE_SECONDS = 4.0   # 0 = keep visible while the range stays selected
OFFSET_X = 6              # px to the right of the selection's last cell
OFFSET_Y = 6              # px below the selection's last cell
PAD_X, PAD_Y = 6, 3       # inner padding of the tooltip
# -----------------------------------------------------------------------------

_BORDER = uno.getConstantByName("com.sun.star.awt.WindowAttribute.BORDER")


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

    def notify(self, generation):
        self.watcher.auto_hide(generation)


class ViewWatcher(unohelper.Base, XSelectionChangeListener,
                  XEnhancedMouseClickHandler, XTopWindowListener):

    def __init__(self, ctx, controller, on_dispose=None):
        self.ctx = ctx
        self.ctrl = controller
        self.on_dispose = on_dispose
        self.tip = None
        self.label = None
        self.formatter = None
        self.last_key = None
        self.generation = 0
        self.async_cb = None
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
        if self.tip is not None:
            try:
                self.tip.setVisible(False)
            except Exception:
                pass

    def auto_hide(self, generation):
        if self.alive and generation == self.generation:
            self.hide()

    def _schedule_hide(self):
        if AUTO_HIDE_SECONDS <= 0:
            return
        self.generation += 1
        gen = self.generation
        if self.async_cb is None:
            self.async_cb = self.ctx.ServiceManager.createInstanceWithContext(
                "com.sun.star.awt.AsyncCallback", self.ctx)
        cb, hider = self.async_cb, _Hider(self)

        def fire():
            try:
                cb.addCallback(hider, gen)
            except Exception:
                pass
        t = threading.Timer(AUTO_HIDE_SECONDS, fire)
        t.daemon = True
        t.start()

    # ------------------------------------------------------------- formatting
    def _format(self, value, first_cell):
        try:
            if self.formatter is None:
                self.formatter = self.ctx.ServiceManager.createInstanceWithContext(
                    "com.sun.star.util.NumberFormatter", self.ctx)
                self.formatter.attachNumberFormatsSupplier(self.ctrl.Model)
            return self.formatter.convertNumberToString(first_cell.NumberFormat, value)
        except Exception:
            return ("%.10g" % value)

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
            total = sel.computeFunction(SUM)
            first = sheet.getCellByPosition(addrs[0].StartColumn, addrs[0].StartRow)
            text += "   Soma: " + self._format(total, first)

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
        self.alive = False
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
