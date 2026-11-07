# Handler reachable only via getattr/dynamic dispatch
# This should NOT be called "dead code" because it's dynamically registered

import tkinter as tk
from tkinter import messagebox


class DynamicHandlerApp:
    """Application with a handler reachable only via getattr."""

    def __init__(self, root: tk.Tk, handler_mode: str = "dynamic") -> None:
        self.root = root
        self.root.title("Dynamic Handler App")
        self.handler_mode = handler_mode

        # Handler registered dynamically (simulating plugin system)
        self._dynamic_handlers: dict[str, callable] = {}

        if handler_mode == "dynamic":
            # Register handler dynamically - this is how plugins work
            self._dynamic_handlers["run_analysis"] = self._dynamic_run_analysis
            # The handler is in the dict, not as a direct method call
            btn_text = "Run Dynamic Analysis"
        else:
            btn_text = "Run Direct Analysis"

        self.btn = tk.Button(
            root,
            text=btn_text,
            command=self._invoke_handler,
        )
        self.btn.pack(pady=20)

        self.result_label = tk.Label(root, text="", fg="blue")
        self.result_label.pack(pady=10)

    def _invoke_handler(self) -> None:
        """Invoke the handler - dynamically via getattr or directly."""
        if self.handler_mode == "dynamic":
            # Dynamic dispatch - getattr from the registry dict
            handler = self._dynamic_handlers.get("run_analysis")
            if handler:
                handler()
        else:
            # Direct call
            self._direct_run_analysis()

    def _dynamic_run_analysis(self) -> None:
        """Handler that's only accessible via getattr/dynamic dispatch."""
        messagebox.showinfo("Dynamic", "Handler reached via dynamic dispatch!")

    def _direct_run_analysis(self) -> None:
        """Handler called directly - not dynamic."""
        messagebox.showinfo("Direct", "Handler called directly!")


if __name__ == "__main__":
    root = tk.Tk()
    app = DynamicHandlerApp(root, handler_mode="dynamic")
    root.mainloop()