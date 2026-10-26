#!/usr/bin/env python3
"""
富途牛牛 單一個股懸浮視窗

透過 Futu OpenAPI（需先在本機開啟並登入 OpenD）訂閱一檔股票的即時報價，
以一個永遠置頂、無邊框、可拖曳的小視窗顯示。

用法：
    python floating_quote.py                  # 使用上次的股票（預設 US.BE Bloom Energy）
    python floating_quote.py --code US.TSLA   # 指定股票
    python floating_quote.py --demo           # 不連 OpenD，用假資料看外觀

操作：
    左鍵拖曳     移動視窗
    雙擊         換股票
    右鍵         選單（換股票 / 漲跌顏色 / 透明度 / 精簡模式 / 結束）
"""

import argparse
import json
import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import simpledialog

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "floating_quote.json")

DEFAULT_CONFIG = {
    "code": "US.BE",       # Bloom Energy
    "host": "127.0.0.1",
    "port": 11111,
    "x": None,
    "y": None,
    "alpha": 0.92,
    "red_up": True,      # 台股習慣：紅漲綠跌
    "compact": False,
    "topmost": True,
}

# 與交易統計報表同一組深色配色
BG = "#10151d"
BG_HEAD = "#141a24"
RULE = "#1e2531"
INK = "#d8dde6"
INK_SOFT = "#7f8a9a"
INK_FAINT = "#566070"
ACCENT = "#f0a500"
RED = "#f6465d"
GREEN = "#0ecb81"
FLASH_RED = "#3a1a22"
FLASH_GREEN = "#12322a"

HEARTBEAT_SEC = 10   # 每隔幾秒主動查一次報價，順便偵測斷線


# ---------------------------------------------------------------- 工具函式

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


MARKETS = {"US", "HK", "SH", "SZ", "SG", "JP", "AU", "MY", "CA", "FX"}


def normalize_code(text):
    """把使用者輸入轉成富途代碼：AAPL→US.AAPL、700→HK.00700、600519→SH.600519。"""
    s = text.strip().upper().replace(" ", "")
    if not s:
        return None
    market, _, sym = s.partition(".")
    if sym and market in MARKETS:
        if market == "HK" and sym.isdigit():
            sym = sym.zfill(5)
        return f"{market}.{sym}"
    if s.isdigit():
        if len(s) <= 5:
            return "HK." + s.zfill(5)
        if len(s) == 6:
            return ("SH." if s[0] in "569" else "SZ.") + s
    return "US." + s


def num(v):
    """把富途回傳的值轉成 float；'N/A'、空值、NaN 一律回傳 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def fmt_price(p):
    if p is None:
        return "--"
    if abs(p) < 1:
        return f"{p:.4f}"
    if abs(p) < 10:
        return f"{p:.3f}"
    return f"{p:,.2f}"


def fmt_volume(v):
    if v is None:
        return "--"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:.0f}"


def pick_font(candidates, fallback):
    try:
        families = set(tkfont.families())
    except tk.TclError:
        return fallback
    for name in candidates:
        if name in families:
            return name
    return fallback


# ---------------------------------------------------------------- 資料來源

class FutuWorker(threading.Thread):
    """在背景執行緒連線 OpenD、訂閱報價，把結果丟進 out_q 給 UI。"""

    def __init__(self, host, port, out_q):
        super().__init__(daemon=True)
        self.host, self.port, self.out_q = host, port, out_q
        self.cmd_q = queue.Queue()
        self.code = None

    def switch(self, code):
        self.cmd_q.put(("switch", code))

    def stop(self):
        self.cmd_q.put(("stop", None))

    def _status(self, text, ok=False):
        self.out_q.put(("status", (text, ok)))

    def _emit(self, df):
        for _, row in df.iterrows():
            if row.get("code") == self.code:
                self.out_q.put(("quote", row.to_dict()))

    def run(self):
        try:
            from futu import (OpenQuoteContext, RET_OK, StockQuoteHandlerBase,
                              SubType, SysConfig)
        except ImportError:
            self._status("缺少 futu-api：pip install futu-api")
            return

        SysConfig.set_all_thread_daemon(True)
        worker = self

        class Handler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == RET_OK:
                    worker._emit(data)
                return ret, data

        self._status(f"連線 OpenD {self.host}:{self.port} …")
        # OpenD 沒開時這裡會一直等待重試，所以放在背景執行緒
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        ctx.set_handler(Handler())
        self._status("已連線", ok=True)

        while True:
            try:
                cmd, arg = self.cmd_q.get(timeout=HEARTBEAT_SEC)
            except queue.Empty:
                cmd, arg = "heartbeat", None

            if cmd == "stop":
                ctx.close()
                return

            if cmd == "switch":
                if self.code and self.code != arg:
                    # 訂閱未滿 1 分鐘不能退訂，失敗就算了（額度很夠）
                    ctx.unsubscribe([self.code], [SubType.QUOTE])
                self.code = arg
                ret, err = ctx.subscribe([arg], [SubType.QUOTE])
                if ret != RET_OK:
                    self._status(f"訂閱失敗：{err}")
                    continue
                self._status("已訂閱", ok=True)

            if self.code:
                ret, data = ctx.get_stock_quote([self.code])
                if ret == RET_OK:
                    self._emit(data)
                    self._status("", ok=True)
                else:
                    self._status(f"查詢失敗：{data}")


class DemoWorker(threading.Thread):
    """不連 OpenD，產生隨機報價，用來看視窗外觀。"""

    def __init__(self, out_q):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.code = None
        self.price = 100.0
        self._stop = threading.Event()

    def switch(self, code):
        self.code = code
        self.price = random.uniform(20, 400)
        self.prev = self.price * random.uniform(0.95, 1.05)

    def stop(self):
        self._stop.set()

    def run(self):
        self.out_q.put(("status", ("DEMO 假資料", True)))
        while not self._stop.wait(1.0):
            if not self.code:
                continue
            self.price *= 1 + random.gauss(0, 0.002)
            self.out_q.put(("quote", {
                "code": self.code,
                "name": "示範股票",
                "data_time": time.strftime("%H:%M:%S"),
                "last_price": self.price,
                "prev_close_price": self.prev,
                "high_price": max(self.price, self.prev) * 1.01,
                "low_price": min(self.price, self.prev) * 0.99,
                "volume": random.randint(1_000_000, 50_000_000),
                "after_price": self.price * 1.003 if self.code.startswith("US.") else "N/A",
                "after_change_val": self.price * 0.003,
                "after_change_rate": 0.3,
            }))


# ---------------------------------------------------------------- 視窗

class FloatingQuote:
    def __init__(self, cfg, worker, data_q):
        self.cfg = cfg
        self.worker = worker
        self.q = data_q
        self.last_price = None
        self._drag = (0, 0)
        self._flash_job = None

        root = self.root = tk.Tk()
        root.title("Futu 懸浮報價")
        root.overrideredirect(True)
        root.configure(bg=RULE)  # 1px 外框
        root.attributes("-topmost", cfg["topmost"])
        self._set_alpha(cfg["alpha"])

        sans = pick_font(["Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC",
                          "Noto Sans TC", "Segoe UI", "Helvetica"], "TkDefaultFont")
        mono = pick_font(["IBM Plex Mono", "Consolas", "Menlo", "SF Mono",
                          "DejaVu Sans Mono"], "TkFixedFont")

        self.body = tk.Frame(root, bg=BG, padx=12, pady=8)
        self.body.pack(padx=1, pady=1, fill="both", expand=True)

        head = tk.Frame(self.body, bg=BG)
        head.pack(fill="x")
        self.lbl_code = tk.Label(head, text=cfg["code"], fg=ACCENT, bg=BG, font=(mono, 10, "bold"))
        self.lbl_code.pack(side="left")
        self.lbl_dot = tk.Label(head, text="●", fg=INK_FAINT, bg=BG, font=(sans, 8))
        self.lbl_dot.pack(side="right")
        self.lbl_name = tk.Label(self.body, text="", fg=INK_SOFT, bg=BG, font=(sans, 9), anchor="w")
        self.lbl_name.pack(fill="x")

        self.lbl_price = tk.Label(self.body, text="--", fg=INK, bg=BG, font=(mono, 22, "bold"), anchor="w")
        self.lbl_price.pack(fill="x")
        self.lbl_change = tk.Label(self.body, text="", fg=INK_SOFT, bg=BG, font=(mono, 11), anchor="w")
        self.lbl_change.pack(fill="x")

        # 以下為詳細資訊，精簡模式時隱藏
        self.detail = tk.Frame(self.body, bg=BG)
        self.lbl_ext = tk.Label(self.detail, text="", fg=INK_SOFT, bg=BG, font=(mono, 9), anchor="w")
        self.lbl_ext.pack(fill="x")
        self.lbl_hl = tk.Label(self.detail, text="", fg=INK_SOFT, bg=BG, font=(mono, 9), anchor="w")
        self.lbl_hl.pack(fill="x")
        self.lbl_status = tk.Label(self.detail, text="", fg=INK_FAINT, bg=BG, font=(sans, 8), anchor="w")
        self.lbl_status.pack(fill="x")
        if not cfg["compact"]:
            self.detail.pack(fill="x", pady=(4, 0))

        self._build_menu()
        self._bind_all(root)

        root.update_idletasks()
        x, y = cfg.get("x"), cfg.get("y")
        if x is None or y is None:
            x = root.winfo_screenwidth() - 240
            y = 80
        root.geometry(f"+{int(x)}+{int(y)}")

        root.after(100, self._poll)

    # ---- 互動

    def _bind_all(self, widget):
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._on_drag)
        widget.bind("<ButtonRelease-1>", self._end_drag)
        widget.bind("<Double-Button-1>", lambda e: self.ask_code())
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            widget.bind(seq, self._popup)
        for child in widget.winfo_children():
            self._bind_all(child)

    def _start_drag(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _on_drag(self, e):
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _end_drag(self, _e):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)

    def _build_menu(self):
        m = self.menu = tk.Menu(self.root, tearoff=0)
        m.add_command(label="換股票…", command=self.ask_code)
        m.add_separator()
        self.var_red_up = tk.BooleanVar(value=self.cfg["red_up"])
        m.add_checkbutton(label="紅漲綠跌", variable=self.var_red_up, command=self._toggle_color)
        self.var_compact = tk.BooleanVar(value=self.cfg["compact"])
        m.add_checkbutton(label="精簡模式", variable=self.var_compact, command=self._toggle_compact)
        self.var_top = tk.BooleanVar(value=self.cfg["topmost"])
        m.add_checkbutton(label="永遠置頂", variable=self.var_top, command=self._toggle_top)
        alpha = tk.Menu(m, tearoff=0)
        for a in (1.0, 0.92, 0.8, 0.65, 0.5):
            alpha.add_command(label=f"{int(a * 100)}%", command=lambda a=a: self._set_alpha(a, save=True))
        m.add_cascade(label="透明度", menu=alpha)
        m.add_separator()
        m.add_command(label="結束", command=self.quit)

    def _popup(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def ask_code(self):
        text = simpledialog.askstring("換股票", "輸入代碼（例：AAPL、US.TSLA、700、HK.00700、600519）",
                                      initialvalue=self.cfg["code"], parent=self.root)
        code = normalize_code(text or "")
        if not code:
            return
        self.cfg["code"] = code
        save_config(self.cfg)
        self.last_price = None
        self.lbl_code.config(text=code)
        self.lbl_name.config(text="")
        self.lbl_price.config(text="--", fg=INK)
        self.lbl_change.config(text="")
        self.lbl_ext.config(text="")
        self.lbl_hl.config(text="")
        self.worker.switch(code)

    def _toggle_color(self):
        self.cfg["red_up"] = self.var_red_up.get()
        save_config(self.cfg)

    def _toggle_compact(self):
        self.cfg["compact"] = self.var_compact.get()
        if self.cfg["compact"]:
            self.detail.pack_forget()
        else:
            self.detail.pack(fill="x", pady=(4, 0))
        save_config(self.cfg)

    def _toggle_top(self):
        self.cfg["topmost"] = self.var_top.get()
        self.root.attributes("-topmost", self.cfg["topmost"])
        save_config(self.cfg)

    def _set_alpha(self, a, save=False):
        try:
            self.root.attributes("-alpha", a)
        except tk.TclError:
            pass  # 部分 Linux 視窗管理器不支援透明度
        self.cfg["alpha"] = a
        if save:
            save_config(self.cfg)

    def quit(self):
        self.worker.stop()
        save_config(self.cfg)
        self.root.destroy()

    # ---- 更新畫面

    def _color(self, change):
        if change is None or change == 0:
            return INK
        up = change > 0
        return (RED if up else GREEN) if self.cfg["red_up"] else (GREEN if up else RED)

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "quote":
                    self._show_quote(payload)
                elif kind == "status":
                    text, ok = payload
                    if text:
                        self.lbl_status.config(text=text)
                    self.lbl_dot.config(fg=GREEN if ok else RED)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _show_quote(self, d):
        price = num(d.get("last_price"))
        prev = num(d.get("prev_close_price"))
        change = price - prev if price is not None and prev else None
        rate = change / prev * 100 if change is not None else None

        name = d.get("name")
        if name and name != "N/A":
            self.lbl_name.config(text=name)

        color = self._color(change)
        self.lbl_price.config(text=fmt_price(price), fg=color)
        if change is not None:
            self.lbl_change.config(text=f"{change:+.2f}   {rate:+.2f}%", fg=color)

        # 美股盤前 / 盤後 / 夜盤（有資料才顯示；漲跌幅欄位本身就是百分比）
        ext = ""
        for key, label in (("overnight", "夜盤"), ("after", "盤後"), ("pre", "盤前")):
            p = num(d.get(f"{key}_price"))
            if p:
                cv = num(d.get(f"{key}_change_val")) or 0
                cr = num(d.get(f"{key}_change_rate")) or 0
                ext = f"{label} {fmt_price(p)}  {cr:+.2f}%"
                self.lbl_ext.config(fg=self._color(cv))
                break
        self.lbl_ext.config(text=ext)

        hi, lo = num(d.get("high_price")), num(d.get("low_price"))
        vol = num(d.get("volume"))
        self.lbl_hl.config(text=f"H {fmt_price(hi)}  L {fmt_price(lo)}  V {fmt_volume(vol)}")
        t = d.get("data_time") or ""
        if t:
            self.lbl_status.config(text=f"更新 {t}")

        if self.last_price is not None and price is not None and price != self.last_price:
            self._flash(price > self.last_price)
        self.last_price = price

    def _flash(self, up):
        tint = (FLASH_RED if up else FLASH_GREEN) if self.cfg["red_up"] else (FLASH_GREEN if up else FLASH_RED)
        self._set_bg(tint)
        if self._flash_job:
            self.root.after_cancel(self._flash_job)
        self._flash_job = self.root.after(350, lambda: self._set_bg(BG))

    def _set_bg(self, color):
        def paint(w):
            if isinstance(w, (tk.Frame, tk.Label)):
                w.config(bg=color)
            for c in w.winfo_children():
                paint(c)
        paint(self.body)

    def run(self):
        self.worker.switch(self.cfg["code"])
        self.worker.start()
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="富途牛牛 單一個股懸浮視窗")
    ap.add_argument("--code", help="股票代碼，例如 US.AAPL、HK.00700、AAPL、700")
    ap.add_argument("--host", help="OpenD 位址（預設 127.0.0.1）")
    ap.add_argument("--port", type=int, help="OpenD 埠號（預設 11111）")
    ap.add_argument("--demo", action="store_true", help="不連 OpenD，用假資料展示")
    args = ap.parse_args()

    cfg = load_config()
    if args.code:
        cfg["code"] = normalize_code(args.code) or cfg["code"]
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    save_config(cfg)

    data_q = queue.Queue()
    worker = DemoWorker(data_q) if args.demo else FutuWorker(cfg["host"], cfg["port"], data_q)
    FloatingQuote(cfg, worker, data_q).run()


if __name__ == "__main__":
    sys.exit(main())
