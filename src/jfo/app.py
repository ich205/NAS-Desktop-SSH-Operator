import sys
import tkinter as tk
from tkinter import messagebox

from jfo.ui.main_window import MainWindow


def main() -> int:
    try:
        root = tk.Tk()
        root.title("Jellyfin Organizer (Plan-First)")
        # Let the OS control scaling; Tk will use system DPI.
        root.geometry("1280x800")
        app = MainWindow(root)
        app.pack(fill=tk.BOTH, expand=True)
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        try:
            messagebox.showerror("Fatal Error", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
