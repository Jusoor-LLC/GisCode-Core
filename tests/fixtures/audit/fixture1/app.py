import tkinter as tk
from tkinter import messagebox


class StubApp:
    """Application with a stub handler that only shows a message."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Stub Application")

        # Stub handler - only shows a message, does nothing useful
        self.btn_stub = tk.Button(
            root,
            text="Stub Action",
            command=self._stub_handler,  # <-- STUB: just shows a message
        )
        self.btn_stub.pack(pady=20)

        # Real handler that does actual work
        self.btn_real = tk.Button(
            root,
            text="Real Action",
            command=self._real_handler,
        )
        self.btn_real.pack(pady=10)

    def _stub_handler(self) -> None:
        """Stub handler that only shows a message box."""
        messagebox.showinfo("Stub", "This is a stub handler - no real action taken")

    def _real_handler(self) -> None:
        """Real handler that performs actual work."""
        messagebox.showinfo("Real", "This handler performs real action")


if __name__ == "__main__":
    root = tk.Tk()
    app = StubApp(root)
    root.mainloop()