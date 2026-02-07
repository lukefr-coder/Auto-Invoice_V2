import tkinter as tk
from tkinter import ttk

from ui.window import AppWindow


def main() -> None:
    root = tk.Tk()
    root.title("Auto-Invoice V2")
    root.minsize(900, 600)

    try:
        style = ttk.Style(root)
        # 'clam' is a solid modern-ish default for ttk.
        style.theme_use("clam")
    except Exception:
        pass

    AppWindow(root)
    root.mainloop()

if __name__ == "__main__":
    main()
