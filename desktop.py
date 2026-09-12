"""Native local control window. AppSecret and chat text stay in process memory."""
import asyncio
import fcntl
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser

from live import Service
from rules import attributed, GPT_NOTE

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".qq-state"
EVENTS = queue.Queue()


class Worker:
    def __init__(self):
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        self.ready.wait(5)

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.service = Service(STATE_DIR, lambda k, v: EVENTS.put((k, v)))
        self.ready.set()
        self.loop.run_forever()

    def call(self, fn, *args):
        async def execute():
            try:
                result = fn(*args)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                EVENTS.put(("status", "Operation failed (" + type(exc).__name__ + "). No credentials were logged."))
        return asyncio.run_coroutine_threadsafe(execute(), self.loop)

    async def shutdown(self):
        await self.service.disconnect()
        remaining = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        self.service.ledger.db.close()


class Window(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("QQ GPT Auto Reply")
        self.geometry("840x820")
        self.minsize(760, 760)
        self.configure(bg="#f3f6fa")
        self.worker = Worker()
        self.service = self.worker.service
        self.messages = {}
        self.ai_draft = False
        self.busy_gpt = False
        self.closing = False
        self.status = tk.StringVar(value="Test GPT locally, then enter your QQ bot's AppID and AppSecret to connect.")
        self.app_id = tk.StringVar()
        self.secret = tk.StringVar()
        self.auto = tk.BooleanVar(value=False)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa")
        style.configure("TLabel", background="#f3f6fa", foreground="#17263b", font=("Arial", 12))
        style.configure("Title.TLabel", font=("Arial", 25, "bold"))
        style.configure("TButton", padding=(12, 8), font=("Arial", 11))
        style.configure("TCheckbutton", background="#f3f6fa", font=("Arial", 11))
        style.configure("TLabelframe", background="#f3f6fa")
        style.configure("TLabelframe.Label", background="#f3f6fa", font=("Arial", 12, "bold"))
        style.configure("Treeview", rowheight=28, font=("Arial", 11))

        main = ttk.Frame(self, padding=22)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="QQ GPT Auto Reply", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, text="Official QQ connection · GPT through your existing ChatGPT login").pack(anchor="w", pady=(5, 15))

        connect = ttk.LabelFrame(main, text="1  Connect your bot", padding=12)
        connect.pack(fill="x")
        ttk.Label(connect, text="AppID").grid(row=0, column=0, sticky="w")
        self.app_id_field = ttk.Entry(connect, textvariable=self.app_id, width=38)
        self.app_id_field.grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Label(connect, text="AppSecret").grid(row=1, column=0, sticky="w", pady=10)
        self.secret_field = ttk.Entry(connect, textvariable=self.secret, show="•", width=38)
        self.secret_field.grid(row=1, column=1, sticky="ew", padx=10)
        self.connect_button = ttk.Button(connect, text="Connect QQ", command=self.connect)
        self.connect_button.grid(row=1, column=2)
        connect.columnconfigure(1, weight=1)
        ttk.Label(connect, text="Entered locally, sent only to QQ for bot sign-in, and not saved to disk.", wraplength=710).grid(row=2, column=0, columnspan=3, sticky="w")
        links = ttk.Frame(connect)
        links.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(links, text="Open QQ Open Platform", command=lambda: webbrowser.open("https://q.qq.com/")).pack(side="left", padx=(0, 8))
        self.test_button = ttk.Button(links, text="Test GPT locally", command=self.test_gpt)
        self.test_button.pack(side="left")

        mode = ttk.LabelFrame(main, text="2  Choose how human replies are observed", padding=12)
        mode.pack(fill="x", pady=12)
        ttk.Label(mode, text="This bot receives its own private chats and explicit @bot group messages.\nIt cannot see your personal QQ inbox, @you-only messages, or replies sent in your personal QQ app.", wraplength=710).pack(anchor="w")
        self.auto_button = ttk.Checkbutton(mode, text="Use this window for human replies and enable 60-second auto replies",
                                          variable=self.auto, command=self.set_auto, state="disabled")
        self.auto_button.pack(anchor="w", pady=(10, 4))
        ttk.Label(mode, text=f"Off by default. Enabling applies to new messages only. Send human replies below to cancel GPT.\nKeep this window open and your computer awake. Every GPT reply ends with {GPT_NOTE}.", wraplength=710).pack(anchor="w")

        inbox = ttk.LabelFrame(main, text="3  Messages to the bot", padding=12)
        inbox.pack(fill="both", expand=True)
        self.table = ttk.Treeview(inbox, columns=("scope", "message", "status"), show="headings", height=5, selectmode="browse")
        for name, width in (("scope", 70), ("message", 410), ("status", 190)):
            self.table.heading(name, text=name.title())
            self.table.column(name, width=width, minwidth=60)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.select)
        self.current = ttk.Label(inbox, text="Connect QQ, then send a message to your bot. It will appear here.", wraplength=710)
        self.current.pack(anchor="w", pady=(8, 4))
        self.draft = tk.Text(inbox, height=3, wrap="word", font=("Arial", 12), relief="solid", borderwidth=1)
        self.draft.pack(fill="x")
        actions = ttk.Frame(inbox)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Send my reply", command=self.send_manual).pack(side="left")
        ttk.Button(actions, text="Draft with GPT", command=self.draft_with_gpt).pack(side="left", padx=8)
        ttk.Button(actions, text="Cancel pending auto reply", command=self.cancel).pack(side="left")
        ttk.Button(actions, text="Disconnect", command=self.disconnect).pack(side="right")

        ttk.Label(main, textvariable=self.status, wraplength=770).pack(anchor="w", pady=(12, 0))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(200, self.poll)

    def connect(self):
        app_id = self.app_id.get().strip()
        if not app_id.isascii() or not app_id.isdigit():
            self.status.set("Enter the numeric AppID from QQ Open Platform.")
            self.app_id_field.focus_set()
            return
        secret = self.secret.get().strip()
        if not secret:
            self.status.set("Enter AppSecret from QQ bot settings. Do not paste it into the chat.")
            self.secret_field.focus_set()
            return
        self.secret.set("")
        self.connect_button.configure(state="disabled")
        self.app_id_field.configure(state="disabled")
        self.worker.call(self.service.connect, app_id, secret)

    def set_auto(self):
        self.worker.call(self.service.set_auto, self.auto.get())

    def disconnect(self):
        self.auto.set(False)
        self.worker.call(self.service.disconnect)

    def selected_id(self):
        values = self.table.selection()
        return values[0] if values else None

    def select(self, _event=None):
        mid = self.selected_id()
        if mid in self.messages:
            self.current.configure(text=self.messages[mid].content[:400] or "[Non-text message]")
            self.draft.delete("1.0", "end")
            self.ai_draft = False

    def send_manual(self):
        mid = self.selected_id()
        text = self.draft.get("1.0", "end").strip()
        if not mid or not text:
            self.status.set("Select a message and enter your reply first.")
            return
        if self.ai_draft:
            text = attributed(text)
        self.worker.call(self.service.manual_reply, mid, text, self.ai_draft)

    def cancel(self):
        mid = self.selected_id()
        if mid:
            self.worker.call(self.service.cancel, mid)

    def test_gpt(self):
        self.generate("Please confirm in one short sentence that this QQ bot's GPT connection is working.", None)

    def draft_with_gpt(self):
        mid = self.selected_id()
        if mid:
            self.generate(self.messages[mid].content, mid)

    def generate(self, text, mid):
        if self.busy_gpt:
            return
        self.busy_gpt = True
        self.status.set("GPT is writing… no message will be sent by this preview.")
        async def preview():
            try:
                result = attributed(await self.service.gpt.generate(text))
                EVENTS.put(("preview", (mid, result)))
            except Exception as exc:
                EVENTS.put(("gpt_error", type(exc).__name__))
        self.worker.call(preview)

    def poll(self):
        self.worker.call(self.service.heartbeat)
        for _ in range(100):
            try:
                kind, value = EVENTS.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status.set(value)
            elif kind == "connection":
                self.connect_button.configure(state="disabled" if value else "normal")
                self.app_id_field.configure(state="disabled" if value else "normal")
                self.auto_button.configure(state="normal" if value else "disabled")
                if not value:
                    self.auto.set(False)
            elif kind == "message":
                event_key, message, state = value
                self.messages[event_key] = message
                values = (message.scope, message.content.replace("\n", " ")[:75], state)
                if self.table.exists(event_key):
                    self.table.item(event_key, values=values)
                else:
                    self.table.insert("", 0, iid=event_key, values=values)
                if len(self.messages) > 200:
                    last = self.table.get_children()[-1]
                    self.table.delete(last)
                    self.messages.pop(last, None)
            elif kind == "preview":
                self.busy_gpt = False
                mid, answer = value
                if mid is None:
                    messagebox.showinfo("GPT connection verified", answer, parent=self)
                elif mid == self.selected_id():
                    self.draft.delete("1.0", "end")
                    self.draft.insert("1.0", answer)
                    self.ai_draft = True
                self.status.set("GPT preview ready. No QQ message was sent.")
            elif kind == "gpt_error":
                self.busy_gpt = False
                self.status.set("GPT test failed (" + value + "). Check Codex login or usage allowance.")
        if not self.closing:
            self.after(200, self.poll)

    def close(self):
        self.closing = True
        self.auto.set(False)
        future = self.worker.call(self.worker.shutdown)
        def done():
            if future.done():
                self.worker.loop.call_soon_threadsafe(self.worker.loop.stop)
                self.destroy()
            else:
                self.after(100, done)
        done()


if __name__ == "__main__":
    lock_dir = STATE_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "desktop.lock").open("a") as desktop_lock:
        try:
            fcntl.flock(desktop_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            notice = tk.Tk()
            notice.withdraw()
            messagebox.showinfo("QQ Bot is already open", "Use the existing QQ GPT Auto Reply window.", parent=notice)
            notice.destroy()
        else:
            Window().mainloop()
