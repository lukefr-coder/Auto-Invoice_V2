import tkinter as tk
from tkinter import ttk

from ui.window import AppWindow


def _apply_native_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    for theme in ("vista", "xpnative"):
        try:
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        except Exception:
            continue

    try:
        root.configure(bg="#f2f2f2")
    except Exception:
        pass


def main() -> None:
	root = tk.Tk()
	root.title("Auto-Invoice V2")
	root.minsize(900, 600)

	_apply_native_theme(root)

	AppWindow(root)
	root.mainloop()

if __name__ == "__main__":
    main()
